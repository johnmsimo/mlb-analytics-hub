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


# ─── League priors ───────────────────────────────────────────
LEAGUE_PRIOR_HIT1   = 0.66
WIDE_CI_THRESHOLD   = 0.35


# ─── Model loading ───────────────────────────────────────────

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


# ─── Math helpers ────────────────────────────────────────────

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



# ─── Smart Consensus (Step 3) ───────────────────────────────────────────
# Dynamic Brier-weighting between XGB and BATX. The weights only shift away from
# the coverage heuristic when there is *measured* per-model accuracy on disk —
# we never fabricate one model's skill from the other's. Until the tracker has
# accumulated graded picks carrying both per-model probabilities (see
# update_model_accuracies), _get_consensus_weights returns None and the blend
# falls back to the original, well-tested coverage heuristic.
_ACCURACY_PATH = "/app/data/model_accuracies.json" if os.path.exists("/app/data") else os.path.join(_HERE, "data", "model_accuracies.json")
_model_accuracies = None  # None = not yet loaded; {} = loaded, no real data

def load_model_accuracies(force: bool = False):
    global _model_accuracies
    if _model_accuracies is not None and not force:
        return
    _model_accuracies = {}
    if os.path.exists(_ACCURACY_PATH):
        try:
            with open(_ACCURACY_PATH, 'r') as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                _model_accuracies = loaded
        except Exception:
            _model_accuracies = {}

def _get_consensus_weights(market_key):
    """Inverse-Brier weights (w_xgb, w_batx) from *measured* accuracy, or None
    when no real data exists for this market (caller uses the heuristic)."""
    load_model_accuracies()
    acc = _model_accuracies.get(market_key) or _model_accuracies.get("default")
    if not acc:
        return None
    try:
        xgb_b = float(acc.get("xgb"))
        batx_b = float(acc.get("batx"))
        n = int(acc.get("n", 0))
    except (TypeError, ValueError):
        return None
    # Require a minimum graded sample before trusting the measured Brier.
    if n < 40 or xgb_b <= 0 or batx_b <= 0:
        return None
    inv_xgb = 1.0 / xgb_b
    inv_batx = 1.0 / batx_b
    total = inv_xgb + inv_batx
    if total <= 0:
        return None
    return inv_xgb / total, inv_batx / total

def _logistic_blend(xgb_p: float, batx_p: float,
                    coverage: float, exp_pa: float, bvp_pa: float,
                    market_key: str = "default") -> float:
    # Coverage heuristic: low XGB coverage -> lean on the context-light BATX.
    h_w_xgb = _clamp(_coverage_weight(coverage) - _bvp_pa_weight(bvp_pa), 0.15, 0.85)

    acc_weights = _get_consensus_weights(market_key)
    if acc_weights is None:
        # No measured per-model accuracy yet -> original behavior, unchanged.
        w_xgb = h_w_xgb
    else:
        # Blend measured skill (70%) with the coverage heuristic (30%).
        w_xgb = _clamp(0.7 * acc_weights[0] + 0.3 * h_w_xgb, 0.10, 0.90)
    w_batx = 1.0 - w_xgb

    z = w_xgb * _logit(xgb_p) + w_batx * _logit(batx_p)
    p = _sigmoid(z)
    div = abs(_logit(xgb_p) - _logit(batx_p))
    if div > 0.55 and (coverage < 0.40 or bvp_pa < 5):
        shrink_w = _clamp((div - 0.55) * 0.30, 0.0, 0.35)
        p = (1 - shrink_w) * p + shrink_w * LEAGUE_PRIOR_HIT1
    return p


def _analytic_sigma_logit(xgb_p: float, batx_p: float,
                          coverage: float, exp_pa: float, bvp_pa: float) -> float:
    """Heuristic logit-scale standard deviation of the blended estimate.

    Combines model disagreement, XGB input coverage, and expected-PA sample
    size into one uncertainty figure. Kept separate from _analytic_ci so the
    sigma can be fused with an empirical (Monte Carlo) interval.
    """
    div_logit = abs(_logit(xgb_p) - _logit(batx_p))
    cov_unc   = max(0.0, 0.50 - coverage) * 0.60
    pa_unc    = _exp_pa_uncertainty(exp_pa) * 2.0
    sigma_logit = math.sqrt(
        (div_logit * 0.18) ** 2 + cov_unc ** 2 + pa_unc ** 2
    )
    return _clamp(sigma_logit, 0.08, 0.55)


