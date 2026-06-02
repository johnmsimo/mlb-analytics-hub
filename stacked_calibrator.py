"""
stacked_calibrator.py — Single-probability hit-prop meta-learner
═══════════════════════════════════════════════════════════════════════════════
Design
------
Two component models (XGB and BATX) produce raw probabilities. Their
disagreement is informative. We learn the right way to combine them via:

  1. Logistic blend          — deterministic, no training required (fallback)
  2. Per-market isotonic     — fitted on graded outcomes, one model per market
                               key (batter_hits, pitcher_strikeouts, batter_hr,
                               batter_tb, batter_rbi). Isotonic regression is
                               the right choice here: it is monotone, non-
                               parametric, and guaranteed to only improve
                               calibration (it cannot make a well-ordered model
                               worse, only map its scores to true probabilities).

Step 2A additions
-----------------
  • Per-market isotonic models loaded from models/iso_{market_key}.pkl
  • apply_isotonic(raw_p, market_key) — standalone post-XGB calibration
    callable from xgb_prop_scorer without needing BATX
  • train_all_markets(tracker_path, min_picks) — fits one isotonic per market
  • evaluate_calibration(tracker_path, market_key) — returns Brier Score +
    Expected Calibration Error (ECE) per market so you can track drift
  • calibrate() extended: applies per-market isotonic before the blend step

Public API
----------
    calibrate(xgb_p, batx_p, *, coverage, exp_pa, bvp_pa, park_factor,
              market_key) -> dict
    apply_isotonic(raw_p, market_key) -> float
    load_calibrator(market_key) -> bool
    train_from_tracker(tracker_path, min_picks) -> dict   # hits only (legacy)
    train_all_markets(tracker_path, min_picks) -> dict    # all markets
    evaluate_calibration(tracker_path, market_key, n_bins) -> dict
"""
from __future__ import annotations

import json
import math
import os
import threading
from typing import Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_MODEL_DIR = os.path.join(_HERE, "models")

# Legacy single-market path (kept for backwards compat)
_MODEL_PATH = os.path.join(_MODEL_DIR, "stacked_hit_calibrator.pkl")

# Per-market isotonic paths: models/iso_{market_key}.pkl
_MARKET_KEYS = (
    "batter_hits",
    "pitcher_strikeouts",
    "batter_hr",
    "batter_tb",
    "batter_rbi",
)

def _iso_path(market_key: str) -> str:
    safe = market_key.replace(" ", "_").lower()
    return os.path.join(_MODEL_DIR, f"iso_{safe}.pkl")


_lock = threading.Lock()
# {market_key: isotonic_model | None}
_isotonics: dict[str, Optional[object]] = {}
_loaded_markets: set[str] = set()

# Legacy single-market state
_isotonic: Optional[object] = None
_loaded = False


# ─── League priors ───────────────────────────────────────────────────────────
LEAGUE_PRIOR_HIT1   = 0.66
WIDE_CI_THRESHOLD   = 0.35


# ─── Model loading ───────────────────────────────────────────────────────────

def load_calibrator(market_key: str = "batter_hits") -> bool:
    """Best-effort load of the trained isotonic for a given market.
    Returns True if a model was loaded. Idempotent."""
    global _loaded, _isotonic

    with _lock:
        # Per-market load
        if market_key not in _loaded_markets:
            _loaded_markets.add(market_key)
            path = _iso_path(market_key)
            if os.path.exists(path):
                try:
                    import joblib
                    _isotonics[market_key] = joblib.load(path)
                except Exception:
                    _isotonics[market_key] = None
            else:
                _isotonics[market_key] = None

        # Legacy hits model (backwards compat)
        if not _loaded:
            _loaded = True
            legacy_path = _iso_path("batter_hits") if os.path.exists(
                _iso_path("batter_hits")) else _MODEL_PATH
            if os.path.exists(legacy_path):
                try:
                    import joblib
                    _isotonic = joblib.load(legacy_path)
                except Exception:
                    _isotonic = None

    return _isotonics.get(market_key) is not None


