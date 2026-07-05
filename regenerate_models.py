"""
regenerate_models.py — Honest XGBoost prop-model regeneration + calibration
═══════════════════════════════════════════════════════════════════════════════
Why this exists
---------------
The committed models/xgb_*.pkl artifacts were raw, UNCALIBRATED XGBClassifiers
trained by an older pipeline on a feature schema most of whose columns were
silently zero-filled (the opponent-pitcher join was never wired, and K%/BB%/
barrel%/whiff% never merged). That is why the scorer's A1 fix gates them off:
their predict_proba was bimodal noise.

This script regenerates the four production models the right way:

  1. Real features only. Every training feature is populated from a real source
     and on the SAME scale the live scorer feeds at inference, so there is no
     train/serve skew:
       • batter/pitcher season skill  ← local data/fg_{batting,pitching}_*.csv
         joined by xMLBAMID (identical source + ×100 K%/BB% normalisation the
         scorer applies in _build_hit_features / _build_k_features)
       • per-game outcomes + rolling form ← Statcast (pybaseball), aggregated
         per game, with the OPPOSING STARTER captured so opp_* is real, not 0
     Features that cannot be reconstructed historically (park/weather/lineup/
     umpire/bvp) are DROPPED from the model's feature list rather than trained
     on a constant — the scorer selects columns by the saved feature list, so a
     dropped feature is simply never fed.

  2. Calibrated. Each model is wrapped in CalibratedClassifierCV (isotonic),
     so predict_proba is a true probability — the scorer can use it directly.

  3. Honestly validated. 2021-2024 train, 2025 held-out TEST. Reported AUC/Brier
     are out-of-sample (the old pipeline reported in-sample AUC, ~0.94 vanity).

Artifacts (drop-in compatible with xgb_prop_scorer._load_models):
    models/xgb_hits_over_0.5.pkl   {model, features, meta}
    models/xgb_k_over_3.5.pkl
    models/xgb_k_over_4.5.pkl
    models/xgb_k_over_5.5.pkl
    models/xgb_feature_cols.json

Usage:
    python regenerate_models.py                 # all markets, 2021-25
    python regenerate_models.py --markets hits  # subset
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import warnings
from datetime import datetime, timedelta
from typing import Optional

import joblib
import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score
import xgboost as xgb

warnings.filterwarnings("ignore")

SEED = 42
np.random.seed(SEED)

_HERE = os.path.dirname(os.path.abspath(__file__))
_MODEL_DIR = os.path.join(_HERE, "models")
_DATA_DIR = os.path.join(_HERE, "data")
os.makedirs(_MODEL_DIR, exist_ok=True)

TRAIN_SEASONS = [2021, 2022, 2023, 2024]
TEST_SEASON = 2025
ALL_SEASONS = TRAIN_SEASONS + [TEST_SEASON]

# ── Feature lists: ONLY features we can populate with real historical data.
#    Each must be a subset of what xgb_prop_scorer._build_hit/_build_k_features
#    produces, so inference column-selection matches.
HITS_FEATURES = [
    "sv_xba", "sv_xwoba", "sv_xslg", "sv_ev", "sv_brl_pct", "sv_hh_pct",
    "sv_ss_pct", "sv_la", "sv_k_pct", "sv_bb_pct",
    "opp_xera", "opp_k_pct", "opp_bb_pct", "opp_whiff",
    "bats_L", "throws_R", "platoon_adv",
    "l7_hits", "l14_hits", "l7_hit_rate",
    # Playing-time / lineup role — THE dominant signal for "≥1 hit in a game"
    # and already served live by the scorer (lineup_loader). Reconstructed
    # historically from Statcast at-bat order; expected_pa uses the SAME
    # slot→PA table the scorer feeds, so there is no train/serve skew.
    "batting_order", "expected_pa",
    # NOTE: "heater" momentum (l7_ev/l7_hh/l7_whiff/ev_momentum/whiff_momentum)
    # was tested here and left OUT — it was flat on the ≥1-hit market (held-out
    # 0.6221 vs 0.6223). Momentum lives in HR_FEATURES, where it helps. The
    # rolling columns are still computed in build_batter_matrix (HR consumes the
    # EV/barrel ones); they're simply not selected for the hits model.
]

# Mirror of lineup_loader._PA_BY_SLOT / _DEFAULT_PA so the historical
# reconstruction of expected_pa matches inference exactly.
_PA_BY_SLOT = {1: 4.60, 2: 4.52, 3: 4.44, 4: 4.36, 5: 4.28,
               6: 4.18, 7: 4.08, 8: 3.96, 9: 3.84}
_DEFAULT_PA = 4.20
K_FEATURES = [
    "sv_xera", "sv_era", "sv_k_pct", "sv_bb_pct", "sv_whiff_pct",
    "l3_ks", "l5_ks", "l5_k_rate", "l10_ks", "l3_ip", "l5_ip", "days_rest",
]

# Home-run (≥1 HR in a game). Power/loft skills + opposing pitcher HR-allowed
# profile + handedness + lineup volume + "heater" momentum (recent EV / barrel
# vs the batter's own 30-game baseline). Every feature is served live by the
# scorer's _build_hr_features from the SAME source + scale.
HR_FEATURES = [
    "sv_xslg", "sv_iso", "sv_ev", "sv_brl_pct", "sv_hh_pct", "sv_la",
    "sv_hrfb", "sv_fb_pct", "sv_maxev", "sv_k_pct",
    "opp_hr9", "opp_hrfb", "opp_fb_pct", "opp_barrel", "opp_xera",
    "bats_L", "throws_R", "platoon_adv",
    "batting_order", "expected_pa",
    "l7_ev", "l7_barrel", "ev_momentum", "barrel_momentum",
    # Hand-aware HR park multiplier — the venue IS known historically (the
    # game's home team), so unlike weather/umpire this reconstructs exactly.
    "park_hr",
    # Bat-tracking (2024+; NaN before — never imputed, see train_market).
    "bt_bat_speed", "bt_fast_swing", "bt_squared_up", "bt_blast",
]

# Total bases (≥2 TB in a game). Broader than HR — rewards extra-base power AND
# volume of contact, so it carries the hit/contact skills alongside the power and
# loft ones, plus lineup PA volume and EV/barrel momentum.
TB_FEATURES = [
    "sv_xba", "sv_xwoba", "sv_xslg", "sv_iso", "sv_ev", "sv_brl_pct",
    "sv_hh_pct", "sv_la", "sv_k_pct", "sv_bb_pct",
    "opp_xera", "opp_k_pct", "opp_bb_pct", "opp_hr9", "opp_barrel",
    "bats_L", "throws_R", "platoon_adv",
    "batting_order", "expected_pa",
    "l7_hits", "l7_ev", "l7_barrel", "ev_momentum", "barrel_momentum",
    "park_factor",
    # Bat-tracking (2024+; NaN before — never imputed, see train_market).
    "bt_bat_speed", "bt_fast_swing", "bt_squared_up", "bt_blast",
]

# RBI (≥1 RBI in a game). Heavily lineup-context driven (cleanup spots get the
# most chances), so batting_order / expected_pa matter most, alongside power.
RBI_FEATURES = [
    "sv_xslg", "sv_iso", "sv_xwoba", "sv_ev", "sv_brl_pct", "sv_hh_pct",
    "sv_la", "sv_hrfb", "sv_k_pct",
    "opp_xera", "opp_k_pct", "opp_hr9", "opp_barrel",
    "bats_L", "throws_R", "platoon_adv",
    "batting_order", "expected_pa",
    "l7_ev", "l7_barrel", "ev_momentum", "barrel_momentum",
    # Bat-tracking (2024+; NaN before — never imputed, see train_market).
    "bt_bat_speed", "bt_fast_swing", "bt_squared_up", "bt_blast",
]

# ── Park factors — VERBATIM copies of app.py's PARK_FACTORS /
#    HR_PARK_FACTORS / HR_PARK_FACTORS_HAND, keyed by MLBAM home-team id.
#    Train/serve parity demands the identical source and scale: the live
#    scorer is fed these exact values via parkFactor / parkHr, so the
#    historical reconstruction must use them too. If app.py's tables change,
#    change these with them (and retrain).
PARK_FACTORS = {
    133: 1.08, 144: 0.92, 110: 0.97, 111: 1.04, 112: 0.97, 137: 0.95, 109: 1.06,
    145: 1.03, 116: 1.00, 158: 0.97, 142: 1.00, 147: 0.97, 143: 1.03, 140: 1.05,
    146: 0.95, 121: 0.97, 136: 0.93, 138: 1.02, 141: 0.98, 139: 0.99, 108: 0.96,
    117: 0.97, 135: 0.98, 120: 0.98, 134: 0.97, 119: 0.95, 118: 1.02, 114: 1.01,
    113: 0.94, 115: 1.00,
}
HR_PARK_FACTORS = {
    109: 114, 144: 94,  110: 108, 111: 101, 112: 109, 137: 92,
    115: 112, 116: 100, 117: 103, 118: 108, 119: 104, 108: 95,
    146: 95,  158: 99,  142: 99,  121: 100, 147: 118, 133: 107,
    143: 111, 134: 95,  136: 95,  138: 98,  139: 96,  140: 112,
    141: 102, 120: 99,  135: 97,  145: 95,  113: 100, 114: 98,
}
HR_PARK_FACTORS_HAND = {
    147: {"L": 134, "R": 100}, 137: {"L": 80,  "R": 96},
    111: {"L": 90,  "R": 104}, 110: {"L": 112, "R": 95},
    117: {"L": 99,  "R": 110}, 134: {"L": 104, "R": 92},
    143: {"L": 114, "R": 108}, 116: {"L": 96,  "R": 92},
}
# Statcast `home_team` abbreviation → MLBAM team id (both old and new codes).
TEAM_ABBR_TO_ID = {
    "LAA": 108, "AZ": 109, "ARI": 109, "BAL": 110, "BOS": 111, "CHC": 112,
    "CIN": 113, "CLE": 114, "COL": 115, "DET": 116, "HOU": 117, "KC": 118,
    "KCR": 118, "LAD": 119, "WSH": 120, "WSN": 120, "NYM": 121, "OAK": 133,
    "ATH": 133, "PIT": 134, "SD": 135, "SDP": 135, "SEA": 136, "SF": 137,
    "SFG": 137, "STL": 138, "TB": 139, "TBR": 139, "TEX": 140, "TOR": 141,
    "MIN": 142, "PHI": 143, "ATL": 144, "CWS": 145, "CHW": 145, "MIA": 146,
    "NYY": 147, "MIL": 158,
}
# Hand-resolved HR park multipliers (mirror app._hr_park_factor_hand: curated
# split when listed, symmetric index otherwise; /100, rounded to 2dp).
_PARK_HR_BY_HAND = {
    hand: {tid: round(HR_PARK_FACTORS_HAND.get(tid, {}).get(
               hand, HR_PARK_FACTORS.get(tid, 100)) / 100.0, 2)
           for tid in PARK_FACTORS}
    for hand in ("L", "R")
}
# Curated LHB/RHB asymmetry as a RATIO around the park's symmetric level, so it
# can be applied on top of a learned (season-specific) level.
_HAND_ASYM_RATIO = {
    hand: {tid: round(_PARK_HR_BY_HAND[hand][tid]
                      / max(0.01, HR_PARK_FACTORS.get(tid, 100) / 100.0), 4)
           for tid in PARK_FACTORS}
    for hand in ("L", "R")
}


# ── Learned season-specific park factors (Stage 2b) ─────────────────────────
# The static tables above are a multi-year average; the extremes (Coors,
# Camden, the A's Sacramento move) drift season to season. These factors are
# computed from the SAME Statcast pull the models train on, using the classic
# home-vs-road comparison per team (all PAs in team T's home games vs all PAs
# in T's road games — both sides bat in both, so team quality cancels).
# Leakage-safe: season S training rows use a trailing window ENDING S-1.
# Limitation: keyed by team, so a mid-window venue change (ATH 2025) is only
# recency-weighted in, not isolated.
_PARK_TRAIL_WINDOW = 3          # trailing seasons pooled
_PARK_RECENCY_W = {0: 3.0, 1: 2.0, 2: 1.0}   # season lag -> weight
_PARK_SHRINK = 0.70             # shrink pooled ratio toward 1.0
_PARK_MIN_PA = 3000             # below this pooled PA the learned factor is unused


def compute_park_factor_counts(bg: pd.DataFrame) -> dict:
    """Per (season, team_id) home/road counts for park-factor ratios.
    Returns {(season, tid): {pa_h, tb_h, hr_h, pa_r, tb_r, hr_r}}."""
    if "home_team" not in bg.columns or "away_team" not in bg.columns:
        return {}
    d = bg.copy()
    d["_hid"] = d["home_team"].astype(str).str.upper().map(TEAM_ABBR_TO_ID)
    d["_aid"] = d["away_team"].astype(str).str.upper().map(TEAM_ABBR_TO_ID)
    counts: dict = {}
    for side, tid_col in (("h", "_hid"), ("r", "_aid")):
        g = (d.dropna(subset=[tid_col])
              .groupby(["season", tid_col])
              .agg(pa=("pa", "sum"), tb=("tb", "sum"), hr=("hr", "sum")))
        for (season, tid), row in g.iterrows():
            slot = counts.setdefault((int(season), int(tid)),
                                     {"pa_h": 0, "tb_h": 0, "hr_h": 0,
                                      "pa_r": 0, "tb_r": 0, "hr_r": 0})
            slot[f"pa_{side}"] = float(row["pa"])
            slot[f"tb_{side}"] = float(row["tb"])
            slot[f"hr_{side}"] = float(row["hr"])
    return counts


def trailing_park_factors(counts: dict, upto_season: int) -> dict:
    """Recency-weighted pooled park factors from the trailing window ending at
    `upto_season` (inclusive). Returns {tid: {"pf": tb_factor, "hr_pf": hr_factor}}
    — shrunk toward 1.0; teams without enough pooled PA are omitted (callers
    fall back to the static tables)."""
    out = {}
    tids = {tid for (_, tid) in counts}
    for tid in tids:
        agg = {"pa_h": 0.0, "tb_h": 0.0, "hr_h": 0.0,
               "pa_r": 0.0, "tb_r": 0.0, "hr_r": 0.0}
        for lag, w in _PARK_RECENCY_W.items():
            season = upto_season - lag
            c = counts.get((season, tid))
            if not c:
                continue
            for k in agg:
                agg[k] += w * c[k]
        if agg["pa_h"] < _PARK_MIN_PA or agg["pa_r"] < _PARK_MIN_PA:
            continue
        tb_ratio = (agg["tb_h"] / agg["pa_h"]) / max(1e-9, agg["tb_r"] / agg["pa_r"])
        hr_ratio = (agg["hr_h"] / agg["pa_h"]) / max(1e-9, agg["hr_r"] / agg["pa_r"])
        out[int(tid)] = {
            "pf":    round(1.0 + (tb_ratio - 1.0) * _PARK_SHRINK, 3),
            "hr_pf": round(1.0 + (hr_ratio - 1.0) * _PARK_SHRINK, 3),
        }
    return out

XGB_PARAMS_HITS = dict(n_estimators=300, max_depth=4, learning_rate=0.04,
                       subsample=0.80, colsample_bytree=0.80,
                       min_child_weight=3, gamma=0.05)
XGB_PARAMS_K = dict(n_estimators=350, max_depth=4, learning_rate=0.035,
                    subsample=0.78, colsample_bytree=0.78,
                    min_child_weight=4, gamma=0.08)
# HR is rare (~12% of games) → shallower/heavier regularisation + scale_pos_weight
# (added in train_market) to keep the calibrated probabilities honest.
XGB_PARAMS_HR = dict(n_estimators=320, max_depth=4, learning_rate=0.03,
                     subsample=0.80, colsample_bytree=0.75,
                     min_child_weight=5, gamma=0.10)
# TB pos_rate ~0.30, RBI ~0.28 — less rare than HR, so a touch more depth/rounds.
XGB_PARAMS_TB = dict(n_estimators=340, max_depth=4, learning_rate=0.035,
                     subsample=0.80, colsample_bytree=0.80,
                     min_child_weight=4, gamma=0.06)
XGB_PARAMS_RBI = dict(n_estimators=340, max_depth=4, learning_rate=0.035,
                      subsample=0.80, colsample_bytree=0.78,
                      min_child_weight=4, gamma=0.08)

MARKETS = {
    "hits":  dict(file_key="xgb_hits_over_0.5", features=HITS_FEATURES,
                  target="hit_over_0.5", source="batter", line=0.5,
                  params=XGB_PARAMS_HITS),
    "k_3.5": dict(file_key="xgb_k_over_3.5", features=K_FEATURES,
                  target="k_over_3.5", source="pitcher", line=3.5,
                  params=XGB_PARAMS_K),
    "k_4.5": dict(file_key="xgb_k_over_4.5", features=K_FEATURES,
                  target="k_over_4.5", source="pitcher", line=4.5,
                  params=XGB_PARAMS_K),
    "k_5.5": dict(file_key="xgb_k_over_5.5", features=K_FEATURES,
                  target="k_over_5.5", source="pitcher", line=5.5,
                  params=XGB_PARAMS_K),
    "hr":    dict(file_key="xgb_hr_over_0.5", features=HR_FEATURES,
                  target="hr_over_0.5", source="batter", line=0.5,
                  params=XGB_PARAMS_HR),
    "tb":    dict(file_key="xgb_tb_over_1.5", features=TB_FEATURES,
                  target="tb_over_1.5", source="batter", line=1.5,
                  params=XGB_PARAMS_TB),
    "rbi":   dict(file_key="xgb_rbi_over_0.5", features=RBI_FEATURES,
                  target="rbi_over_0.5", source="batter", line=0.5,
                  params=XGB_PARAMS_RBI),
    # ── Alt-line variants (Stage 3): same family features/params, different
    #    target threshold, so the stacked fusion covers the whole board instead
    #    of only each market's standard line.
    "hits_1.5": dict(file_key="xgb_hits_over_1.5", features=HITS_FEATURES,
                     target="hit_over_1.5", source="batter", line=1.5,
                     params=XGB_PARAMS_HITS),
    "tb_2.5":  dict(file_key="xgb_tb_over_2.5", features=TB_FEATURES,
                    target="tb_over_2.5", source="batter", line=2.5,
                    params=XGB_PARAMS_TB),
    "tb_3.5":  dict(file_key="xgb_tb_over_3.5", features=TB_FEATURES,
                    target="tb_over_3.5", source="batter", line=3.5,
                    params=XGB_PARAMS_HR),   # rare event (~11%) — HR-style regularization
    "rbi_1.5": dict(file_key="xgb_rbi_over_1.5", features=RBI_FEATURES,
                    target="rbi_over_1.5", source="batter", line=1.5,
                    params=XGB_PARAMS_HR),   # rare event (~10%)
    "k_2.5":   dict(file_key="xgb_k_over_2.5", features=K_FEATURES,
                    target="k_over_2.5", source="pitcher", line=2.5,
                    params=XGB_PARAMS_K),
    "k_6.5":   dict(file_key="xgb_k_over_6.5", features=K_FEATURES,
                    target="k_over_6.5", source="pitcher", line=6.5,
                    params=XGB_PARAMS_K),
    "k_7.5":   dict(file_key="xgb_k_over_7.5", features=K_FEATURES,
                    target="k_over_7.5", source="pitcher", line=7.5,
                    params=XGB_PARAMS_K),
}


# ════════════════════════════════════════════════════════════════════════
# 1. Statcast game logs (per game outcomes + opposing starter)
# ════════════════════════════════════════════════════════════════════════

def _windows(season: int, days: int = 16):
    start, final = datetime(season, 3, 20), datetime(season, 10, 5)
    cur = start
    while cur <= final:
        wend = min(cur + timedelta(days=days - 1), final)
        yield cur.strftime("%Y-%m-%d"), wend.strftime("%Y-%m-%d")
        cur = wend + timedelta(days=1)


def _agg_chunk(sc: pd.DataFrame, season: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Aggregate one raw-Statcast chunk to per-game batter + pitcher rows.
    Batter rows carry opp_starter (the opposing team's starting pitcher id)."""
    need = ["batter", "pitcher", "game_pk", "events", "inning_topbot", "at_bat_number"]
    sc = sc.dropna(subset=["batter", "pitcher", "game_pk"])
    if len(sc) == 0:
        return pd.DataFrame(), pd.DataFrame()
    for c in ("inning_topbot", "at_bat_number"):
        if c not in sc.columns:
            sc[c] = np.nan

    sc["is_hit"] = sc["events"].isin(["single", "double", "triple", "home_run"]).astype(int)
    sc["is_pa"] = (~sc["events"].isna()).astype(int)
    sc["is_k"] = sc["events"].isin(["strikeout", "strikeout_double_play"]).astype(int)
    sc["is_hr"] = (sc["events"] == "home_run").astype(int)
    sc["is_double"] = (sc["events"] == "double").astype(int)
    sc["is_triple"] = (sc["events"] == "triple").astype(int)
    sc["is_ab"] = sc["events"].isin([
        "single", "double", "triple", "home_run", "strikeout", "field_out",
        "grounded_into_double_play", "double_play", "force_out",
        "fielders_choice", "fielders_choice_out", "strikeout_double_play",
    ]).astype(int)
    # Total bases: single 1, double 2, triple 3, HR 4.
    sc["tb"] = sc["is_hit"] + sc["is_double"] + 2 * sc["is_triple"] + 3 * sc["is_hr"]

    pa = sc[sc["is_pa"] == 1].copy()
    if len(pa) == 0:
        return pd.DataFrame(), pd.DataFrame()

    # RBI proxy: runs scored by the batting team during the plate appearance
    # (post_bat_score − bat_score on the PA-ending pitch). This matches official
    # RBI for the overwhelming majority of PAs; it can marginally overcount runs
    # that score on errors/wild pitches mid-AB — acceptable noise for "≥1 RBI".
    if "post_bat_score" in pa.columns and "bat_score" in pa.columns:
        pa["rbi"] = (pd.to_numeric(pa["post_bat_score"], errors="coerce")
                     - pd.to_numeric(pa["bat_score"], errors="coerce")).clip(lower=0).fillna(0)
    else:
        pa["rbi"] = 0.0

    # ── Starter per (game, side): pitcher with the smallest at_bat_number.
    #    A batter in inning_topbot=='Top' faces the home pitcher (Top-side
    #    starter); 'Bot' faces the away pitcher (Bot-side starter).
    side_first = (pa.sort_values("at_bat_number")
                    .groupby(["game_pk", "inning_topbot"])["pitcher"]
                    .first().reset_index().rename(columns={"pitcher": "opp_starter"}))

    bat = (pa.groupby(["game_pk", "game_date", "batter"])
             .agg(hits=("is_hit", "sum"), ab=("is_ab", "sum"), pa=("is_pa", "sum"),
                  tb=("tb", "sum"), hr=("is_hr", "sum"), rbi=("rbi", "sum"),
                  inning_topbot=("inning_topbot", "first"),
                  stand=("stand", "first") if "stand" in pa.columns else ("is_pa", "size"),
                  p_throws=("p_throws", "first") if "p_throws" in pa.columns else ("is_pa", "size"),
                  home_team=("home_team", "first") if "home_team" in pa.columns else ("is_pa", "size"),
                  away_team=("away_team", "first") if "away_team" in pa.columns else ("is_pa", "size"))
             .reset_index())
    bat = bat.merge(side_first, on=["game_pk", "inning_topbot"], how="left")

    # ── Per-game contact-quality + swing-and-miss (momentum inputs). Computed
    #    from the SAME raw Statcast columns the live scorer reads via
    #    statcast_batter (launch_speed, launch_speed_angle, description), so the
    #    rolling form built downstream is train/serve identical. barrel = the
    #    Statcast barrel classification (launch_speed_angle == 6).
    if "launch_speed" in sc.columns:
        bb = sc.dropna(subset=["launch_speed"]).copy()
        if "launch_speed_angle" not in bb.columns:
            bb["launch_speed_angle"] = np.nan
        if len(bb):
            bb["launch_speed_angle"] = pd.to_numeric(bb["launch_speed_angle"], errors="coerce")
            g_ev = (bb.groupby(["game_pk", "batter"])
                      .agg(g_ev=("launch_speed", "mean"),
                           g_hh=("launch_speed", lambda x: float((x >= 95.0).mean())),
                           g_barrel=("launch_speed_angle", lambda x: float((x.fillna(-1) == 6).mean())))
                      .reset_index())
            bat = bat.merge(g_ev, on=["game_pk", "batter"], how="left")
    if "description" in sc.columns:
        desc = sc["description"].astype(str)
        sc["_is_swstr"] = desc.isin(["swinging_strike", "swinging_strike_blocked", "foul_tip"])
        sc["_is_swing"] = desc.isin(["swinging_strike", "swinging_strike_blocked",
                                     "foul_tip", "foul", "hit_into_play"])
        gw = (sc.groupby(["game_pk", "batter"])
                .agg(_sw=("_is_swing", "sum"), _ws=("_is_swstr", "sum"))
                .reset_index())
        gw["g_whiff"] = gw["_ws"] / gw["_sw"].clip(lower=1)
        bat = bat.merge(gw[["game_pk", "batter", "g_whiff"]],
                        on=["game_pk", "batter"], how="left")

    # ── Lineup slot (1-9) from at-bat order within each side: the batter whose
    #    first at_bat_number is smallest leads off. Late subs / pinch hitters
    #    rank past 9 and clip to 9 (a low-PA role). Mirrors the serve-time
    #    batting_order so expected_pa stays train/serve consistent.
    fab = (pa.groupby(["game_pk", "inning_topbot", "batter"], as_index=False)["at_bat_number"]
             .min())
    fab["batting_order"] = (fab.groupby(["game_pk", "inning_topbot"])["at_bat_number"]
                            .rank(method="first").clip(upper=9))
    bat = bat.merge(fab[["game_pk", "inning_topbot", "batter", "batting_order"]],
                    on=["game_pk", "inning_topbot", "batter"], how="left")

    bat["hit_over_0.5"] = (bat["hits"] >= 1).astype(int)
    bat["hit_over_1.5"] = (bat["hits"] >= 2).astype(int)
    bat["hr_over_0.5"] = (bat["hr"] >= 1).astype(int)
    bat["tb_over_1.5"] = (bat["tb"] >= 2).astype(int)
    bat["tb_over_2.5"] = (bat["tb"] >= 3).astype(int)
    bat["tb_over_3.5"] = (bat["tb"] >= 4).astype(int)
    bat["rbi_over_0.5"] = (bat["rbi"] >= 1).astype(int)
    bat["rbi_over_1.5"] = (bat["rbi"] >= 2).astype(int)
    bat["season"] = season

    pit = (pa.groupby(["game_pk", "game_date", "pitcher"])
             .agg(ks=("is_k", "sum"), bf=("is_pa", "sum"))
             .reset_index())
    pit["k_over_2.5"] = (pit["ks"] >= 3).astype(int)
    pit["k_over_3.5"] = (pit["ks"] >= 4).astype(int)
    pit["k_over_4.5"] = (pit["ks"] >= 5).astype(int)
    pit["k_over_5.5"] = (pit["ks"] >= 6).astype(int)
    pit["k_over_6.5"] = (pit["ks"] >= 7).astype(int)
    pit["k_over_7.5"] = (pit["ks"] >= 8).astype(int)
    pit["season"] = season
    return bat, pit


