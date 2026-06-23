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

    # ── Pitcher season stats ───────────────────────────────────────────────
    sv_p = sv_pit[sv_pit["season"] == season].copy()
    sv_p["player_id"] = sv_p["player_id"].astype(int)
    sv_p = sv_p.rename(columns={
        "xera":        "sv_era_p",
        "k_percent":   "sv_k_pct_p",
        "est_woba":    "sv_xwoba_p",
        "whiff_percent":"sv_whiff_p",
    })
    pit_sv_cols = ["player_id","sv_era_p","sv_k_pct_p","sv_xwoba_p","sv_whiff_p"]
    sv_p = sv_p[[c for c in pit_sv_cols if c in sv_p.columns]]

    # Add pitcher rolling game-log features (from batter game log context)
    # For now use the statcast agg — intra-game features added in Cell 5

    # ── Rolling batter form ────────────────────────────────────────────────
    df = df.sort_values(["batter","game_date"])
    df["l5_hits"]  = df.groupby("batter")["hits"].transform(
        lambda x: x.shift(1).rolling(5, min_periods=1).mean())
    df["l10_hits"] = df.groupby("batter")["hits"].transform(
        lambda x: x.shift(1).rolling(10, min_periods=1).mean())
    df["l5_ab"]    = df.groupby("batter")["ab"].transform(
        lambda x: x.shift(1).rolling(5, min_periods=1).mean())
    df["l5_pa"]    = df.groupby("batter")["pa"].transform(
        lambda x: x.shift(1).rolling(5, min_periods=1).mean())

    df["season"] = season
    return df


print("\n🔧 Building batter feature matrix (2024)...")
bat_features = build_batter_features(
    bg_2024, sv_bat_all, fg_bat_all, sv_pit_all, fg_pit_all, season=2024
)
print(f"   Shape: {bat_features.shape}")
bat_features.to_csv(f"{OUTPUT_DIR}/bat_features_2024.csv", index=False)
print("✅ bat_features_2024.csv saved")


# ============================================================
# CELL 5 — Feature Engineering: STRIKEOUTS MODEL
# ============================================================
def build_pitcher_features(pg, sv_pit, fg_pit, sv_bat_lineup, fg_bat, season, umpire_features=None, lineup_stats=None):
    pg = pg.copy()
    pg["pitcher"] = pg["pitcher"].astype(int)

    sv_p = sv_pit[sv_pit["season"] == season].copy()
    sv_p["player_id"] = sv_p["player_id"].astype(int)
    sv_p = sv_p.rename(columns={
        "xera": "sv_xera",
        "era": "sv_era",
        "k_percent": "sv_k_pct",
        "bb_percent": "sv_bb_pct",
        "whiff_percent": "sv_whiff_pct",
    })
    pit_cols = ["player_id", "sv_xera", "sv_era", "sv_k_pct", "sv_bb_pct", "sv_whiff_pct"]
    sv_p = sv_p[[c for c in pit_cols if c in sv_p.columns]]

    df = pg.merge(sv_p, left_on="pitcher", right_on="player_id", how="left")
    df = df.sort_values(["pitcher", "game_date"])

    if "bf" not in df.columns and "ks" in df.columns:
        df["bf"] = 18

    if "outs_recorded" in df.columns:
        df["ip"] = df["outs_recorded"] / 3.0
    elif "bf" in df.columns:
        df["ip"] = np.clip(df["bf"] / 4.2, 0, 9)
    else:
        df["ip"] = 5.0

    df["l3_ks"] = df.groupby("pitcher")["ks"].transform(
        lambda x: x.shift(1).rolling(3, min_periods=1).mean()
    )
    df["l5_ks"] = df.groupby("pitcher")["ks"].transform(
        lambda x: x.shift(1).rolling(5, min_periods=1).mean()
    )
    df["l10_ks"] = df.groupby("pitcher")["ks"].transform(
        lambda x: x.shift(1).rolling(10, min_periods=1).mean()
    )

    df["l3_ip"] = df.groupby("pitcher")["ip"].transform(
        lambda x: x.shift(1).rolling(3, min_periods=1).mean()
    )
    df["l5_ip"] = df.groupby("pitcher")["ip"].transform(
        lambda x: x.shift(1).rolling(5, min_periods=1).mean()
    )

    df["prev_game_date"] = df.groupby("pitcher")["game_date"].shift(1)
    df["days_rest"] = (
        pd.to_datetime(df["game_date"]) - pd.to_datetime(df["prev_game_date"])
    ).dt.days.fillna(5).clip(lower=0, upper=14)

    # --- Real lineup stats (injected from lineup_loader) ---
    if lineup_stats and "k_pct" in lineup_stats:
        df["opp_lineup_k_pct_proxy"] = float(lineup_stats["k_pct"])
    else:
        df["opp_lineup_k_pct_proxy"] = 22.0
    if lineup_stats and "xwoba" in lineup_stats:
        df["opp_lineup_xwoba_proxy"] = float(lineup_stats["xwoba"])
    else:
        df["opp_lineup_xwoba_proxy"] = 0.320

    # --- Umpire features (from umpire_loader) ---
    df["ump_zone_size"] = float((umpire_features or {}).get("ump_zone_size", 0.0))
    df["ump_k_boost"]   = float((umpire_features or {}).get("ump_k_boost",   0.0))

    df["season"] = season
    return df