def apply_isotonic(raw_p: float, market_key: str = "batter_hits") -> float:
    """
    Post-XGB isotonic calibration — standalone, no BATX required.

    Called directly from xgb_prop_scorer after predict_proba() to map the
    raw XGB score to a true calibrated probability. Returns raw_p unchanged
    if no isotonic model is available for this market.

    Parameters
    ----------
    raw_p      : float  Raw probability from model.predict_proba()[0, 1]
    market_key : str    One of the _MARKET_KEYS constants

    Returns
    -------
    float — calibrated probability in [0.01, 0.99]
    """
    load_calibrator(market_key)
    iso = _isotonics.get(market_key)
    if iso is None:
        return _clamp(float(raw_p), 0.01, 0.99)
    try:
        calibrated = float(iso.predict([float(raw_p)])[0])
        return _clamp(calibrated, 0.01, 0.99)
    except Exception:
        return _clamp(float(raw_p), 0.01, 0.99)


# ─── Math helpers ────────────────────────────────────────────────────────────

def _logit(p: float) -> float:
    p = max(1e-6, min(1 - 1e-6, p))
    return math.log(p / (1 - p))


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _coverage_weight(coverage: float) -> float:
    c = _clamp(coverage, 0.0, 1.0)
    return 0.20 + 0.55 * c


def _bvp_pa_weight(bvp_pa: float) -> float:
    pa = max(0.0, float(bvp_pa or 0))
    return _clamp(pa / 300.0, 0.0, 0.20)


def _exp_pa_uncertainty(exp_pa: float) -> float:
    pa = float(exp_pa or 4.2)
    return _clamp(abs(pa - 4.2) * 0.04, 0.0, 0.08)


def _logistic_blend(xgb_p: float, batx_p: float,
                    coverage: float, exp_pa: float, bvp_pa: float) -> float:
    w_xgb  = _clamp(_coverage_weight(coverage) - _bvp_pa_weight(bvp_pa), 0.15, 0.85)
    w_batx = 1.0 - w_xgb
    z = w_xgb * _logit(xgb_p) + w_batx * _logit(batx_p)
    p = _sigmoid(z)
    div = abs(_logit(xgb_p) - _logit(batx_p))
    if div > 0.55 and (coverage < 0.40 or bvp_pa < 5):
        shrink_w = _clamp((div - 0.55) * 0.30, 0.0, 0.35)
        p = (1 - shrink_w) * p + shrink_w * LEAGUE_PRIOR_HIT1
    return p


def _analytic_ci(xgb_p: float, batx_p: float,
                 coverage: float, exp_pa: float, bvp_pa: float,
                 blended: float) -> tuple[float, float]:
    div_logit = abs(_logit(xgb_p) - _logit(batx_p))
    cov_unc   = max(0.0, 0.50 - coverage) * 0.60
    pa_unc    = _exp_pa_uncertainty(exp_pa) * 2.0
    sigma_logit = math.sqrt(
        (div_logit * 0.18) ** 2 + cov_unc ** 2 + pa_unc ** 2
    )
    sigma_logit = _clamp(sigma_logit, 0.08, 0.55)
    z  = _logit(blended)
    lo = _sigmoid(z - 1.96 * sigma_logit)
    hi = _sigmoid(z + 1.96 * sigma_logit)
    return round(lo, 3), round(hi, 3)


# ─── Verdict tiering ─────────────────────────────────────────────────────────
_TIERS = (
    (0.78, "STRONG_BET",  "STRONG BET",  "green"),
    (0.66, "LEAN_OVER",   "LEAN OVER",   "teal"),
    (0.55, "PASS",        "PASS",        "gray"),
    (0.40, "LEAN_UNDER",  "LEAN UNDER",  "amber"),
    (0.00, "STRONG_FADE", "STRONG FADE", "red"),
)


def _tier_for(p: float) -> tuple[str, str, str]:
    for thresh, key, label, color in _TIERS:
        if p >= thresh:
            return key, label, color
    return "STRONG_FADE", "STRONG FADE", "red"


