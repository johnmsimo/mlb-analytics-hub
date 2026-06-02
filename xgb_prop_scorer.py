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
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import json
import os
import threading
import traceback
from typing import Optional

import numpy as np

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
        return {"expected_pa": 4.20, "batting_order": 0, "lineup_confirmed": 0}

_HERE = os.path.dirname(os.path.abspath(__file__))
_MODEL_DIR = os.path.join(_HERE, "models")
_FEAT_FILE = os.path.join(_MODEL_DIR, "xgb_feature_cols.json")

_MODEL_PATHS = {
    "hits":  os.path.join(_MODEL_DIR, "xgb_hits_over_0.5.pkl"),
    "k_3.5": os.path.join(_MODEL_DIR, "xgb_k_over_3.5.pkl"),
    "k_4.5": os.path.join(_MODEL_DIR, "xgb_k_over_4.5.pkl"),
    "k_5.5": os.path.join(_MODEL_DIR, "xgb_k_over_5.5.pkl"),
    "hr":    os.path.join(_MODEL_DIR, "xgb_hr_over_0.5.pkl"),
    "tb":    os.path.join(_MODEL_DIR, "xgb_tb_over_1.5.pkl"),
    "rbi":   os.path.join(_MODEL_DIR, "xgb_rbi_over_0.5.pkl"),
}

_lock = threading.Lock()
_models: dict = {}
_feat_cols: dict = {}
_loaded = False


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
                    print(
                        f"[xgb_scorer] loaded {key} from {path} "
                        f"(target={meta.get('target', 'unknown')})"
                    )
                else:
                    _models[key] = payload
                    _feat_cols[key] = feat_map.get(key, [])
                    print(f"[xgb_scorer] loaded {key} from {path} (legacy direct-model artifact)")
        except Exception:
            print("[xgb_scorer] model load failed —", traceback.format_exc())
        finally:
            _loaded = True


def xgb_ready(market: str = "hits") -> bool:
    if not _loaded:
        _load_models()
    if market == "hits":
        return "hits" in _models
    if market == "k":
        return any(k.startswith("k_") for k in _models)
    if market in ("hr", "tb", "rbi"):
        return market in _models
    return False


def _enrich_batter_from_fg(d: dict) -> dict:
    if not _FG_AVAILABLE:
        return d

    pid = str(d.get("fgId") or d.get("playerid") or d.get("fg_id") or "")
    name = str(d.get("name") or d.get("Name") or d.get("PlayerName") or "")

    fg = {}
    if pid and pid not in ("", "None", "0"):
        fg = get_batter_stats(player_id=pid)
    if not fg and name:
        fg = get_batter_stats(name=name)
    if not fg:
        return d

    fg_map = {
        "xAVG": "svxba",
        "xwOBA": "svxwoba",
        "xSLG": "svxslg",
        "EV": "svev",
        "Barrel%": "svbrlpct",
        "HardHit%": "svhhpct",
        "SwStr%": "svsspct",
        "LA": "svla",
        "K%": "fgkpct",
        "BB%": "fgbbpct",
        "wOBA": "fgwoba",
        "SLG": "fgslg",
        "Bats": "fgbats",
        "xMLBAMID": "mlbamid",
        "playerid": "fgId",
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
            ("wOBA", "projwoba"),
            ("HR", "projhr"),
            ("H", "projh"),
            ("AVG", "projava"),
            ("OBP", "projobp"),
            ("SLG", "projslg"),
        ]:
            if key not in enriched or enriched[key] is None:
                val = proj.get(col)
                if val is not None:
                    enriched[key] = val

    return enriched