def _sigma_from_interval(lo: float, hi: float, coverage_z: float = 1.645) -> Optional[float]:
    """Recover a logit-scale sigma from an interval [lo, hi].

    Defaults to a 90% interval (z=1.645), matching the XGB prediction interval
    that the Monte Carlo simulation is anchored to. Returns None for a
    degenerate / unusable interval.
    """
    try:
        lo = _clamp(float(lo), 0.001, 0.999)
        hi = _clamp(float(hi), 0.001, 0.999)
        if hi <= lo:
            return None
        sig = (_logit(hi) - _logit(lo)) / (2.0 * coverage_z)
        if not math.isfinite(sig) or sig <= 0:
            return None
        return _clamp(sig, 0.04, 0.80)
    except Exception:
        return None


def _ci_from_sigma(blended: float, sigma_logit: float) -> tuple[float, float]:
    z  = _logit(blended)
    lo = _sigmoid(z - 1.96 * sigma_logit)
    hi = _sigmoid(z + 1.96 * sigma_logit)
    return round(lo, 3), round(hi, 3)


def _analytic_ci(xgb_p: float, batx_p: float,
                 coverage: float, exp_pa: float, bvp_pa: float,
                 blended: float) -> tuple[float, float]:
    sigma_logit = _analytic_sigma_logit(xgb_p, batx_p, coverage, exp_pa, bvp_pa)
    return _ci_from_sigma(blended, sigma_logit)


# ─── Verdict tiering (market-aware) ──────────────────
# Per-market base rate (P(over) for a typical *starter* at the standard line).
# The verdict is the probability's position RELATIVE to this base rate, so the
# same thresholds work for hits (~0.66), total bases (~0.40), RBI (~0.34) and
# home runs (~0.13) instead of the old absolute thresholds that only made sense
# for hits. Keyed by both book keys and the scorer's short names.
_MARKET_BASE_RATE = {
    "batter_hits":        0.66,
    "batter_total_bases": 0.40, "batter_tb":  0.40,
    "batter_rbis":        0.34, "batter_rbi": 0.34,
    "batter_home_runs":   0.13, "batter_hr":  0.13,
    "pitcher_strikeouts": 0.50,
}
_DEFAULT_BASE_RATE = 0.50


def _base_rate_for(market_key: str) -> float:
    return _MARKET_BASE_RATE.get(market_key, _DEFAULT_BASE_RATE)


# Thresholds on the ratio prob / base_rate. Chosen so that at the hits base rate
# (0.66) they reproduce the original absolute tiers 0.78/0.66/0.55/0.40 exactly
# (0.66×1.182=0.78, ×1.0=0.66, ×0.833=0.55, ×0.606=0.40) — hits behavior is
# preserved while the tiers generalize to every market's own base rate.
_TIER_RATIOS = (
    (1.182, "STRONG_BET",  "STRONG BET",  "green"),
    (1.000, "LEAN_OVER",   "LEAN OVER",   "teal"),
    (0.833, "PASS",        "PASS",        "gray"),
    (0.606, "LEAN_UNDER",  "LEAN UNDER",  "amber"),
    (0.000, "STRONG_FADE", "STRONG FADE", "red"),
)


def _tier_for(p: float, base_rate: float = 0.66) -> tuple[str, str, str]:
    ratio = p / max(1e-6, base_rate)
    for thresh, key, label, color in _TIER_RATIOS:
        if ratio >= thresh:
            return key, label, color
    return "STRONG_FADE", "STRONG FADE", "red"


def _demote_if_wide_ci(p: float, ci_lo: float, ci_hi: float,
                       confidence: float, base_rate: float = 0.66) -> tuple[str, str, str]:
    ci_width = ci_hi - ci_lo
    base_key, base_label, base_color = _tier_for(p, base_rate)
    # CI straddles the market's base rate → we can't say the player is above or
    # below average, so there's no edge to bet (market-aware analogue of the old
    # "straddles 50%" guard).
    if ci_lo < base_rate < ci_hi and base_key != "PASS":
        return "PASS", "PASS · STRADDLES BASE", "gray"
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


# ─── Main calibrate() ────────────────────────────────────────

