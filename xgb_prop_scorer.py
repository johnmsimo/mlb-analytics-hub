"""
xgb_prop_scorer.py  —  Production XGBoost Prop Scorer
═══════════════════════════════════════════════════════════════════════════════
Drop-in module for app.py.  Import at the top of app.py:

    from xgb_prop_scorer import xgb_hit_prob, xgb_k_prob, xgb_ready

Then blend into your existing probability estimates:

    # Inside enrichbatters() → enrich_one() after rawProb is calculated:
    if xgb_ready('hits'):
        xp = xgb_hit_prob(merged_batter_dict, pitcher_dict)
        if xp is not None:
            row['rawProb'] = 0.40 * row['rawProb'] + 0.60 * xp
            row['xgbHitProb'] = round(xp, 4)

    # Inside projectpitcher() after kprob is calculated:
    if xgb_ready('k'):
        line = float(row.get('line', 4.5))
        xkp = xgb_k_prob(pitcher_dict, line=line)
        if xkp is not None:
            kprob = 0.40 * kprob + 0.60 * xkp
            row['xgbKProb'] = round(xkp, 4)

Model files (produced by xgb_training_pipeline.py / Colab notebook):
    models/xgb_hits.pkl          — binary hit prop model
    models/xgb_k_3_5.pkl         — K over/under 3.5
    models/xgb_k_4_5.pkl         — K over/under 4.5
    models/xgb_k_5_5.pkl         — K over/under 5.5
    models/xgb_feature_cols.json — feature column order for each model

All functions fail silently (return None) if the model files are absent,
so the existing formula continues to run without modification.
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import json
import os
import threading
import traceback
from typing import Optional

import numpy as np

# ── Paths ───────────────────────────────────────────────────────────────────
_HERE        = os.path.dirname(os.path.abspath(__file__))
_MODEL_DIR   = os.path.join(_HERE, 'models')
_FEAT_FILE   = os.path.join(_MODEL_DIR, 'xgb_feature_cols.json')

_MODEL_PATHS = {
    'hits':  os.path.join(_MODEL_DIR, 'xgb_hits.pkl'),
    'k_3.5': os.path.join(_MODEL_DIR, 'xgb_k_3_5.pkl'),
    'k_4.5': os.path.join(_MODEL_DIR, 'xgb_k_4_5.pkl'),
    'k_5.5': os.path.join(_MODEL_DIR, 'xgb_k_5_5.pkl'),
}

# ── Model registry (lazy-loaded once) ───────────────────────────────────────
_lock    = threading.Lock()
_models  : dict = {}           # key → XGBClassifier
_feat_cols: dict = {}          # key → list[str]
_loaded  = False


def _load_models() -> None:
    """Load all available .pkl model files once at first call."""
    global _loaded
    with _lock:
        if _loaded:
            return
        try:
            import pickle
            feat_map: dict = {}
            if os.path.exists(_FEAT_FILE):
                with open(_FEAT_FILE) as f:
                    feat_map = json.load(f)

            for key, path in _MODEL_PATHS.items():
                if os.path.exists(path):
                    with open(path, 'rb') as f:
                        _models[key] = pickle.load(f)
                    _feat_cols[key] = feat_map.get(key, [])
                    print(f'[xgb_scorer] loaded {key} from {path}')

        except Exception:
            print('[xgb_scorer] model load failed —', traceback.format_exc())
        finally:
            _loaded = True


def xgb_ready(market: str = 'hits') -> bool:
    """
    Returns True if the model for the given market is loaded.
    market: 'hits' | 'k'  (k checks for at least one K model)
    """
    if not _loaded:
        _load_models()
    if market == 'hits':
        return 'hits' in _models
    if market == 'k':
        return any(k.startswith('k_') for k in _models)
    return False


# ── Feature helpers ──────────────────────────────────────────────────────────

def _sf(d: dict, *keys, default: float = 0.0) -> float:
    """Safe float extraction — tries each key in order, returns default."""
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


def _build_hit_features(batter: dict, pitcher: dict, feat_order: list) -> Optional[np.ndarray]:
    """
    Build the 19-feature hit vector.
    Keys match xgb_training_pipeline.py HITS_FEATURES list.
    """
    bat_side  = (batter.get('fgbats') or batter.get('bats') or 'R').upper()[:1]
    pit_hand  = (pitcher.get('pitchHand') or pitcher.get('throws') or 'R').upper()[:1]
    platoon   = 1 if (bat_side == 'L' and pit_hand == 'R') or \
                     (bat_side == 'R' and pit_hand == 'L') else 0

    raw = {
        # Batter Statcast
        'sv_xba':      _sf(batter, 'svxba',    'xba',    default=0.250),
        'sv_xwoba':    _sf(batter, 'svxwoba',  'fgwoba', default=0.320),
        'sv_xslg':     _sf(batter, 'svxslg',   'fgslg',  default=0.400),
        'sv_ev':       _sf(batter, 'svev',      'ev',     default=88.0),
        'sv_brl_pct':  _sf(batter, 'svbrlpct', 'brlpct', default=4.0),
        'sv_hh_pct':   _sf(batter, 'svhhpct',  'hhpct',  default=35.0),
        'sv_ss_pct':   _sf(batter, 'svsspct',  'sspct',  default=10.0),
        'sv_la':       _sf(batter, 'svla',      'la',     default=12.0),
        'sv_k_pct':    _sf(batter, 'fgkpct',   'svkpct', default=22.0),
        'sv_bb_pct':   _sf(batter, 'fgbbpct',  'svbbpct',default=8.0),
        # Pitcher opposition metrics
        'opp_xera':    _sf(pitcher, 'svxera',  'fgera',  default=4.50),
        'opp_k_pct':   _sf(pitcher, 'fgkpct',  'svkpct', default=22.0),
        'opp_bb_pct':  _sf(pitcher, 'fgbbpct', 'svbbpct',default=8.0),
        'opp_whiff':   _sf(pitcher, 'svwhiffpct','whiffpct',default=24.0),
        # Platoon
        'bats_L':      1 if bat_side == 'L' else 0,
        'throws_R':    1 if pit_hand == 'R' else 0,
        'platoon_adv': platoon,
        # Recent form (populated by buildplayertrends if available)
        'l7_hits':     _sf(batter, 'l7Hits',   'l7hits',  default=1.5),
        'l7_hit_rate': _sf(batter, 'l7HitRate','l7hitrate',default=0.50),
    }

    # Normalise percentage columns stored as 0-1 floats to 0-100
    for pct_key in ('sv_k_pct', 'sv_bb_pct', 'opp_k_pct', 'opp_bb_pct'):
        if 0 < raw[pct_key] <= 1.0:
            raw[pct_key] *= 100

    if feat_order:
        try:
            return np.array([[raw.get(c, 0.0) for c in feat_order]], dtype=np.float32)
        except Exception:
            return None
    # Fallback: fixed order matching training HITS_FEATURES
    HITS_FEATURES = [
        'sv_xba','sv_xwoba','sv_xslg','sv_ev','sv_brl_pct','sv_hh_pct',
        'sv_ss_pct','sv_la','sv_k_pct','sv_bb_pct',
        'opp_xera','opp_k_pct','opp_bb_pct','opp_whiff',
        'bats_L','throws_R','platoon_adv',
        'l7_hits','l7_hit_rate',
    ]
    return np.array([[raw[c] for c in HITS_FEATURES]], dtype=np.float32)


def _build_k_features(pitcher: dict, feat_order: list) -> Optional[np.ndarray]:
    """
    Build the 10-feature strikeout vector.
    Keys match xgb_training_pipeline.py K_FEATURES list.
    """
    raw = {
        'sv_xera':    _sf(pitcher, 'svxera',     'fgera',    default=4.50),
        'sv_era':     _sf(pitcher, 'fgera',       'era',      default=4.50),
        'sv_k_pct':   _sf(pitcher, 'fgkpct',     'svkpct',   default=22.0),
        'sv_bb_pct':  _sf(pitcher, 'fgbbpct',    'svbbpct',  default=8.0),
        'sv_whiff':   _sf(pitcher, 'svwhiffpct', 'whiffpct', default=24.0),
        'l5_ks':      _sf(pitcher, 'l5Ks',       'l5ks',     default=4.5),
        'l5_k_rate':  _sf(pitcher, 'l5KRate',    'l5krate',  default=0.22),
        'l10_ks':     _sf(pitcher, 'l10Ks',      'l10ks',    default=4.5),
        'opp_lineup_k_pct':   _sf(pitcher, 'oppKPct',  'opponentkpct', default=22.0),
        'opp_lineup_xwoba':   _sf(pitcher, 'oppWoba',  'opponentxwoba',default=0.320),
    }

    for pct_key in ('sv_k_pct', 'sv_bb_pct', 'opp_lineup_k_pct'):
        if 0 < raw[pct_key] <= 1.0:
            raw[pct_key] *= 100
    if 0 < raw['l5_k_rate'] <= 1.0:
        raw['l5_k_rate'] *= 100

    K_FEATURES = [
        'sv_xera','sv_era','sv_k_pct','sv_bb_pct','sv_whiff',
        'l5_ks','l5_k_rate','l10_ks',
        'opp_lineup_k_pct','opp_lineup_xwoba',
    ]

    if feat_order:
        try:
            return np.array([[raw.get(c, 0.0) for c in feat_order]], dtype=np.float32)
        except Exception:
            return None
    return np.array([[raw[c] for c in K_FEATURES]], dtype=np.float32)


# ── Public API ───────────────────────────────────────────────────────────────

def xgb_hit_prob(batter: dict, pitcher: dict) -> Optional[float]:
    """
    Returns probability [0,1] that batter records ≥1 hit.
    Returns None if model is not loaded (graceful fallback).
    """
    if not _loaded:
        _load_models()
    model = _models.get('hits')
    if model is None:
        return None
    try:
        feat_order = _feat_cols.get('hits', [])
        X = _build_hit_features(batter, pitcher, feat_order)
        if X is None:
            return None
        prob = float(model.predict_proba(X)[0, 1])
        return round(min(0.97, max(0.03, prob)), 4)
    except Exception:
        return None


def xgb_k_prob(pitcher: dict, line: float = 4.5) -> Optional[float]:
    """
    Returns probability [0,1] that pitcher OVERS the given K line.
    Supported lines: 3.5, 4.5, 5.5.  Returns None if model absent.
    """
    if not _loaded:
        _load_models()
    # Round to nearest supported line
    line_key = f'k_{line}'
    if line_key not in _models:
        # Try fallback to nearest
        for candidate in ('k_4.5', 'k_3.5', 'k_5.5'):
            if candidate in _models:
                line_key = candidate
                break
        else:
            return None
    try:
        feat_order = _feat_cols.get(line_key, [])
        X = _build_k_features(pitcher, feat_order)
        if X is None:
            return None
        prob = float(_models[line_key].predict_proba(X)[0, 1])
        return round(min(0.97, max(0.03, prob)), 4)
    except Exception:
        return None


def xgb_hit_prob_bulk(batters: list, pitcher: dict) -> dict:
    """
    Batch version — returns {batter_name: prob} dict.
    Slightly faster than calling xgb_hit_prob in a loop for large lineups.
    """
    if not _loaded:
        _load_models()
    model = _models.get('hits')
    if model is None or not batters:
        return {}
    try:
        feat_order = _feat_cols.get('hits', [])
        rows, names = [], []
        for b in batters:
            X = _build_hit_features(b, pitcher, feat_order)
            if X is not None:
                rows.append(X[0])
                names.append(b.get('name', ''))
        if not rows:
            return {}
        probs = model.predict_proba(np.array(rows, dtype=np.float32))[:, 1]
        return {
            name: round(min(0.97, max(0.03, float(p))), 4)
            for name, p in zip(names, probs)
        }
    except Exception:
        return {}


# ── App.py wiring reference ──────────────────────────────────────────────────
"""
WIRING GUIDE — where to add XGBoost blending in app.py
═══════════════════════════════════════════════════════

