"""
train_prop_models.py  —  Prop-Type-Specific XGBoost Model Training
═════════════════════════════════════════════════════════════════════

Each of the seven prop markets gets its own model with purpose-built
feature sets and tuned hyperparameters. The previous pipeline trained
a single shared XGB for all batter props and another for all K lines—
this replaces that with fully independent models.

Why separate models per prop type?
  - Hits:  driven by contact quality (xBA, EV, LA, platoon edge)
  - HR:    driven by power metrics (Barrel%, SLG, park factor, wind)
  - TB:    driven by both contact + power (blended feature set)
  - RBI:   driven by lineup context (batting order, runners on rate)
  - K3.5:  starter durability as important as pure K rate
  - K4.5:  balanced K rate + opponent lineup K% (the bread-and-butter line)
  - K5.5:  rare event — needs class-weight=balanced, fewer trees

Output artifacts (compatible with xgb_prop_scorer._load_models()):
    models/xgb_{market_key}.pkl  → {model, features, meta}
    models/xgb_feature_cols.json → {market_key: [feature_col, ...], ...}

Usage:
    python train_prop_models.py               # train all markets
    python train_prop_models.py --market hits  # train one market only
    python train_prop_models.py --seasons 2022 2023 2024 2025

Prerequisites:
    pip install xgboost pybaseball scikit-learn joblib pandas numpy
    (same dependencies as xgb_prop_pipeline.py)
"""

from __future__ import annotations

import argparse
import gc
import io
import json
import os
import urllib.request
import warnings
from datetime import datetime, timedelta
from typing import Callable, Optional

import joblib
import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV, calibration_curve
from sklearn.metrics import (
    brier_score_loss, log_loss, roc_auc_score, classification_report,
)
from sklearn.model_selection import StratifiedKFold, cross_val_score
from sklearn.preprocessing import LabelEncoder
import xgboost as xgb

warnings.filterwarnings("ignore")

SEED = 42
np.random.seed(SEED)

_HERE       = os.path.dirname(os.path.abspath(__file__))
_MODEL_DIR  = os.path.join(_HERE, "models")
_OUTPUT_DIR = os.path.join(_HERE, "data", "train_output")
os.makedirs(_MODEL_DIR,  exist_ok=True)
os.makedirs(_OUTPUT_DIR, exist_ok=True)

DEFAULT_SEASONS = [2021, 2022, 2023, 2024, 2025]
PA_MIN  = 50
IP_MIN  = 10


# ═════════════════════════════════════════════════════════════════════
# PROP-TYPE FEATURE SCHEMAS
# Each dict defines the canonical feature columns for a market.
# These must match what _build_hit_features / _build_k_features in
# xgb_prop_scorer.py produce. The scorer uses _feat_cols[key] as the
# column order for the inference matrix.
# ═════════════════════════════════════════════════════════════════════

# ── Hits ───────────────────────────────────────────────────────────────
# Contact quality + plate discipline + recent form + platoon edge.
# Wind/temp matter less for hits than for HR; excluded here.
HITS_FEATURES = [
    # Contact quality
    "sv_xba",           # expected batting average (Statcast)
    "sv_xwoba",         # expected wOBA
    "sv_ev",            # average exit velocity
    "sv_hh_pct",        # hard-hit rate (EV ≥95)
    "sv_ss_pct",        # sweet-spot rate
    "sv_la",            # average launch angle
    "sv_brl_pct",       # barrel rate
    # Plate discipline
    "sv_k_pct",         # K rate (high K = fewer ABs)
    "sv_bb_pct",        # BB rate (more PA = more hit chances)
    # Opponent pitcher quality
    "opp_xera",         # opponent xERA
    "opp_k_pct",        # opponent K rate
    "opp_bb_pct",       # opponent BB rate
    "opp_whiff",        # opponent whiff rate
    # Handedness / platoon
    "bats_L",
    "throws_R",
    "platoon_adv",      # 1 = batter has platoon advantage
    # Recent form (rolling game log)
    "l7_hits",          # hits per game, last 7 games
    "l14_hits",
    "l7_hit_rate",      # H/AB last 7
    # Lineup context (from lineup_loader)
    "expected_pa",      # projected plate appearances this game
    "batting_order",    # lineup slot (1-9)
    "lineup_confirmed", # 1 = official lineup posted
    # Park & weather adjustment scalars
    "park_factor",
    "wx_temp_mult",
    "wx_wind_mult",
    # Matchup-specific edges
    "pitch_mix_slg_edge",
    "bvp_woba_edge_shrunk",
    "split_ops_edge",
]

# ── Home Runs ─────────────────────────────────────────────────────────────
# Power-first: Barrel%, SLG, ISO dominate. Park + wind are the biggest
# situational adjusters for HR specifically. Class imbalance (~15% hit rate)
# requires scale_pos_weight tuning.
HR_FEATURES = [
    # Power metrics
    "sv_brl_pct",       # barrel rate — #1 HR predictor
    "sv_ev",            # exit velocity
    "sv_hh_pct",        # hard-hit rate
    "sv_xslg",          # expected SLG (proxy for power)
    "sv_xwoba",
    "sv_la",            # launch angle matters more for HR than for hits
    # Plate discipline (affects AB count and fly-ball opportunities)
    "sv_k_pct",
    "sv_bb_pct",
    # Opponent pitcher
    "opp_xera",
    "opp_k_pct",
    "opp_whiff",
    # Handedness
    "bats_L",
    "throws_R",
    "platoon_adv",
    # Recent HR form
    "l7_hr",            # HR per game, last 7
    "l14_hr",
    "l7_hr_rate",       # HR per AB last 7
    # Lineup context
    "expected_pa",
    "batting_order",
    "lineup_confirmed",
    # Park + weather (critical for HR)
    "park_factor",
    "park_hr_factor",   # park-specific HR factor (separate from overall park factor)
    "wx_temp_mult",
    "wx_wind_mult",
    "wind_direction_factor",  # blowout vs. headwind toward CF
    "temperature",      # ball carries farther in warm air
]