def fetch_game_logs(seasons: list[int]) -> tuple[pd.DataFrame, pd.DataFrame]:
    import pybaseball as pb
    pb.cache.enable()
    bf, pf = [], []
    windows = [(s, ws, we) for s in seasons for (ws, we) in _windows(s)]
    for i, (season, ws, we) in enumerate(windows, 1):
        print(f"  [{i}/{len(windows)}] Statcast {ws} → {we}", flush=True)
        try:
            sc = pb.statcast(start_dt=ws, end_dt=we)
        except Exception as e:
            print(f"    ⚠️ {ws}→{we} failed: {e}")
            continue
        if sc is None or len(sc) == 0:
            continue
        try:
            b, p = _agg_chunk(sc, season)
        finally:
            del sc
            gc.collect()
        if len(b):
            bf.append(b)
        if len(p):
            pf.append(p)
    bg = pd.concat(bf, ignore_index=True) if bf else pd.DataFrame()
    pg = pd.concat(pf, ignore_index=True) if pf else pd.DataFrame()
    print(f"  Batter game rows: {len(bg):,}  Pitcher game rows: {len(pg):,}")
    return bg, pg


# ════════════════════════════════════════════════════════════════════════
# 2. Local FanGraphs season stats (same source + scale as the live scorer)
# ════════════════════════════════════════════════════════════════════════

