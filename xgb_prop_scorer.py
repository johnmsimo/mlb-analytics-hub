# xgb_prop_scorer.py
# Drop into your project root alongside app.py.
# Loaded once at startup; called inside _project_batter_vs_pitcher()
# and the pitcher K projection path to AUGMENT formula output.
#
# app.py import block (already present):
#   try:
#       from xgb_prop_scorer import xgb_hit_prob, xgb_k_prob
#       _XGB_AVAILABLE = True
#   except ImportError:
#       _XGB_AVAILABLE = False
#       def xgb_hit_prob(*a, **k): return None
#       def xgb_k_prob(*a, **k):   return None

import os
import glob
import joblib
import numpy as np

_MODEL_DIR = os.environ.get(
    "XGB_MODEL_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "models"),
)
_REGISTRY: dict = {}


def _load_models() -> None:
    """Load all trained .pkl models from MODEL_DIR at startup."""
    global _REGISTRY
    loaded = 0
    for path in sorted(glob.glob(os.path.join(_MODEL_DIR, "xgb_*.pkl"))):
        key = os.path.basename(path).replace("xgb_", "").replace(".pkl", "")
        try:
            _REGISTRY[key] = joblib.load(path)
            print(f"[xgb] Loaded model: {key}")
            loaded += 1
        except Exception as e:
            print(f"[xgb] Failed to load {path}: {e}")
    if loaded == 0:
        print(f"[xgb] WARNING: no models found in {_MODEL_DIR}")
    else:
        print(f"[xgb] {loaded} model(s) ready")


_load_models()


def _safe(v, default: float = 0.0) -> float:
    """Coerce v to float; return default on failure or NaN."""
    try:
        f = float(v)
        return f if f == f else default  # NaN guard
    except Exception:
        return default


def xgb_hit_prob(batter_stats: dict, pitcher_stats: dict) -> float | None:
    """
    Returns calibrated P(hits >= 1) for a batter vs. a pitcher.
    Returns None when the model is not loaded so the caller falls back to
    the formula-only estimate.

    Key mappings (batter_stats dict keys used by app.py):
        sv_xba, sv_xwoba, sv_xslg, sv_ev / sv_avg_ev,
        sv_brl_pct, sv_hh_pct, sv_ss_pct, sv_la / sv_avg_la,
        sv_k_pct / fg_kpct, sv_bb_pct / fg_bbpct,
        fg_bats / bats  ("L" | "R" | "S")
        l7_hits, l14_hits, l7_hit_rate  (rolling form — 0 if unavailable)

    Key mappings (pitcher_stats dict keys):
        sv_xera / fg_era, sv_k_pct / fg_kpct,
        sv_bb_pct / fg_bbpct, sv_whiff / sv_whiff_pct,
        pitch_hand / throws  ("L" | "R")
    """
    reg = _REGISTRY.get("hits_over_0.5")
    if not reg:
        return None

    feat_cols: list = reg["features"]
    b = batter_stats
    p = pitcher_stats

    bats  = str(b.get("fg_bats") or b.get("bats") or "R").upper().strip()
    hand  = str(p.get("pitch_hand") or p.get("throws") or "R").upper().strip()
    bats_L   = 1 if bats == "L" else 0
    throws_R = 1 if hand == "R" else 0

    row = {
        # Batter quality
        "sv_xba":      _safe(b.get("sv_xba"),                                0.250),
        "sv_xwoba":    _safe(b.get("sv_xwoba") or b.get("fg_woba"),          0.320),
        "sv_xslg":     _safe(b.get("sv_xslg"),                               0.400),
        "sv_ev":       _safe(b.get("sv_ev") or b.get("sv_avg_ev"),           88.5),
        "sv_brl_pct":  _safe(b.get("sv_brl_pct"),                            6.0),
        "sv_hh_pct":   _safe(b.get("sv_hh_pct"),                            37.0),
        "sv_ss_pct":   _safe(b.get("sv_ss_pct"),                            30.0),
        "sv_la":       _safe(b.get("sv_la") or b.get("sv_avg_la"),          12.0),
        "sv_k_pct":    _safe(b.get("sv_k_pct") or b.get("fg_kpct"),          0.22),
        "sv_bb_pct":   _safe(b.get("sv_bb_pct") or b.get("fg_bbpct"),        0.08),
        # Opposing pitcher
        "opp_xera":    _safe(p.get("sv_xera") or p.get("fg_era"),            4.20),
        "opp_k_pct":   _safe(p.get("sv_k_pct") or p.get("fg_kpct"),          0.22),
        "opp_bb_pct":  _safe(p.get("sv_bb_pct") or p.get("fg_bbpct"),        0.08),
        "opp_whiff":   _safe(p.get("sv_whiff") or p.get("sv_whiff_pct"),    22.0),
        # Handedness / platoon
        "bats_L":      bats_L,
        "throws_R":    throws_R,
        "platoon_adv": int(
            (bats_L == 1 and throws_R == 1) or
            (bats_L == 0 and throws_R == 0)
        ),
        # Rolling form (defaults to league-avg when not supplied)
        "l7_hits":     _safe(b.get("l7_hits"),     1.0),
        "l14_hits":    _safe(b.get("l14_hits"),     1.0),
        "l7_hit_rate": _safe(b.get("l7_hit_rate"), 0.65),
    }

    X = np.array([[row.get(f, 0.0) for f in feat_cols]], dtype=float)
    try:
        return float(reg["model"].predict_proba(X)[0, 1])
    except Exception as e:
        print(f"[xgb] xgb_hit_prob error: {e}")
        return None