# ── Total Bases ────────────────────────────────────────────────────────────
# TB = H + XBH bonus. Needs both contact quality (xBA) AND power (Barrel%)
# plus park/wind. More balanced feature set than pure hits or pure HR.
TB_FEATURES = [
    # Contact quality
    "sv_xba",
    "sv_xwoba",
    "sv_ev",
    "sv_hh_pct",
    "sv_ss_pct",
    "sv_la",
    # Power (XBH component)
    "sv_brl_pct",
    "sv_xslg",
    # Plate discipline
    "sv_k_pct",
    "sv_bb_pct",
    # Opponent
    "opp_xera",
    "opp_k_pct",
    "opp_whiff",
    # Handedness
    "bats_L",
    "throws_R",
    "platoon_adv",
    # Recent form (blended: hits + TB)
    "l7_hits",
    "l7_tb",            # total bases per game, last 7
    "l14_tb",
    "l7_tb_rate",       # TB/AB last 7
    # Lineup context
    "expected_pa",
    "batting_order",
    "lineup_confirmed",
    # Park + weather
    "park_factor",
    "park_hr_factor",
    "wx_temp_mult",
    "wx_wind_mult",
    "wind_direction_factor",
    "temperature",
]

# ── RBI ───────────────────────────────────────────────────────────────────
# RBI is heavily context-dependent: lineup position, team run environment,
# opponent ERA. Power metrics matter but lineup slot dominates.
RBI_FEATURES = [
    # Power + contact (drives RBI production)
    "sv_xwoba",
    "sv_xslg",
    "sv_brl_pct",
    "sv_ev",
    "sv_k_pct",
    "sv_bb_pct",
    # Opponent
    "opp_xera",
    "opp_k_pct",
    "opp_bb_pct",
    # Handedness
    "bats_L",
    "throws_R",
    "platoon_adv",
    # Lineup context — most predictive features for RBI
    "expected_pa",
    "batting_order",    # 3-5 hitters get more RBI opportunities
    "lineup_confirmed",
    # Team run environment proxy
    "team_obp_last14",  # team OBP last 14 days (runners on base)
    "team_wrc_plus",    # team wRC+
    # Recent RBI form
    "l7_rbi",
    "l14_rbi",
    # Park
    "park_factor",
    "wx_temp_mult",
]

# ── Strikeouts (all three lines share features; only hyperparams differ) ─────
# K3.5: durability (IP, days_rest) weighs more — starter must pitch deep enough
# K4.5: balanced K rate + lineup quality
# K5.5: rare event — needs class-weight correction; fewer noisy features
K_FEATURES_BASE = [
    # Pitcher K ability
    "sv_xera",
    "sv_era",
    "sv_k_pct",         # season K rate
    "sv_bb_pct",
    "sv_whiff_pct",     # whiff rate = most predictive single K feature
    # Pitcher rolling form
    "l3_ks",            # avg Ks last 3 starts
    "l5_ks",
    "l5_k_rate",        # K/BF last 5
    "l10_ks",
    "l3_ip",            # IP last 3 starts (durability)
    "l5_ip",
    "days_rest",        # fatigue proxy
    # Opponent lineup
    "opp_lineup_k_pct", # confirmed lineup avg K%
    "opp_lineup_xwoba", # confirmed lineup avg xwOBA
    # Pitch mix / arsenal swing-and-miss (usage-weighted across arsenal)
    "arsenal_whiff_pct",   # usage-weighted whiff% — leading K indicator
    "arsenal_putaway_pct", # usage-weighted 2-strike putaway rate
    # Umpire features (from umpire_loader)
    "ump_zone_size",
    "ump_k_boost",
    # Weather (affects grip, movement, stamina)
    "temperature",
    "wind_speed",
]

# K5.5 drops some noisy rolling features to reduce overfitting on a rare event
K55_FEATURES = [
    "sv_xera", "sv_k_pct", "sv_whiff_pct",
    "l5_ks", "l5_k_rate", "l10_ks",
    "l3_ip", "l5_ip", "days_rest",
    "opp_lineup_k_pct", "opp_lineup_xwoba",
    "arsenal_whiff_pct", "arsenal_putaway_pct",
    "ump_zone_size", "ump_k_boost",
    "temperature",
]

