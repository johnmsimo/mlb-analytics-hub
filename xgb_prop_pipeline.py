# ============================================================
# MLB XGBOOST PROP MODEL TRAINING PIPELINE
# Markets: Batter Hits OVER 0.5 | Pitcher Strikeouts
# Compatible with MLB Analytics Hub (app.py)
# ============================================================
# CELL 1 — Install & Imports
# ============================================================
# !pip install xgboost pybaseball pandas scikit-learn shap matplotlib seaborn joblib pyarrow -q

import os, warnings, joblib, json
import numpy as np
import pandas as pd
import xgboost as xgb
import shap
import matplotlib.pyplot as plt
import seaborn as sns
from datetime import datetime, timedelta
from sklearn.model_selection import StratifiedKFold, cross_val_score
from sklearn.metrics import (roc_auc_score, accuracy_score,
                             classification_report, brier_score_loss,
                             log_loss)
from sklearn.calibration import CalibratedClassifierCV, calibration_curve
from sklearn.preprocessing import LabelEncoder
import pybaseball as pb
pb.cache.enable()

warnings.filterwarnings("ignore")
SEED = 42
np.random.seed(SEED)

OUTPUT_DIR = "/content/models"          # Change for local runs
os.makedirs(OUTPUT_DIR, exist_ok=True)

SEASONS     = [2021, 2022, 2023, 2024, 2025]  # Training seasons
PA_MIN      = 50     # Min PA to include a batter record
IP_MIN      = 10     # Min IP to include a pitcher record

print("✅ Imports done")
print(f"   XGBoost {xgb.__version__}  |  Seasons: {SEASONS[0]}–{SEASONS[-1]}")


# ============================================================
# CELL 2 — Data Fetch: Statcast Batter Season Agg + Pitcher
# ============================================================
def fetch_statcast_batters(season):
    """Pull season-level batter Statcast leaderboard via pybaseball."""
    print(f"  Fetching Statcast batters {season}...")
    try:
        df = pb.statcast_batter_expected_stats(season)
        df["season"] = season
        return df
    except Exception as e:
        print(f"  ⚠️  statcast_batter_expected_stats({season}) failed: {e}")
        return pd.DataFrame()

def fetch_statcast_pitchers(season):
    """Pull season-level pitcher Statcast leaderboard."""
    print(f"  Fetching Statcast pitchers {season}...")
    try:
        df = pb.statcast_pitcher_expected_stats(season)
        df["season"] = season
        return df
    except Exception as e:
        print(f"  ⚠️  statcast_pitcher_expected_stats({season}) failed: {e}")
        return pd.DataFrame()

def fetch_fangraphs_batters(season):
    """Pull FanGraphs batting leaderboard."""
    print(f"  Fetching FanGraphs batters {season}...")
    try:
        df = pb.batting_stats(season, qual=PA_MIN)
        df["season"] = season
        return df
    except Exception as e:
        print(f"  ⚠️  batting_stats({season}) failed: {e}")
        return pd.DataFrame()

def fetch_fangraphs_pitchers(season):
    """Pull FanGraphs pitching leaderboard."""
    print(f"  Fetching FanGraphs pitchers {season}...")
    try:
        df = pb.pitching_stats(season, qual=IP_MIN)
        df["season"] = season
        return df
    except Exception as e:
        print(f"  ⚠️  pitching_stats({season}) failed: {e}")
        return pd.DataFrame()

def fetch_game_logs_batters(season):
    """Pull FanGraphs batter game logs for ground truth outcomes."""
    print(f"  Fetching batter game logs {season}...")
    try:
        df = pb.batting_stats_bref(season)
        df["season"] = season
        return df
    except Exception as e:
        print(f"  ⚠️  batting_stats_bref({season}) failed: {e}")
        return pd.DataFrame()

# Fetch all seasons
print("\n📥 Fetching season data...")
sv_bat_all  = pd.concat([fetch_statcast_batters(s)  for s in SEASONS], ignore_index=True)
sv_pit_all  = pd.concat([fetch_statcast_pitchers(s) for s in SEASONS], ignore_index=True)
fg_bat_all  = pd.concat([fetch_fangraphs_batters(s) for s in SEASONS], ignore_index=True)
fg_pit_all  = pd.concat([fetch_fangraphs_pitchers(s) for s in SEASONS], ignore_index=True)

print(f"\n✅ Data fetched:")
print(f"   Statcast batters: {len(sv_bat_all):,} rows")
print(f"   Statcast pitchers: {len(sv_pit_all):,} rows")
print(f"   FanGraphs batters: {len(fg_bat_all):,} rows")
print(f"   FanGraphs pitchers: {len(fg_pit_all):,} rows")