def _norm_pct(s: pd.Series) -> pd.Series:
    """Mirror the scorer: values in (0,1] are ×100 to a percent scale."""
    s = pd.to_numeric(s, errors="coerce")
    return np.where((s > 0) & (s <= 1.0), s * 100.0, s)


def load_fg_batting(season: int) -> pd.DataFrame:
    path = os.path.join(_DATA_DIR, f"fg_batting_{season}.csv")
    if not os.path.exists(path):
        return pd.DataFrame()
    df = pd.read_csv(path)
    if "xMLBAMID" not in df.columns:
        return pd.DataFrame()
    out = pd.DataFrame()
    out["mlbam"] = pd.to_numeric(df["xMLBAMID"], errors="coerce")
    # contact quality (decimals / raw, unscaled — matches serve)
    out["sv_xba"] = pd.to_numeric(df.get("xAVG"), errors="coerce")
    out["sv_xwoba"] = pd.to_numeric(df.get("xwOBA"), errors="coerce")
    out["sv_xslg"] = pd.to_numeric(df.get("xSLG"), errors="coerce")
    out["sv_ev"] = pd.to_numeric(df.get("EV"), errors="coerce")
    out["sv_brl_pct"] = pd.to_numeric(df.get("Barrel%"), errors="coerce")   # fraction, unscaled
    out["sv_hh_pct"] = pd.to_numeric(df.get("HardHit%"), errors="coerce")   # fraction, unscaled
    out["sv_ss_pct"] = pd.to_numeric(df.get("SwStr%"), errors="coerce")     # fraction, unscaled
    out["sv_la"] = pd.to_numeric(df.get("LA"), errors="coerce")
    out["sv_k_pct"] = _norm_pct(df.get("K%"))    # ×100 → percent scale
    out["sv_bb_pct"] = _norm_pct(df.get("BB%"))  # ×100 → percent scale
    # HR-specific power/loft skills (raw FG values, unscaled — matches serve).
    out["sv_iso"] = pd.to_numeric(df.get("ISO"), errors="coerce")
    out["sv_hrfb"] = pd.to_numeric(df.get("HR/FB"), errors="coerce")   # fraction
    out["sv_fb_pct"] = pd.to_numeric(df.get("FB%"), errors="coerce")   # fraction
    out["sv_maxev"] = pd.to_numeric(df.get("maxEV"), errors="coerce")
    bats = df.get("Bats")
    out["bats_L"] = (bats == "L").astype(int) if bats is not None else 0
    out = out.dropna(subset=["mlbam"])
    out["mlbam"] = out["mlbam"].astype(int)
    return out.drop_duplicates(subset=["mlbam"])


