"""
xgb_prop_scorer.py  —  Production XGBoost Prop Scorer
═══════════════════════════════════════════════════════════════════════
Drop-in module for app.py. Import at the top of app.py:

    from xgb_prop_scorer import xgb_hit_prob, xgb_k_prob, xgb_ready

Artifact format:
    {"model": <estimator>, "features": [str, ...], "meta": {...}}

Supports:
    models/xgb_hits_over_0.5.pkl
    models/xgb_k_over_3.5.pkl
    models/xgb_k_over_4.5.pkl
    models/xgb_k_over_5.5.pkl
    models/xgb_hr_over_0.5.pkl
    models/xgb_tb_over_1.5.pkl
    models/xgb_rbi_over_0.5.pkl

Step 3: Monte Carlo anchored to XGB prediction intervals
───────────────────────────────────────────────────────────────────────
Previously Monte Carlo ran as a parallel/independent system alongside XGB.
Now it is anchored:

  1. XGB predict_proba() → raw_p
  2. apply_isotonic(raw_p, market_key) → cal_p  (2A calibration)
  3. _xgb_interval(X, model, feat_order) → (p_lo, p_hi)  [tree-level
     variance across the XGB ensemble, giving a true prediction interval]
  4. mc_simulate(cal_p, p_lo, p_hi, line, ...) → dict
     MC draws from a Beta distribution parameterised by (cal_p, p_lo, p_hi)
     so every simulation is pinned to the model’s own confidence range —
     not a free-floating simulation.

New public surface:
    xgb_hit_prob(batter, pitcher)         → float  (unchanged signature)
    xgb_hit_prob_full(batter, pitcher)    → dict   (prob + interval + MC)
    xgb_k_prob(pitcher, line)             → float  (unchanged signature)
    xgb_k_prob_full(pitcher, line)        → dict   (prob + interval + MC)
    xgb_hr_prob / xgb_tb_prob / xgb_rbi_prob  → float (unchanged)
    xgb_hr_prob_full / xgb_tb_prob_full / xgb_rbi_prob_full  → dict
    mc_simulate(cal_p, p_lo, p_hi, line, n_sims) → dict
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import traceback
from typing import Optional

import numpy as np

from config import settings
from rbi_opportunity import LEAGUE_OBP, RBI_TRAFFIC_FEATURE
from scoring_result_cache import ScoringResultCache

try:
    from stacked_calibrator import apply_isotonic as _apply_isotonic
    from stacked_calibrator import load_calibrator as _load_calibrator
    _CAL_AVAILABLE = True
except ImportError:
    _CAL_AVAILABLE = False
    def _apply_isotonic(p: float, market_key: str = "batter_hits") -> float:
        return float(p)
    def _load_calibrator(market_key: str = "batter_hits") -> bool:
        return False


def _xgb_calibrated(market_key: str) -> bool:
    """Whether this market's XGB probability is trustworthy as a calibrated value.

    Two independent ways a market qualifies:

      1. **Self-calibrated model.** The model artifact is itself a
         ``CalibratedClassifierCV`` (isotonic/sigmoid), so ``predict_proba`` is
         already a true probability. This is what ``regenerate_models.py`` /
         ``train_prop_models.py`` produce. Detected at load time
         (``_self_cal[model_key]``).
      2. **Post-hoc isotonic.** A trained ``models/iso_{market}.pkl`` exists and
         maps a raw score to a probability (the original two-stage design).

    A raw, uncalibrated ``XGBClassifier`` qualifies under NEITHER — its
    ``predict_proba`` is bimodal/extreme (~0.0015 or ~0.998 for ordinary
    hitters). When a market fails both checks we return ``False`` so the scorer
    emits nothing and callers fall back to the analytic model — strictly more
    accurate than blending uncalibrated XGB output (which otherwise dragged a
    true ~0.65 hitter down to ~0.30 in the stacked calibrator)."""
    try:
        if _market_is_self_calibrated(market_key):
            return True
        return bool(_load_calibrator(market_key))
    except Exception:
        return False


def _is_self_calibrated(model, meta: Optional[dict]) -> bool:
    """A model whose own predict_proba is already a calibrated probability —
    i.e. a fitted CalibratedClassifierCV. Detected structurally (no sklearn
    import needed) with a meta fallback."""
    try:
        if hasattr(model, "calibrated_classifiers_"):
            return True
    except Exception:
        pass
    m = meta or {}
    if "Calibrated" in str(m.get("model_type", "")):
        return True
    if str(m.get("calibration", "")).lower() in ("isotonic", "sigmoid") or \
       str(m.get("calibration", "")).lower().startswith("isotonic"):
        return True
    return False


def _market_is_self_calibrated(market_key: str) -> bool:
    """True if any loaded model that maps to this stacked-calibrator market_key
    is a self-calibrated artifact."""
    if not _loaded:
        _load_models()
    for mk, is_cal in _self_cal.items():
        if is_cal and _MARKET_KEY_MAP.get(mk) == market_key:
            return True
    return False


try:
    from fangraphs_loader import (
        get_batter_stats,
        get_pitcher_stats,
        get_batter_projection,
        get_pitcher_projection,
    )
    _FG_AVAILABLE = True
except ImportError:
    _FG_AVAILABLE = False
    def get_batter_stats(*a, **kw): return {}
    def get_pitcher_stats(*a, **kw): return {}
    def get_batter_projection(*a, **kw): return {}
    def get_pitcher_projection(*a, **kw): return {}

try:
    from savant_bat_tracking import bat_tracking as _sv_bat_tracking
    _BT_AVAILABLE = True
except ImportError:
    _BT_AVAILABLE = False
    def _sv_bat_tracking(*a, **kw): return {}

try:
    from umpire_loader import get_umpire_features as _get_ump_features
    _UMP_AVAILABLE = True
except ImportError:
    _UMP_AVAILABLE = False
    def _get_ump_features(name: str) -> dict:
        return {"ump_zone_size": 0.0, "ump_k_boost": 0.0}

try:
    from lineup_loader import get_lineup_features as _get_lineup_features
    _LINEUP_AVAILABLE = True
except ImportError:
    _LINEUP_AVAILABLE = False
    def _get_lineup_features(**kw) -> dict:
        return {
            "expected_pa": 4.20,
            "batting_order": 0,
            "lineup_confirmed": 0,
            RBI_TRAFFIC_FEATURE: LEAGUE_OBP,
        }

# Mirror of lineup_loader._PA_BY_SLOT / regenerate_models._PA_BY_SLOT so an
# explicitly-supplied batting-order slot maps to the SAME expected_pa the models
# trained on (no train/serve skew).
_PA_BY_SLOT = {1: 4.60, 2: 4.52, 3: 4.44, 4: 4.36, 5: 4.28,
               6: 4.18, 7: 4.08, 8: 3.96, 9: 3.84}
_DEFAULT_PA = 4.20


def _resolve_lineup_role(
    d: dict,
    mlbam_id,
    player_name: str,
    *,
    include_rbi_context: bool = False,
) -> dict:
    """Resolve train/serve-consistent lineup role and optional RBI traffic."""
    lookup = {}
    if include_rbi_context:
        try:
            lookup = _get_lineup_features(mlbam_id=mlbam_id, player_name=player_name) or {}
        except Exception:
            lookup = {}

    bo_explicit = d.get("batting_order")
    if bo_explicit is None:
        bo_explicit = d.get("slot")
    if bo_explicit is not None:
        try:
            bo = int(bo_explicit)
        except (TypeError, ValueError):
            bo = 0
        epa = d.get("expected_pa")
        try:
            epa = float(epa) if epa is not None else _PA_BY_SLOT.get(bo, _DEFAULT_PA)
        except (TypeError, ValueError):
            epa = _PA_BY_SLOT.get(bo, _DEFAULT_PA)
        lc = d.get("lineup_confirmed")
        if lc is None:
            lc = 1 if 1 <= bo <= 9 else 0
    else:
        if not lookup:
            lookup = _get_lineup_features(mlbam_id=mlbam_id, player_name=player_name)
        bo = float(lookup.get("batting_order", 0) or 0)
        epa = float(lookup.get("expected_pa", _DEFAULT_PA))
        lc = float(lookup.get("lineup_confirmed", 0) or 0)

    raw_context = d.get("rbiTrafficObp")
    if raw_context is None:
        raw_context = d.get(RBI_TRAFFIC_FEATURE)
    if raw_context is None:
        raw_context = lookup.get(RBI_TRAFFIC_FEATURE, LEAGUE_OBP)
    try:
        context = float(raw_context)
        if not 0.0 <= context <= 1.0:
            context = LEAGUE_OBP
    except (TypeError, ValueError):
        context = LEAGUE_OBP
    return {
        "expected_pa": float(epa),
        "batting_order": float(bo),
        "lineup_confirmed": float(lc),
        RBI_TRAFFIC_FEATURE: context,
    }

_HERE = os.path.dirname(os.path.abspath(__file__))
_MODEL_DIR = os.path.join(_HERE, "models")
_FEAT_FILE = os.path.join(_MODEL_DIR, "xgb_feature_cols.json")

_MODEL_PATHS = {
    "hits":     os.path.join(_MODEL_DIR, "xgb_hits_over_0.5.pkl"),
    "hits_1.5": os.path.join(_MODEL_DIR, "xgb_hits_over_1.5.pkl"),
    "k_2.5": os.path.join(_MODEL_DIR, "xgb_k_over_2.5.pkl"),
    "k_3.5": os.path.join(_MODEL_DIR, "xgb_k_over_3.5.pkl"),
    "k_4.5": os.path.join(_MODEL_DIR, "xgb_k_over_4.5.pkl"),
    "k_5.5": os.path.join(_MODEL_DIR, "xgb_k_over_5.5.pkl"),
    "k_6.5": os.path.join(_MODEL_DIR, "xgb_k_over_6.5.pkl"),
    "k_7.5": os.path.join(_MODEL_DIR, "xgb_k_over_7.5.pkl"),
    "hr":      os.path.join(_MODEL_DIR, "xgb_hr_over_0.5.pkl"),
    "tb":      os.path.join(_MODEL_DIR, "xgb_tb_over_1.5.pkl"),
    "tb_2.5":  os.path.join(_MODEL_DIR, "xgb_tb_over_2.5.pkl"),
    "tb_3.5":  os.path.join(_MODEL_DIR, "xgb_tb_over_3.5.pkl"),
    "rbi":     os.path.join(_MODEL_DIR, "xgb_rbi_over_0.5.pkl"),
    "rbi_1.5": os.path.join(_MODEL_DIR, "xgb_rbi_over_1.5.pkl"),
}

# Map scorer model keys -> stacked_calibrator market keys
_MARKET_KEY_MAP = {
    "hits":     "batter_hits",
    "hits_1.5": "batter_hits",
    "k_2.5": "pitcher_strikeouts",
    "k_3.5": "pitcher_strikeouts",
    "k_4.5": "pitcher_strikeouts",
    "k_5.5": "pitcher_strikeouts",
    "k_6.5": "pitcher_strikeouts",
    "k_7.5": "pitcher_strikeouts",
    "hr":      "batter_hr",
    "tb":      "batter_tb",
    "tb_2.5":  "batter_tb",
    "tb_3.5":  "batter_tb",
    "rbi":     "batter_rbi",
    "rbi_1.5": "batter_rbi",
}

# (family, line) -> model key for the batter markets. The XGB probability is
# only a valid P(over) AT its trained threshold, so callers route by the row's
# actual line and fall back to the analytic model for unmapped lines.
_BATTER_LINE_MODELS = {
    ("hits", 0.5): "hits", ("hits", 1.5): "hits_1.5",
    ("tb", 1.5): "tb", ("tb", 2.5): "tb_2.5", ("tb", 3.5): "tb_3.5",
    ("hr", 0.5): "hr",
    ("rbi", 0.5): "rbi", ("rbi", 1.5): "rbi_1.5",
}

_lock = threading.Lock()
_models: dict = {}
_feat_cols: dict = {}
_self_cal: dict = {}   # {model_key: bool} — True if the artifact is self-calibrated
_loaded = False

# Number of MC trials. 10 000 gives <0.005 SE on a 50% probability.
_MC_N_SIMS = 10_000
_MC_RNG = np.random.default_rng(seed=42)
_SCORE_CACHE = ScoringResultCache()


def _score_cache_key(
    output_kind: str,
    model_key: str,
    market_key: str,
    line: float | None,
    X: np.ndarray,
) -> tuple:
    """Build a compact identity from the exact final model feature vector."""
    features = np.ascontiguousarray(X, dtype=np.float32)
    digest = hashlib.blake2b(features.tobytes(), digest_size=16).digest()
    return (
        output_kind,
        model_key,
        id(_models.get(model_key)),
        market_key,
        None if line is None else round(float(line), 4),
        features.shape,
        digest,
    )


def xgb_score_cache_status() -> dict:
    """Return secret-free process-local scoring cache diagnostics."""
    return {
        **_SCORE_CACHE.status(),
        "ttl_seconds": settings.xgb_score_cache_ttl,
        "max_entries": settings.xgb_score_cache_max_entries,
    }


def xgb_score_cache_clear(*, reset_metrics: bool = False) -> None:
    """Clear memoized scores, primarily for model reloads and diagnostics."""
    _SCORE_CACHE.clear(reset_metrics=reset_metrics)


# ─── Model loading ───────────────────────────────────────────────────────────

def _load_models() -> None:
    global _loaded
    with _lock:
        if _loaded:
            return
        try:
            import joblib

            feat_map = {}
            if os.path.exists(_FEAT_FILE):
                with open(_FEAT_FILE) as f:
                    feat_map = json.load(f)

            for key, path in _MODEL_PATHS.items():
                if not os.path.exists(path):
                    continue
                try:
                    payload = joblib.load(path)
                except Exception:
                    print(f"[xgb_scorer] failed to load {path} — {traceback.format_exc()}")
                    continue

                if isinstance(payload, dict) and "model" in payload:
                    _models[key] = payload["model"]
                    _feat_cols[key] = payload.get("features") or feat_map.get(key, [])
                    meta = payload.get("meta", {})
                    _self_cal[key] = _is_self_calibrated(payload["model"], meta)
                    print(
                        f"[xgb_scorer] loaded {key} from {path} "
                        f"(target={meta.get('target', 'unknown')}, "
                        f"calibrated={_self_cal[key]})"
                    )
                else:
                    _models[key] = payload
                    _feat_cols[key] = feat_map.get(key, [])
                    _self_cal[key] = _is_self_calibrated(payload, None)
                    print(f"[xgb_scorer] loaded {key} (legacy artifact, "
                          f"calibrated={_self_cal[key]})")
        except Exception:
            print("[xgb_scorer] model load failed —", traceback.format_exc())
        finally:
            _loaded = True


_MARKET_KEY_FOR = {
    "hits": "batter_hits",
    "k":    "pitcher_strikeouts",
    "hr":   "batter_hr",
    "tb":   "batter_tb",
    "rbi":  "batter_rbi",
}


def xgb_ready(market: str = "hits") -> bool:
    """A market is "ready" only when its model is loaded AND a trained isotonic
    calibrator exists for it. A raw XGBClassifier's probabilities are
    uncalibrated (see _xgb_calibrated), so without the calibrator we report not
    ready and callers use the analytic model instead."""
    if not _loaded:
        _load_models()
    if market == "hits":
        model_ok = "hits" in _models
    elif market == "k":
        model_ok = any(k.startswith("k_") for k in _models)
    elif market in ("hr", "tb", "rbi"):
        model_ok = market in _models
    else:
        return False
    return model_ok and _xgb_calibrated(_MARKET_KEY_FOR[market])


def xgb_feature_in_use(feature: str, model_prefix: str = "k_") -> bool:
    """True when any loaded model whose key starts with `model_prefix` trains
    on `feature`. Lets callers skip an expensive enrichment (e.g. the live
    Stuff+ Statcast pull) while the committed models don't consume it yet."""
    if not _loaded:
        _load_models()
    return any(feature in (_feat_cols.get(k) or [])
               for k in _models if k.startswith(model_prefix))