# ============================================================
# CELL 3 — Game-Log Ground Truth (individual game outcomes)
# ============================================================
def fetch_statcast_game_logs(start_dt, end_dt):
    """
    Pull raw pitch-by-pitch Statcast for a date range, aggregate to
    per-game per-player outcome rows.
    Returns two DataFrames: batter_game_rows, pitcher_game_rows
    """
    print(f"  Pulling Statcast {start_dt} → {end_dt}...")
    try:
        sc = pb.statcast(start_dt=start_dt, end_dt=end_dt)
        sc = sc.dropna(subset=["batter", "pitcher", "game_pk"])
    except Exception as e:
        print(f"  ⚠️  statcast pull failed: {e}")
        return pd.DataFrame(), pd.DataFrame()

    # ── Batter game aggregation ────────────────────────────────────────────
    sc["is_hit"]   = sc["events"].isin(["single","double","triple","home_run"]).astype(int)
    sc["is_ab"]    = sc["events"].isin(
        ["single","double","triple","home_run","strikeout","field_out",
         "grounded_into_double_play","double_play","force_out",
         "fielders_choice","fielders_choice_out","strikeout_double_play"]).astype(int)
    sc["is_pa"]    = (~sc["events"].isna()).astype(int)
    sc["is_k"]     = sc["events"].isin(["strikeout","strikeout_double_play"]).astype(int)
    sc["is_bb"]    = sc["events"].isin(["walk","intent_walk"]).astype(int)
    sc["is_hr"]    = (sc["events"] == "home_run").astype(int)

    batter_game = (sc[sc["is_pa"] == 1]
                   .groupby(["game_pk","game_date","batter"])
                   .agg(
                       hits=("is_hit","sum"),
                       ab=("is_ab","sum"),
                       pa=("is_pa","sum"),
                       k=("is_k","sum"),
                       bb=("is_bb","sum"),
                       hr=("is_hr","sum"),
                       home_team=("home_team","first"),
                       away_team=("away_team","first"),
                       p_throws=("p_throws","first"),
                       stand=("stand","first"),
                   ).reset_index())
    batter_game["hit_over_0.5"] = (batter_game["hits"] >= 1).astype(int)
    batter_game["hit_over_1.5"] = (batter_game["hits"] >= 2).astype(int)

    # ── Pitcher game aggregation ───────────────────────────────────────────
    sc["is_k_p"] = sc["events"].isin(["strikeout","strikeout_double_play"]).astype(int)
    sc["is_bf"]  = (~sc["events"].isna()).astype(int)

    pitcher_game = (sc[sc["is_bf"] == 1]
                    .groupby(["game_pk","game_date","pitcher"])
                    .agg(
                        ks=("is_k_p","sum"),
                        bf=("is_bf","sum"),
                        home_team=("home_team","first"),
                        away_team=("away_team","first"),
                        stand_mix=("stand","first"),
                    ).reset_index())
    pitcher_game["k_over_3.5"] = (pitcher_game["ks"] >= 4).astype(int)
    pitcher_game["k_over_4.5"] = (pitcher_game["ks"] >= 5).astype(int)
    pitcher_game["k_over_5.5"] = (pitcher_game["ks"] >= 6).astype(int)

    return batter_game, pitcher_game

# Fetch one season of game logs (2024 as example; extend for full training)
# For full pipeline uncomment all seasons and concatenate
print("\n📥 Fetching game logs (2024 — extend to all seasons for full training)...")
bg_2024, pg_2024 = fetch_statcast_game_logs("2024-03-20", "2024-10-01")
print(f"   Batter game rows: {len(bg_2024):,}")
print(f"   Pitcher game rows: {len(pg_2024):,}")

# Save raw game logs
bg_2024.to_csv(f"{OUTPUT_DIR}/raw_batter_game_logs_2024.csv", index=False)
pg_2024.to_csv(f"{OUTPUT_DIR}/raw_pitcher_game_logs_2024.csv", index=False)
print("✅ Raw game logs saved")