print("\n🔧 Building pitcher feature matrix (2024)...")
pit_features = build_pitcher_features(
    pg_2024, sv_pit_all, fg_pit_all, sv_bat_all, fg_bat_all, season=2024
)
print(f"   Shape: {pit_features.shape}")
pit_features.to_csv(f"{OUTPUT_DIR}/pit_features_2024.csv", index=False)
print("✅ pit_features_2024.csv saved")


# ============================================================
# CELL 6 — Model Training: HITS XGBoost
# ============================================================
HITS_FEATURES_BASE = [
    # Batter quality
    "sv_xba", "sv_xwoba", "sv_xslg",
    "sv_k_pct", "sv_bb_pct", "sv_ev", "sv_brl_pct", "sv_ss_pct",
    # Pitcher quality (opponent)
    "sv_era_p", "sv_k_pct_p", "sv_xwoba_p", "sv_whiff_p",
    # Recent form
    "l5_hits", "l10_hits", "l5_ab", "l5_pa",
    # Context
    "p_throws",
]

def train_xgb_hits(df, target="hit_over_0.5"):
    feat_cols = [f for f in HITS_FEATURES_BASE if f in df.columns]

    if "p_throws" in feat_cols:
        df = df.copy()
        df["p_throws"] = LabelEncoder().fit_transform(df["p_throws"].fillna("R"))

    keep_cols = feat_cols + [target]
    df_model = df[keep_cols].dropna(subset=[target]).copy()
    df_model[feat_cols] = df_model[feat_cols].fillna(df_model[feat_cols].median())
    df_model = df_model[df_model["l5_ab"] >= 2]

    X = df_model[feat_cols].values
    y = df_model[target].values

    skf = StratifiedKFold(n_splits=5, shuffle=True, random_state=SEED)
    model = xgb.XGBClassifier(
        n_estimators=300,
        max_depth=4,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        use_label_encoder=False,
        eval_metric="logloss",
        random_state=SEED,
    )
    cv_scores = cross_val_score(model, X, y, cv=skf, scoring="roc_auc")
    print(f"  CV AUC: {cv_scores.mean():.4f} ± {cv_scores.std():.4f}")

    model.fit(X, y)
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
    # Umpire zone tendencies
    "ump_zone_size", "ump_k_boost",
]

