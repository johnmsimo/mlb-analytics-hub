"""
stacked_calibrator.py — Single-probability hit-prop meta-learner

Replaces the XGB-vs-BATX "models disagree, you decide" UX with a single
calibrated probability + decisive verdict tier + 95% credible interval.

Design
------
The two component models (XGB and BATX) already produce probabilities. Their
disagreement is informative — it tells us when one model is operating outside
its sweet spot. We learn the right way to combine them via either:

  • a trained isotonic regression on historical outcomes (preferred), or
  • a deterministic logistic blend (fallback when no training data exists)

The fallback blend is principled, not arbitrary:
  • Trust XGB more when its input coverage is high (Statcast / FG well-populated)
  • Trust BATX more when BvP sample is large (well-observed matchup)
  • Trust XGB more when expected PA count is in the typical 4-5 band
  • Pull both toward a league prior (0.66) under high disagreement +
    low overall information

Public API
----------
    calibrate(xgb_p, batx_p, *, coverage, exp_pa, bvp_pa, park_factor) -> dict
        Returns: {
            "probability": float,       # single calibrated probability
            "ci_lo": float, "ci_hi": float,
            "verdict": str,             # STRONG_BET / LEAN_OVER / PASS / LEAN_UNDER / STRONG_FADE
            "verdict_label": str,       # human-readable
            "verdict_color": str,       # green / teal / gray / amber / red
            "confidence": float,        # 0..1 — narrow CI + tier-stable
            "source": str,              # "isotonic" or "logistic_fallback"
        }

    load_calibrator() -> bool
        Loads models/stacked_hit_calibrator.pkl if present. Idempotent.
"""
from __future__ import annotations

import math
import os
import threading
from typing import Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_MODEL_PATH = os.path.join(_HERE, "models", "stacked_hit_calibrator.pkl")

_lock = threading.Lock()
_isotonic: Optional[object] = None
_loaded = False


# ─── League priors used in the deterministic fallback ───────────────────────
LEAGUE_PRIOR_HIT1 = 0.66    # Typical 1+ hit probability for a starter
WIDE_CI_THRESHOLD = 0.20    # CI wider than 20pts → demote tier toward PASS


def load_calibrator() -> bool:
    """Best-effort load of the trained isotonic. Returns True if loaded."""
    global _loaded, _isotonic
    with _lock:
        if _loaded:
            return _isotonic is not None
        _loaded = True
        try:
            if not os.path.exists(_MODEL_PATH):
                return False
            import joblib
            _isotonic = joblib.load(_MODEL_PATH)
            return True
        except Exception:
            _isotonic = None
            return False


def _logit(p: float) -> float:
    p = max(1e-6, min(1 - 1e-6, p))
    return math.log(p / (1 - p))


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _coverage_weight(coverage: float) -> float:
    """Map XGB coverage to XGB's blend weight in [0.20, 0.75].

    < 0.40 coverage → trust BATX more (XGB inputs are thin)
    > 0.85 coverage → trust XGB more (full Statcast/FG context)
    """
    c = _clamp(coverage, 0.0, 1.0)
    return 0.20 + 0.55 * c


def _bvp_pa_weight(bvp_pa: float) -> float:
    """BvP sample size pulls weight toward BATX (which incorporates BvP).

    < 5 PA  → 0.00 (no shift)
    > 60 PA → 0.20 (modest shift toward BATX)
    """
    pa = max(0.0, float(bvp_pa or 0))
    return _clamp(pa / 300.0, 0.0, 0.20)


def _exp_pa_uncertainty(exp_pa: float) -> float:
    """Expected PA outside the typical 4-5 band increases output uncertainty."""
    pa = float(exp_pa or 4.2)
    return _clamp(abs(pa - 4.2) * 0.04, 0.0, 0.08)