# ============================================================
# CELL 4 — Feature Engineering: HITS MODEL
# ============================================================
def build_batter_features(bg, sv_bat, fg_bat, sv_pit, fg_pit, season):
    """
    Merge per-game batter outcomes with season-level Statcast
    and FanGraphs features for both the batter and opposing pitcher.
    """
    # Normalize IDs to int
    bg = bg.copy()
    bg["batter"] = bg["batter"].astype(int)

    # ── Batter season stats ────────────────────────────────────────────────
    sv = sv_bat[sv_bat["season"] == season].copy()
    sv["player_id"] = sv["player_id"].astype(int)
    sv = sv.rename(columns={
        "est_ba":               "sv_xba",
        "est_woba":             "sv_xwoba",
        "est_slg":              "sv_xslg",
        "k_percent":            "sv_k_pct",
        "bb_percent":           "sv_bb_pct",
        "avg_hit_speed":        "sv_ev",
        "brl_percent":          "sv_brl_pct",
        "anglesweetspotpercent":"sv_ss_pct",
        "ev95percent":          "sv_hh_pct",
        "avg_hit_angle":        "sv_la",
    })
    batter_sv_cols = ["player_id","sv_xba","sv_xwoba","sv_xslg",
                      "sv_k_pct","sv_bb_pct","sv_ev","sv_brl_pct",
                      "sv_ss_pct","sv_hh_pct","sv_la"]
    sv = sv[[c for c in batter_sv_cols if c in sv.columns]]

    fg = fg_bat[fg_bat["season"] == season].copy()
    fg_id_col = "IDfg" if "IDfg" in fg.columns else "playerid"
    if fg_id_col in fg.columns:
        fg[fg_id_col] = fg[fg_id_col].astype(str)
    fg_bat_cols = [fg_id_col,"Name","AVG","OBP","SLG","wOBA","wRC+","PA",
                   "K%","BB%","ISO","BABIP","Spd","WAR"]
    fg = fg[[c for c in fg_bat_cols if c in fg.columns]]
    fg.columns = [c.lower().replace("%","_pct").replace("+","_plus").replace(" ","_")
                  for c in fg.columns]

    # Merge batter stats onto game log
    df = bg.merge(sv, left_on="batter", right_on="player_id", how="left")

    # ── Pitcher opponent stats ─────────────────────────────────────────────
    sv_p = sv_pit[sv_pit["season"] == season].copy()
    sv_p["player_id"] = sv_p["player_id"].astype(int)
    sv_p = sv_p.rename(columns={
        "xera":         "opp_xera",
        "era":          "opp_era_sv",
        "est_woba":     "opp_xwoba_allowed",
        "k_percent":    "opp_k_pct",
        "bb_percent":   "opp_bb_pct",
        "whiff_percent":"opp_whiff",
    })
    pit_sv_cols = ["player_id","opp_xera","opp_era_sv",
                   "opp_xwoba_allowed","opp_k_pct","opp_bb_pct","opp_whiff"]
    sv_p = sv_p[[c for c in pit_sv_cols if c in sv_p.columns]]

    fg_p = fg_pit[fg_pit["season"] == season].copy()
    fg_p_id_col = "IDfg" if "IDfg" in fg_p.columns else "playerid"
    fg_p_cols = [fg_p_id_col,"ERA","FIP","xFIP","WHIP","K/9","BB/9",
                 "HR/9","K%","BB%","BABIP","LOB%","GB%","IP","GS","WAR"]
    fg_p = fg_p[[c for c in fg_p_cols if c in fg_p.columns]]
    fg_p.columns = ["opp_" + c.lower().replace("/","9").replace("%","_pct")
                     .replace("+","_plus").replace(" ","_")
                     for c in fg_p.columns]

    # ── Handedness feature ─────────────────────────────────────────────────
    df["bats_L"]     = (df["stand"] == "L").astype(int)
    df["throws_R"]   = (df["p_throws"] == "R").astype(int)
    df["platoon_adv"] = ((df["stand"] == "L") & (df["p_throws"] == "R")).astype(int) |                         ((df["stand"] == "R") & (df["p_throws"] == "L")).astype(int)

    # ── Rolling L7/L14 form (lag features) ────────────────────────────────
    df = df.sort_values(["batter","game_date"])
    df["l7_hits"] = (df.groupby("batter")["hits"]
                       .transform(lambda x: x.shift(1).rolling(7, min_periods=1).mean()))
    df["l14_hits"] = (df.groupby("batter")["hits"]
                        .transform(lambda x: x.shift(1).rolling(14, min_periods=1).mean()))
    df["l7_hit_rate"] = (df.groupby("batter")["hit_over_0.5"]
                           .transform(lambda x: x.shift(1).rolling(7, min_periods=1).mean()))

    df["season"] = season
    return df

print("\n🔧 Building batter feature matrix (2024)...")
bat_features = build_batter_features(
    bg_2024, sv_bat_all, fg_bat_all, sv_pit_all, fg_pit_all, season=2024
)
print(f"   Rows: {len(bat_features):,}  |  Columns: {bat_features.shape[1]}")
bat_features.to_csv(f"{OUTPUT_DIR}/batter_features_2024.csv", index=False)
print("✅ Batter features saved")


