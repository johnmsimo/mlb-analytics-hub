"""
train_hr_tb_rbi.py — Train HR / TB / RBI XGBoost prop models for BvP.

Output artifacts (auto-loaded by xgb_prop_scorer.py):
  models/xgb_hr_over_0.5.pkl   — P(>= 1 HR in game)
  models/xgb_tb_over_1.5.pkl   — P(>= 2 TB in game)
  models/xgb_rbi_over_0.5.pkl  — P(>= 1 RBI in game)

Updates models/xgb_feature_cols.json with feature lists for the new keys
("hr", "tb", "rbi") so the production loader can pick them up unchanged.

Usage:
  python train_hr_tb_rbi.py                         # default: 2021-2025
  python train_hr_tb_rbi.py --seasons 2023 2024 2025
  python train_hr_tb_rbi.py --test-year 2025 --quick   # smaller grid, faster
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import warnings
from datetime import datetime
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

# ────────────────────────────────────────────────────────────────────────────
# Feature schema — mirrors HITS_FEATURES so xgb_prop_scorer's existing
# _build_hit_features() can be reused in production with no extra wiring.
# ────────────────────────────────────────────────────────────────────────────
SHARED_FEATURES = [
    "sv_xba", "sv_xwoba", "sv_xslg", "sv_ev", "sv_brl_pct", "sv_hh_pct",
    "sv_ss_pct", "sv_la", "sv_k_pct", "sv_bb_pct",
    "opp_xera", "opp_k_pct", "opp_bb_pct", "opp_whiff",
    "bats_L", "throws_R", "platoon_adv",
    "l7_hits", "l14_hits", "l7_hit_rate",
]

FEATURE_MEDIANS: Dict[str, float] = {
    "sv_xba": 0.250, "sv_xwoba": 0.320, "sv_xslg": 0.400,
    "sv_ev": 88.0,  "sv_brl_pct": 4.0,  "sv_hh_pct": 35.0,
    "sv_ss_pct": 10.0, "sv_la": 12.0,
    "sv_k_pct": 22.0, "sv_bb_pct": 8.0,
    "opp_xera": 4.50, "opp_k_pct": 22.0,
    "opp_bb_pct": 8.0, "opp_whiff": 24.0,
    "bats_L": 0, "throws_R": 1, "platoon_adv": 0,
    "l7_hits": 1.5, "l14_hits": 3.0, "l7_hit_rate": 0.50,
}

XGB_PARAMS_FULL = dict(
    n_estimators=600, max_depth=5, learning_rate=0.04,
    subsample=0.80, colsample_bytree=0.75, min_child_weight=6,
    gamma=0.05, reg_alpha=0.10, reg_lambda=1.5,
    eval_metric="logloss", random_state=42, n_jobs=-1,
)
XGB_PARAMS_QUICK = dict(
    n_estimators=200, max_depth=4, learning_rate=0.06,
    subsample=0.85, colsample_bytree=0.80, min_child_weight=5,
    eval_metric="logloss", random_state=42, n_jobs=-1,
)

OUTDIR = "models"
HITTING_EVENTS_AB = {
    "single", "double", "triple", "home_run",
    "field_out", "force_out", "double_play", "triple_play",
    "grounded_into_double_play", "fielders_choice", "fielders_choice_out",
    "strikeout", "strikeout_double_play",
}
HIT_EVENTS = {"single", "double", "triple", "home_run"}


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--seasons", nargs="+", type=int,
                   default=[2021, 2022, 2023, 2024, 2025],
                   help="Seasons to pull Statcast for (default: 2021-2025)")
    p.add_argument("--test-year", type=int, default=2025,
                   help="Hold-out season for evaluation (default: 2025)")
    p.add_argument("--min-pa", type=int, default=50,
                   help="FanGraphs qualifier PA cut (default: 50)")
    p.add_argument("--min-bf", type=int, default=50,
                   help="FanGraphs qualifier BF cut (default: 50)")
    p.add_argument("--quick", action="store_true",
                   help="Smaller XGB grid for fast iteration")
    p.add_argument("--outdir", default=OUTDIR,
                   help="Output dir for models + metrics (default: models)")
    p.add_argument("--no-cache", action="store_true",
                   help="Disable pyBaseball cache (default: cache on)")
    p.add_argument("--markets", nargs="+", default=["hr", "tb", "rbi"],
                   choices=["hr", "tb", "rbi"],
                   help="Which markets to train (default: all three)")
    return p.parse_args()


def _bases_to_runners(row) -> int:
    """Count runners on base prior to a Statcast pitch event."""
    return int(bool(row.get("on_1b"))) + int(bool(row.get("on_2b"))) + int(bool(row.get("on_3b")))


def pull_statcast(seasons: List[int], use_cache: bool = True) -> pd.DataFrame:
    """Pull Statcast pitch-by-pitch for the listed seasons and stitch to one DF."""
    from pybaseball import statcast, cache as pb_cache
    if use_cache:
        pb_cache.enable()
    frames = []
    for yr in seasons:
        print(f"[statcast] {yr} pulling…", flush=True)
        try:
            sc = statcast(f"{yr}-03-15", f"{yr}-11-05")
            sc = sc[sc["game_type"] == "R"].copy()
            sc["season"] = yr
            frames.append(sc)
            print(f"[statcast] {yr} got {len(sc):,} pitches", flush=True)
        except Exception as ex:
            print(f"[statcast] {yr} FAILED: {ex}", flush=True)
    if not frames:
        raise RuntimeError("No Statcast data could be pulled")
    return pd.concat(frames, ignore_index=True)


def build_batter_game_outcomes(sc: pd.DataFrame) -> pd.DataFrame:
    """Aggregate pitch-level Statcast into per-batter-per-game stats with
    the three new binary targets: hr_binary, tb_over_1.5, rbi_binary.

    RBI is approximated from event type + baserunners (Statcast doesn't
    provide an explicit per-event RBI count, but the post_bat_score - pre
    delta is reliable when present).
    """
    print(f"[outcomes] aggregating {len(sc):,} pitches → batter-games…", flush=True)

    # Pitch-event flags
    sc = sc.copy()
    sc["is_hit"]   = sc["events"].isin(HIT_EVENTS).astype(int)
    sc["is_hr"]    = (sc["events"] == "home_run").astype(int)
    sc["is_2b"]    = (sc["events"] == "double").astype(int)
    sc["is_3b"]    = (sc["events"] == "triple").astype(int)
    sc["is_1b"]    = (sc["events"] == "single").astype(int)
    sc["tb_event"] = sc["is_1b"] + 2 * sc["is_2b"] + 3 * sc["is_3b"] + 4 * sc["is_hr"]
    sc["is_k"]     = sc["events"].isin({"strikeout", "strikeout_double_play"}).astype(int)
    sc["is_bb"]    = sc["events"].isin({"walk", "intent_walk"}).astype(int)
    sc["is_hbp"]   = (sc["events"] == "hit_by_pitch").astype(int)
    sc["is_sf"]    = (sc["events"] == "sac_fly").astype(int)
    sc["is_ab"]    = sc["events"].isin(HITTING_EVENTS_AB).astype(int)

    # Per-event RBI estimate: prefer post_bat_score - pre delta when present;
    # fall back to (HR × (1 + runners_on)) for HR rows and ~0.4 × runners for
    # non-HR hits with runners on. We're aiming for a *binary* 1+ RBI label.
    if {"post_bat_score", "bat_score"}.issubset(sc.columns):
        delta = (sc["post_bat_score"].fillna(0) - sc["bat_score"].fillna(0)).clip(lower=0)
        sc["rbi_event"] = delta.astype(int)
    else:
        runners = sc[["on_1b", "on_2b", "on_3b"]].notna().sum(axis=1) if "on_1b" in sc.columns else 0
        sc["rbi_event"] = sc["is_hr"] * (1 + runners) + (sc["is_hit"] - sc["is_hr"]) * (runners >= 1).astype(int)

    grp_keys = ["game_pk", "game_date", "batter", "pitcher", "p_throws", "season"]
    bat_game = (
        sc.groupby(grp_keys)
          .agg(
              hits=("is_hit", "sum"),
              hr=("is_hr", "sum"),
              tb=("tb_event", "sum"),
              rbi=("rbi_event", "sum"),
              k_batter=("is_k", "sum"),
              bb=("is_bb", "sum"),
              hbp=("is_hbp", "sum"),
              sf=("is_sf", "sum"),
              abs=("is_ab", "sum"),
          )
          .reset_index()
    )
    bat_game["pa"] = bat_game["abs"] + bat_game["bb"] + bat_game["hbp"] + bat_game["sf"]

    # Binary prop targets
    bat_game["hr_binary"]  = (bat_game["hr"]  >= 1).astype(int)
    bat_game["tb_o15"]     = (bat_game["tb"]  >= 2).astype(int)
    bat_game["rbi_binary"] = (bat_game["rbi"] >= 1).astype(int)

    # Drop trivial appearances (PH, defensive sub, etc.)
    bat_game = bat_game[bat_game["pa"] >= 1].copy()
    print(f"[outcomes] kept {len(bat_game):,} batter-game rows", flush=True)
    return bat_game


def attach_rolling_form(bat_game: pd.DataFrame) -> pd.DataFrame:
    """Add l7/l14 rolling features lagged by 1 game so we don't peek at the label."""
    bat_game = bat_game.sort_values(["batter", "game_date"]).copy()
    g = bat_game.groupby("batter", group_keys=False)
    bat_game["l7_hits"]     = g["hits"].apply(lambda s: s.shift(1).rolling(7,  min_periods=1).sum())
    bat_game["l14_hits"]    = g["hits"].apply(lambda s: s.shift(1).rolling(14, min_periods=1).sum())
    bat_game["l7_hit_rate"] = g.apply(lambda d: (d["hits"] >= 1).astype(int).shift(1).rolling(7, min_periods=1).mean())
    return bat_game