def _logistic_blend(xgb_p: float, batx_p: float,
                    coverage: float, exp_pa: float, bvp_pa: float) -> float:
    """Logit-space weighted average of the two models with coverage- and
    sample-aware weights. Deterministic, no training required."""
    w_xgb = _coverage_weight(coverage) - _bvp_pa_weight(bvp_pa)
    w_xgb = _clamp(w_xgb, 0.15, 0.85)
    w_batx = 1.0 - w_xgb

    z = w_xgb * _logit(xgb_p) + w_batx * _logit(batx_p)
    p = _sigmoid(z)

    # Shrink to league prior when models disagree wildly AND both sides
    # are low-information (analogue of the existing v1 shrink-to-prior).
    div = abs(_logit(xgb_p) - _logit(batx_p))
    if div > 0.55 and (coverage < 0.40 or bvp_pa < 5):
        shrink_w = _clamp((div - 0.55) * 0.30, 0.0, 0.35)
        p = (1 - shrink_w) * p + shrink_w * LEAGUE_PRIOR_HIT1
    return p


def _analytic_ci(xgb_p: float, batx_p: float,
                 coverage: float, exp_pa: float, bvp_pa: float,
                 blended: float) -> tuple[float, float]:
    """Approximate 95% CI for the blended probability.

    Three uncertainty sources, combined in quadrature on the logit scale:
      • model disagreement (|logit(xgb) - logit(batx)|)
      • coverage shortfall (XGB inputs missing → wider band)
      • expected-PA deviation from the typical 4-5 range
    """
    div_logit = abs(_logit(xgb_p) - _logit(batx_p))
    cov_unc   = max(0.0, 0.50 - coverage) * 0.80
    pa_unc    = _exp_pa_uncertainty(exp_pa) * 3.0

    sigma_logit = math.sqrt(
        (div_logit * 0.45) ** 2 +
        cov_unc ** 2 +
        pa_unc ** 2
    )
    sigma_logit = _clamp(sigma_logit, 0.10, 1.80)

    z = _logit(blended)
    lo = _sigmoid(z - 1.96 * sigma_logit)
    hi = _sigmoid(z + 1.96 * sigma_logit)
    return round(lo, 3), round(hi, 3)


# ─── Verdict tiering ────────────────────────────────────────────────────────
_TIERS = (
    (0.78, "STRONG_BET",   "STRONG BET",   "green"),
    (0.66, "LEAN_OVER",    "LEAN OVER",    "teal"),
    (0.55, "PASS",         "PASS",         "gray"),
    (0.40, "LEAN_UNDER",   "LEAN UNDER",   "amber"),
    (0.00, "STRONG_FADE",  "STRONG FADE",  "red"),
)


def _tier_for(p: float) -> tuple[str, str, str]:
    for thresh, key, label, color in _TIERS:
        if p >= thresh:
            return key, label, color
    return "STRONG_FADE", "STRONG FADE", "red"


def _demote_if_wide_ci(p: float, ci_lo: float, ci_hi: float) -> tuple[str, str, str]:
    """If the CI straddles a tier boundary, demote to the safer call.

    Width > WIDE_CI_THRESHOLD AND CI spans a boundary → PASS, full stop.
    Narrow CI but spans boundary → demote one tier toward PASS.
    """
    ci_width = ci_hi - ci_lo
    base_key, base_label, base_color = _tier_for(p)

    lo_key, _, _ = _tier_for(ci_lo)
    hi_key, _, _ = _tier_for(ci_hi)

    if ci_width >= WIDE_CI_THRESHOLD and lo_key != hi_key:
        return "PASS", "PASS · WIDE CI", "gray"

    # If CI crosses 50% in either direction we can't commit to an Over/Under
    if (ci_lo < 0.50 < ci_hi) and base_key not in ("PASS",):
        if base_key in ("STRONG_BET", "LEAN_OVER", "STRONG_FADE", "LEAN_UNDER"):
            return "PASS", "PASS · STRADDLES 50%", "gray"

    return base_key, base_label, base_color