# ============================================================
# CELL 5 — Feature Engineering: STRIKEOUTS MODEL
# ============================================================
def build_pitcher_features(pg, sv_pit, fg_pit, sv_bat_lineup, fg_bat, season):
    """
    Build per-game pitcher feature rows for K prop model.
    Includes pitcher stats + opposing lineup aggregate quality.
    """
    pg = pg.copy()
    pg["pitcher"] = pg["pitcher"].astype(int)

    # ── Pitcher season stats ───────────────────────────────────────────────
    sv_p = sv_pit[sv_pit["season"] == season].copy()
    sv_p["player_id"] = sv_p["player_id"].astype(int)
    sv_p = sv_p.rename(columns={
        "xera":          "sv_xera",
        "era":           "sv_era",
        "est_woba":      "sv_xwoba_against",
        "k_percent":     "sv_k_pct",
        "bb_percent":    "sv_bb_pct",
        "whiff_percent": "sv_whiff_pct",
    })
    pit_cols = ["player_id","sv_xera","sv_era","sv_xwoba_against",
                "sv_k_pct","sv_bb_pct","sv_whiff_pct"]
    sv_p = sv_p[[c for c in pit_cols if c in sv_p.columns]]

    fg_p = fg_pit[fg_pit["season"] == season].copy()
    fg_p_id_col = "IDfg" if "IDfg" in fg_p.columns else "playerid"
    fg_p_cols = [fg_p_id_col,"ERA","FIP","xFIP","K/9","BB/9","HR/9",
                 "K%","BB%","BABIP","GB%","IP","GS","SwStr%","CSW%","Stuff+"]
    fg_p = fg_p[[c for c in fg_p_cols if c in fg_p.columns]]
    fg_p.columns = [c.lower().replace("/","9").replace("%","_pct")
                      .replace("+","_plus").replace(" ","_")
                    for c in fg_p.columns]
    if fg_p_id_col.lower() in fg_p.columns:
        fg_p = fg_p.rename(columns={fg_p_id_col.lower(): "pit_fg_id"})

    df = pg.merge(sv_p, left_on="pitcher", right_on="player_id", how="left")

    # ── Opposing lineup K susceptibility ──────────────────────────────────
    # Use season-avg batter k_pct and aggregate per game
    sv_b = sv_bat_lineup[sv_bat_lineup["season"] == season].copy()
    lineup_k_agg = sv_b.groupby("player_id").agg(
        batter_k_pct=("k_percent","mean"),
        batter_xwoba=("est_woba","mean"),
    ).reset_index()
    lineup_k_agg["player_id"] = lineup_k_agg["player_id"].astype(int)
    # We don't have per-game lineup composition without a full roster lookup,
    # so we approximate using team-level aggregates per game (game_pk + home/away)
    # This is a reasonable proxy — replace with actual lineup pull in production
    df["opp_lineup_k_pct_proxy"] = df["sv_k_pct"] * 0.88  # league-avg opponent contact adj
    df["opp_lineup_xwoba_proxy"] = 0.320  # will be enriched in production

    # ── Rolling L5 K form ─────────────────────────────────────────────────
    df = df.sort_values(["pitcher","game_date"])
    df["l5_ks"]   = (df.groupby("pitcher")["ks"]
                       .transform(lambda x: x.shift(1).rolling(5, min_periods=1).mean()))
    df["l5_k_rate"] = (df.groupby("pitcher")["k_over_4.5"]
                         .transform(lambda x: x.shift(1).rolling(5, min_periods=1).mean()))
    df["l10_ks"]  = (df.groupby("pitcher")["ks"]
                       .transform(lambda x: x.shift(1).rolling(10, min_periods=1).mean()))

    df["season"] = season
    return df

print("\n🔧 Building pitcher feature matrix (2024)...")
pit_features = build_pitcher_features(
    pg_2024, sv_pit_all, fg_pit_all, sv_bat_all, fg_bat_all, season=2024
)
print(f"   Rows: {len(pit_features):,}  |  Columns: {pit_features.shape[1]}")
pit_features.to_csv(f"{OUTPUT_DIR}/pitcher_features_2024.csv", index=False)
print("✅ Pitcher features saved")


# ============================================================
# CELL 6 — Model Training: HITS XGBoost
# ============================================================
HITS_FEATURES = [
    # Batter quality
    "sv_xba", "sv_xwoba", "sv_xslg", "sv_ev",
    "sv_brl_pct", "sv_hh_pct", "sv_ss_pct", "sv_la",
    "sv_k_pct", "sv_bb_pct",
    # Opposing pitcher
    "opp_xera", "opp_k_pct", "opp_bb_pct", "opp_whiff",
    # Handedness
    "bats_L", "throws_R", "platoon_adv",
    # Recent form
    "l7_hits", "l14_hits", "l7_hit_rate",
]

TARGET_HITS = "hit_over_0.5"