def _enrich_pitcher_from_fg(d: dict) -> dict:
    if not _FG_AVAILABLE:
        return d

    pid = str(d.get("fgId") or d.get("playerid") or d.get("fg_id") or "")
    name = str(d.get("name") or d.get("Name") or d.get("PlayerName") or "")

    fg = {}
    if pid and pid not in ("", "None", "0"):
        fg = get_pitcher_stats(player_id=pid)
    if not fg and name:
        fg = get_pitcher_stats(name=name)
    if not fg:
        return d

    fg_map = {
        "xERA": "svxera",
        "ERA": "fgera",
        "K%": "fgkpct",
        "BB%": "fgbbpct",
        "SwStr%": "svwhiffpct",
        "playerid": "fgId",
        "xMLBAMID": "mlbamid",
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
            ("ERA", "projera"),
            ("K%", "projkpct"),
            ("BB%", "projbbpct"),
            ("IP", "projip"),
            ("K/9", "projk9"),
            ("FIP", "projfip"),
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


def _build_hit_features(batter: dict, pitcher: dict, feat_order: list) -> Optional[np.ndarray]:
    bat_side = (batter.get("fgbats") or batter.get("bats") or "R").upper()[:1]
    pit_hand = (pitcher.get("pitchHand") or pitcher.get("throws") or "R").upper()[:1]
    platoon = 1 if (bat_side == "L" and pit_hand == "R") or (bat_side == "R" and pit_hand == "L") else 0

    # ── Lineup PA features: pull from confirmed lineup if available ──────────
    mlbam_id    = batter.get("mlbamid") or batter.get("xMLBAMID")
    player_name = batter.get("name") or batter.get("Name") or batter.get("PlayerName")
    lineup_feats = _get_lineup_features(mlbam_id=mlbam_id, player_name=player_name)
    expected_pa      = lineup_feats["expected_pa"]
    batting_order    = lineup_feats["batting_order"]
    lineup_confirmed = lineup_feats["lineup_confirmed"]

    raw = {
        "sv_xba": _sf(batter, "svxba", "xAVG", default=0.250),
        "sv_xwoba": _sf(batter, "svxwoba", "xwOBA", "fgwoba", default=0.320),
        "sv_xslg": _sf(batter, "svxslg", "xSLG", "fgslg", default=0.400),
        "sv_ev": _sf(batter, "svev", "EV", default=88.0),
        "sv_brl_pct": _sf(batter, "svbrlpct", "Barrel%", default=4.0),
        "sv_hh_pct": _sf(batter, "svhhpct", "HardHit%", default=35.0),
        "sv_ss_pct": _sf(batter, "svsspct", "SwStr%", default=10.0),
        "sv_la": _sf(batter, "svla", "LA", default=12.0),
        "sv_k_pct": _sf(batter, "fgkpct", "K%", "svkpct", default=22.0),
        "sv_bb_pct": _sf(batter, "fgbbpct", "BB%", "svbbpct", default=8.0),
        "opp_xera": _sf(pitcher, "svxera", "xERA", "fgera", default=4.50),
        "opp_k_pct": _sf(pitcher, "fgkpct", "K%", "svkpct", default=22.0),
        "opp_bb_pct": _sf(pitcher, "fgbbpct", "BB%", "svbbpct", default=8.0),
        "opp_whiff": _sf(pitcher, "svwhiffpct", "SwStr%", "whiffpct", default=24.0),
        "bats_L": 1 if bat_side == "L" else 0,
        "throws_R": 1 if pit_hand == "R" else 0,
        "platoon_adv": platoon,
        "l7_hits": _sf(batter, "l7Hits", "l7hits", default=1.5),
        "l14_hits": _sf(batter, "l14Hits", "l14hits", default=3.0),
        "l7_hit_rate": _sf(batter, "l7HitRate", "l7hitrate", default=0.50),
        # ── Lineup context (1C) ──────────────────────────────────────────────
        "expected_pa":       expected_pa,
        "batting_order":     float(batting_order),
        "lineup_confirmed":  float(lineup_confirmed),
        # ── BATX-parity features (v2) ────────────────────────────────────────
        "park_factor":          _sf(batter, "parkFactor", "park_factor", default=1.00),
        "wx_temp_mult":         _sf(batter, "wxTempMult", "wx_temp_mult", default=1.00),
        "wx_wind_mult":         _sf(batter, "wxWindMult", "wx_wind_mult", default=1.00),
        "pitch_mix_slg_edge":   _sf(batter, "pitchMixSlgEdge", "pitch_mix_slg_edge", default=0.00),
        "bvp_woba_edge_shrunk": _sf(batter, "bvpWobaEdge", "bvp_woba_edge_shrunk", default=0.00),
        "split_ops_edge":       _sf(batter, "splitOpsEdge", "split_ops_edge", default=0.00),
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
        "sv_xba", "sv_xwoba", "sv_xslg", "sv_ev", "sv_brl_pct", "sv_hh_pct",
        "sv_ss_pct", "sv_la", "sv_k_pct", "sv_bb_pct",
        "opp_xera", "opp_k_pct", "opp_bb_pct", "opp_whiff",
        "bats_L", "throws_R", "platoon_adv",
        "l7_hits", "l14_hits", "l7_hit_rate",
        "expected_pa", "batting_order", "lineup_confirmed",
        "park_factor", "wx_temp_mult", "wx_wind_mult",
        "pitch_mix_slg_edge", "bvp_woba_edge_shrunk",
        "split_ops_edge",
    ]
    return np.array([[raw[c] for c in hits_features]], dtype=np.float32)


def _build_k_features(pitcher: dict, feat_order: list) -> Optional[np.ndarray]:
    raw = {
        "sv_xera": _sf(pitcher, "svxera", "xERA", "fgera", default=4.50),
        "sv_era": _sf(pitcher, "fgera", "ERA", default=4.50),
        "sv_k_pct": _sf(pitcher, "fgkpct", "K%", "svkpct", default=22.0),
        "sv_bb_pct": _sf(pitcher, "fgbbpct", "BB%", "svbbpct", default=8.0),
        "sv_whiff_pct": _sf(pitcher, "svwhiffpct", "SwStr%", "whiffpct", default=24.0),
        "l3_ks": _sf(pitcher, "l3Ks", "l3ks", default=4.5),
        "l5_ks": _sf(pitcher, "l5Ks", "l5ks", default=4.5),
        "l5_k_rate": _sf(pitcher, "l5KRate", "l5krate", default=0.22),
        "l10_ks": _sf(pitcher, "l10Ks", "l10ks", default=4.5),
        "l3_ip": _sf(pitcher, "l3IP", "l3ip", default=5.0),
        "l5_ip": _sf(pitcher, "l5IP", "l5ip", default=5.0),
        "days_rest": _sf(pitcher, "daysRest", "days_rest", default=5.0),
        "opp_lineup_k_pct": _sf(
            pitcher, "oppKPct", "opponentkpct", "opp_lineup_k_pct", default=22.0
        ),
        "opp_lineup_xwoba": _sf(
            pitcher, "oppWoba", "opponentxwoba", "opp_lineup_xwoba", default=0.320
        ),
        "ump_zone_size": _sf(pitcher, "ump_zone_size", default=0.0),
        "ump_k_boost":   _sf(pitcher, "ump_k_boost",   default=0.0),
    }

    for pct_key in ("sv_k_pct", "sv_bb_pct", "opp_lineup_k_pct"):
        if 0 < raw[pct_key] <= 1.0:
            raw[pct_key] *= 100.0

    raw["days_rest"] = max(0.0, min(14.0, raw["days_rest"]))

    k_features = [
        "sv_xera", "sv_era", "sv_k_pct", "sv_bb_pct", "sv_whiff_pct",
        "l3_ks", "l5_ks", "l5_k_rate", "l10_ks",
        "l3_ip", "l5_ip",
        "days_rest",
        "opp_lineup_k_pct", "opp_lineup_xwoba",
        "ump_zone_size", "ump_k_boost",
    ]

    if feat_order:
        try:
            return np.array([[raw.get(c, 0.0) for c in feat_order]], dtype=np.float32)
        except Exception:
            return None

    return np.array([[raw[c] for c in k_features]], dtype=np.float32)


def xgb_hit_prob(batter: dict, pitcher: dict) -> Optional[float]:
    if not _loaded:
        _load_models()
    model = _models.get("hits")
    if model is None:
        print("[xgb DEBUG] ❌ No hits model loaded!")
        return None
    try:
        batter_enriched = _enrich_batter_from_fg(batter)
        pitcher_enriched = _enrich_pitcher_from_fg(pitcher)
        feat_order = _feat_cols.get("hits", [])
        X = _build_hit_features(batter_enriched, pitcher_enriched, feat_order)
        print(f"[xgb DEBUG] Player: {batter.get('name', 'unknown')}")
        print(f"[xgb DEBUG] xBA={batter_enriched.get('svxba')} EV={batter_enriched.get('svev')} HardHit%={batter_enriched.get('svhhpct')}")
        print(f"[xgb DEBUG] l7_hits={batter_enriched.get('l7Hits')} l7_hit_rate={batter_enriched.get('l7HitRate')}")
        print(f"[xgb DEBUG] park_factor={batter_enriched.get('parkFactor')} opp_xera={pitcher_enriched.get('svxera')}")
        mlbam_id    = batter_enriched.get("mlbamid") or batter_enriched.get("xMLBAMID")
        player_name = batter_enriched.get("name") or batter_enriched.get("Name")
        lf = _get_lineup_features(mlbam_id=mlbam_id, player_name=player_name)
        print(f"[xgb DEBUG] batting_order={lf['batting_order']} expected_pa={lf['expected_pa']} confirmed={lf['lineup_confirmed']}")
        if X is None:
            return None
        prob = float(model.predict_proba(X)[0, 1])
        print(f"[xgb DEBUG] RAW prob before clamp = {prob:.4f}")
        return round(min(0.97, max(0.03, prob)), 4)

    except Exception:
        return None


def xgb_k_prob(pitcher: dict, line: float = 4.5) -> Optional[float]:
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
        if not candidates:
            return None
        line_key = min(candidates)[1]

    try:
        pitcher_enriched = _enrich_pitcher_from_fg(pitcher)
        ump_name = pitcher.get("umpire") or pitcher.get("hp_umpire") or ""
        ump_feats = _get_ump_features(ump_name) if _UMP_AVAILABLE else {}
        pitcher_enriched["ump_zone_size"] = ump_feats.get("ump_zone_size", 0.0)
        pitcher_enriched["ump_k_boost"]   = ump_feats.get("ump_k_boost",   0.0)
        feat_order = _feat_cols.get(line_key, [])
        X = _build_k_features(pitcher_enriched, feat_order)
        if X is None:
            return None
        prob = float(_models[line_key].predict_proba(X)[0, 1])
        return round(min(0.97, max(0.03, prob)), 4)
    except Exception:
        return None


def xgb_hit_prob_bulk(batters: list, pitcher: dict) -> dict:
    if not _loaded:
        _load_models()
    model = _models.get("hits")
    if model is None or not batters:
        return {}

    try:
        pitcher_enriched = _enrich_pitcher_from_fg(pitcher)
        feat_order = _feat_cols.get("hits", [])
        rows, names = [], []
        for b in batters:
            b_enriched = _enrich_batter_from_fg(b)
            X = _build_hit_features(b_enriched, pitcher_enriched, feat_order)
            if X is not None:
                rows.append(X[0])
                names.append(b.get("name", ""))
        if not rows:
            return {}
        probs = model.predict_proba(np.array(rows, dtype=np.float32))[:, 1]
        return {name: round(min(0.97, max(0.03, float(p))), 4) for name, p in zip(names, probs)}
    except Exception:
        return {}


def _predict_batter_market(market_key: str, batter: dict, pitcher: dict) -> Optional[float]:
    if not _loaded:
        _load_models()
    model = _models.get(market_key)
    if model is None:
        return None
    try:
        batter_enriched  = _enrich_batter_from_fg(batter)
        pitcher_enriched = _enrich_pitcher_from_fg(pitcher)
        feat_order = _feat_cols.get(market_key, [])
        X = _build_hit_features(batter_enriched, pitcher_enriched, feat_order)
        if X is None:
            return None
        prob = float(model.predict_proba(X)[0, 1])
        return round(min(0.97, max(0.03, prob)), 4)
    except Exception:
        return None


def xgb_hr_prob(batter: dict, pitcher: dict) -> Optional[float]:
    return _predict_batter_market("hr", batter, pitcher)


def xgb_tb_prob(batter: dict, pitcher: dict) -> Optional[float]:
    return _predict_batter_market("tb", batter, pitcher)


def xgb_rbi_prob(batter: dict, pitcher: dict) -> Optional[float]:
    return _predict_batter_market("rbi", batter, pitcher)


enrich_batter = _enrich_batter_from_fg
enrich_pitcher = _enrich_pitcher_from_fg