def _demote_if_wide_ci(p: float, ci_lo: float, ci_hi: float,
                       confidence: float) -> tuple[str, str, str]:
    ci_width = ci_hi - ci_lo
    base_key, base_label, base_color = _tier_for(p)
    if ci_lo < 0.50 < ci_hi and base_key != "PASS":
        return "PASS", "PASS · STRADDLES 50%", "gray"
    if confidence < 0.55:
        if base_key == "STRONG_BET":
            return "LEAN_OVER",   "LEAN OVER · LOW CONF",  "teal"
        if base_key == "STRONG_FADE":
            return "LEAN_UNDER",  "LEAN UNDER · LOW CONF", "amber"
    if ci_width >= WIDE_CI_THRESHOLD:
        if base_key == "STRONG_BET":
            return "LEAN_OVER",   "LEAN OVER · WIDE CI",   "teal"
        if base_key == "STRONG_FADE":
            return "LEAN_UNDER",  "LEAN UNDER · WIDE CI",  "amber"
    return base_key, base_label, base_color


# ─── Main calibrate() ────────────────────────────────────────────────────────

def calibrate(xgb_p: Optional[float], batx_p: Optional[float], *,
              coverage: float = 0.0,
              exp_pa: float = 4.2,
              bvp_pa: float = 0.0,
              park_factor: float = 1.0,
              market_key: str = "batter_hits") -> dict:
    """Combine XGB + BATX into a single calibrated probability with verdict.

    Step 2A: applies per-market isotonic calibration to BOTH raw inputs
    before the logistic blend, so the blend operates on already-calibrated
    scores rather than raw model outputs.
    """
    if xgb_p is None and batx_p is None:
        return {
            "probability":   LEAGUE_PRIOR_HIT1,
            "ci_lo": 0.35,   "ci_hi": 0.85,
            "verdict":       "PASS",
            "verdict_label": "PASS · NO DATA",
            "verdict_color": "gray",
            "confidence":    0.0,
            "source":        "league_prior",
        }

    if xgb_p is None:
        xgb_p  = batx_p
    if batx_p is None:
        batx_p = xgb_p

    xgb_p  = _clamp(float(xgb_p),  0.01, 0.99)
    batx_p = _clamp(float(batx_p), 0.01, 0.99)

    # ── 2A: calibrate each raw score via per-market isotonic ─────────────────
    xgb_cal  = apply_isotonic(xgb_p,  market_key)
    batx_cal = apply_isotonic(batx_p, market_key)
    source   = "isotonic" if _isotonics.get(market_key) is not None else "logistic_fallback"

    blended = _logistic_blend(xgb_cal, batx_cal, coverage, exp_pa, bvp_pa)

    ci_lo, ci_hi = _analytic_ci(xgb_cal, batx_cal, coverage, exp_pa, bvp_pa, blended)

    ci_width    = ci_hi - ci_lo
    width_conf  = _clamp(1.0 - (ci_width / 0.40), 0.0, 1.0)
    cov_conf    = _clamp(coverage, 0.0, 1.0)
    sample_conf = _clamp(bvp_pa / 80.0, 0.0, 1.0)
    confidence  = round(0.55 * width_conf + 0.30 * cov_conf + 0.15 * sample_conf, 3)

    tier_key, tier_label, tier_color = _demote_if_wide_ci(blended, ci_lo, ci_hi, confidence)

    return {
        "probability":   round(blended, 3),
        "ci_lo":         ci_lo,
        "ci_hi":         ci_hi,
        "verdict":       tier_key,
        "verdict_label": tier_label,
        "verdict_color": tier_color,
        "confidence":    confidence,
        "source":        source,
        "components": {
            "xgb":       round(xgb_p,  3),
            "xgb_cal":   round(xgb_cal, 3),
            "batx":      round(batx_p,  3),
            "batx_cal":  round(batx_cal, 3),
            "coverage":  round(float(coverage), 3),
            "exp_pa":    round(float(exp_pa),   2),
            "bvp_pa":    int(bvp_pa or 0),
            "market":    market_key,
        },
    }