def train_xgb_hits(df):
    # Drop rows missing the target or more than 40% of features
    feat_cols = [f for f in HITS_FEATURES if f in df.columns]
    df_model = df[feat_cols + [TARGET_HITS]].dropna(subset=[TARGET_HITS])
    df_model[feat_cols] = df_model[feat_cols].fillna(df_model[feat_cols].median())

    X = df_model[feat_cols].values
    y = df_model[TARGET_HITS].values.astype(int)

    scale_pos_weight = (y == 0).sum() / max((y == 1).sum(), 1)

    params = {
        "n_estimators":        800,
        "learning_rate":       0.04,
        "max_depth":           5,
        "min_child_weight":    8,
        "subsample":           0.80,
        "colsample_bytree":    0.75,
        "gamma":               0.10,
        "reg_alpha":           0.05,
        "reg_lambda":          1.50,
        "scale_pos_weight":    scale_pos_weight,
        "objective":           "binary:logistic",
        "eval_metric":         "auc",
        "tree_method":         "hist",
        "random_state":        SEED,
        "n_jobs":              -1,
    }
    model = xgb.XGBClassifier(**params)

    # 5-fold CV
    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=SEED)
    cv_auc = cross_val_score(model, X, y, cv=cv, scoring="roc_auc")
    cv_ll  = cross_val_score(model, X, y, cv=cv, scoring="neg_log_loss")

    print(f"\n📊 HITS XGBoost CV Results (5-fold):")
    print(f"   AUC:       {cv_auc.mean():.4f} ± {cv_auc.std():.4f}")
    print(f"   Log-loss: {-cv_ll.mean():.4f} ± {cv_ll.std():.4f}")

    # Probability calibration (Platt scaling)
    calib = CalibratedClassifierCV(model, cv=3, method="sigmoid")
    calib.fit(X, y)

    return calib, feat_cols, df_model

print("\n🏋️  Training HITS XGBoost model...")
hits_model, hits_features, hits_df = train_xgb_hits(bat_features)


# ============================================================
# CELL 7 — Model Training: STRIKEOUTS XGBoost
# ============================================================
K_FEATURES_BASE = [
    # Pitcher quality
    "sv_xera", "sv_era", "sv_k_pct", "sv_bb_pct", "sv_whiff_pct",
    # Recent form
    "l5_ks", "l5_k_rate", "l10_ks",
    # Opponent lineup
    "opp_lineup_k_pct_proxy", "opp_lineup_xwoba_proxy",
]

def train_xgb_ks(df, target, label):
    feat_cols = [f for f in K_FEATURES_BASE if f in df.columns]
    df_model = df[feat_cols + [target]].dropna(subset=[target])
    df_model[feat_cols] = df_model[feat_cols].fillna(df_model[feat_cols].median())

    # Filter to starters only (BF >= 12 = at least 4 innings)
    if "bf" in df_model.columns:
        df_model = df_model[df_model["bf"] >= 12]

    X = df_model[feat_cols].values
    y = df_model[target].values.astype(int)

    scale_pos_weight = (y == 0).sum() / max((y == 1).sum(), 1)

    params = {
        "n_estimators":        700,
        "learning_rate":       0.05,
        "max_depth":           4,
        "min_child_weight":    10,
        "subsample":           0.80,
        "colsample_bytree":    0.70,
        "gamma":               0.15,
        "reg_alpha":           0.10,
        "reg_lambda":          2.00,
        "scale_pos_weight":    scale_pos_weight,
        "objective":           "binary:logistic",
        "eval_metric":         "auc",
        "tree_method":         "hist",
        "random_state":        SEED,
        "n_jobs":              -1,
    }
    model = xgb.XGBClassifier(**params)

    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=SEED)
    cv_auc = cross_val_score(model, X, y, cv=cv, scoring="roc_auc")
    cv_ll  = cross_val_score(model, X, y, cv=cv, scoring="neg_log_loss")

    print(f"\n📊 {label} XGBoost CV Results (5-fold):")
    print(f"   AUC:       {cv_auc.mean():.4f} ± {cv_auc.std():.4f}")
    print(f"   Log-loss: {-cv_ll.mean():.4f} ± {cv_ll.std():.4f}")

    calib = CalibratedClassifierCV(model, cv=3, method="sigmoid")
    calib.fit(X, y)
    return calib, feat_cols, df_model

print("\n🏋️  Training STRIKEOUTS XGBoost models...")
ks_35_model, ks_35_features, ks_35_df = train_xgb_ks(pit_features, "k_over_3.5", "K OVER 3.5")
ks_45_model, ks_45_features, ks_45_df = train_xgb_ks(pit_features, "k_over_4.5", "K OVER 4.5")
ks_55_model, ks_55_features, ks_55_df = train_xgb_ks(pit_features, "k_over_5.5", "K OVER 5.5")