def train_xgb_ks(df, target, label):
    feat_cols = [f for f in K_FEATURES_BASE if f in df.columns]

    keep_cols = feat_cols + [target]
    for extra_col in ["ip", "bf"]:
        if extra_col in df.columns and extra_col not in keep_cols:
            keep_cols.append(extra_col)

    df_model = df[keep_cols].dropna(subset=[target]).copy()
    df_model[feat_cols] = df_model[feat_cols].fillna(df_model[feat_cols].median())

    # Filter to likely starter outings
    if "ip" in df_model.columns:
        df_model = df_model[df_model["ip"] >= 4.0]
    elif "bf" in df_model.columns:
        df_model = df_model[df_model["bf"] >= 15]

    X = df_model[feat_cols].values
    y = df_model[target].values

    skf = StratifiedKFold(n_splits=5, shuffle=True, random_state=SEED)
    model = xgb.XGBClassifier(
        n_estimators=400,
        max_depth=5,
        learning_rate=0.04,
        subsample=0.75,
        colsample_bytree=0.75,
        min_child_weight=3,
        use_label_encoder=False,
        eval_metric="logloss",
        random_state=SEED,
    )
    cv_scores = cross_val_score(model, X, y, cv=skf, scoring="roc_auc")
    print(f"  [{label}] CV AUC: {cv_scores.mean():.4f} ± {cv_scores.std():.4f}")

    model.fit(X, y)
    calib = CalibratedClassifierCV(model, cv=3, method="sigmoid")
    calib.fit(X, y)

    return calib, feat_cols, df_model

print("\n🏋️  Training K OVER 3.5 model...")
k35_model, k35_features, k35_df = train_xgb_ks(pit_features, "k_over_3.5", "K>3.5")
print("\n🏋️  Training K OVER 4.5 model...")
k45_model, k45_features, k45_df = train_xgb_ks(pit_features, "k_over_4.5", "K>4.5")
print("\n🏋️  Training K OVER 5.5 model...")
k55_model, k55_features, k55_df = train_xgb_ks(pit_features, "k_over_5.5", "K>5.5")


# ============================================================
# CELL 8 — Model Evaluation
# ============================================================
def evaluate_model(model, feat_cols, df_model, target, label):
    X = df_model[feat_cols].values
    y = df_model[target].values
    probs = model.predict_proba(X)[:, 1]
    preds = (probs >= 0.5).astype(int)

    auc   = roc_auc_score(y, probs)
    brier = brier_score_loss(y, probs)
    ll    = log_loss(y, probs)

    print(f"\n── {label} ──")
    print(f"  AUC:         {auc:.4f}")
    print(f"  Brier Score: {brier:.4f}  (lower is better, 0.25 = random)")
    print(f"  Log Loss:    {ll:.4f}")
    print(classification_report(y, preds, target_names=["UNDER","OVER"]))

    fig, axes = plt.subplots(1, 2, figsize=(12, 4))
    frac_pos, mean_pred = calibration_curve(y, probs, n_bins=10)
    axes[0].plot(mean_pred, frac_pos, marker="o", label="Model")
    axes[0].plot([0,1],[0,1], "--", label="Perfect")
    axes[0].set_title(f"{label} Calibration")
    axes[0].legend()

    importances = None
    try:
        raw_model = model.estimators_[0] if hasattr(model, "estimators_") else model
        importances = raw_model.feature_importances_
        axes[1].barh(feat_cols, importances)
        axes[1].set_title(f"{label} Feature Importance")
    except Exception:
        axes[1].set_visible(False)

    plt.tight_layout()
    plt.savefig(f"{OUTPUT_DIR}/{label.replace('>','_over_').replace(' ','_')}_eval.png", dpi=120)
    plt.close()

    return {"auc": auc, "brier": brier, "log_loss": ll}

print("\n📊 Evaluating models...")
hits_eval = evaluate_model(hits_model, hits_features, hits_df, "hit_over_0.5", "Hits>0.5")
k35_eval  = evaluate_model(k35_model,  k35_features,  k35_df,  "k_over_3.5",   "K>3.5")
k45_eval  = evaluate_model(k45_model,  k45_features,  k45_df,  "k_over_4.5",   "K>4.5")
k55_eval  = evaluate_model(k55_model,  k55_features,  k55_df,  "k_over_5.5",   "K>5.5")