# ─── Calibration evaluation (2A) ─────────────────────────────────────────────

def evaluate_calibration(
    tracker_path: str = "data/daily_tracker.json",
    market_key: str = "batter_hits",
    n_bins: int = 10,
) -> dict:
    """
    Compute Brier Score and Expected Calibration Error (ECE) for a market.

    Reads graded picks from daily_tracker.json where:
      pick["marketKey"]  == market_key
      pick["outcome"]    in ("win", "loss")
      pick["blendedProb"] or pick["adjProb"] is the model's predicted probability

    Returns
    -------
    {
      "market":       str,
      "n":            int,      # graded picks used
      "brier_score":  float,    # lower is better; perfect = 0.0
      "ece":          float,    # lower is better; perfect = 0.0
      "bins":         list,     # [{bin_lower, bin_upper, mean_pred, mean_actual, n}]
      "overconfident": bool,    # True if model predicts too high on average
    }
    """
    if not os.path.exists(tracker_path):
        return {"error": "tracker_missing", "market": market_key, "n": 0}

    with open(tracker_path) as f:
        tracker = json.load(f)

    xs, ys = [], []
    for _date, picks in (tracker or {}).items():
        for pick in (picks or []):
            if pick.get("marketKey") != market_key:
                continue
            if pick.get("outcome") not in ("win", "loss"):
                continue
            prob = pick.get("blendedProb") or pick.get("adjProb") or pick.get("probability")
            if prob is None:
                continue
            try:
                xs.append(float(prob))
                ys.append(1.0 if pick["outcome"] == "win" else 0.0)
            except (TypeError, ValueError):
                continue

    n = len(xs)
    if n == 0:
        return {"error": "no_graded_picks", "market": market_key, "n": 0}

    # Brier Score
    brier = sum((p - y) ** 2 for p, y in zip(xs, ys)) / n

    # ECE — equal-width bins
    bin_size  = 1.0 / n_bins
    bins_data = []
    ece_sum   = 0.0
    for i in range(n_bins):
        lo = i * bin_size
        hi = lo + bin_size
        in_bin = [(p, y) for p, y in zip(xs, ys) if lo <= p < hi]
        if not in_bin:
            continue
        mean_pred   = sum(p for p, _ in in_bin) / len(in_bin)
        mean_actual = sum(y for _, y in in_bin) / len(in_bin)
        ece_sum    += (len(in_bin) / n) * abs(mean_pred - mean_actual)
        bins_data.append({
            "bin_lower":   round(lo, 2),
            "bin_upper":   round(hi, 2),
            "mean_pred":   round(mean_pred,   3),
            "mean_actual": round(mean_actual, 3),
            "n":           len(in_bin),
        })

    mean_pred_all   = sum(xs) / n
    mean_actual_all = sum(ys) / n
    overconfident   = mean_pred_all > mean_actual_all

    return {
        "market":       market_key,
        "n":            n,
        "brier_score":  round(brier, 4),
        "ece":          round(ece_sum, 4),
        "mean_pred":    round(mean_pred_all,   3),
        "mean_actual":  round(mean_actual_all, 3),
        "overconfident": overconfident,
        "bins":         bins_data,
    }


# ─── Training ─────────────────────────────────────────────────────────────────