# ── Master model config ────────────────────────────═──────────────────────────
# Each entry: (model_file_key, features_list, target_col, xgb_params, calibration_method)
# scale_pos_weight = (n_negative / n_positive) tuned per market at train time.
MODEL_CONFIGS: dict[str, dict] = {
    "hits": {
        "file_key":   "xgb_hits_over_0.5",
        "features":   HITS_FEATURES,
        "target":     "hit_over_0.5",
        "source":     "batter",
        "line":       0.5,
        "xgb_params": {
            "n_estimators":     350,
            "max_depth":        4,
            "learning_rate":    0.04,
            "subsample":        0.80,
            "colsample_bytree": 0.80,
            "min_child_weight": 2,
            "gamma":            0.05,
            # scale_pos_weight set dynamically at train time
        },
        "calibration": "isotonic",
        "cv_folds":    5,
    },
    "hr": {
        "file_key":   "xgb_hr_over_0.5",
        "features":   HR_FEATURES,
        "target":     "hr_over_0.5",
        "source":     "batter",
        "line":       0.5,
        "xgb_params": {
            # Rare event (~15% positive rate): more trees, lower LR,
            # scale_pos_weight handles class imbalance dynamically.
            "n_estimators":     500,
            "max_depth":        4,
            "learning_rate":    0.03,
            "subsample":        0.75,
            "colsample_bytree": 0.70,
            "min_child_weight": 5,
            "gamma":            0.10,
        },
        "calibration": "isotonic",
        "cv_folds":    5,
    },
    "tb": {
        "file_key":   "xgb_tb_over_1.5",
        "features":   TB_FEATURES,
        "target":     "tb_over_1.5",
        "source":     "batter",
        "line":       1.5,
        "xgb_params": {
            "n_estimators":     400,
            "max_depth":        5,
            "learning_rate":    0.035,
            "subsample":        0.78,
            "colsample_bytree": 0.75,
            "min_child_weight": 3,
            "gamma":            0.05,
        },
        "calibration": "isotonic",
        "cv_folds":    5,
    },
    "rbi": {
        "file_key":   "xgb_rbi_over_0.5",
        "features":   RBI_FEATURES,
        "target":     "rbi_over_0.5",
        "source":     "batter",
        "line":       0.5,
        "xgb_params": {
            "n_estimators":     300,
            "max_depth":        4,
            "learning_rate":    0.04,
            "subsample":        0.80,
            "colsample_bytree": 0.80,
            "min_child_weight": 3,
            "gamma":            0.05,
        },
        "calibration": "isotonic",
        "cv_folds":    5,
    },
    "k_3.5": {
        "file_key":   "xgb_k_over_3.5",
        "features":   K_FEATURES_BASE,
        "target":     "k_over_3.5",
        "source":     "pitcher",
        "line":       3.5,
        "xgb_params": {
            # K>3.5 has highest positive rate (~70%) so class weighting not needed;
            # focus on durability signal (l3_ip) with moderate depth.
            "n_estimators":     300,
            "max_depth":        4,
            "learning_rate":    0.05,
            "subsample":        0.80,
            "colsample_bytree": 0.80,
            "min_child_weight": 3,
            "gamma":            0.05,
        },
        "calibration": "sigmoid",
        "cv_folds":    5,
    },
    "k_4.5": {
        "file_key":   "xgb_k_over_4.5",
        "features":   K_FEATURES_BASE,
        "target":     "k_over_4.5",
        "source":     "pitcher",
        "line":       4.5,
        "xgb_params": {
            # Balanced line (~50% hit rate): standard deep tuning.
            "n_estimators":     450,
            "max_depth":        5,
            "learning_rate":    0.035,
            "subsample":        0.75,
            "colsample_bytree": 0.75,
            "min_child_weight": 3,
            "gamma":            0.08,
        },
        "calibration": "isotonic",
        "cv_folds":    5,
    },
    "k_5.5": {
        "file_key":   "xgb_k_over_5.5",
        "features":   K55_FEATURES,    # trimmed feature set for rare event
        "target":     "k_over_5.5",
        "source":     "pitcher",
        "line":       5.5,
        "xgb_params": {
            # Rare event (~30% hit rate): class-weight + conservative depth.
            "n_estimators":     400,
            "max_depth":        4,
            "learning_rate":    0.025,
            "subsample":        0.70,
            "colsample_bytree": 0.65,
            "min_child_weight": 8,
            "gamma":            0.15,
            # scale_pos_weight set dynamically
        },
        "calibration": "isotonic",
        "cv_folds":    5,
    },
}


# ═════════════════════════════════════════════════════════════════════
# DATA LOADING
# ═════════════════════════════════════════════════════════════════════

def _load_pybaseball():
    try:
        import pybaseball as pb
        pb.cache.enable()
        return pb
    except ImportError:
        raise RuntimeError("pybaseball not installed. Run: pip install pybaseball")


def _safe_concat(frames: list) -> pd.DataFrame:
    valid = [f for f in frames if f is not None and len(f) > 0]
    return pd.concat(valid, ignore_index=True) if valid else pd.DataFrame()


def _emit(progress_cb: Optional[Callable], phase: str, log_line: Optional[str] = None) -> None:
    """Report a phase/log line to the optional progress callback (and stdout)."""
    if log_line:
        print(log_line)
    elif phase:
        print(phase)
    if progress_cb:
        try:
            progress_cb(phase, log_line)
        except Exception:
            pass


def _statcast_windows(season: int, days: int = 16):
    """Yield (start, end) date-string windows spanning a season, so Statcast is
    pulled in memory-bounded chunks instead of one multi-GB season-wide call."""
    start = datetime(season, 3, 20)
    final = datetime(season, 10, 5)
    cur = start
    while cur <= final:
        wend = min(cur + timedelta(days=days - 1), final)
        yield cur.strftime("%Y-%m-%d"), wend.strftime("%Y-%m-%d")
        cur = wend + timedelta(days=1)


def fetch_all_seasons(seasons: list[int],
                      progress_cb: Optional[Callable] = None) -> dict[str, pd.DataFrame]:
    """
    Fetch Statcast + FanGraphs data for all training seasons.
    Returns a dict with keys: sv_bat, sv_pit, fg_bat, fg_pit.
    """
    pb = _load_pybaseball()
    _emit(progress_cb, "Fetching Statcast + FanGraphs season stats…",
          f"══ Fetching season stats: {seasons} ══")

    def _try(fn, *args, label="", **kwargs):
        try:
            df = fn(*args, **kwargs)
            df["season"] = args[-1] if args else 0
            return df
        except Exception as e:
            print(f"  ⚠️  {label} failed: {e}")
            return pd.DataFrame()

    sv_bat = _safe_concat([
        _try(pb.statcast_batter_expected_stats, s, label=f"sv_bat {s}") for s in seasons
    ])
    sv_pit = _safe_concat([
        _try(pb.statcast_pitcher_expected_stats, s, label=f"sv_pit {s}") for s in seasons
    ])
    fg_bat = _safe_concat([
        _try(pb.batting_stats, s, qual=PA_MIN, label=f"fg_bat {s}") for s in seasons
    ])
    fg_pit = _safe_concat([
        _try(pb.pitching_stats, s, qual=IP_MIN, label=f"fg_pit {s}") for s in seasons
    ])

    print(f"  sv_bat: {len(sv_bat):,} rows  sv_pit: {len(sv_pit):,} rows")
    print(f"  fg_bat: {len(fg_bat):,} rows  fg_pit: {len(fg_pit):,} rows")
    return {"sv_bat": sv_bat, "sv_pit": sv_pit, "fg_bat": fg_bat, "fg_pit": fg_pit}