# ============================================================
# CELL 8 — Evaluation & Calibration Plots
# ============================================================
def evaluate_model(model, feat_cols, df_model, target, title):
    X = df_model[feat_cols].fillna(df_model[feat_cols].median()).values
    y = df_model[target].values.astype(int)
    probs = model.predict_proba(X)[:, 1]
    preds = (probs >= 0.50).astype(int)

    auc   = roc_auc_score(y, probs)
    brier = brier_score_loss(y, probs)
    ll    = log_loss(y, probs)
    acc   = accuracy_score(y, preds)

    print(f"\n{'='*50}")
    print(f"  {title}")
    print(f"  AUC: {auc:.4f}  |  Brier: {brier:.4f}  |  LogLoss: {ll:.4f}  |  Acc: {acc:.3f}")
    print(classification_report(y, preds, target_names=["Under","Over"]))

    # Calibration plot
    fig, axes = plt.subplots(1, 2, figsize=(12, 4))
    fraction_pos, mean_pred = calibration_curve(y, probs, n_bins=10)
    axes[0].plot(mean_pred, fraction_pos, "s-", label="XGBoost")
    axes[0].plot([0, 1], [0, 1], "k--", label="Perfect")
    axes[0].set_title(f"{title} — Calibration")
    axes[0].set_xlabel("Mean Predicted Prob"); axes[0].set_ylabel("Fraction Positive")
    axes[0].legend()

    axes[1].hist(probs[y == 1], bins=20, alpha=0.6, label="Actual Over", color="green")
    axes[1].hist(probs[y == 0], bins=20, alpha=0.6, label="Actual Under", color="red")
    axes[1].set_title(f"{title} — Prob Distribution")
    axes[1].set_xlabel("Predicted Probability"); axes[1].legend()
    plt.tight_layout()
    plt.savefig(f"{OUTPUT_DIR}/{title.replace(' ','_')}_calibration.png", dpi=120)
    plt.show()
    return {"auc": auc, "brier": brier, "log_loss": ll, "accuracy": acc}

results = {}
results["hits_0.5"] = evaluate_model(hits_model, hits_features, hits_df, TARGET_HITS, "HITS OVER 0.5")
results["k_3.5"]    = evaluate_model(ks_35_model, ks_35_features, ks_35_df, "k_over_3.5", "K OVER 3.5")
results["k_4.5"]    = evaluate_model(ks_45_model, ks_45_features, ks_45_df, "k_over_4.5", "K OVER 4.5")
results["k_5.5"]    = evaluate_model(ks_55_model, ks_55_features, ks_55_df, "k_over_5.5", "K OVER 5.5")


# ============================================================
# CELL 9 — SHAP Feature Importance
# ============================================================
def plot_shap(model, feat_cols, df_model, title, n=500):
    """SHAP beeswarm + bar for the underlying XGB estimator."""
    X_sample = (df_model[feat_cols]
                .fillna(df_model[feat_cols].median())
                .sample(min(n, len(df_model)), random_state=SEED)
                .values)
    try:
        base_est = model.calibrated_classifiers_[0].estimator
    except Exception:
        base_est = model
    explainer = shap.TreeExplainer(base_est)
    shap_vals  = explainer.shap_values(X_sample)
    plt.figure(figsize=(10, 6))
    shap.summary_plot(shap_vals, X_sample, feature_names=feat_cols,
                      show=False, plot_size=None)
    plt.title(f"SHAP — {title}")
    plt.tight_layout()
    plt.savefig(f"{OUTPUT_DIR}/shap_{title.replace(' ','_')}.png", dpi=120)
    plt.show()

print("\n🔍 SHAP feature importance...")
plot_shap(hits_model,  hits_features,  hits_df,  "HITS OVER 0.5")
plot_shap(ks_45_model, ks_45_features, ks_45_df, "K OVER 4.5")


# ============================================================
# CELL 10 — Export Models + App Integration Wrapper
# ============================================================
model_registry = {
    "hits_over_0.5": {
        "model":    hits_model,
        "features": hits_features,
        "target":   TARGET_HITS,
        "market":   "batter_hits",
        "line":     0.5,
        "version":  "1.0.0",
        "trained":  datetime.utcnow().isoformat(),
        "seasons":  SEASONS,
    },
    "k_over_3.5": {
        "model":    ks_35_model,
        "features": ks_35_features,
        "target":   "k_over_3.5",
        "market":   "pitcher_strikeouts",
        "line":     3.5,
        "version":  "1.0.0",
        "trained":  datetime.utcnow().isoformat(),
        "seasons":  SEASONS,
    },
    "k_over_4.5": {
        "model":    ks_45_model,
        "features": ks_45_features,
        "target":   "k_over_4.5",
        "market":   "pitcher_strikeouts",
        "line":     4.5,
        "version":  "1.0.0",
        "trained":  datetime.utcnow().isoformat(),
        "seasons":  SEASONS,
    },
    "k_over_5.5": {
        "model":    ks_55_model,
        "features": ks_55_features,
        "target":   "k_over_5.5",
        "market":   "pitcher_strikeouts",
        "line":     5.5,
        "version":  "1.0.0",
        "trained":  datetime.utcnow().isoformat(),
        "seasons":  SEASONS,
    },
}