def xgb_k_prob(pitcher_stats: dict, line: float = 4.5) -> float | None:
    """
    Returns calibrated P(Ks >= ceil(line)) for a starting pitcher.
    line should be 3.5, 4.5, or 5.5.  Returns None if model not loaded.

    Key mappings (pitcher_stats dict keys used by app.py):
        sv_xera / fg_era, sv_era_p / fg_era,
        sv_k_pct / fg_kpct, sv_bb_pct / fg_bbpct,
        sv_whiff / sv_whiff_pct,
        l5_ks, l5_k_rate, l10_ks  (rolling form — defaults used if absent)
    """
    key_map = {3.5: "k_over_3.5", 4.5: "k_over_4.5", 5.5: "k_over_5.5"}
    model_key = key_map.get(float(line), "k_over_4.5")
    reg = _REGISTRY.get(model_key)
    if not reg:
        return None

    feat_cols: list = reg["features"]
    p = pitcher_stats

    k_pct = _safe(p.get("sv_k_pct") or p.get("fg_kpct"), 0.22)

    row = {
        "sv_xera":                _safe(p.get("sv_xera") or p.get("fg_era"),         4.20),
        "sv_era":                 _safe(p.get("sv_era_p") or p.get("fg_era"),         4.20),
        "sv_k_pct":               k_pct,
        "sv_bb_pct":              _safe(p.get("sv_bb_pct") or p.get("fg_bbpct"),      0.08),
        "sv_whiff_pct":           _safe(p.get("sv_whiff") or p.get("sv_whiff_pct"),  22.0),
        "l5_ks":                  _safe(p.get("l5_ks"),     4.5),
        "l5_k_rate":              _safe(p.get("l5_k_rate"), 0.55),
        "l10_ks":                 _safe(p.get("l10_ks"),    4.5),
        "opp_lineup_k_pct_proxy": k_pct * 0.88,
        "opp_lineup_xwoba_proxy": 0.320,
    }

    X = np.array([[row.get(f, 0.0) for f in feat_cols]], dtype=float)
    try:
        return float(reg["model"].predict_proba(X)[0, 1])
    except Exception as e:
        print(f"[xgb] xgb_k_prob error: {e}")
        return None


def get_model_info() -> dict:
    """Return a summary of loaded models — useful for /api/status health checks."""
    return {
        key: {
            "features":     reg.get("features", []),
            "feature_count": len(reg.get("features", [])),
            "meta":         reg.get("meta", {}),
        }
        for key, reg in _REGISTRY.items()
    }