def _fetch_csv_savant(url: str) -> pd.DataFrame:
    """Fetch a Baseball Savant leaderboard CSV (UA header required)."""
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (compatible; MLBAnalyticsHub-training/1.0)"}
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = resp.read().decode("utf-8", "replace")
    return pd.read_csv(io.StringIO(data))


def fetch_arsenal_stats(seasons: list[int],
                        progress_cb: Optional[Callable] = None) -> pd.DataFrame:
    """Per-pitcher, per-season usage-weighted arsenal swing-and-miss features.

    Pulls the same Savant pitch-arsenal-stats leaderboard the live app uses, so
    the trained columns (arsenal_whiff_pct / arsenal_putaway_pct) are produced
    from an identical source to xgb_prop_scorer's inference inputs.

    Returns columns: player_id, season, arsenal_whiff_pct, arsenal_putaway_pct.
    """
    BASE = "https://baseballsavant.mlb.com"
    frames = []
    for s in seasons:
        url = (f"{BASE}/leaderboard/pitch-arsenal-stats?type=pitcher&pitchType="
               f"&year={s}&team=&min=25&csv=true")
        _emit(progress_cb, f"Fetching pitch-arsenal stats {s}…", f"  arsenal stats {s}…")
        try:
            df = _fetch_csv_savant(url)
        except Exception as e:
            _emit(progress_cb, None, f"  ⚠️  arsenal stats {s} failed: {e}")
            continue
        if df is None or len(df) == 0 or "player_id" not in df.columns:
            continue
        for src in ("pitch_usage", "whiff_percent", "k_percent"):
            if src not in df.columns:
                df[src] = np.nan
            df[src] = pd.to_numeric(df[src], errors="coerce")
        df["pitch_usage"] = df["pitch_usage"].fillna(0.0).clip(lower=0.0)

        def _agg(x: pd.DataFrame) -> pd.Series:
            w = x["pitch_usage"]
            tot = float(w.sum())
            if tot <= 0:
                return pd.Series({
                    "arsenal_whiff_pct":   x["whiff_percent"].mean(),
                    "arsenal_putaway_pct": x["k_percent"].mean(),
                })
            return pd.Series({
                "arsenal_whiff_pct":   float((x["whiff_percent"].fillna(0) * w).sum() / tot),
                "arsenal_putaway_pct": float((x["k_percent"].fillna(0)     * w).sum() / tot),
            })

        g = df.groupby("player_id").apply(_agg).reset_index()
        g["season"] = s
        frames.append(g)

    out = _safe_concat(frames)
    _emit(progress_cb, None, f"  arsenal stats: {len(out):,} pitcher-seasons")
    return out