def attach_fg_features(bat_game: pd.DataFrame, seasons: List[int],
                        min_pa: int, min_bf: int) -> pd.DataFrame:
    """Join batter and pitcher FanGraphs season-priors as model features."""
    from pybaseball import batting_stats, pitching_stats

    bat_frames, pit_frames = [], []
    for yr in seasons:
        try:
            bf = batting_stats(yr, qual=min_pa); bf["season"] = yr
            bat_frames.append(bf)
        except Exception as ex:
            print(f"[fg bat] {yr}: {ex}")
        try:
            pf = pitching_stats(yr, qual=min_bf); pf["season"] = yr
            pit_frames.append(pf)
        except Exception as ex:
            print(f"[fg pit] {yr}: {ex}")

    fg_bat = pd.concat(bat_frames, ignore_index=True) if bat_frames else pd.DataFrame()
    fg_pit = pd.concat(pit_frames, ignore_index=True) if pit_frames else pd.DataFrame()

    # Map FG → model feature names
    bat_rename = {
        "xBA": "sv_xba", "xwOBA": "sv_xwoba", "xSLG": "sv_xslg",
        "EV": "sv_ev", "Barrel%": "sv_brl_pct", "HardHit%": "sv_hh_pct",
        "SwStr%": "sv_ss_pct", "LA": "sv_la",
        "K%": "sv_k_pct", "BB%": "sv_bb_pct", "Bats": "_bats",
        "xMLBAMID": "_mlbamid",
    }
    pit_rename = {
        "xERA": "opp_xera", "K%": "opp_k_pct", "BB%": "opp_bb_pct",
        "SwStr%": "opp_whiff", "xMLBAMID": "_p_mlbamid",
    }
    if not fg_bat.empty:
        for old, new in bat_rename.items():
            if old in fg_bat.columns:
                fg_bat[new] = fg_bat[old]
        if "_mlbamid" in fg_bat.columns:
            fg_bat["batter"] = pd.to_numeric(fg_bat["_mlbamid"], errors="coerce")
    if not fg_pit.empty:
        for old, new in pit_rename.items():
            if old in fg_pit.columns:
                fg_pit[new] = fg_pit[old]
        if "_p_mlbamid" in fg_pit.columns:
            fg_pit["pitcher"] = pd.to_numeric(fg_pit["_p_mlbamid"], errors="coerce")

    bat_keep = ["batter", "season", "sv_xba", "sv_xwoba", "sv_xslg",
                "sv_ev", "sv_brl_pct", "sv_hh_pct", "sv_ss_pct", "sv_la",
                "sv_k_pct", "sv_bb_pct", "_bats"]
    pit_keep = ["pitcher", "season", "opp_xera", "opp_k_pct",
                "opp_bb_pct", "opp_whiff"]
    bat_keep = [c for c in bat_keep if c in fg_bat.columns]
    pit_keep = [c for c in pit_keep if c in fg_pit.columns]

    if bat_keep:
        bat_game = bat_game.merge(fg_bat[bat_keep].drop_duplicates(["batter", "season"]),
                                   on=["batter", "season"], how="left")
    if pit_keep:
        bat_game = bat_game.merge(fg_pit[pit_keep].drop_duplicates(["pitcher", "season"]),
                                   on=["pitcher", "season"], how="left")

    # Handedness flags
    bat_game["bats_L"] = (bat_game.get("_bats", "R").fillna("R") == "L").astype(int)
    if "p_throws" in bat_game.columns:
        bat_game["throws_R"] = (bat_game["p_throws"] == "R").astype(int)
    else:
        bat_game["throws_R"] = 1
    bat_game["platoon_adv"] = (
        ((bat_game["bats_L"] == 1) & (bat_game["throws_R"] == 1)) |
        ((bat_game["bats_L"] == 0) & (bat_game["throws_R"] == 0))
    ).astype(int)

    # Fill medians for any remaining missingness
    for col, med in FEATURE_MEDIANS.items():
        if col not in bat_game.columns:
            bat_game[col] = med
        else:
            bat_game[col] = bat_game[col].fillna(med)

    return bat_game