def load_bat_tracking(season: int) -> pd.DataFrame:
    """Season bat-tracking leaderboard (Statcast, 2024+) keyed by MLBAM id.
    Pre-2024 seasons have no file → empty frame → rows keep NaN, which the
    trainer deliberately does NOT impute for bt_* features (XGB's native
    missing-handling learns the pre-bat-tracking-era direction instead of
    training on fabricated medians)."""
    path = os.path.join(_DATA_DIR, f"savant_bat_tracking_{season}.csv")
    if not os.path.exists(path):
        return pd.DataFrame()
    df = pd.read_csv(path, low_memory=False)
    if "id" not in df.columns:
        return pd.DataFrame()
    out = pd.DataFrame()
    out["mlbam"] = pd.to_numeric(df["id"], errors="coerce")
    out["bt_bat_speed"]  = pd.to_numeric(df.get("avg_bat_speed"), errors="coerce")
    out["bt_fast_swing"] = pd.to_numeric(df.get("hard_swing_rate"), errors="coerce")
    out["bt_squared_up"] = pd.to_numeric(df.get("squared_up_per_swing"), errors="coerce")
    out["bt_blast"]      = pd.to_numeric(df.get("blast_per_swing"), errors="coerce")
    out = out.dropna(subset=["mlbam"])
    out["mlbam"] = out["mlbam"].astype(int)
    return out.drop_duplicates(subset=["mlbam"])