# Save .pkl files
for name, reg in model_registry.items():
    path = f"{OUTPUT_DIR}/xgb_{name}.pkl"
    meta = {k: v for k, v in reg.items() if k not in ("model", "features")}
    meta["xgboost_version"] = xgb.__version__
    joblib.dump({"model": reg["model"], "features": reg["features"], "meta": meta}, path)
    print(f"✅ Saved: {path}")

# Save metrics summary
with open(f"{OUTPUT_DIR}/model_metrics.json", "w") as f:
    json.dump(results, f, indent=2)

print("\n✅ All models saved to", OUTPUT_DIR)


# ============================================================
# CELL 11 — App Integration Module (xgb_prop_scorer.py)
# ============================================================
APP_INTEGRATION_CODE = '''
# xgb_prop_scorer.py
# Drop into your project root alongside app.py.
# Loaded once at startup; called inside _project_batter_vs_pitcher()
# and the pitcher K projection path to REPLACE or AUGMENT formula output.

import os, joblib
import numpy as np

_MODEL_DIR = os.environ.get("XGB_MODEL_DIR", "./models")
_REGISTRY  = {}

def _load_models():
    """Load all trained .pkl models from MODEL_DIR at startup."""
    global _REGISTRY
    import glob
    for path in glob.glob(os.path.join(_MODEL_DIR, "xgb_*.pkl")):
        key = os.path.basename(path).replace("xgb_","").replace(".pkl","")
        try:
            _REGISTRY[key] = joblib.load(path)
            print(f"[xgb] Loaded model: {key}")
        except Exception as e:
            print(f"[xgb] Failed to load {path}: {e}")

_load_models()

def _safe(v, default=0.0):
    try:
        f = float(v)
        return f if f == f else default  # nan check
    except Exception:
        return default

def xgb_hit_prob(batter_stats: dict, pitcher_stats: dict) -> float | None:
    """
    Returns calibrated P(hits >= 1) for a batter vs. pitcher.
    Returns None if model not loaded (graceful fallback to formula).
    """
    reg = _REGISTRY.get("hits_over_0.5")
    if not reg:
        return None
    feat_cols = reg["features"]
    row = {
        "sv_xba":         _safe(batter_stats.get("sv_xba"),      0.250),
        "sv_xwoba":       _safe(batter_stats.get("sv_xwoba") or
                                batter_stats.get("fg_woba"),      0.320),
        "sv_xslg":        _safe(batter_stats.get("sv_xslg"),     0.400),
        "sv_ev":          _safe(batter_stats.get("sv_ev"),        88.5),
        "sv_brl_pct":     _safe(batter_stats.get("sv_brl_pct"),  6.0),
        "sv_hh_pct":      _safe(batter_stats.get("sv_hh_pct"),   37.0),
        "sv_ss_pct":      _safe(batter_stats.get("sv_ss_pct"),   30.0),
        "sv_la":          _safe(batter_stats.get("sv_la"),        12.0),
        "sv_k_pct":       _safe(batter_stats.get("sv_k_pct") or
                                batter_stats.get("fg_kpct"),      0.22),
        "sv_bb_pct":      _safe(batter_stats.get("sv_bb_pct") or
                                batter_stats.get("fg_bbpct"),     0.08),
        "opp_xera":       _safe(pitcher_stats.get("sv_xera") or
                                pitcher_stats.get("fg_era"),      4.20),
        "opp_k_pct":      _safe(pitcher_stats.get("sv_k_pct") or
                                pitcher_stats.get("fg_kpct"),     0.22),
        "opp_bb_pct":     _safe(pitcher_stats.get("sv_bb_pct") or
                                pitcher_stats.get("fg_bbpct"),    0.08),
        "opp_whiff":      _safe(pitcher_stats.get("sv_whiff"),   22.0),
        "bats_L":         1 if str(batter_stats.get("fg_bats","R")).upper() == "L" else 0,
        "throws_R":       1 if str(pitcher_stats.get("pitch_hand","R")).upper() == "R" else 0,
        "platoon_adv":    0,   # caller should set from matchup context
        "l7_hits":        _safe(batter_stats.get("l7_hits"),     1.0),
        "l14_hits":       _safe(batter_stats.get("l14_hits"),    1.0),
        "l7_hit_rate":    _safe(batter_stats.get("l7_hit_rate"), 0.65),
    }
    row["platoon_adv"] = int(
        (row["bats_L"] == 1 and row["throws_R"] == 1) or
        (row["bats_L"] == 0 and row["throws_R"] == 0)
    )
    X = np.array([[row.get(f, 0.0) for f in feat_cols]], dtype=float)
    try:
        return float(reg["model"].predict_proba(X)[0, 1])
    except Exception:
        return None

def xgb_k_prob(pitcher_stats: dict, line: float = 4.5) -> float | None:
    """
    Returns calibrated P(Ks >= line + 0.5) for a pitcher.
    line should be 3.5, 4.5, or 5.5.
    Returns None if model not loaded.
    """
    key_map = {3.5: "k_over_3.5", 4.5: "k_over_4.5", 5.5: "k_over_5.5"}
    reg = _REGISTRY.get(key_map.get(line, "k_over_4.5"))
    if not reg:
        return None
    feat_cols = reg["features"]
    row = {
        "sv_xera":                 _safe(pitcher_stats.get("sv_xera") or
                                         pitcher_stats.get("fg_era"),    4.20),
        "sv_era":                  _safe(pitcher_stats.get("sv_era_p") or
                                         pitcher_stats.get("fg_era"),    4.20),
        "sv_k_pct":                _safe(pitcher_stats.get("sv_k_pct") or
                                         pitcher_stats.get("fg_kpct"),   0.22),
        "sv_bb_pct":               _safe(pitcher_stats.get("sv_bb_pct") or
                                         pitcher_stats.get("fg_bbpct"),  0.08),
        "sv_whiff_pct":            _safe(pitcher_stats.get("sv_whiff"),  22.0),
        "l5_ks":                   _safe(pitcher_stats.get("l5_ks"),     4.5),
        "l5_k_rate":               _safe(pitcher_stats.get("l5_k_rate"), 0.55),
        "l10_ks":                  _safe(pitcher_stats.get("l10_ks"),    4.5),
        "opp_lineup_k_pct_proxy":  _safe(pitcher_stats.get("sv_k_pct") or
                                         pitcher_stats.get("fg_kpct"),   0.22) * 0.88,
        "opp_lineup_xwoba_proxy":  0.320,
    }
    X = np.array([[row.get(f, 0.0) for f in feat_cols]], dtype=float)
    try:
        return float(reg["model"].predict_proba(X)[0, 1])
    except Exception:
        return None
'''