def train_one(label_col: str, market_key: str, df: pd.DataFrame, test_year: int,
               outdir: str, params: dict) -> Tuple[object, dict]:
    """Train one binary CalibratedClassifierCV(XGBClassifier) for a single market."""
    from sklearn.calibration import CalibratedClassifierCV
    from sklearn.metrics import roc_auc_score, log_loss, brier_score_loss
    from xgboost import XGBClassifier

    train = df[df["season"] != test_year].copy()
    test  = df[df["season"] == test_year].copy()

    X_train = train[SHARED_FEATURES].values.astype(np.float32)
    y_train = train[label_col].values.astype(np.int32)
    X_test  = test[SHARED_FEATURES].values.astype(np.float32)
    y_test  = test[label_col].values.astype(np.int32)

    pos = max(1, int((y_train == 1).sum()))
    neg = max(1, int((y_train == 0).sum()))
    spw = neg / pos
    print(f"[{market_key}] train n={len(y_train):,} pos%={pos / len(y_train):.3f} "
          f"spw={spw:.2f} test n={len(y_test):,}", flush=True)

    base = XGBClassifier(**params, scale_pos_weight=spw, use_label_encoder=False)
    model = CalibratedClassifierCV(base, method="sigmoid", cv=5)
    model.fit(X_train, y_train)

    metrics = {"target": label_col, "n_train": int(len(y_train)),
               "n_test": int(len(y_test)), "pos_pct_train": round(pos / len(y_train), 4)}
    if len(y_test) > 0 and y_test.sum() > 0:
        p = model.predict_proba(X_test)[:, 1]
        metrics["auc"]   = round(float(roc_auc_score(y_test, p)),    4)
        metrics["log_loss"]   = round(float(log_loss(y_test, p)),         4)
        metrics["brier"] = round(float(brier_score_loss(y_test, p)), 4)
        print(f"[{market_key}] AUC {metrics['auc']:.4f} | LL {metrics['log_loss']:.4f} | "
              f"Brier {metrics['brier']:.4f}", flush=True)
    return model, metrics