# ============================================================
# CELL 9 — SHAP Explainability
# ============================================================
def plot_shap(model, feat_cols, df_model, label, n=500):
    try:
        raw = (model.estimators_[0]
               if hasattr(model, "estimators_") else model)
        explainer = shap.TreeExplainer(raw)
        sample = df_model[feat_cols].sample(min(n, len(df_model)), random_state=SEED)
        shap_vals = explainer.shap_values(sample)
        shap.summary_plot(shap_vals, sample, show=False)
        plt.title(f"SHAP — {label}")
        plt.tight_layout()
        plt.savefig(f"{OUTPUT_DIR}/shap_{label.replace('>','_over_').replace(' ','_')}.png", dpi=120)
        plt.close()
        print(f"  SHAP plot saved for {label}")
    except Exception as e:
        print(f"  SHAP failed for {label}: {e}")

print("\n🔍 Generating SHAP plots...")
plot_shap(hits_model, hits_features, hits_df, "Hits_0.5")
plot_shap(k35_model,  k35_features,  k35_df,  "K_3.5")
plot_shap(k45_model,  k45_features,  k45_df,  "K_4.5")


# ============================================================
# CELL 10 — Save Models
# ============================================================
_REGISTRY: dict[str, dict] = {}

def save_model(model, feat_cols, line_key, meta=None):
    path = os.path.join(OUTPUT_DIR, f"xgb_{line_key}.pkl")
    _REGISTRY[line_key] = {"model": model, "features": feat_cols}
    joblib.dump({"model": model, "features": feat_cols, "meta": meta or {}}, path)
    print(f"  Saved → {path}")

results = {
    "hits_over_0.5": hits_eval,
    "k_over_3.5":    k35_eval,
    "k_over_4.5":    k45_eval,
    "k_over_5.5":    k55_eval,
}

print("\n💾 Saving models...")
save_model(hits_model, hits_features, "hits_over_0.5", {"target": "hits", "line": 0.5})
save_model(k35_model,  k35_features,  "k_over_3.5",   {"target": "ks",   "line": 3.5})
save_model(k45_model,  k45_features,  "k_over_4.5",   {"target": "ks",   "line": 4.5})
save_model(k55_model,  k55_features,  "k_over_5.5",   {"target": "ks",   "line": 5.5})

with open(f"{OUTPUT_DIR}/eval_results.json", "w") as f:
    json.dump(results, f, indent=2)
print("✅ eval_results.json saved")
print("\n✅ All models saved to", OUTPUT_DIR)