def calibrate(xgb_p: Optional[float], batx_p: Optional[float], *,
              coverage: float = 0.0,
              exp_pa: float = 4.2,
              bvp_pa: float = 0.0,
              park_factor: float = 1.0,
              market_key: str = "batter_hits",
              mc_ci: Optional[tuple] = None) -> dict:
    """Combine XGB + BATX into a single calibrated probability with verdict.

    Step 2A: applies per-market isotonic calibration to BOTH raw inputs
    before the logistic blend, so the blend operates on already-calibrated
    scores rather than raw model outputs.

    mc_ci: optional (lo, hi) empirical interval from the XGB Monte Carlo
        prediction interval. When supplied, its logit-scale uncertainty is
        fused with the heuristic analytic uncertainty via inverse-variance
        (precision) weighting, producing a tighter, simulation-grounded CI.
        The 'source' field is suffixed with '+mc' when this path is taken.
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

    blended = _logistic_blend(xgb_cal, batx_cal, coverage, exp_pa, bvp_pa, market_key)

    # Heuristic uncertainty (always available).
    sigma_analytic = _analytic_sigma_logit(xgb_cal, batx_cal, coverage, exp_pa, bvp_pa)

    # Fuse with the empirical Monte Carlo interval when provided. Two
    # independent uncertainty estimates of the same quantity combine by
    # inverse-variance weighting: 1/σ² = 1/σ_a² + 1/σ_mc². This can only
    # tighten the interval, and weights the more precise estimate higher.
    sigma_mc = _sigma_from_interval(*mc_ci) if (mc_ci and len(mc_ci) == 2) else None
    if sigma_mc is not None:
        sigma_combined = math.sqrt(1.0 / (1.0 / sigma_analytic ** 2 + 1.0 / sigma_mc ** 2))
        sigma_combined = _clamp(sigma_combined, 0.05, 0.55)
        ci_lo, ci_hi = _ci_from_sigma(blended, sigma_combined)
        source = f"{source}+mc"
    else:
        ci_lo, ci_hi = _ci_from_sigma(blended, sigma_analytic)

    ci_width    = ci_hi - ci_lo
    width_conf  = _clamp(1.0 - (ci_width / 0.40), 0.0, 1.0)
    cov_conf    = _clamp(coverage, 0.0, 1.0)
    sample_conf = _clamp(bvp_pa / 80.0, 0.0, 1.0)
    confidence  = round(0.55 * width_conf + 0.30 * cov_conf + 0.15 * sample_conf, 3)

    base_rate = _base_rate_for(market_key)
    tier_key, tier_label, tier_color = _demote_if_wide_ci(blended, ci_lo, ci_hi, confidence, base_rate)

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


# ─── Calibration evaluation (2A) ────────────────────────────────────

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


# ─── Training ───────────────────────────────────────────────

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


# ─── CLI ─────────────────────────────────────────────────

def update_model_accuracies(tracker_path="data/daily_tracker.json", min_n: int = 40):
    """Measure per-model Brier (XGB vs BATX) from graded tracker history and
    persist it for the consensus weighter.

    Honest by construction: a market is written ONLY when its graded picks
    actually carry *both* per-model probabilities. We never derive one model's
    skill from the other's. Until the scoring path persists `xgbProb`/`batxProb`
    onto entries, this writes an empty file and the consensus falls back to the
    coverage heuristic (no behavior change). Returns the stats dict it wrote.
    """
    if not os.path.exists(tracker_path):
        return {}

    try:
        with open(tracker_path) as f:
            tracker = json.load(f)
    except Exception:
        return {}

    # Candidate field names for each model's probability, in priority order.
    XGB_FIELDS = ("xgbProb", "xgbHitProb", "xgbKProb")
    BATX_FIELDS = ("batxProb", "batxHitProb")

    def _first(entry, fields):
        for fld in fields:
            v = entry.get(fld)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
        return None

    def _outcome(entry):
        g = entry.get("grade") or entry.get("outcome")
        if g == "win":
            return 1.0
        if g == "loss":
            return 0.0
        return None  # push / pending -> excluded

    # market_key -> {"xgb": [sq_err...], "batx": [sq_err...]}
    acc = {}
    for _date, rec in (tracker or {}).items():
        entries = rec.get("entries") if isinstance(rec, dict) else rec
        for e in (entries or []):
            mk = e.get("marketKey")
            if not mk:
                continue
            y = _outcome(e)
            if y is None:
                continue
            xp = _first(e, XGB_FIELDS)
            bp = _first(e, BATX_FIELDS)
            if xp is None or bp is None:
                continue
            bucket = acc.setdefault(mk, {"xgb": [], "batx": []})
            bucket["xgb"].append((xp - y) ** 2)
            bucket["batx"].append((bp - y) ** 2)

    stats = {}
    for mk, b in acc.items():
        n = len(b["xgb"])
        if n < min_n:
            continue
        stats[mk] = {
            "xgb": round(sum(b["xgb"]) / n, 5),
            "batx": round(sum(b["batx"]) / n, 5),
            "n": n,
        }

    os.makedirs(os.path.dirname(_ACCURACY_PATH), exist_ok=True)
    with open(_ACCURACY_PATH, 'w') as f:
        json.dump(stats, f, indent=2)
    # Invalidate the in-process cache so the next blend picks up fresh weights.
    load_model_accuracies(force=True)
    print(f"[SmartConsensus] Wrote {len(stats)} market(s) to {_ACCURACY_PATH}")
    return stats
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