def save_artifact(market_key: str, model, metrics: dict, outdir: str) -> str:
    """Save model in the dict format the production loader prefers."""
    import joblib
    name_map = {
        "hr":  "xgb_hr_over_0.5.pkl",
        "tb":  "xgb_tb_over_1.5.pkl",
        "rbi": "xgb_rbi_over_0.5.pkl",
    }
    fname = name_map[market_key]
    path = os.path.join(outdir, fname)
    payload = {
        "model":    model,
        "features": SHARED_FEATURES,
        "meta": {
            "target":     metrics["target"],
            "n_train":    metrics["n_train"],
            "n_test":     metrics["n_test"],
            "auc":        metrics.get("auc"),
            "log_loss":   metrics.get("log_loss"),
            "brier":      metrics.get("brier"),
            "trainedAt":  datetime.utcnow().isoformat() + "Z",
            "schema":     "shared_hits_features_v1",
        },
    }
    joblib.dump(payload, path)
    print(f"[save] {market_key} → {path} ({os.path.getsize(path)//1024} KB)", flush=True)
    return path


def update_feature_cols_json(outdir: str, markets: List[str]) -> None:
    """Merge the new market keys into models/xgb_feature_cols.json without
    clobbering existing entries (hits, k_3.5, k_4.5, k_5.5)."""
    path = os.path.join(outdir, "xgb_feature_cols.json")
    existing: Dict[str, list] = {}
    if os.path.exists(path):
        try:
            with open(path) as f:
                existing = json.load(f)
        except Exception:
            existing = {}
    for mk in markets:
        existing[mk] = SHARED_FEATURES
    with open(path, "w") as f:
        json.dump(existing, f, indent=2, sort_keys=True)
    print(f"[features] updated {path}", flush=True)