# ============================================================
# CELL 11 — xgb_prop_scorer.py (app.py integration snippet)
# ============================================================
APP_INTEGRATION_CODE = '''
"""
xgb_prop_scorer.py — XGBoost Prop Probability Scorer
=====================================================
Drop this into your project root.  Loaded once by app.py at startup.

Usage:
    from xgb_prop_scorer import load_models, xgb_hits_prob, xgb_k_prob

    load_models()   # call once, e.g. in app.py create_app()

    p_hit  = xgb_hits_prob(batter_stats)
    p_k35  = xgb_k_prob(pitcher_stats, line=3.5)
    p_k45  = xgb_k_prob(pitcher_stats, line=4.5)
    p_k55  = xgb_k_prob(pitcher_stats, line=5.5)
"""
from __future__ import annotations
import os
import joblib
import numpy as np

_MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
_REGISTRY: dict[str, dict] = {}

def _safe(v, default=0.0):
    try:
        f = float(v)
        return f if not (f != f) else default   # guard NaN
    except (TypeError, ValueError):
        return default

def load_models():
    """Load all .pkl model files from the models/ directory into _REGISTRY."""
    for fname in os.listdir(_MODELS_DIR):
        if not fname.endswith(".pkl"):
            continue
        key = fname.replace("xgb_","").replace(".pkl","")
        try:
            obj = joblib.load(os.path.join(_MODELS_DIR, fname))
            _REGISTRY[key] = obj
            print(f"[xgb_scorer] loaded {key}")
        except Exception as e:
            print(f"[xgb_scorer] failed to load {fname}: {e}")

def xgb_hits_prob(batter_stats: dict) -> float | None:
    """
    Probability of batter recording ≥1 hit.
    batter_stats keys match the HITS_FEATURES_BASE list.
    Returns float [0,1] or None if model not loaded.
    """
    if "hits_over_0.5" not in _REGISTRY:
        return None
    reg = _REGISTRY["hits_over_0.5"]
    feat_cols = reg["features"]
    row = {
        "sv_xba":    _safe(batter_stats.get("sv_xba"),    0.250),
        "sv_xwoba":  _safe(batter_stats.get("sv_xwoba"),  0.320),
        "sv_xslg":   _safe(batter_stats.get("sv_xslg"),   0.420),
        "sv_k_pct":  _safe(batter_stats.get("sv_k_pct"),  22.0),
        "sv_bb_pct": _safe(batter_stats.get("sv_bb_pct"),  8.5),
        "sv_ev":     _safe(batter_stats.get("sv_ev"),     88.5),
        "sv_brl_pct":_safe(batter_stats.get("sv_brl_pct"), 7.0),
        "sv_ss_pct": _safe(batter_stats.get("sv_ss_pct"), 30.0),
        "sv_era_p":  _safe(batter_stats.get("sv_era_p"),   4.20),
        "sv_k_pct_p":_safe(batter_stats.get("sv_k_pct_p"),22.0),
        "sv_xwoba_p":_safe(batter_stats.get("sv_xwoba_p"), 0.310),
        "sv_whiff_p":_safe(batter_stats.get("sv_whiff_p"),22.0),
        "l5_hits":   _safe(batter_stats.get("l5_hits"),    0.8),
        "l10_hits":  _safe(batter_stats.get("l10_hits"),   0.9),
        "l5_ab":     _safe(batter_stats.get("l5_ab"),      3.5),
        "l5_pa":     _safe(batter_stats.get("l5_pa"),      4.0),
        "p_throws":  0 if str(batter_stats.get("p_throws","R")).upper() == "L" else 1,
    }
    X = np.array([[row.get(f, 0.0) for f in feat_cols]], dtype=float)
    try:
        return float(reg["model"].predict_proba(X)[0, 1])
    except Exception:
        return None

def xgb_k_prob(pitcher_stats: dict, line: float = 4.5) -> float | None:
    """
    Probability of pitcher recording strikeouts OVER `line`.
    pitcher_stats keys match K_FEATURES_BASE.
    Pass ump_zone_size and ump_k_boost in pitcher_stats for umpire-adjusted probs.
    Returns float [0,1] or None if model not loaded.
    """
    line_map  = {3.5: "k_over_3.5", 4.5: "k_over_4.5", 5.5: "k_over_5.5"}
    line_key  = line_map.get(line, "k_over_4.5")
    if line_key not in _REGISTRY:
        return None
    reg = _REGISTRY[line_key]
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
        "l5_k_rate":               _safe(pitcher_stats.get("l5_k_rate"), 0.22),
        "l10_ks":                  _safe(pitcher_stats.get("l10_ks"),    4.5),
        "opp_lineup_k_pct_proxy":  _safe(pitcher_stats.get("sv_k_pct") or
                                         pitcher_stats.get("fg_kpct"),   0.22) * 0.88,
        "opp_lineup_xwoba_proxy":  _safe(pitcher_stats.get("opp_xwoba"), 0.320),
        # Umpire features — injected by xgb_k_prob caller or scorer wrapper
        "ump_zone_size":           _safe(pitcher_stats.get("ump_zone_size"), 0.0),
        "ump_k_boost":             _safe(pitcher_stats.get("ump_k_boost"),   0.0),
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

2. At app startup (create_app or equivalent):
   from xgb_prop_scorer import load_models
   load_models()

3. In your props route / scorer:
   from xgb_prop_scorer import xgb_hits_prob, xgb_k_prob

   # For a batter prop:
   p = xgb_hits_prob(batter_stats_dict)

   # For a K prop (umpire-adjusted):
   import umpire_loader
   uf = umpire_loader.get_umpire_features(game_hp_umpire_name)
   pitcher_stats_dict.update(uf)   # injects ump_zone_size, ump_k_boost
   p = xgb_k_prob(pitcher_stats_dict, line=4.5)

4. Retrain periodically (weekly or end-of-season) with the full pipeline.
"""
print(PATCH_NOTES)