def _fit_isotonic_for_market(
    tracker: dict,
    market_key: str,
    min_picks: int,
) -> dict:
    """Fit one isotonic regression for market_key. Returns metrics dict."""
    xs, ys = [], []
    for _date, picks in (tracker or {}).items():
        for pick in (picks or []):
            if pick.get("marketKey") != market_key:
                continue
            if pick.get("outcome") not in ("win", "loss"):
                continue
            prob = pick.get("blendedProb") or pick.get("adjProb") or pick.get("probability")
            if prob is None:
                continue
            try:
                xs.append(float(prob))
                ys.append(1.0 if pick["outcome"] == "win" else 0.0)
            except (TypeError, ValueError):
                continue

    if len(xs) < min_picks:
        return {
            "market":  market_key,
            "trained": False,
            "reason":  "insufficient_data",
            "n":       len(xs),
        }

    from sklearn.isotonic import IsotonicRegression
    import joblib

    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.01, y_max=0.99)
    iso.fit(xs, ys)

    os.makedirs(_MODEL_DIR, exist_ok=True)
    path = _iso_path(market_key)
    joblib.dump(iso, path)

    # Update in-memory cache immediately
    with _lock:
        _isotonics[market_key] = iso
        _loaded_markets.add(market_key)

    preds = iso.predict(xs)
    brier = sum((p - y) ** 2 for p, y in zip(preds, ys)) / len(ys)
    return {
        "market":  market_key,
        "trained": True,
        "n":       len(xs),
        "brier":   round(brier, 4),
        "path":    path,
    }


def train_all_markets(
    tracker_path: str = "data/daily_tracker.json",
    min_picks: int = 200,
) -> dict:
    """
    Fit a separate isotonic regression for each of the five prop markets.

    Run this weekly (or after every ~50 new graded picks) from cron:
        python stacked_calibrator.py --train-all

    Returns a dict keyed by market_key with per-market metrics.
    """
    if not os.path.exists(tracker_path):
        return {"error": "tracker_missing"}

    with open(tracker_path) as f:
        tracker = json.load(f)

    results = {}
    for mk in _MARKET_KEYS:
        results[mk] = _fit_isotonic_for_market(tracker, mk, min_picks)
        status = "✓ trained" if results[mk]["trained"] else f"✗ {results[mk].get('reason')}"
        print(f"[calibrator] {mk:<28} n={results[mk]['n']:<5}  {status}")

    return results


def train_from_tracker(
    tracker_path: str = "data/daily_tracker.json",
    min_picks: int = 200,
) -> dict:
    """Legacy single-market trainer (hits only). Kept for backwards compat."""
    if not os.path.exists(tracker_path):
        return {"trained": False, "reason": "tracker_missing"}
    with open(tracker_path) as f:
        tracker = json.load(f)
    result = _fit_isotonic_for_market(tracker, "batter_hits", min_picks)
    # Also write to legacy path so load_calibrator() still finds it
    if result.get("trained"):
        import joblib
        iso = _isotonics.get("batter_hits")
        if iso:
            joblib.dump(iso, _MODEL_PATH)
    return result


# ─── CLI ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="stacked_calibrator utilities")
    parser.add_argument("--train",      action="store_true",
                        help="Fit isotonic for batter_hits only (legacy)")
    parser.add_argument("--train-all",  action="store_true",
                        help="Fit isotonic for all prop markets")
    parser.add_argument("--eval",       type=str, default=None,
                        metavar="MARKET",
                        help="Evaluate calibration for a market key")
    parser.add_argument("--eval-all",   action="store_true",
                        help="Evaluate all markets")
    parser.add_argument("--tracker",    type=str,
                        default="data/daily_tracker.json")
    parser.add_argument("--min-picks",  type=int, default=200)
    parser.add_argument("--bins",       type=int, default=10)
    args = parser.parse_args()

    if args.train_all:
        results = train_all_markets(args.tracker, args.min_picks)
        print(json.dumps(results, indent=2))

    elif args.train:
        result = train_from_tracker(args.tracker, args.min_picks)
        print(json.dumps(result, indent=2))

    elif args.eval_all:
        for mk in _MARKET_KEYS:
            ev = evaluate_calibration(args.tracker, mk, args.bins)
            print(f"\n── {mk} ──")
            print(json.dumps(ev, indent=2))

    elif args.eval:
        ev = evaluate_calibration(args.tracker, args.eval, args.bins)
        print(json.dumps(ev, indent=2))

    else:
        # Smoke test
        out = calibrate(0.72, 0.74, coverage=0.92, exp_pa=4.36,
                        bvp_pa=12, market_key="batter_hits")
        print(json.dumps(out, indent=2))