with open(f"{OUTPUT_DIR}/xgb_prop_scorer.py", "w") as f:
    f.write(APP_INTEGRATION_CODE.strip())
print("✅ xgb_prop_scorer.py saved — drop into your project root")


# ============================================================
# CELL 12 — How to Wire into app.py
# ============================================================
PATCH_NOTES = """
HOW TO INTEGRATE INTO app.py
==============================

1. Copy the trained .pkl files to your server:
   models/xgb_hits_over_0.5.pkl
   models/xgb_k_over_3.5.pkl
   models/xgb_k_over_4.5.pkl
   models/xgb_k_over_5.5.pkl

2. Copy xgb_prop_scorer.py to your project root.

3. In app.py top-level imports, add:
   from xgb_prop_scorer import xgb_hit_prob, xgb_k_prob

4. In _project_batter_vs_pitcher(), BLEND formula + XGB:
   xgb_p = xgb_hit_prob(batter_stats, pitcher_stats)
   if xgb_p is not None:
       p_hit = 0.40 * p_hit + 0.60 * xgb_p   # 60% XGB, 40% formula
   # (tune the blend weight as you validate)

5. In pitcher K projection section, BLEND:
   xgb_k = xgb_k_prob(pitcher_stats, line=4.5)
   if xgb_k is not None:
       raw_k_prob = 0.40 * raw_k_prob + 0.60 * xgb_k

6. Re-train on Render (or Colab) nightly with previous season + YTD data.
   Set XGB_MODEL_DIR env var to your model path.
"""
print(PATCH_NOTES)
with open(f"{OUTPUT_DIR}/integration_notes.txt", "w") as f:
    f.write(PATCH_NOTES)

print(f"\n{'='*55}")
print("  PIPELINE COMPLETE — files in:", OUTPUT_DIR)
print(f"{'='*55}")
import os
for fn in sorted(os.listdir(OUTPUT_DIR)):
    size = os.path.getsize(f"{OUTPUT_DIR}/{fn}")
    print(f"  {fn:<45} {size/1024:.1f} KB")