def calibrate(xgb_p: Optional[float], batx_p: Optional[float], *,
              coverage: float = 0.0,
              exp_pa: float = 4.2,
              bvp_pa: float = 0.0,
              park_factor: float = 1.0) -> dict:
    """Combine XGB + BATX into a single calibrated probability with verdict.

    Either input may be None — the function falls back to the available one
    (or returns the league prior if both are missing).
    """
    # Defensive: handle missing inputs
    if xgb_p is None and batx_p is None:
        return {
            "probability": LEAGUE_PRIOR_HIT1,
            "ci_lo": 0.35, "ci_hi": 0.85,
            "verdict": "PASS", "verdict_label": "PASS · NO DATA",
            "verdict_color": "gray",
            "confidence": 0.0,
            "source": "league_prior",
        }
    if xgb_p is None:
        xgb_p = batx_p
    if batx_p is None:
        batx_p = xgb_p

    xgb_p  = _clamp(float(xgb_p),  0.01, 0.99)
    batx_p = _clamp(float(batx_p), 0.01, 0.99)

    blended = _logistic_blend(xgb_p, batx_p, coverage, exp_pa, bvp_pa)
    source  = "logistic_fallback"

    # Apply trained isotonic on top if available — it has seen historical
    # outcomes and knows the true mapping from blended → actual hit rate.
    if load_calibrator() and _isotonic is not None:
        try:
            calibrated = float(_isotonic.predict([blended])[0])
            blended = _clamp(calibrated, 0.01, 0.99)
            source = "isotonic"
        except Exception:
            pass

    ci_lo, ci_hi = _analytic_ci(xgb_p, batx_p, coverage, exp_pa, bvp_pa, blended)

    # Confidence score: narrow CI + tier-stable → high confidence
    ci_width   = ci_hi - ci_lo
    width_conf = _clamp(1.0 - (ci_width / 0.40), 0.0, 1.0)
    cov_conf   = _clamp(coverage, 0.0, 1.0)
    sample_conf = _clamp(bvp_pa / 80.0, 0.0, 1.0)
    confidence = round(0.55 * width_conf + 0.30 * cov_conf + 0.15 * sample_conf, 3)

    tier_key, tier_label, tier_color = _demote_if_wide_ci(blended, ci_lo, ci_hi)

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
            "xgb":      round(xgb_p, 3),
            "batx":     round(batx_p, 3),
            "coverage": round(float(coverage), 3),
            "exp_pa":   round(float(exp_pa),   2),
            "bvp_pa":   int(bvp_pa or 0),
        },
    }


# ─── Training script (run manually after enough graded picks accumulate) ────
def train_from_tracker(tracker_path: str = "data/daily_tracker.json",
                       min_picks: int = 200) -> dict:
    """Fit an isotonic regression on graded 1+ hit picks. Writes to disk.

    Returns metrics dict.
    """
    import json
    if not os.path.exists(tracker_path):
        return {"trained": False, "reason": "tracker_missing"}

    with open(tracker_path) as f:
        tracker = json.load(f)

    xs, ys = [], []
    for _date, picks in (tracker or {}).items():
        for pick in (picks or []):
            if pick.get("marketKey") != "batter_hits":
                continue
            if pick.get("outcome") not in ("win", "loss"):
                continue
            blended_p = pick.get("blendedProb") or pick.get("adjProb")
            if blended_p is None:
                continue
            try:
                xs.append(float(blended_p))
                ys.append(1.0 if pick.get("outcome") == "win" else 0.0)
            except (TypeError, ValueError):
                continue

    if len(xs) < min_picks:
        return {"trained": False, "reason": "insufficient_data", "n": len(xs)}

    from sklearn.isotonic import IsotonicRegression
    import joblib
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.01, y_max=0.99)
    iso.fit(xs, ys)

    os.makedirs(os.path.dirname(_MODEL_PATH), exist_ok=True)
    joblib.dump(iso, _MODEL_PATH)

    # Reset cache so app picks up new model
    global _loaded, _isotonic
    with _lock:
        _isotonic = iso
        _loaded = True

    # Simple Brier on training set (no holdout — too few picks for split)
    preds = iso.predict(xs)
    brier = sum((p - y) ** 2 for p, y in zip(preds, ys)) / len(ys)
    return {"trained": True, "n": len(xs), "brier": round(brier, 4),
            "path": _MODEL_PATH}


if __name__ == "__main__":
    import argparse, json
    parser = argparse.ArgumentParser()
    parser.add_argument("--train", action="store_true",
                        help="Fit isotonic from data/daily_tracker.json")
    parser.add_argument("--min-picks", type=int, default=200)
    args = parser.parse_args()
    if args.train:
        result = train_from_tracker(min_picks=args.min_picks)
        print(json.dumps(result, indent=2))
    else:
        # Smoke test
        out = calibrate(0.97, 0.74, coverage=0.92, exp_pa=4.2, bvp_pa=0)
        print(json.dumps(out, indent=2))