def _aggregate_statcast_chunk(sc: pd.DataFrame, season: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Compute event flags and aggregate one raw-Statcast chunk to per-game
    batter and pitcher outcome rows. Kept separate so callers can free the raw
    (multi-100k-row) chunk immediately after aggregating it. A game never spans
    two date windows, so per-window aggregation is exact."""
    sc = sc.dropna(subset=["batter", "pitcher", "game_pk"])
    if len(sc) == 0:
        return pd.DataFrame(), pd.DataFrame()

    # ─ Event flags ────────────────────────────────────────────────────────────
    sc["is_hit"]    = sc["events"].isin(["single","double","triple","home_run"]).astype(int)
    sc["is_ab"]     = sc["events"].isin([
        "single","double","triple","home_run","strikeout","field_out",
        "grounded_into_double_play","double_play","force_out",
        "fielders_choice","fielders_choice_out","strikeout_double_play",
    ]).astype(int)
    sc["is_pa"]     = (~sc["events"].isna()).astype(int)
    sc["is_k"]      = sc["events"].isin(["strikeout","strikeout_double_play"]).astype(int)
    sc["is_hr"]     = (sc["events"] == "home_run").astype(int)
    sc["is_double"] = (sc["events"] == "double").astype(int)
    sc["is_triple"] = (sc["events"] == "triple").astype(int)
    sc["tb"]        = sc["is_hit"] + sc["is_hr"] + sc["is_double"] + sc["is_triple"]

    # ─ Batter game aggregation ───────────────────────────────────────────────
    bat_agg = (
        sc[sc["is_pa"] == 1]
        .groupby(["game_pk", "game_date", "batter"])
        .agg(
            hits=("is_hit",  "sum"),
            ab=("is_ab",   "sum"),
            pa=("is_pa",   "sum"),
            hr=("is_hr",   "sum"),
            tb=("tb",      "sum"),
            home_team=("home_team", "first"),
            away_team=("away_team", "first"),
            p_throws=("p_throws",   "first"),
            stand=("stand",         "first"),
        )
        .reset_index()
    )
    # RBI requires retrosheet/baseball-reference; approximated from events
    if "post_bat_score" in sc.columns and "bat_score" in sc.columns:
        sc["rbi_est"] = (sc["post_bat_score"] - sc["bat_score"]).clip(0, 4)
        rbi_agg = (
            sc[sc["is_ab"] == 1]
            .groupby(["game_pk","game_date","batter"])
            .agg(rbi=("rbi_est","sum"))
            .reset_index()
        )
        bat_agg = bat_agg.merge(rbi_agg, on=["game_pk","game_date","batter"], how="left")
        bat_agg["rbi"] = bat_agg["rbi"].fillna(0)
    else:
        bat_agg["rbi"] = 0

    # Binary targets
    bat_agg["hit_over_0.5"] = (bat_agg["hits"] >= 1).astype(int)
    bat_agg["hr_over_0.5"]  = (bat_agg["hr"]   >= 1).astype(int)
    bat_agg["tb_over_1.5"]  = (bat_agg["tb"]   >= 2).astype(int)
    bat_agg["rbi_over_0.5"] = (bat_agg["rbi"]  >= 1).astype(int)
    bat_agg["season"]       = season

    # ─ Pitcher game aggregation ───────────────────────────────────────────────
    pit_agg = (
        sc[sc["is_pa"] == 1]
        .groupby(["game_pk","game_date","pitcher"])
        .agg(
            ks=("is_k", "sum"),
            bf=("is_pa","sum"),
            home_team=("home_team","first"),
            away_team=("away_team","first"),
        )
        .reset_index()
    )
    pit_agg["k_over_3.5"] = (pit_agg["ks"] >= 4).astype(int)
    pit_agg["k_over_4.5"] = (pit_agg["ks"] >= 5).astype(int)
    pit_agg["k_over_5.5"] = (pit_agg["ks"] >= 6).astype(int)
    pit_agg["season"]      = season

    return bat_agg, pit_agg


def fetch_game_logs(seasons: list[int],
                    progress_cb: Optional[Callable] = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Pull raw Statcast and aggregate to per-game batter + pitcher outcome rows.

    Statcast is fetched in ~2-week windows and aggregated immediately, so peak
    memory is bounded to a single window (~tens of thousands of rows) rather
    than a whole season (~700k+ rows × 90 columns) — which previously risked
    OOM on the 4GB box and gave no progress feedback for many minutes."""
    pb = _load_pybaseball()
    batter_frames, pitcher_frames = [], []

    windows = [(s, ws, we) for s in seasons for (ws, we) in _statcast_windows(s)]
    total_w = len(windows) or 1

    for i, (season, start, end) in enumerate(windows, 1):
        _emit(progress_cb,
              f"Fetching Statcast {start} → {end}  ({i}/{total_w})",
              f"  [{i}/{total_w}] Statcast {start} → {end}…")
        try:
            sc = pb.statcast(start_dt=start, end_dt=end)
        except Exception as e:
            _emit(progress_cb, None, f"  ⚠️  Statcast {start}→{end} failed: {e}")
            continue
        if sc is None or len(sc) == 0:
            continue
        try:
            bat_agg, pit_agg = _aggregate_statcast_chunk(sc, season)
        finally:
            del sc
            gc.collect()
        if len(bat_agg):
            batter_frames.append(bat_agg)
        if len(pit_agg):
            pitcher_frames.append(pit_agg)

    bg_all = _safe_concat(batter_frames)
    pg_all = _safe_concat(pitcher_frames)
    _emit(progress_cb, "Statcast game logs assembled",
          f"  Batter game rows: {len(bg_all):,}  Pitcher game rows: {len(pg_all):,}")
    return bg_all, pg_all


# ═════════════════════════════════════════════════════════════════════
# FEATURE MATRIX BUILDERS
# ═════════════════════════════════════════════════════════════════════

def _add_rolling_batter(bg: pd.DataFrame) -> pd.DataFrame:
    """Add rolling game-level features for batter markets."""
    bg = bg.sort_values(["batter", "game_date"]).copy()
    for col, alias in [("hits","hits"),("hr","hr"),("tb","tb"),("rbi","rbi")]:
        if col not in bg.columns:
            bg[col] = 0
        bg[f"l7_{alias}"]  = bg.groupby("batter")[col].transform(
            lambda x: x.shift(1).rolling(7,  min_periods=1).mean()
        )
        bg[f"l14_{alias}"] = bg.groupby("batter")[col].transform(
            lambda x: x.shift(1).rolling(14, min_periods=1).mean()
        )

    # Rate features
    bg["ab_safe"] = bg["ab"].clip(lower=1)
    bg["l7_hit_rate"] = bg.groupby("batter").apply(
        lambda x: (x["hits"] / x["ab_safe"]).shift(1).rolling(7, min_periods=1).mean()
    ).reset_index(level=0, drop=True)
    bg["l7_hr_rate"]  = bg.groupby("batter").apply(
        lambda x: (x["hr"]   / x["ab_safe"]).shift(1).rolling(7, min_periods=1).mean()
    ).reset_index(level=0, drop=True)
    bg["l7_tb_rate"]  = bg.groupby("batter").apply(
        lambda x: (x["tb"]   / x["ab_safe"]).shift(1).rolling(7, min_periods=1).mean()
    ).reset_index(level=0, drop=True)
    return bg


def _add_rolling_pitcher(pg: pd.DataFrame) -> pd.DataFrame:
    """Add rolling game-level features for K markets."""
    pg = pg.sort_values(["pitcher", "game_date"]).copy()
    if "bf" not in pg.columns:
        pg["bf"] = 18
    pg["ip"] = np.clip(pg["bf"] / 4.2, 0, 9)

    pg["prev_date"] = pg.groupby("pitcher")["game_date"].shift(1)
    pg["days_rest"] = (
        pd.to_datetime(pg["game_date"]) - pd.to_datetime(pg["prev_date"])
    ).dt.days.fillna(5).clip(0, 14)

    for w, col in [(3,"l3_ks"),(5,"l5_ks"),(10,"l10_ks")]:
        pg[col] = pg.groupby("pitcher")["ks"].transform(
            lambda x, w=w: x.shift(1).rolling(w, min_periods=1).mean()
        )
    for w, col in [(3,"l3_ip"),(5,"l5_ip")]:
        pg[col] = pg.groupby("pitcher")["ip"].transform(
            lambda x, w=w: x.shift(1).rolling(w, min_periods=1).mean()
        )
    bf_safe = pg["bf"].clip(lower=1)
    pg["l5_k_rate"] = pg.groupby("pitcher").apply(
        lambda x: (x["ks"] / x["bf"].clip(lower=1)).shift(1).rolling(5, min_periods=1).mean()
    ).reset_index(level=0, drop=True)
    return pg


def build_batter_matrix(
    bg: pd.DataFrame,
    sv_bat: pd.DataFrame,
    fg_bat: pd.DataFrame,
    sv_pit: pd.DataFrame,
    season: int,
) -> pd.DataFrame:
    """Merge per-game batter outcomes with Statcast + FanGraphs season stats."""
    bg = _add_rolling_batter(bg[bg["season"] == season].copy())

    # Batter Statcast
    svb = sv_bat[sv_bat["season"] == season].copy()
    if "player_id" in svb.columns:
        svb["player_id"] = svb["player_id"].astype(int)
    col_renames = {
        "est_ba": "sv_xba", "est_woba": "sv_xwoba", "est_slg": "sv_xslg",
        "k_percent": "sv_k_pct", "bb_percent": "sv_bb_pct",
        "avg_hit_speed": "sv_ev", "brl_percent": "sv_brl_pct",
        "ev95percent": "sv_hh_pct", "anglesweetspotpercent": "sv_ss_pct",
        "avg_hit_angle": "sv_la",
    }
    svb = svb.rename(columns=col_renames)
    sv_cols = [c for c in ["player_id"] + list(col_renames.values()) if c in svb.columns]
    svb = svb[sv_cols]

    bg = bg.merge(svb, left_on="batter", right_on="player_id", how="left")

    # Opponent Statcast pitcher
    svp = sv_pit[sv_pit["season"] == season].copy()
    if "player_id" in svp.columns:
        svp["player_id"] = svp["player_id"].astype(int)
    svp = svp.rename(columns={
        "xera": "sv_xera", "k_percent": "sv_k_pct_p",
        "est_woba": "sv_xwoba_p", "whiff_percent": "sv_whiff_p",
    })
    pit_cols = [c for c in ["player_id","sv_xera","sv_k_pct_p","sv_xwoba_p","sv_whiff_p"]
                if c in svp.columns]
    svp = svp[pit_cols].rename(columns={
        "sv_xera": "opp_xera", "sv_k_pct_p": "opp_k_pct",
        "sv_xwoba_p": "opp_xwoba", "sv_whiff_p": "opp_whiff",
    })

    # Fill gap columns with league-average defaults for missing features
    for col, default in [
        ("bats_L", 0), ("throws_R", 1), ("platoon_adv", 0),
        ("expected_pa", 4.2), ("batting_order", 5.0), ("lineup_confirmed", 0),
        ("park_factor", 1.0), ("park_hr_factor", 1.0),
        ("wx_temp_mult", 1.0), ("wx_wind_mult", 1.0),
        ("wind_direction_factor", 0.0), ("temperature", 72.0),
        ("pitch_mix_slg_edge", 0.0), ("bvp_woba_edge_shrunk", 0.0),
        ("split_ops_edge", 0.0),
        ("team_obp_last14", 0.320), ("team_wrc_plus", 100.0),
        ("l7_hr", 0.0), ("l14_hr", 0.0), ("l7_hr_rate", 0.0),
        ("l7_tb", 1.0), ("l14_tb", 1.5), ("l7_tb_rate", 0.3),
        ("l7_rbi", 0.4), ("l14_rbi", 0.4),
    ]:
        if col not in bg.columns:
            bg[col] = default

    # Handedness encoding
    if "stand" in bg.columns:
        bg["bats_L"] = (bg["stand"] == "L").astype(int)
    if "p_throws" in bg.columns:
        bg["throws_R"] = (bg["p_throws"] == "R").astype(int)
        bg["platoon_adv"] = (
            ((bg["stand"] == "L") & (bg["p_throws"] == "R")) |
            ((bg["stand"] == "R") & (bg["p_throws"] == "L"))
        ).astype(int)

    # Pct normalisation (convert 0-1 to 0-100 where needed)
    for col in ("sv_k_pct", "sv_bb_pct", "opp_k_pct", "opp_bb_pct"):
        if col in bg.columns:
            mask = bg[col].between(0.0, 1.0, inclusive="right")
            bg.loc[mask, col] = bg.loc[mask, col] * 100

    return bg


def build_pitcher_matrix(
    pg: pd.DataFrame,
    sv_pit: pd.DataFrame,
    season: int,
    arsenal_df: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """Merge per-game pitcher outcomes with Statcast season + arsenal stats."""
    pg = _add_rolling_pitcher(pg[pg["season"] == season].copy())
    pg["pitcher"] = pd.to_numeric(pg["pitcher"], errors="coerce").astype("Int64")

    svp = sv_pit[sv_pit["season"] == season].copy()
    if "player_id" in svp.columns:
        svp["player_id"] = svp["player_id"].astype(int)
    svp = svp.rename(columns={
        "xera": "sv_xera", "era": "sv_era",
        "k_percent": "sv_k_pct", "bb_percent": "sv_bb_pct",
        "whiff_percent": "sv_whiff_pct",
    })
    pit_cols = [c for c in
                ["player_id","sv_xera","sv_era","sv_k_pct","sv_bb_pct","sv_whiff_pct"]
                if c in svp.columns]
    pg = pg.merge(svp[pit_cols], left_on="pitcher", right_on="player_id", how="left")

    # Arsenal pitch-mix features (usage-weighted whiff% / putaway%)
    if arsenal_df is not None and len(arsenal_df) and "season" in arsenal_df.columns:
        ad = arsenal_df[arsenal_df["season"] == season].copy()
        if "player_id" in ad.columns and len(ad):
            ad["pitcher"] = pd.to_numeric(ad["player_id"], errors="coerce").astype("Int64")
            pg = pg.merge(
                ad[["pitcher", "arsenal_whiff_pct", "arsenal_putaway_pct"]],
                on="pitcher", how="left",
            )
    # Fill unmatched arsenal rows with league-average constants.
    if "arsenal_whiff_pct" in pg.columns:
        pg["arsenal_whiff_pct"] = pg["arsenal_whiff_pct"].fillna(24.5)
    if "arsenal_putaway_pct" in pg.columns:
        pg["arsenal_putaway_pct"] = pg["arsenal_putaway_pct"].fillna(18.0)

    for col, default in [
        ("opp_lineup_k_pct",  22.0),
        ("opp_lineup_xwoba",   0.320),
        ("arsenal_whiff_pct",  24.5),
        ("arsenal_putaway_pct",18.0),
        ("ump_zone_size",       0.0),
        ("ump_k_boost",         0.0),
        ("temperature",        72.0),
        ("wind_speed",          8.0),
    ]:
        if col not in pg.columns:
            pg[col] = default

    # Starter filter: keep only outings with ≥4 IP (starters only)
    if "ip" in pg.columns:
        pg = pg[pg["ip"] >= 4.0].copy()

    for col in ("sv_k_pct", "sv_bb_pct", "opp_lineup_k_pct"):
        if col in pg.columns:
            mask = pg[col].between(0.0, 1.0, inclusive="right")
            pg.loc[mask, col] = pg.loc[mask, col] * 100

    return pg


# ═════════════════════════════════════════════════════════════════════
# MODEL TRAINING
# ═════════════════════════════════════════════════════════════════════

def _prep_matrix(
    df: pd.DataFrame,
    feat_cols: list[str],
    target: str,
    min_rows: int = 200,
) -> Optional[tuple[np.ndarray, np.ndarray, list[str]]]:
    """
    Select and validate the feature matrix for one model.
    Returns (X, y, used_feat_cols) or None if insufficient data.
    """
    available = [c for c in feat_cols if c in df.columns]
    missing   = [c for c in feat_cols if c not in df.columns]
    if missing:
        print(f"  ⚠️  Missing features (will be zero-filled): {missing}")
        for c in missing:
            df[c] = 0.0
        available = feat_cols

    keep = available + [target]
    sub  = df[keep].dropna(subset=[target]).copy()
    sub[available] = sub[available].fillna(sub[available].median())

    if len(sub) < min_rows:
        print(f"  ✗  Insufficient rows: {len(sub)} < {min_rows} required")
        return None

    X = sub[available].values.astype(np.float32)
    y = sub[target].values.astype(int)
    return X, y, available


def train_one_market(
    market_key: str,
    df: pd.DataFrame,
    config: dict,
    seasons: list[int],
) -> Optional[dict]:
    """
    Train, calibrate, and evaluate one prop-type XGBoost model.
    Returns a result dict or None on failure.
    """
    feat_cols = config["features"]
    target    = config["target"]
    params    = dict(config["xgb_params"])
    cal_meth  = config["calibration"]
    cv_folds  = config["cv_folds"]
    line      = config["line"]
    file_key  = config["file_key"]

    label = f"{market_key} ({target})"
    print(f"\n── {label} ──")

    prep = _prep_matrix(df, feat_cols, target)
    if prep is None:
        return None
    X, y, used_cols = prep

    n_pos = int(y.sum())
    n_neg = int(len(y) - n_pos)
    pos_rate = n_pos / max(len(y), 1)
    print(f"  Rows: {len(y):,}  Positive rate: {pos_rate:.1%}  ({n_pos:,} pos / {n_neg:,} neg)")

    # Dynamic class-weight for imbalanced markets (HR, K>5.5)
    if pos_rate < 0.35 and "scale_pos_weight" not in params:
        spw = round(n_neg / max(n_pos, 1), 2)
        params["scale_pos_weight"] = spw
        print(f"  scale_pos_weight = {spw:.2f} (auto-set for imbalanced target)")

    # ─ Cross-validation ──────────────────────────────────────────────────────────
    base_model = xgb.XGBClassifier(
        **params,
        use_label_encoder=False,
        eval_metric="logloss",
        random_state=SEED,
        n_jobs=-1,
    )
    skf = StratifiedKFold(n_splits=cv_folds, shuffle=True, random_state=SEED)
    cv_auc = cross_val_score(base_model, X, y, cv=skf, scoring="roc_auc", n_jobs=-1)
    print(f"  CV AUC: {cv_auc.mean():.4f} ± {cv_auc.std():.4f}  (n_splits={cv_folds})")

    # ─ Full-data fit + calibration ─────────────────────────────────────────────────
    base_model.fit(X, y)
    calib = CalibratedClassifierCV(base_model, cv=3, method=cal_meth)
    calib.fit(X, y)

    # ─ Evaluation ─────────────────────────═─────────────────────────────────────
    probs  = calib.predict_proba(X)[:, 1]
    preds  = (probs >= 0.5).astype(int)
    auc    = roc_auc_score(y, probs)
    brier  = brier_score_loss(y, probs)
    ll     = log_loss(y, probs)
    print(f"  AUC: {auc:.4f}  Brier: {brier:.4f}  LogLoss: {ll:.4f}")

    # Feature importances summary (top 10)
    try:
        fi = base_model.feature_importances_
        top = sorted(zip(used_cols, fi), key=lambda x: -x[1])[:10]
        print(f"  Top features:")
        for fname, fval in top:
            print(f"    {fname:<35} {fval:.4f}")
    except Exception:
        pass

    # ─ Save model artifact ────────────────────────────────────────────────────────
    out_path = os.path.join(_MODEL_DIR, f"{file_key}.pkl")
    artifact = {
        "model":    calib,
        "features": used_cols,
        "meta": {
            "market":      market_key,
            "target":      target,
            "line":        line,
            "cv_auc_mean": round(float(cv_auc.mean()), 4),
            "cv_auc_std":  round(float(cv_auc.std()),  4),
            "auc":         round(auc,   4),
            "brier":       round(brier, 4),
            "log_loss":    round(ll,    4),
            "n_rows":      len(y),
            "pos_rate":    round(pos_rate, 4),
            "seasons":     seasons,
            "trained_at":  datetime.utcnow().isoformat(),
            "xgb_params":  params,
            "calibration": cal_meth,
        },
    }
    joblib.dump(artifact, out_path)
    print(f"  ✓  Saved → {out_path}")

    return {
        "market_key": market_key,
        "file_key":   file_key,
        "features":   used_cols,
        "meta":       artifact["meta"],
    }


# ═════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ═════════════════════════════════════════════════════════════════════

def train_all(
    markets:     Optional[list[str]] = None,
    seasons:     Optional[list[int]] = None,
    progress_cb: Optional[Callable]  = None,
) -> dict:
    """
    Train all (or a subset of) prop-type models.

    Parameters
    ----------
    markets : list of market keys to train, e.g. ["hits", "k_4.5"].
              Defaults to all markets in MODEL_CONFIGS.
    seasons : list of training seasons. Defaults to DEFAULT_SEASONS.

    Returns
    -------
    dict: {market_key: result_dict or None}
    """
    seasons  = seasons or DEFAULT_SEASONS
    markets  = markets or list(MODEL_CONFIGS.keys())
    markets  = [m for m in markets if m in MODEL_CONFIGS]

    if not markets:
        raise ValueError(f"No valid markets specified. Options: {list(MODEL_CONFIGS.keys())}")

    print(f"═══════════════════════════════════════════════════════")
    print(f" Prop-Type XGB Training — {datetime.utcnow():%Y-%m-%d %H:%M} UTC")
    print(f" Markets : {markets}")
    print(f" Seasons : {seasons}")
    print(f"═══════════════════════════════════════════════════════")

    # Fetch all data
    season_data = fetch_all_seasons(seasons, progress_cb=progress_cb)
    arsenal_df  = fetch_arsenal_stats(seasons, progress_cb=progress_cb)
    _emit(progress_cb, "Fetching Statcast game logs…", "══ Fetching game logs ══")
    bg_all, pg_all = fetch_game_logs(seasons, progress_cb=progress_cb)

    # Build feature matrices once per source type
    _emit(progress_cb, "Building feature matrices…")
    bat_matrix = _safe_concat([
        build_batter_matrix(bg_all, season_data["sv_bat"], season_data["fg_bat"],
                            season_data["sv_pit"], s)
        for s in seasons
    ])
    pit_matrix = _safe_concat([
        build_pitcher_matrix(pg_all, season_data["sv_pit"], s, arsenal_df=arsenal_df)
        for s in seasons
    ])
    print(f"  Batter matrix: {bat_matrix.shape}  Pitcher matrix: {pit_matrix.shape}")

    results = {}
    feat_cols_map = {}

    for mi, mkey in enumerate(markets, 1):
        config = MODEL_CONFIGS[mkey]
        source = config["source"]
        df     = bat_matrix if source == "batter" else pit_matrix
        _emit(progress_cb, f"Training {mkey}  ({mi}/{len(markets)})")
        result = train_one_market(mkey, df.copy(), config, seasons)
        results[mkey] = result
        if result:
            feat_cols_map[mkey] = result["features"]
            # Also register under the file_key for legacy scorer lookup
            feat_cols_map[result["file_key"].replace("xgb_","")] = result["features"]

    # Save feature-column map (used by xgb_prop_scorer._load_models)
    feat_file = os.path.join(_MODEL_DIR, "xgb_feature_cols.json")
    with open(feat_file, "w") as f:
        json.dump(feat_cols_map, f, indent=2)
    print(f"\n✓  xgb_feature_cols.json saved → {feat_file}")

    # Save full training summary
    summary = {
        k: (v["meta"] if v else None)
        for k, v in results.items()
    }
    summary_path = os.path.join(_OUTPUT_DIR, "train_summary.json")
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"✓  Training summary → {summary_path}")

    print("\n══ Training Complete ══")
    for mkey, res in results.items():
        if res:
            m = res["meta"]
            print(f"  {mkey:<12} AUC={m['auc']:.4f}  Brier={m['brier']:.4f}  rows={m['n_rows']:,}")
        else:
            print(f"  {mkey:<12} ✗ FAILED")

    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Train prop-type-specific XGBoost models")
    parser.add_argument(
        "--market", nargs="+", default=None,
        help=f"Market(s) to train. Options: {list(MODEL_CONFIGS.keys())}",
    )
    parser.add_argument(
        "--seasons", nargs="+", type=int, default=None,
        help="Training seasons (e.g. 2022 2023 2024 2025)",
    )
    args = parser.parse_args()
    train_all(markets=args.market, seasons=args.seasons)