def xgb_line_ready(family: str, line) -> bool:
    """Line-aware readiness: True only when a calibrated model trained at THIS
    exact threshold is loaded. Off-line rows must fall back to the analytic
    model rather than borrow a neighbouring line's probability."""
    if not _loaded:
        _load_models()
    try:
        line = float(line)
    except (TypeError, ValueError):
        return False
    key = f"k_{line}" if family == "k" else _BATTER_LINE_MODELS.get((family, line))
    if key is None or key not in _models:
        return False
    return _xgb_calibrated(_MARKET_KEY_MAP.get(key, ""))


# ─── XGB prediction interval (tree-level variance) ───────────────────────────────

def _xgb_interval(
    X: np.ndarray,
    model,
    market_key: str,
    alpha: float = 0.10,   # 90% interval by default
) -> tuple[float, float]:
    """
    Derive a prediction interval from the XGB model's own tree ensemble.

    Strategy: collect leaf-node predictions from every tree (via
    apply() or staged_predict_proba equivalent), then take the
    (alpha/2) and (1 - alpha/2) quantiles across trees as the
    interval bounds. This is the XGB-native analogue of a jackknife
    prediction interval and costs no extra model training.

    Fallback: if staged_predict_proba is unavailable (pure XGBClassifier
    without iteration tracking), we derive the interval analytically
    from the calibrated probability using a Beta distribution width
    informed by the model's number of estimators and feature coverage.

    Returns (p_lo, p_hi) as calibrated floats in [0.01, 0.99].
    """
    # ── Attempt 1: tree-margin variance via XGBoost booster interface ───────
    try:
        booster = getattr(model, "get_booster", None)
        if booster is not None:
            bst = booster()
            import xgboost as xgb
            dmat = xgb.DMatrix(X)
            # pred_contribs=True gives per-tree margins; ntree_limit iterates trees
            n_trees = bst.num_boosted_rounds()
            margins = []
            for t in range(0, n_trees, max(1, n_trees // 50)):  # sample 50 checkpoints
                m = bst.predict(dmat, ntree_limit=t + 1, output_margin=True)
                margins.append(float(m[0]))
            if len(margins) >= 5:
                margins_arr = np.array(margins)
                lo_margin = float(np.quantile(margins_arr, alpha / 2))
                hi_margin = float(np.quantile(margins_arr, 1 - alpha / 2))
                sigmoid = lambda z: 1.0 / (1.0 + math.exp(-z))
                p_lo = _apply_isotonic(max(0.01, sigmoid(lo_margin)), market_key)
                p_hi = _apply_isotonic(min(0.99, sigmoid(hi_margin)), market_key)
                if p_lo < p_hi:
                    return round(p_lo, 3), round(p_hi, 3)
    except Exception:
        pass

    # ── Fallback: analytic Beta-width from calibrated p + n_estimators ──────
    try:
        cal_p = float(_apply_isotonic(
            float(model.predict_proba(X)[0, 1]), market_key
        ))
        n_est = getattr(model, "n_estimators", 100)
        # Width shrinks with more trees (larger ensemble = narrower interval)
        # Empirically: 100 trees ≈ 0.08 half-width, 500 trees ≈ 0.04 half-width
        half_w = 0.40 / math.sqrt(n_est)
        half_w = max(0.03, min(0.15, half_w))
        p_lo = max(0.01, cal_p - half_w)
        p_hi = min(0.99, cal_p + half_w)
        return round(p_lo, 3), round(p_hi, 3)
    except Exception:
        return 0.30, 0.70  # safe wide fallback


# ─── Monte Carlo simulation anchored to XGB interval ────────────────────────────

def mc_simulate(
    cal_p: float,
    p_lo: float,
    p_hi: float,
    line: float = 0.5,
    n_sims: int = _MC_N_SIMS,
    rng: Optional[np.random.Generator] = None,
) -> dict:
    """
    Monte Carlo simulation anchored to the XGB model's prediction interval.

    Instead of running MC independently, we:
      1. Parameterise a Beta(alpha, beta) distribution whose mean = cal_p
         and whose 90% interval matches (p_lo, p_hi). This pins the MC
         distribution to exactly what the XGB model believes.
      2. Draw n_sims probability samples p_i ~ Beta(a, b).
      3. For each p_i, simulate one Bernoulli outcome (or Poisson for K props).
      4. Aggregate over/under rates, percentiles, and variance.

    Parameters
    ----------
    cal_p   : float  Calibrated probability from apply_isotonic()
    p_lo    : float  Lower bound of XGB prediction interval
    p_hi    : float  Upper bound of XGB prediction interval
    line    : float  The prop line (e.g. 0.5 for hits, 4.5 for Ks)
    n_sims  : int    Number of Monte Carlo trials
    rng     : optional numpy Generator (for testing with fixed seeds)

    Returns
    -------
    {
      "mc_prob_over":  float,   # fraction of sims where outcome > line
      "mc_prob_under": float,   # 1 - mc_prob_over
      "mc_mean":       float,   # mean simulated probability across trials
      "mc_p10":        float,   # 10th percentile of simulated probs
      "mc_p25":        float,
      "mc_p75":        float,
      "mc_p90":        float,   # 90th percentile of simulated probs
      "mc_std":        float,   # std dev of simulated probs
      "beta_alpha":    float,   # fitted Beta param
      "beta_beta":     float,   # fitted Beta param
      "n_sims":        int,
      "anchored":      bool,    # True = interval came from XGB, not analytic
    }
    """
    rng = rng or _MC_RNG
    cal_p = float(np.clip(cal_p, 0.01, 0.99))
    p_lo  = float(np.clip(p_lo,  0.01, 0.99))
    p_hi  = float(np.clip(p_hi,  0.01, 0.99))

    # Fit Beta(a, b) such that mean = cal_p and the 90% CI ≈ [p_lo, p_hi].
    # The Beta mean = a / (a+b) and variance = a*b / ((a+b)^2 * (a+b+1)).
    # We set the variance from the interval width: var ≈ (width / 3.29)^2
    # (3.29 ≈ z_{0.95} * 2 for a 90% interval on a normal approximation).
    width  = max(0.02, p_hi - p_lo)
    var    = (width / 3.29) ** 2
    mean   = cal_p
    conc   = max(1.0, mean * (1 - mean) / var - 1.0)  # concentration = a+b
    a      = mean * conc
    b      = (1.0 - mean) * conc
    a      = max(0.5, a)
    b      = max(0.5, b)

    # Draw p_i samples from the fitted Beta
    p_samples = rng.beta(a, b, size=n_sims)           # shape (n_sims,)
    p_samples = np.clip(p_samples, 0.001, 0.999)

    # For each sampled p_i, simulate a Bernoulli "did this trial clear the
    # line" outcome. cal_p (and therefore every p_i drawn from the Beta) is
    # already the calibrated probability of going OVER this specific line, so an
    # outcome of 1 means that trial went over. The over-rate is just the mean of
    # the outcomes — the line is already baked into cal_p and must NOT be
    # re-applied here. (The previous `outcomes > line - 1` test compared a 0/1
    # Bernoulli against the raw line, forcing mc_prob_over to 1.0 for every
    # hits/HR prop and 0.0 for every K prop.)
    outcomes  = (rng.uniform(size=n_sims) < p_samples).astype(np.float32)
    prob_over  = float(np.mean(outcomes))
    prob_under = 1.0 - prob_over

    return {
        "mc_prob_over":  round(prob_over,  4),
        "mc_prob_under": round(prob_under, 4),
        "mc_mean":       round(float(np.mean(p_samples)),               4),
        "mc_p10":        round(float(np.percentile(p_samples,  10)),    4),
        "mc_p25":        round(float(np.percentile(p_samples,  25)),    4),
        "mc_p75":        round(float(np.percentile(p_samples,  75)),    4),
        "mc_p90":        round(float(np.percentile(p_samples,  90)),    4),
        "mc_std":        round(float(np.std(p_samples)),                4),
        "beta_alpha":    round(a, 3),
        "beta_beta":     round(b, 3),
        "n_sims":        n_sims,
        "anchored":      True,
    }


# ─── FanGraphs enrichment (unchanged) ────────────────────────────────────────────

def _enrich_batter_from_fg(d: dict) -> dict:
    if not _FG_AVAILABLE:
        return d
    pid  = str(d.get("fgId") or d.get("playerid") or d.get("fg_id") or "")
    name = str(d.get("name") or d.get("Name") or d.get("PlayerName") or "")
    fg = {}
    if pid and pid not in ("", "None", "0"):
        fg = get_batter_stats(player_id=pid)
    if not fg and name:
        fg = get_batter_stats(name=name)
    if not fg:
        return d
    fg_map = {
        "xAVG": "svxba", "xwOBA": "svxwoba", "xSLG": "svxslg",
        "EV": "svev", "Barrel%": "svbrlpct", "HardHit%": "svhhpct",
        "SwStr%": "svsspct", "LA": "svla", "K%": "fgkpct",
        "BB%": "fgbbpct", "wOBA": "fgwoba", "SLG": "fgslg",
        "Bats": "fgbats", "xMLBAMID": "mlbamid", "playerid": "fgId",
        # HR power/loft skills (raw FG values; consumed by _build_hr_features).
        "ISO": "fgiso", "HR/FB": "fghrfb", "FB%": "fgfbpct", "maxEV": "fgmaxev",
    }
    enriched = dict(d)
    for fg_col, scorer_key in fg_map.items():
        if scorer_key not in enriched or enriched[scorer_key] is None:
            val = fg.get(fg_col)
            if val is not None:
                enriched[scorer_key] = val
    proj = {}
    if pid and pid not in ("", "None", "0"):
        proj = get_batter_projection(player_id=pid)
    if not proj and name:
        proj = get_batter_projection(name=name)
    if proj:
        for col, key in [
            ("wOBA", "projwoba"), ("HR", "projhr"), ("H", "projh"),
            ("AVG", "projava"), ("OBP", "projobp"), ("SLG", "projslg"),
        ]:
            if key not in enriched or enriched[key] is None:
                val = proj.get(col)
                if val is not None:
                    enriched[key] = val
    return enriched


def _enrich_pitcher_from_fg(d: dict) -> dict:
    if not _FG_AVAILABLE:
        return d
    pid  = str(d.get("fgId") or d.get("playerid") or d.get("fg_id") or "")
    name = str(d.get("name") or d.get("Name") or d.get("PlayerName") or "")
    fg = {}
    if pid and pid not in ("", "None", "0"):
        fg = get_pitcher_stats(player_id=pid)
    if not fg and name:
        fg = get_pitcher_stats(name=name)
    if not fg:
        return d
    fg_map = {
        "xERA": "svxera", "ERA": "fgera", "K%": "fgkpct",
        "BB%": "fgbbpct", "SwStr%": "svwhiffpct",
        "playerid": "fgId", "xMLBAMID": "mlbamid",
        # HR-allowed profile (raw FG values; consumed by _build_hr_features).
        "HR/9": "fghr9", "HR/FB": "fghrfb", "FB%": "fgfbpct", "Barrel%": "fgbrlpct",
    }
    enriched = dict(d)
    for fg_col, scorer_key in fg_map.items():
        if scorer_key not in enriched or enriched[scorer_key] is None:
            val = fg.get(fg_col)
            if val is not None:
                enriched[scorer_key] = val
    proj = {}
    if pid and pid not in ("", "None", "0"):
        proj = get_pitcher_projection(player_id=pid)
    if not proj and name:
        proj = get_pitcher_projection(name=name)
    if proj:
        for col, key in [
            ("ERA", "projera"), ("K%", "projkpct"), ("BB%", "projbbpct"),
            ("IP", "projip"), ("K/9", "projk9"), ("FIP", "projfip"),
        ]:
            if key not in enriched or enriched[key] is None:
                val = proj.get(col)
                if val is not None:
                    enriched[key] = val
    return enriched


def _sf(d: dict, *keys, default: float = 0.0) -> float:
    for k in keys:
        v = d.get(k)
        if v is None:
            continue
        try:
            f = float(v)
            if not np.isnan(f) and not np.isinf(f):
                return f
        except (TypeError, ValueError):
            continue
    return default


# ─── Feature builders (unchanged) ───────────────────────────────────────────────

def _build_hit_features(batter: dict, pitcher: dict, feat_order: list) -> Optional[np.ndarray]:
    bat_side = (batter.get("fgbats") or batter.get("bats") or "R").upper()[:1]
    pit_hand = (pitcher.get("pitchHand") or pitcher.get("throws") or "R").upper()[:1]
    platoon  = 1 if (bat_side == "L" and pit_hand == "R") or (bat_side == "R" and pit_hand == "L") else 0
    mlbam_id    = batter.get("mlbamid") or batter.get("xMLBAMID")
    player_name = batter.get("name") or batter.get("Name") or batter.get("PlayerName")
    lineup_feats     = _resolve_lineup_role(batter, mlbam_id, player_name)
    expected_pa      = lineup_feats["expected_pa"]
    batting_order    = lineup_feats["batting_order"]
    lineup_confirmed = lineup_feats["lineup_confirmed"]
    raw = {
        "sv_xba":               _sf(batter,  "svxba",  "xAVG",   default=0.250),
        "sv_xwoba":             _sf(batter,  "svxwoba","xwOBA","fgwoba",  default=0.320),
        "sv_xslg":              _sf(batter,  "svxslg", "xSLG",  "fgslg",  default=0.400),
        "sv_ev":                _sf(batter,  "svev",   "EV",               default=88.0),
        "sv_brl_pct":           _sf(batter,  "svbrlpct","Barrel%",         default=4.0),
        "sv_hh_pct":            _sf(batter,  "svhhpct","HardHit%",         default=35.0),
        "sv_ss_pct":            _sf(batter,  "svsspct","SwStr%",            default=10.0),
        "sv_la":                _sf(batter,  "svla",   "LA",               default=12.0),
        "sv_k_pct":             _sf(batter,  "fgkpct", "K%",  "svkpct",   default=22.0),
        "sv_bb_pct":            _sf(batter,  "fgbbpct","BB%", "svbbpct",  default=8.0),
        "opp_xera":             _sf(pitcher, "svxera", "xERA","fgera",    default=4.50),
        "opp_k_pct":            _sf(pitcher, "fgkpct", "K%",  "svkpct",   default=22.0),
        "opp_bb_pct":           _sf(pitcher, "fgbbpct","BB%", "svbbpct",  default=8.0),
        "opp_whiff":            _sf(pitcher, "svwhiffpct","SwStr%","whiffpct", default=24.0),
        "bats_L":               1 if bat_side == "L" else 0,
        "throws_R":             1 if pit_hand == "R" else 0,
        "platoon_adv":          platoon,
        "l7_hits":              _sf(batter,  "l7Hits",   "l7hits",   default=1.5),
        "l14_hits":             _sf(batter,  "l14Hits",  "l14hits",  default=3.0),
        "l7_hit_rate":          _sf(batter,  "l7HitRate","l7hitrate",default=0.50),
        "expected_pa":          expected_pa,
        "batting_order":        float(batting_order),
        "lineup_confirmed":     float(lineup_confirmed),
        "park_factor":          _sf(batter,  "parkFactor","park_factor",       default=1.00),
        "wx_temp_mult":         _sf(batter,  "wxTempMult","wx_temp_mult",      default=1.00),
        "wx_wind_mult":         _sf(batter,  "wxWindMult","wx_wind_mult",      default=1.00),
        "pitch_mix_slg_edge":   _sf(batter,  "pitchMixSlgEdge","pitch_mix_slg_edge",  default=0.00),
        "bvp_woba_edge_shrunk": _sf(batter,  "bvpWobaEdge","bvp_woba_edge_shrunk",   default=0.00),
        "split_ops_edge":       _sf(batter,  "splitOpsEdge","split_ops_edge",         default=0.00),
    }
    for pct_key in ("sv_k_pct", "sv_bb_pct", "opp_k_pct", "opp_bb_pct"):
        if 0 < raw[pct_key] <= 1.0:
            raw[pct_key] *= 100.0
    if feat_order:
        try:
            return np.array([[raw.get(c, 0.0) for c in feat_order]], dtype=np.float32)
        except Exception:
            return None
    hits_features = [
        "sv_xba","sv_xwoba","sv_xslg","sv_ev","sv_brl_pct","sv_hh_pct",
        "sv_ss_pct","sv_la","sv_k_pct","sv_bb_pct",
        "opp_xera","opp_k_pct","opp_bb_pct","opp_whiff",
        "bats_L","throws_R","platoon_adv",
        "l7_hits","l14_hits","l7_hit_rate",
        "expected_pa","batting_order","lineup_confirmed",
        "park_factor","wx_temp_mult","wx_wind_mult",
        "pitch_mix_slg_edge","bvp_woba_edge_shrunk","split_ops_edge",
    ]
    return np.array([[raw[c] for c in hits_features]], dtype=np.float32)


def _build_k_features(pitcher: dict, feat_order: list) -> Optional[np.ndarray]:
    raw = {
        "sv_xera":          _sf(pitcher, "svxera","xERA","fgera",         default=4.50),
        "sv_era":           _sf(pitcher, "fgera","ERA",                   default=4.50),
        "sv_k_pct":         _sf(pitcher, "fgkpct","K%","svkpct",         default=22.0),
        "sv_bb_pct":        _sf(pitcher, "fgbbpct","BB%","svbbpct",      default=8.0),
        "sv_whiff_pct":     _sf(pitcher, "svwhiffpct","SwStr%","whiffpct",default=24.0),
        "l3_ks":            _sf(pitcher, "l3Ks","l3ks",                  default=4.5),
        "l5_ks":            _sf(pitcher, "l5Ks","l5ks",                  default=4.5),
        "l5_k_rate":        _sf(pitcher, "l5KRate","l5krate",            default=0.22),
        "l10_ks":           _sf(pitcher, "l10Ks","l10ks",                default=4.5),
        "l3_ip":            _sf(pitcher, "l3IP","l3ip",                  default=5.0),
        "l5_ip":            _sf(pitcher, "l5IP","l5ip",                  default=5.0),
        "days_rest":        _sf(pitcher, "daysRest","days_rest",          default=5.0),
        "opp_lineup_k_pct": _sf(pitcher, "oppKPct","opponentkpct","opp_lineup_k_pct", default=22.0),
        "opp_lineup_xwoba": _sf(pitcher, "oppWoba","opponentxwoba","opp_lineup_xwoba",default=0.320),
        "ump_zone_size":    _sf(pitcher, "ump_zone_size",                 default=0.0),
        "ump_k_boost":      _sf(pitcher, "ump_k_boost",                   default=0.0),
        # Pitch-mix / arsenal swing-and-miss (usage-weighted across the arsenal).
        # Outcome-based leading indicators of K upside beyond season K rate.
        "arsenal_whiff_pct":   _sf(pitcher, "arsenalWhiff","arsenal_whiff_pct","arsenal_whiff",     default=24.5),
        "arsenal_putaway_pct": _sf(pitcher, "arsenalPutaway","arsenal_putaway_pct","arsenal_putaway", default=18.0),
        # Physics-based Stuff+ (stuff_model.py; league 100, ±10/SD). NaN when
        # no score is available — matching training, where a below-pitch-floor
        # pitcher-season is NaN and never imputed (XGB missing branch).
        "stuff_plus": _sf(pitcher, "stuffPlus", "stuff_plus", default=float("nan")),
    }
    for pct_key in ("sv_k_pct", "sv_bb_pct", "opp_lineup_k_pct"):
        if 0 < raw[pct_key] <= 1.0:
            raw[pct_key] *= 100.0
    raw["days_rest"] = max(0.0, min(14.0, raw["days_rest"]))
    k_features = [
        "sv_xera","sv_era","sv_k_pct","sv_bb_pct","sv_whiff_pct",
        "l3_ks","l5_ks","l5_k_rate","l10_ks",
        "l3_ip","l5_ip","days_rest",
        "opp_lineup_k_pct","opp_lineup_xwoba",
        "ump_zone_size","ump_k_boost",
        "arsenal_whiff_pct","arsenal_putaway_pct",
        "stuff_plus",
    ]
    if feat_order:
        try:
            return np.array([[raw.get(c, 0.0) for c in feat_order]], dtype=np.float32)
        except Exception:
            return None
    return np.array([[raw[c] for c in k_features]], dtype=np.float32)


def _build_batter_market_features(batter: dict, pitcher: dict, feat_order: list) -> Optional[np.ndarray]:
    """Shared feature builder for the power/run batter markets (HR, TB, RBI).

    Produces the SUPERSET of keys those three models use; each model selects its
    own columns via `feat_order`. Scales mirror regenerate_models exactly:
    Barrel%/HardHit%/HR-FB/FB% are raw FG fractions (NOT ×100); K%/BB% are
    percent-scaled. Defaults are the batter-market train medians (identical
    across HR/TB/RBI since they share the same training rows) so an un-enriched
    call contributes a neutral row."""
    bat_side = (batter.get("fgbats") or batter.get("bats") or "R").upper()[:1]
    pit_hand = (pitcher.get("pitchHand") or pitcher.get("throws") or "R").upper()[:1]
    platoon  = 1 if (bat_side == "L" and pit_hand == "R") or (bat_side == "R" and pit_hand == "L") else 0
    mlbam_id    = batter.get("mlbamid") or batter.get("xMLBAMID")
    player_name = batter.get("name") or batter.get("Name") or batter.get("PlayerName")
    lineup_feats = _resolve_lineup_role(
        batter,
        mlbam_id,
        player_name,
        include_rbi_context=RBI_TRAFFIC_FEATURE in feat_order,
    )
    expected_pa   = lineup_feats["expected_pa"]
    batting_order = lineup_feats["batting_order"]
    # Bat-tracking (2024+): looked up here so EVERY caller gets serve parity.
    # No row (below-min-swings batter) → NaN, matching training where bt_* is
    # never imputed and XGB's missing-branch handles it.
    bt = {}
    if _BT_AVAILABLE and (player_name or mlbam_id):
        try:
            bt = _sv_bat_tracking(name=player_name, player_id=mlbam_id) or {}
        except Exception:
            bt = {}
    _NAN = float("nan")
    raw = {
        "sv_xba":     _sf(batter,  "svxba",  "xAVG",          default=0.2427),
        "sv_xwoba":   _sf(batter,  "svxwoba","xwOBA","fgwoba",default=0.3171),
        "sv_xslg":    _sf(batter,  "svxslg", "xSLG", "fgslg", default=0.406),
        "sv_iso":     _sf(batter,  "fgiso",  "ISO",           default=0.158),
        "sv_ev":      _sf(batter,  "svev",   "EV",            default=88.87),
        "sv_brl_pct": _sf(batter,  "svbrlpct","Barrel%",      default=0.076),   # fraction
        "sv_hh_pct":  _sf(batter,  "svhhpct","HardHit%",      default=0.396),   # fraction
        "sv_la":      _sf(batter,  "svla",   "LA",            default=13.16),
        "sv_hrfb":    _sf(batter,  "fghrfb", "HR/FB",         default=0.120),   # fraction
        "sv_fb_pct":  _sf(batter,  "fgfbpct","FB%",           default=0.376),   # fraction
        "sv_maxev":   _sf(batter,  "fgmaxev","maxEV",         default=110.6),
        "sv_k_pct":   _sf(batter,  "fgkpct", "K%", "svkpct",  default=22.37),   # percent
        "sv_bb_pct":  _sf(batter,  "fgbbpct","BB%", "svbbpct",default=8.27),    # percent
        "opp_hr9":    _sf(pitcher, "fghr9",  "HR/9",          default=1.174),
        "opp_hrfb":   _sf(pitcher, "fghrfb", "HR/FB",         default=0.123),   # fraction
        "opp_fb_pct": _sf(pitcher, "fgfbpct","FB%",           default=0.373),   # fraction
        "opp_barrel": _sf(pitcher, "fgbrlpct","Barrel%",      default=0.079),   # fraction
        "opp_xera":   _sf(pitcher, "svxera", "xERA","fgera",  default=4.158),
        "opp_k_pct":  _sf(pitcher, "fgkpct", "K%", "svkpct",  default=22.29),   # percent
        "opp_bb_pct": _sf(pitcher, "fgbbpct","BB%", "svbbpct",default=7.33),    # percent
        "bats_L":     1 if bat_side == "L" else 0,
        "throws_R":   1 if pit_hand == "R" else 0,
        "platoon_adv":platoon,
        "batting_order": float(batting_order),
        "expected_pa":   expected_pa,
        "rbi_traffic_obp": float(lineup_feats[RBI_TRAFFIC_FEATURE]),
        "l7_hits":        _sf(batter, "l7Hits", "l7hits",                       default=0.8571),
        "l7_ev":          _sf(batter, "l7Ev",          "l7_ev",          default=83.12),
        "l7_barrel":      _sf(batter, "l7Barrel",      "l7_barrel",      default=0.0347),
        "ev_momentum":    _sf(batter, "evMomentum",    "ev_momentum",    default=1.0),
        "barrel_momentum":_sf(batter, "barrelMomentum","barrel_momentum",default=0.954),
        # Venue context (supplied by the caller from the game's home team id;
        # neutral 1.0 when unknown). park_hr is the hand-aware HR multiplier.
        "park_factor":    _sf(batter, "parkFactor", "park_factor",       default=1.0),
        "park_hr":        _sf(batter, "parkHr",     "park_hr",           default=1.0),
        # Opposing starter's physics-based Stuff+ (stuff_model.py; league 100,
        # ±10/SD). NaN = no score, matching un-imputed training (bt_* policy).
        "opp_stuff_plus": _sf(pitcher, "stuffPlus", "oppStuffPlus", "opp_stuff_plus",
                              default=_NAN),
        # Bat-tracking (NaN = unknown, matching un-imputed training).
        "bt_bat_speed":   _sf(batter, "btBatSpeed", "bt_bat_speed",
                              default=(bt.get("bat_speed") if bt.get("bat_speed") is not None else _NAN)),
        "bt_fast_swing":  _sf(batter, "btFastSwing", "bt_fast_swing",
                              default=(bt.get("fast_swing_pct") if bt.get("fast_swing_pct") is not None else _NAN)),
        "bt_squared_up":  _sf(batter, "btSquaredUp", "bt_squared_up",
                              default=(bt.get("squared_up_pct") if bt.get("squared_up_pct") is not None else _NAN)),
        "bt_blast":       _sf(batter, "btBlast", "bt_blast",
                              default=(bt.get("blast_pct") if bt.get("blast_pct") is not None else _NAN)),
    }
    # Percent-scale the rate stats that training ×100s; leave the FG fraction
    # rates (barrel/hardhit/hr-fb/fb) untouched.
    for pct_key in ("sv_k_pct", "sv_bb_pct", "opp_k_pct", "opp_bb_pct"):
        if 0 < raw[pct_key] <= 1.0:
            raw[pct_key] *= 100.0
    if not feat_order:
        return None
    try:
        return np.array([[raw.get(c, 0.0) for c in feat_order]], dtype=np.float32)
    except Exception:
        return None


# HR/TB/RBI all share one builder; each model's saved feature list selects its
# own columns. Thin aliases keep the per-market call sites + parity diag explicit.
def _build_hr_features(batter, pitcher, feat_order):
    return _build_batter_market_features(batter, pitcher, feat_order)

def _build_tb_features(batter, pitcher, feat_order):
    return _build_batter_market_features(batter, pitcher, feat_order)

def _build_rbi_features(batter, pitcher, feat_order):
    return _build_batter_market_features(batter, pitcher, feat_order)


# ─── Internal full-output scorer (shared by all markets) ────────────────────────

def _score_full(
    model_key: str,
    market_key: str,
    X: np.ndarray,
    line: float,
    debug_label: str = "",
) -> dict:
    """
    Core scoring pipeline. Returns a full result dict:
      raw_p      : XGB predict_proba raw output
      cal_p      : after apply_isotonic (2A)
      p_lo/p_hi  : XGB prediction interval
      mc         : Monte Carlo result dict (all mc_* fields)
      prob       : final calibrated probability (= cal_p)
    """
    model = _models.get(model_key)
    if model is None:
        return {}

    # Uncalibrated XGB output is not a probability — suppress until a trained
    # isotonic calibrator exists for this market (see _xgb_calibrated).
    if not _xgb_calibrated(market_key):
        return {}

    key = _score_cache_key("full", model_key, market_key, line, X)

    def _compute() -> dict:
        raw_p = float(model.predict_proba(X)[0, 1])

        # Step 2A: isotonic post-calibration
        cal_p = float(_apply_isotonic(raw_p, market_key))
        cal_p = round(min(0.97, max(0.03, cal_p)), 4)

        # Step 3: derive XGB prediction interval from tree ensemble
        p_lo, p_hi = _xgb_interval(X, model, market_key)

        # Step 3: run Monte Carlo anchored to (cal_p, p_lo, p_hi)
        mc = mc_simulate(cal_p, p_lo, p_hi, line=line)

        return {
            "prob":    cal_p,
            "raw_p":   round(raw_p, 4),
            "cal_p":   cal_p,
            "p_lo":    p_lo,
            "p_hi":    p_hi,
            "mc":      mc,
            "market":  market_key,
            "line":    line,
        }

    return _SCORE_CACHE.get_or_compute(
        key,
        _compute,
        ttl_seconds=settings.xgb_score_cache_ttl,
        max_entries=settings.xgb_score_cache_max_entries,
    )


# ─── Lean prob-only scorer ───────────────────────────────────────────────────────

def _score_prob(model_key: str, market_key: str, X: np.ndarray) -> Optional[float]:
    """Lean scoring for the single-float public APIs: predict + isotonic +
    clamp, nothing else. Returns the same value as _score_full()["prob"] —
    the tree-ensemble interval and the 10k-draw anchored MC that _score_full
    also computes are discarded by the float APIs, and the interval path costs
    a second predict_proba in its fallback, so skipping both roughly halves
    the per-call cost on the hot per-batter paths."""
    model = _models.get(model_key)
    if model is None:
        return None
    if not _xgb_calibrated(market_key):
        return None
    key = _score_cache_key("prob", model_key, market_key, None, X)

    def _compute() -> float:
        raw_p = float(model.predict_proba(X)[0, 1])
        cal_p = float(_apply_isotonic(raw_p, market_key))
        return round(min(0.97, max(0.03, cal_p)), 4)

    return _SCORE_CACHE.get_or_compute(
        key,
        _compute,
        ttl_seconds=settings.xgb_score_cache_ttl,
        max_entries=settings.xgb_score_cache_max_entries,
    )


# ─── Public scoring functions ───────────────────────────────────────────────────────

def xgb_hit_prob(batter: dict, pitcher: dict) -> Optional[float]:
    """Backwards-compatible single-float hit probability."""
    if not _loaded:
        _load_models()
    model = _models.get("hits")
    if model is None:
        return None
    try:
        batter_e  = _enrich_batter_from_fg(batter)
        pitcher_e = _enrich_pitcher_from_fg(pitcher)
        feat_order = _feat_cols.get("hits", [])
        X = _build_hit_features(batter_e, pitcher_e, feat_order)
        if X is None:
            return None
        return _score_prob("hits", "batter_hits", X)
    except Exception:
        return None


def xgb_hit_prob_full(batter: dict, pitcher: dict) -> dict:
    """Full output: calibrated prob + XGB interval + MC simulation."""
    if not _loaded:
        _load_models()
    if _models.get("hits") is None:
        return {}
    try:
        batter_e  = _enrich_batter_from_fg(batter)
        pitcher_e = _enrich_pitcher_from_fg(pitcher)
        feat_order = _feat_cols.get("hits", [])
        X = _build_hit_features(batter_e, pitcher_e, feat_order)
        if X is None:
            return {}
        return _score_full("hits", "batter_hits", X, line=0.5)
    except Exception:
        return {}


def xgb_k_prob(pitcher: dict, line: float = 4.5) -> Optional[float]:
    """Backwards-compatible single-float strikeout probability."""
    if not _loaded:
        _load_models()
    line_key = f"k_{line}"
    if line_key not in _models:
        candidates = []
        for k in _models:
            if k.startswith("k_"):
                try:
                    candidates.append((abs(float(k[2:]) - line), k))
                except ValueError:
                    pass
        # A neighbouring line's P(over) is only a usable stand-in when it is
        # close; borrowing across >1 strikeout mislabels the probability.
        if not candidates or min(candidates)[0] > 1.0:
            return None
        line_key = min(candidates)[1]
    try:
        pitcher_e = _enrich_pitcher_from_fg(pitcher)
        ump_name  = pitcher.get("umpire") or pitcher.get("hp_umpire") or ""
        ump_feats = _get_ump_features(ump_name) if _UMP_AVAILABLE else {}
        pitcher_e["ump_zone_size"] = ump_feats.get("ump_zone_size", 0.0)
        pitcher_e["ump_k_boost"]   = ump_feats.get("ump_k_boost",   0.0)
        feat_order = _feat_cols.get(line_key, [])
        X = _build_k_features(pitcher_e, feat_order)
        if X is None:
            return None
        return _score_prob(line_key, "pitcher_strikeouts", X)
    except Exception:
        return None


def xgb_k_prob_full(pitcher: dict, line: float = 4.5) -> dict:
    """Full output for K props: calibrated prob + XGB interval + MC."""
    if not _loaded:
        _load_models()
    line_key = f"k_{line}"
    if line_key not in _models:
        candidates = [(abs(float(k[2:]) - line), k) for k in _models if k.startswith("k_")]
        if not candidates or min(candidates)[0] > 1.0:
            return {}
        line_key = min(candidates)[1]
    try:
        pitcher_e = _enrich_pitcher_from_fg(pitcher)
        ump_name  = pitcher.get("umpire") or pitcher.get("hp_umpire") or ""
        ump_feats = _get_ump_features(ump_name) if _UMP_AVAILABLE else {}
        pitcher_e["ump_zone_size"] = ump_feats.get("ump_zone_size", 0.0)
        pitcher_e["ump_k_boost"]   = ump_feats.get("ump_k_boost",   0.0)
        feat_order = _feat_cols.get(line_key, [])
        X = _build_k_features(pitcher_e, feat_order)
        if X is None:
            return {}
        return _score_full(line_key, "pitcher_strikeouts", X, line=line)
    except Exception:
        return {}


def xgb_hit_prob_bulk(batters: list, pitcher: dict) -> dict:
    """Batch hit probabilities for a lineup vs one pitcher — ONE predict_proba
    for all batters instead of per-batter calls. CalibratedClassifierCV has
    substantial fixed overhead per predict call (every calibrated fold runs a
    full XGB predict), so batching a 9-man lineup amortizes it to near zero.
    Values are identical to per-batter xgb_hit_prob (same enrichment, feature
    build, isotonic and clamp). Results are keyed by str(MLBAM id) when the
    batter dict carries one ("id"/"mlbamid"), falling back to name — id keys
    can't collide the way names can."""
    if not _loaded:
        _load_models()
    model = _models.get("hits")
    if model is None or not batters:
        return {}
    if not _xgb_calibrated("batter_hits"):
        return {}
    try:
        pitcher_e  = _enrich_pitcher_from_fg(pitcher)
        feat_order = _feat_cols.get("hits", [])
        rows, keys = [], []
        for b in batters:
            b_e = _enrich_batter_from_fg(b)
            X   = _build_hit_features(b_e, pitcher_e, feat_order)
            if X is not None:
                rows.append(X[0])
                keys.append(str(b.get("id") or b.get("mlbamid") or "") or b.get("name", ""))
        if not rows:
            return {}
        raw_probs = model.predict_proba(np.array(rows, dtype=np.float32))[:, 1]
        result = {}
        for key, raw_p in zip(keys, raw_probs):
            cal_p = float(_apply_isotonic(float(raw_p), "batter_hits"))
            result[key] = round(min(0.97, max(0.03, cal_p)), 4)
        return result
    except Exception:
        return {}


def _predict_batter_market_full(
    model_key: str, market_key: str, line: float,
    batter: dict, pitcher: dict
) -> dict:
    if not _loaded:
        _load_models()
    if _models.get(model_key) is None:
        return {}
    try:
        batter_e  = _enrich_batter_from_fg(batter)
        pitcher_e = _enrich_pitcher_from_fg(pitcher)
        feat_order = _feat_cols.get(model_key, [])
        builder = (_build_hit_features if model_key.startswith("hits")
                   else _build_batter_market_features)
        X = builder(batter_e, pitcher_e, feat_order)
        if X is None:
            return {}
        return _score_full(model_key, market_key, X, line=line)
    except Exception:
        return {}


def _predict_batter_market_prob(
    model_key: str, market_key: str, batter: dict, pitcher: dict
) -> Optional[float]:
    """Lean float-only variant of _predict_batter_market_full — same enrichment
    and feature build, but _score_prob instead of the full interval+MC pipeline
    (see _score_prob for why)."""
    if not _loaded:
        _load_models()
    if _models.get(model_key) is None:
        return None
    try:
        batter_e  = _enrich_batter_from_fg(batter)
        pitcher_e = _enrich_pitcher_from_fg(pitcher)
        feat_order = _feat_cols.get(model_key, [])
        builder = (_build_hit_features if model_key.startswith("hits")
                   else _build_batter_market_features)
        X = builder(batter_e, pitcher_e, feat_order)
        if X is None:
            return None
        return _score_prob(model_key, market_key, X)
    except Exception:
        return None


def xgb_batter_prob_full(family: str, line, batter: dict, pitcher: dict) -> dict:
    """Line-aware full output for a batter market family (hits/hr/tb/rbi).
    Routes (family, line) to the model trained at exactly that threshold;
    returns {} for unmapped lines so callers keep the analytic probability."""
    try:
        line = float(line)
    except (TypeError, ValueError):
        return {}
    model_key = _BATTER_LINE_MODELS.get((family, line))
    if model_key is None:
        return {}
    return _predict_batter_market_full(
        model_key, _MARKET_KEY_MAP.get(model_key, "batter_hits"), line, batter, pitcher)


def xgb_hr_prob(batter: dict, pitcher: dict) -> Optional[float]:
    return _predict_batter_market_prob("hr", "batter_hr", batter, pitcher)

def xgb_tb_prob(batter: dict, pitcher: dict) -> Optional[float]:
    return _predict_batter_market_prob("tb", "batter_tb", batter, pitcher)

def xgb_rbi_prob(batter: dict, pitcher: dict) -> Optional[float]:
    return _predict_batter_market_prob("rbi", "batter_rbi", batter, pitcher)

def xgb_hr_prob_full(batter: dict, pitcher: dict) -> dict:
    return _predict_batter_market_full("hr", "batter_hr", 0.5, batter, pitcher)

def xgb_tb_prob_full(batter: dict, pitcher: dict) -> dict:
    return _predict_batter_market_full("tb", "batter_tb", 1.5, batter, pitcher)

def xgb_rbi_prob_full(batter: dict, pitcher: dict) -> dict:
    return _predict_batter_market_full("rbi", "batter_rbi", 0.5, batter, pitcher)


enrich_batter  = _enrich_batter_from_fg
enrich_pitcher = _enrich_pitcher_from_fg


# ── Serve-parity diagnostic ──────────────────────────────────────────────────
# A model only earns its held-out skill in production if the live caller feeds
# the same features it trained on. This reports, for one prediction, which
# features landed on their default (i.e. weren't supplied). The defaults are
# DERIVED from an empty-input build, so they can never drift from the real
# _sf() defaults — whatever `_build_*_features({}, …)` produces IS the default.
# Mirrors the enrichment in xgb_hit_prob / xgb_k_prob so the report reflects
# exactly what those entry points see.
_PARITY_K_REF = "k_4.5"   # all k_* lines share one builder + feature order


def feature_default_report(market, batter=None, pitcher=None):
    if not _loaded:
        _load_models()
    is_k = str(market).startswith("k") or market in ("strikeouts", "pitcher_strikeouts")
    _BATTER_POWER = {"hr": "hr", "batter_hr": "hr", "home_runs": "hr", "batter_home_runs": "hr",
                     "tb": "tb", "batter_tb": "tb", "total_bases": "tb", "batter_total_bases": "tb",
                     "rbi": "rbi", "batter_rbi": "rbi", "rbis": "rbi", "batter_rbis": "rbi"}
    power_key = _BATTER_POWER.get(market)
    key = _PARITY_K_REF if is_k else (power_key if power_key else "hits")
    feat_order = _feat_cols.get(key) or []
    if _models.get(key) is None or not feat_order:
        return None
    try:
        if is_k:
            real = _build_k_features(_enrich_pitcher_from_fg(pitcher or {}), feat_order)
            base = _build_k_features({}, feat_order)
        elif power_key:
            real = _build_batter_market_features(_enrich_batter_from_fg(batter or {}),
                                                 _enrich_pitcher_from_fg(pitcher or {}), feat_order)
            base = _build_batter_market_features({}, {}, feat_order)
        else:
            real = _build_hit_features(_enrich_batter_from_fg(batter or {}),
                                       _enrich_pitcher_from_fg(pitcher or {}), feat_order)
            base = _build_hit_features({}, {}, feat_order)
        if real is None or base is None:
            return None
        real, base = real[0], base[0]
        feats, n_def = [], 0
        for i, f in enumerate(feat_order):
            rv, bv = float(real[i]), float(base[i])
            # NaN is the legit "unknown" value for bt_* features — both-NaN
            # means the caller supplied nothing, i.e. the default.
            is_def = (math.isnan(rv) and math.isnan(bv)) or abs(rv - bv) < 1e-6
            n_def += int(is_def)
            feats.append({"feature": f,
                          "value": None if math.isnan(rv) else round(rv, 4),
                          "default": None if math.isnan(bv) else round(bv, 4),
                          "is_default": bool(is_def)})
        return {"market": key, "n_features": len(feat_order), "n_default": n_def,
                "default_rate": round(n_def / len(feat_order), 4), "features": feats}
    except Exception:
        return None