# ────────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────────

def main() -> int:
    args = parse_args()
    os.makedirs(args.outdir, exist_ok=True)

    print(f"[start] seasons={args.seasons} test_year={args.test_year} "
          f"markets={args.markets} quick={args.quick}", flush=True)

    sc = pull_statcast(args.seasons, use_cache=not args.no_cache)
    bat_game = build_batter_game_outcomes(sc)
    bat_game = attach_rolling_form(bat_game)
    bat_game = attach_fg_features(bat_game, args.seasons, args.min_pa, args.min_bf)

    # Final cleanup: drop rows missing a label (shouldn't happen but be safe)
    bat_game = bat_game.dropna(subset=["hr_binary", "tb_o15", "rbi_binary"]).copy()
    print(f"[ready] {len(bat_game):,} rows ready for training", flush=True)

    params = XGB_PARAMS_QUICK if args.quick else XGB_PARAMS_FULL
    label_for = {"hr": "hr_binary", "tb": "tb_o15", "rbi": "rbi_binary"}

    all_metrics = {"trainedAt": datetime.utcnow().isoformat() + "Z",
                   "seasons": args.seasons, "testYear": args.test_year,
                   "features": SHARED_FEATURES, "markets": {}}

    for mk in args.markets:
        model, metrics = train_one(label_for[mk], mk, bat_game,
                                   args.test_year, args.outdir, params)
        save_artifact(mk, model, metrics, args.outdir)
        all_metrics["markets"][mk] = metrics

    update_feature_cols_json(args.outdir, args.markets)

    metrics_path = os.path.join(args.outdir, "model_metrics_hr_tb_rbi.json")
    with open(metrics_path, "w") as f:
        json.dump(all_metrics, f, indent=2)
    print(f"[done] metrics written to {metrics_path}", flush=True)
    print("\nNew models will auto-load on next app boot — no code changes needed.")
    print("Verify by hitting /api/bvp/<batter_id>/<pitcher_id>/projection and")
    print("checking that probs.xgbHrProb / xgbTbProb / xgbRbiProb are non-null.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