def load_fg_pitching(season: int) -> pd.DataFrame:
    path = os.path.join(_DATA_DIR, f"fg_pitching_{season}.csv")
    if not os.path.exists(path):
        return pd.DataFrame()
    df = pd.read_csv(path)
    if "xMLBAMID" not in df.columns:
        return pd.DataFrame()
    out = pd.DataFrame()
    out["mlbam"] = pd.to_numeric(df["xMLBAMID"], errors="coerce")
    out["sv_xera"] = pd.to_numeric(df.get("xERA"), errors="coerce")
    out["sv_era"] = pd.to_numeric(df.get("ERA"), errors="coerce")
    out["sv_k_pct"] = _norm_pct(df.get("K%"))
    out["sv_bb_pct"] = _norm_pct(df.get("BB%"))
    out["sv_whiff_pct"] = _norm_pct(df.get("SwStr%"))
    # HR-allowed profile (raw FG values, unscaled — matches serve).
    out["p_hr9"] = pd.to_numeric(df.get("HR/9"), errors="coerce")
    out["p_hrfb"] = pd.to_numeric(df.get("HR/FB"), errors="coerce")   # fraction
    out["p_fb_pct"] = pd.to_numeric(df.get("FB%"), errors="coerce")   # fraction
    out["p_barrel"] = pd.to_numeric(df.get("Barrel%"), errors="coerce")  # fraction
    throws = df.get("Throws")
    out["throws_R"] = (throws == "R").astype(int) if throws is not None else 1
    out = out.dropna(subset=["mlbam"])
    out["mlbam"] = out["mlbam"].astype(int)
    return out.drop_duplicates(subset=["mlbam"])


# ════════════════════════════════════════════════════════════════════════
# 3. Feature matrices
# ════════════════════════════════════════════════════════════════════════