1.  TOP OF app.py — add import:

        try:
            from xgb_prop_scorer import xgb_hit_prob, xgb_k_prob, xgb_ready
        except ImportError:
            def xgb_ready(_=None): return False
            def xgb_hit_prob(*a, **kw): return None
            def xgb_k_prob(*a, **kw): return None


2.  Inside  enrichbatters() → enrich_one()  after rawProb is computed:
    (search for 'rawProb' in app.py — it's built from matchupscore)

        # ── XGBoost blend ─────────────────────────────────────────
        if xgb_ready('hits'):
            xp = xgb_hit_prob(merged, opp_pitcher_dict)
            if xp is not None:
                base_prob = row.get('rawProb', 0.50)
                row['rawProb']    = round(0.40 * base_prob + 0.60 * xp, 4)
                row['xgbHitProb'] = xp           # expose on card
        # ──────────────────────────────────────────────────────────

    opp_pitcher_dict should be the merged {**apfg, **apsv, **apst} dict
    already constructed in enrichbatters() as  pfg / psv / pst.


3.  Inside  projectpitcher()  after kprob is calculated:
    (search for 'kprob' in app.py)

        # ── XGBoost K blend ───────────────────────────────────────
        if xgb_ready('k'):
            line_val = float(proj.get('line', 4.5))
            xkp = xgb_k_prob(
                {**pfg, **psv, **pst,
                 'oppKPct':  opp_avg_kpct,      # pre-computed lineup avg
                 'oppWoba':  opp_avg_xwoba,},    # pre-computed lineup avg
                line=line_val
            )
            if xkp is not None:
                kprob = round(0.40 * kprob + 0.60 * xkp, 4)
                proj['xgbKProb'] = xkp
        # ──────────────────────────────────────────────────────────


4.  Lineup-status cache invalidation (in  api_lineup_status):

        if len(all_changes) > 0:
            _matchup_card_cache.pop(game_pk, None)   # matchup card cache
            # No XGBoost cache to clear — scorer is stateless per call.


5.  models/ directory:
    Create  models/  at the repo root (same level as app.py).
    After running xgb_training_pipeline.py, copy the output .pkl files here.
    The scorer will auto-load them on the first request after app restart.


BLEND RATIONALE
───────────────
Start at 40 / 60 (formula / XGBoost).  After 2 weeks of logged results:
  • If XGBoost hit-rate > formula hit-rate by >3 pp → shift to 25/75
  • If XGBoost underperforms → shift back to 60/40 or disable
The blend weight is a single constant — easy to tune without retraining.
"""