def build_batter_matrix(bg: pd.DataFrame) -> pd.DataFrame:
    bg = bg.sort_values(["batter", "game_date"]).copy()
    # Lineup-role features (reconstructed in _agg_chunk). expected_pa via the
    # serve-time slot→PA table; unknown slot (0) → league-default PA.
    bg["batting_order"] = pd.to_numeric(bg.get("batting_order"), errors="coerce").fillna(0).astype(int)
    bg["expected_pa"] = bg["batting_order"].map(_PA_BY_SLOT).fillna(_DEFAULT_PA)
    bg["ab_safe"] = bg["ab"].clip(lower=1)
    bg["l7_hits"] = bg.groupby("batter")["hits"].transform(
        lambda x: x.shift(1).rolling(7, min_periods=1).mean())
    bg["l14_hits"] = bg.groupby("batter")["hits"].transform(
        lambda x: x.shift(1).rolling(14, min_periods=1).mean())
    bg["hit_rate_game"] = bg["hits"] / bg["ab_safe"]
    bg["l7_hit_rate"] = bg.groupby("batter")["hit_rate_game"].transform(
        lambda x: x.shift(1).rolling(7, min_periods=1).mean())

    # ── True-talent latency / "heater" momentum. Recent contact quality vs the
    #    batter's own 30-game baseline. Each rolling window is shifted by one so
    #    the upcoming game's outcome never leaks; at serve the most recent
    #    completed games ARE these prior games (no shift), matching exactly.
    for src in ("g_ev", "g_hh", "g_whiff", "g_barrel"):
        if src not in bg.columns:
            bg[src] = np.nan
    bg["l7_ev"] = bg.groupby("batter")["g_ev"].transform(
        lambda x: x.shift(1).rolling(7, min_periods=1).mean())
    bg["l30_ev"] = bg.groupby("batter")["g_ev"].transform(
        lambda x: x.shift(1).rolling(30, min_periods=3).mean())
    bg["l7_hh"] = bg.groupby("batter")["g_hh"].transform(
        lambda x: x.shift(1).rolling(7, min_periods=1).mean())
    bg["l7_whiff"] = bg.groupby("batter")["g_whiff"].transform(
        lambda x: x.shift(1).rolling(7, min_periods=1).mean())
    bg["l30_whiff"] = bg.groupby("batter")["g_whiff"].transform(
        lambda x: x.shift(1).rolling(30, min_periods=3).mean())
    # Barrel momentum — the strongest "heater" signal for home runs.
    bg["l7_barrel"] = bg.groupby("batter")["g_barrel"].transform(
        lambda x: x.shift(1).rolling(7, min_periods=1).mean())
    bg["l30_barrel"] = bg.groupby("batter")["g_barrel"].transform(
        lambda x: x.shift(1).rolling(30, min_periods=3).mean())
    # Momentum ratio = recent / baseline (>1 = heating up). Neutral 1.0 when the
    # baseline is missing/zero, so an early-season batter is simply "no signal".
    bg["ev_momentum"] = (bg["l7_ev"] / bg["l30_ev"]).replace([np.inf, -np.inf], np.nan)
    bg["whiff_momentum"] = (bg["l7_whiff"] / bg["l30_whiff"]).replace([np.inf, -np.inf], np.nan)
    bg["barrel_momentum"] = (bg["l7_barrel"] / bg["l30_barrel"]).replace([np.inf, -np.inf], np.nan)
    bg["ev_momentum"] = bg["ev_momentum"].fillna(1.0).clip(0.85, 1.15)
    bg["whiff_momentum"] = bg["whiff_momentum"].fillna(1.0).clip(0.5, 2.0)
    bg["barrel_momentum"] = bg["barrel_momentum"].fillna(1.0).clip(0.4, 2.5)

    # ── Park factors from the game's home team — exactly reconstructable
    #    historically (unlike weather/umpire). Level comes from the LEARNED
    #    season-specific factors when the trailing window (ending the row's
    #    season minus one — leakage-safe) has data; static tables otherwise
    #    (all of 2021, thin teams). park_hr applies the curated LHB/RHB
    #    asymmetry ratio on top of the level using the batter's real stand.
    if "home_team" in bg.columns:
        hid = pd.to_numeric(bg["home_team"].astype(str).str.upper().map(TEAM_ABBR_TO_ID),
                            errors="coerce")
        stand_l = bg.get("stand").astype(str).eq("L") if "stand" in bg.columns else \
            pd.Series(False, index=bg.index)
        pf_static = pd.to_numeric(hid.map(PARK_FACTORS), errors="coerce").fillna(1.0)
        hr_static = pd.Series(np.where(
            stand_l,
            pd.to_numeric(hid.map(_PARK_HR_BY_HAND["L"]), errors="coerce"),
            pd.to_numeric(hid.map(_PARK_HR_BY_HAND["R"]), errors="coerce")),
            index=bg.index).fillna(1.0)
        counts = compute_park_factor_counts(bg)
        pf = pf_static.copy()
        park_hr = hr_static.copy()
        for season in sorted(pd.to_numeric(bg["season"], errors="coerce").dropna().unique()):
            learned = trailing_park_factors(counts, int(season) - 1)
            if not learned:
                continue
            mask = bg["season"] == season
            l_pf = hid[mask].map(lambda t: (learned.get(int(t)) or {}).get("pf") if pd.notna(t) else None)
            l_hr = hid[mask].map(lambda t: (learned.get(int(t)) or {}).get("hr_pf") if pd.notna(t) else None)
            asym = pd.Series(np.where(
                stand_l[mask],
                hid[mask].map(_HAND_ASYM_RATIO["L"]),
                hid[mask].map(_HAND_ASYM_RATIO["R"])), index=bg.index[mask])
            asym = pd.to_numeric(asym, errors="coerce").fillna(1.0)
            l_pf = pd.to_numeric(l_pf, errors="coerce")
            l_hr = pd.to_numeric(l_hr, errors="coerce")
            pf.loc[mask] = l_pf.fillna(pf_static[mask])
            park_hr.loc[mask] = (l_hr * asym).round(3).fillna(hr_static[mask])
        bg["park_factor"] = pf
        bg["park_hr"] = park_hr
    else:
        bg["park_factor"] = 1.0
        bg["park_hr"] = 1.0

    frames = []
    for season in sorted(bg["season"].unique()):
        sub = bg[bg["season"] == season].copy()
        fb = load_fg_batting(season)
        fp = load_fg_pitching(season)
        if fb.empty:
            print(f"    ⚠️ no FG batting CSV for {season}; skipping its rows")
            continue
        # batter own skills
        sub = sub.merge(fb, left_on="batter", right_on="mlbam", how="inner",
                        suffixes=("", "_fb"))
        # bat-tracking skills (2024+; earlier seasons stay NaN by design)
        bt = load_bat_tracking(season)
        if not bt.empty:
            sub = sub.merge(bt, left_on="batter", right_on="mlbam",
                            how="left", suffixes=("", "_bt"))
        # opponent starter skills
        if not fp.empty:
            opp = fp.rename(columns={
                "sv_xera": "opp_xera", "sv_k_pct": "opp_k_pct",
                "sv_bb_pct": "opp_bb_pct", "sv_whiff_pct": "opp_whiff",
                "p_hr9": "opp_hr9", "p_hrfb": "opp_hrfb",
                "p_fb_pct": "opp_fb_pct", "p_barrel": "opp_barrel",
            })[["mlbam", "opp_xera", "opp_k_pct", "opp_bb_pct", "opp_whiff",
                "opp_hr9", "opp_hrfb", "opp_fb_pct", "opp_barrel", "throws_R"]]
            sub = sub.merge(opp, left_on="opp_starter", right_on="mlbam",
                            how="left", suffixes=("", "_op"))
        # handedness / platoon from real stand + opposing starter throws
        if "stand" in sub.columns:
            sub["bats_L"] = (sub["stand"] == "L").astype(int)
        sub["throws_R"] = sub.get("throws_R", 1)
        sub["throws_R"] = pd.to_numeric(sub["throws_R"], errors="coerce").fillna(1).astype(int)
        if "stand" in sub.columns:
            sub["platoon_adv"] = (
                ((sub["stand"] == "L") & (sub["throws_R"] == 1)) |
                ((sub["stand"] == "R") & (sub["throws_R"] == 0))
            ).astype(int)
        else:
            sub["platoon_adv"] = 0
        frames.append(sub)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def build_pitcher_matrix(pg: pd.DataFrame) -> pd.DataFrame:
    pg = pg.sort_values(["pitcher", "game_date"]).copy()
    pg["ip"] = np.clip(pg["bf"] / 4.2, 0, 9)
    pg["prev"] = pg.groupby("pitcher")["game_date"].shift(1)
    pg["days_rest"] = (pd.to_datetime(pg["game_date"]) - pd.to_datetime(pg["prev"])
                       ).dt.days.fillna(5).clip(0, 14)
    for w, col in [(3, "l3_ks"), (5, "l5_ks"), (10, "l10_ks")]:
        pg[col] = pg.groupby("pitcher")["ks"].transform(
            lambda x, w=w: x.shift(1).rolling(w, min_periods=1).mean())
    for w, col in [(3, "l3_ip"), (5, "l5_ip")]:
        pg[col] = pg.groupby("pitcher")["ip"].transform(
            lambda x, w=w: x.shift(1).rolling(w, min_periods=1).mean())
    pg["k_rate_game"] = pg["ks"] / pg["bf"].clip(lower=1)
    pg["l5_k_rate"] = pg.groupby("pitcher")["k_rate_game"].transform(
        lambda x: x.shift(1).rolling(5, min_periods=1).mean())

    # starters only (≥4 IP outings)
    pg = pg[pg["ip"] >= 4.0].copy()

    frames = []
    for season in sorted(pg["season"].unique()):
        sub = pg[pg["season"] == season].copy()
        fp = load_fg_pitching(season)
        if fp.empty:
            print(f"    ⚠️ no FG pitching CSV for {season}; skipping its rows")
            continue
        sub = sub.merge(fp, left_on="pitcher", right_on="mlbam", how="inner",
                        suffixes=("", "_fp"))
        frames.append(sub)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


# ════════════════════════════════════════════════════════════════════════
# 4. Train + calibrate + honest holdout eval
# ════════════════════════════════════════════════════════════════════════

def train_market(mkey: str, cfg: dict, df: pd.DataFrame) -> Optional[dict]:
    feats, target, params = cfg["features"], cfg["target"], dict(cfg["params"])
    print(f"\n── {mkey} ({target}) ──")
    for c in feats:
        if c not in df.columns:
            df[c] = np.nan
    sub = df[feats + [target, "season"]].copy()
    # median-impute only genuine gaps in real features (per train set later)
    sub = sub.dropna(subset=[target])
    tr = sub[sub["season"].isin(TRAIN_SEASONS)].copy()
    te = sub[sub["season"] == TEST_SEASON].copy()
    if len(tr) < 500 or len(te) < 100:
        print(f"  ✗ insufficient rows (train={len(tr)}, test={len(te)})")
        return None

    med = tr[feats].median()
    # bt_* features are structurally missing before 2024 (bat tracking didn't
    # exist) — imputing the pooled median would fabricate values for most of
    # the training era. Leave them NaN; XGBoost's native missing-handling
    # learns the correct default direction, and serve passes NaN when a
    # batter has no bat-tracking row (below-min-swings or pre-season).
    impute_cols = [c for c in feats if not c.startswith("bt_")]
    tr[impute_cols] = tr[impute_cols].fillna(med[impute_cols])
    te[impute_cols] = te[impute_cols].fillna(med[impute_cols])
    train_medians = {c: (round(float(med[c]), 5) if pd.notna(med[c]) else None)
                     for c in feats}

    Xtr, ytr = tr[feats].values.astype(np.float32), tr[target].values.astype(int)
    Xte, yte = te[feats].values.astype(np.float32), te[target].values.astype(int)
    pos = ytr.mean()
    print(f"  train={len(ytr):,}  test={len(yte):,}  pos_rate(train)={pos:.3f}")

    if pos < 0.35:
        params["scale_pos_weight"] = round((1 - pos) / max(pos, 1e-6), 2)

    base = xgb.XGBClassifier(**params, eval_metric="logloss",
                             random_state=SEED, n_jobs=-1)
    # Calibrate via internal CV on the TRAIN set only (no test leakage).
    model = CalibratedClassifierCV(base, cv=4, method="isotonic")
    model.fit(Xtr, ytr)

    p_te = model.predict_proba(Xte)[:, 1]
    p_tr = model.predict_proba(Xtr)[:, 1]
    auc_te = roc_auc_score(yte, p_te)
    auc_tr = roc_auc_score(ytr, p_tr)
    brier_te = brier_score_loss(yte, p_te)
    brier_base = brier_score_loss(yte, np.full_like(p_te, yte.mean()))
    ll_te = log_loss(yte, p_te, labels=[0, 1])
    print(f"  TEST  AUC={auc_te:.4f}  (train AUC={auc_tr:.4f})")
    print(f"  TEST  Brier={brier_te:.4f}  vs base-rate Brier={brier_base:.4f}  "
          f"(skill={'+' if brier_te < brier_base else ''}{brier_base - brier_te:.4f})")
    print(f"  TEST  pred mean={p_te.mean():.3f}  actual={yte.mean():.3f}")

    artifact = {
        "model": model,
        "features": feats,
        "meta": {
            "market": mkey, "target": target, "line": cfg["line"],
            "test_auc": round(float(auc_te), 4),
            "train_auc": round(float(auc_tr), 4),
            "test_brier": round(float(brier_te), 4),
            "baserate_brier": round(float(brier_base), 4),
            "test_logloss": round(float(ll_te), 4),
            "n_train": int(len(ytr)), "n_test": int(len(yte)),
            "pos_rate": round(float(pos), 4),
            "train_seasons": TRAIN_SEASONS, "test_season": TEST_SEASON,
            "calibration": "isotonic_cv4",
            "train_medians": train_medians,
            "xgboost_version": xgb.__version__,
            "exported_at_utc": datetime.utcnow().isoformat(),
            "model_type": "CalibratedClassifierCV(XGBClassifier)",
        },
    }
    return {"artifact": artifact, "auc_te": auc_te, "brier_te": brier_te,
            "brier_base": brier_base, "features": feats}


# Model key → tracker marketKey. This is what lets /api/calibration/markets
# line the live tracker Brier up against the right held-out benchmark.
TRACKER_MARKET = {
    "hits":     "batter_hits",
    "hits_1.5": "batter_hits",
    "k_2.5": "pitcher_strikeouts",
    "k_3.5": "pitcher_strikeouts",
    "k_4.5": "pitcher_strikeouts",
    "k_5.5": "pitcher_strikeouts",
    "k_6.5": "pitcher_strikeouts",
    "k_7.5": "pitcher_strikeouts",
    "hr":      "batter_home_runs",
    "tb":      "batter_total_bases",
    "tb_2.5":  "batter_total_bases",
    "tb_3.5":  "batter_total_bases",
    "rbi":     "batter_rbis",
    "rbi_1.5": "batter_rbis",
}
# The line the tracker most commonly grades against, per tracker market —
# used to pick the representative model when several lines share a market.
_REPRESENTATIVE = {"pitcher_strikeouts": "k_4.5"}

_METRICS_PATH = os.path.join(_MODEL_DIR, "model_metrics.json")
_METRIC_KEYS = ("line", "test_auc", "train_auc", "test_brier", "baserate_brier",
                "test_logloss", "n_train", "n_test", "test_season")


def write_model_metrics(metas: dict) -> dict:
    """Merge per-market held-out metas into models/model_metrics.json.

    That file is the benchmark /api/calibration/markets compares live tracker
    Brier against — if a shipped model is missing here, its market can never
    be flagged no_edge/degraded in production. Merging (never overwriting)
    means a partial run (--markets hr) can't drop the benchmarks of markets it
    didn't retrain. `metas` maps model key (hits/k_4.5/hr/…) → artifact meta.
    """
    existing = {}
    if os.path.exists(_METRICS_PATH):
        try:
            with open(_METRICS_PATH) as f:
                existing = json.load(f) or {}
        except Exception:
            existing = {}
    models = existing.get("models", {})
    for mkey, meta in metas.items():
        if not meta or mkey not in TRACKER_MARKET:
            continue
        entry = {"tracker_market": TRACKER_MARKET[mkey]}
        entry.update({k: meta.get(k) for k in _METRIC_KEYS if meta.get(k) is not None})
        models[mkey] = entry

    # Rebuild the tracker-market rollup from the merged model map.
    by_tracker = {}
    for tmk in sorted({v["tracker_market"] for v in models.values()}):
        keys = sorted(k for k, v in models.items() if v["tracker_market"] == tmk)
        rep = _REPRESENTATIVE.get(tmk) if _REPRESENTATIVE.get(tmk) in keys else keys[0]
        m = models[rep]
        roll = {"representative": rep,
                "test_auc": m.get("test_auc"),
                "test_brier": m.get("test_brier"),
                "baserate_brier": m.get("baserate_brier")}
        if len(keys) > 1:
            roll["all_lines"] = {k: models[k].get("test_auc") for k in keys}
        by_tracker[tmk] = roll

    out = {
        "generated_at_utc": datetime.utcnow().isoformat(),
        "note": ("Held-out (2021-24 train / 2025 test) benchmarks from "
                 "regenerate_models.py. Compare live tracker Brier against "
                 "test_brier; both should beat baserate_brier."),
        "models": models,
        "by_tracker_market": by_tracker,
    }
    with open(_METRICS_PATH, "w") as f:
        json.dump(out, f, indent=2)
    return out


def main(markets: list[str]):
    print("═" * 70)
    print(f" Honest XGB regeneration — {datetime.utcnow():%Y-%m-%d %H:%M} UTC")
    print(f" Train seasons {TRAIN_SEASONS}  |  Test season {TEST_SEASON}")
    print("═" * 70)

    print("\n══ Fetching Statcast game logs ══")
    bg, pg = fetch_game_logs(ALL_SEASONS)

    print("\n══ Building feature matrices ══")
    bat = build_batter_matrix(bg) if len(bg) else pd.DataFrame()
    pit = build_pitcher_matrix(pg) if len(pg) else pd.DataFrame()
    print(f"  batter matrix={bat.shape}  pitcher matrix={pit.shape}")

    # Export the learned park factors for the serve side (app.py loads
    # data/park_factors_learned.json and falls back to its static tables).
    # Trailing window ends at the last completed season — correct for serving
    # the following season, no leakage (those games are played).
    if len(bg):
        learned = trailing_park_factors(compute_park_factor_counts(bg), TEST_SEASON)
        if learned:
            park_path = os.path.join(_DATA_DIR, "park_factors_learned.json")
            with open(park_path, "w") as f:
                json.dump({
                    "asof_season": TEST_SEASON,
                    "recency_weights": _PARK_RECENCY_W,
                    "shrink": _PARK_SHRINK,
                    "note": ("Learned home-vs-road park factors (pf=TB-based, "
                             "hr_pf=HR-based), recency-weighted over the trailing "
                             "3 seasons, shrunk toward 1.0. Keyed by MLBAM team id. "
                             "app.py applies the curated LHB/RHB asymmetry ratio "
                             "on top of hr_pf."),
                    "factors": {str(t): v for t, v in sorted(learned.items())},
                }, f, indent=2)
            print(f"  Park factors → {park_path} ({len(learned)} teams)")

    results, feat_map = {}, {}
    for mkey in markets:
        cfg = MARKETS[mkey]
        df = bat if cfg["source"] == "batter" else pit
        if df.empty:
            print(f"\n── {mkey} ── ✗ no data")
            results[mkey] = None
            continue
        r = train_market(mkey, cfg, df.copy())
        results[mkey] = r

    # Decision gate: only ship a model that beats the base-rate Brier on the
    # held-out season (i.e. it has genuine out-of-sample skill).
    print("\n══ Ship decision (held-out skill gate) ══")
    shipped = []
    for mkey, r in results.items():
        if not r:
            print(f"  {mkey:<8} ✗ FAILED")
            continue
        skill = r["brier_base"] - r["brier_te"]
        ok = (r["auc_te"] >= 0.53) and (skill > 0)
        verdict = "✓ SHIP" if ok else "✗ HOLD (no edge over base rate)"
        print(f"  {mkey:<8} testAUC={r['auc_te']:.4f}  brierSkill={skill:+.4f}  {verdict}")
        if ok:
            cfg = MARKETS[mkey]
            joblib.dump(r["artifact"], os.path.join(_MODEL_DIR, f"{cfg['file_key']}.pkl"))
            feat_map[mkey] = r["features"]
            feat_map[cfg["file_key"].replace("xgb_", "")] = r["features"]
            shipped.append(mkey)

    if feat_map:
        # Merge into the existing map so a partial run (e.g. --markets hits)
        # never drops the feature lists of markets it didn't retrain.
        fcols_path = os.path.join(_MODEL_DIR, "xgb_feature_cols.json")
        merged = {}
        if os.path.exists(fcols_path):
            try:
                with open(fcols_path) as f:
                    merged = json.load(f) or {}
            except Exception:
                merged = {}
        merged.update(feat_map)
        with open(fcols_path, "w") as f:
            json.dump(merged, f, indent=2)
    # Merge (not overwrite) so a partial run keeps the other markets' summaries.
    summary_path = os.path.join(_DATA_DIR, "regen_summary.json")
    summary = {}
    if os.path.exists(summary_path):
        try:
            with open(summary_path) as f:
                summary = json.load(f) or {}
        except Exception:
            summary = {}
    summary.update({k: v["artifact"]["meta"] for k, v in results.items() if v})
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)

    # Refresh the held-out benchmarks the live calibration monitor reads —
    # only for markets that actually shipped (a HOLD model has no benchmark
    # to compare live picks against).
    write_model_metrics({k: results[k]["artifact"]["meta"] for k in shipped})

    print(f"\n  Shipped: {shipped or 'NONE'}")
    print(f"  Summary → data/regen_summary.json")
    print(f"  Benchmarks → models/model_metrics.json")
    return results


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--markets", nargs="+", default=list(MARKETS.keys()))
    args = ap.parse_args()
    main([m for m in args.markets if m in MARKETS])
