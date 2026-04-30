"""
xgb_training_pipeline.py  —  XGBoost Prop Training Pipeline
═══════════════════════════════════════════════════════════════════════════════
Run this in Google Colab (or locally) to train the hit and K models.
Outputs:  models/xgb_hits.pkl
          models/xgb_k_3_5.pkl
          models/xgb_k_4_5.pkl
          models/xgb_k_5_5.pkl
          models/xgb_feature_cols.json
          models/model_metrics.json

INSTALLATION (Colab cell 1):
    !pip install pybaseball xgboost scikit-learn shap imbalanced-learn -q

USAGE:
    Run each cell sequentially.  After completion, download the models/
    folder and place it at the root of your mlb-analytics-hub repo.
═══════════════════════════════════════════════════════════════════════════════
"""

# ── Cell 1: Install ──────────────────────────────────────────────────────────
# !pip install pybaseball xgboost scikit-learn shap imbalanced-learn -q

# ── Cell 2: Imports & Config ─────────────────────────────────────────────────
import os, json, pickle, warnings
import numpy as np
import pandas as pd
from datetime import datetime
warnings.filterwarnings('ignore')

SEASONS       = [2021, 2022, 2023, 2024, 2025]  # training window
MIN_PA        = 50      # minimum plate appearances for batter inclusion
MIN_BF        = 50      # minimum batters faced for pitcher inclusion
TEST_YEAR     = 2025    # held-out season for evaluation
N_FOLDS       = 5
XGB_PARAMS    = dict(
    n_estimators=600,
    max_depth=5,
    learning_rate=0.04,
    subsample=0.80,
    colsample_bytree=0.75,
    min_child_weight=6,
    gamma=0.05,
    reg_alpha=0.10,
    reg_lambda=1.5,
    eval_metric='logloss',
    random_state=42,
    n_jobs=-1,
)

OUTDIR = 'models'
os.makedirs(OUTDIR, exist_ok=True)

HITS_FEATURES = [
    'sv_xba','sv_xwoba','sv_xslg','sv_ev','sv_brl_pct','sv_hh_pct',
    'sv_ss_pct','sv_la','sv_k_pct','sv_bb_pct',
    'opp_xera','opp_k_pct','opp_bb_pct','opp_whiff',
    'bats_L','throws_R','platoon_adv',
    'l7_hits','l7_hit_rate',
]

K_FEATURES = [
    'sv_xera','sv_era','sv_k_pct','sv_bb_pct','sv_whiff',
    'l5_ks','l5_k_rate','l10_ks',
    'opp_lineup_k_pct','opp_lineup_xwoba',
]

print(f'Config ready  |  seasons: {SEASONS}  |  test: {TEST_YEAR}')

# ── Cell 3: Pull Statcast + FanGraphs data ────────────────────────────────────
from pybaseball import (
    statcast,
    pitching_stats,
    batting_stats,
    statcast_batter_exitvelo_barrels,
    statcast_pitcher_exitvelo_barrels,
)
from pybaseball import cache
cache.enable()

print('Fetching FanGraphs batting leaderboards...')
fg_bat_frames = []
for yr in SEASONS:
    try:
        df = batting_stats(yr, qual=MIN_PA)
        df['season'] = yr
        fg_bat_frames.append(df)
        print(f'  FG batting {yr}: {len(df)} rows')
    except Exception as e:
        print(f'  FG batting {yr} failed: {e}')
fg_bat = pd.concat(fg_bat_frames, ignore_index=True) if fg_bat_frames else pd.DataFrame()

print('Fetching FanGraphs pitching leaderboards...')
fg_pit_frames = []
for yr in SEASONS:
    try:
        df = pitching_stats(yr, qual=MIN_BF)
        df['season'] = yr
        fg_pit_frames.append(df)
        print(f'  FG pitching {yr}: {len(df)} rows')
    except Exception as e:
        print(f'  FG pitching {yr} failed: {e}')
fg_pit = pd.concat(fg_pit_frames, ignore_index=True) if fg_pit_frames else pd.DataFrame()

print(f'FG bat: {fg_bat.shape}  |  FG pit: {fg_pit.shape}')

# ── Cell 4: Pull per-game Statcast outcomes ───────────────────────────────────
# Each row = one batter-game appearance, labeled: hit (1/0) and Ks (int)
print('Pulling per-game Statcast (this takes ~10 min for 5 seasons)...')

out_rows = []
for yr in SEASONS:
    start = f'{yr}-03-28'
    end   = f'{yr}-10-05'
    try:
        sc = statcast(start_dt=start, end_dt=end)
        sc = sc[sc['game_type'] == 'R'].copy()

        # ── Batter game outcomes ─────────────────────────────────────────────
        hit_events = {'single','double','triple','home_run'}
        sc['is_hit']  = sc['events'].isin(hit_events).astype(int)
        sc['is_ab']   = sc['events'].notna() & ~sc['events'].isin(
            {'walk','hit_by_pitch','sac_fly','sac_bunt','intent_walk'}
        )
        sc['is_k']    = sc['events'].isin({'strikeout','strikeout_double_play'}).astype(int)

        bat_game = (
            sc.groupby(['game_pk','game_date','batter','pitcher','p_throws'])
            .agg(
                hits=('is_hit','sum'),
                abs=('is_ab','sum'),
                k_batter=('is_k','sum'),
            )
            .reset_index()
        )
        bat_game['season']     = yr
        bat_game['hit_binary'] = (bat_game['hits'] >= 1).astype(int)
        out_rows.append(bat_game)
        print(f'  {yr}: {len(bat_game)} batter-game rows')
    except Exception as e:
        print(f'  {yr} failed: {e}')

game_df = pd.concat(out_rows, ignore_index=True) if out_rows else pd.DataFrame()
print(f'Total batter-game rows: {len(game_df):,}')

# ── Cell 5: Build Pitcher-Game K outcomes ─────────────────────────────────────
print('Aggregating pitcher-game K totals...')
pit_out_rows = []
for yr in SEASONS:
    start = f'{yr}-03-28'
    end   = f'{yr}-10-05'
    try:
        sc = statcast(start_dt=start, end_dt=end)
        sc = sc[sc['game_type'] == 'R'].copy()
        sc['is_k'] = sc['events'].isin({'strikeout','strikeout_double_play'}).astype(int)

        pit_game = (
            sc.groupby(['game_pk','game_date','pitcher','stand'])
            .agg(ks=('is_k','sum'), bf=('events','count'))
            .reset_index()
        )
        # Aggregate to pitcher-game total
        pit_total = (
            pit_game.groupby(['game_pk','game_date','pitcher'])
            .agg(total_ks=('ks','sum'), total_bf=('bf','sum'))
            .reset_index()
        )
        # Opponent lineup avg xwOBA / K% as contextual feature
        opp_agg = (
            sc.groupby(['game_pk','pitcher'])
            .agg(opp_k_events=('is_k','sum'), opp_pa=('events','count'))
            .reset_index()
        )
        opp_agg['opp_lineup_k_pct'] = opp_agg['opp_k_events'] / opp_agg['opp_pa'].clip(lower=1)
        pit_total = pit_total.merge(opp_agg[['game_pk','pitcher','opp_lineup_k_pct']],
                                    on=['game_pk','pitcher'], how='left')
        pit_total['season'] = yr
        pit_out_rows.append(pit_total)
        print(f'  {yr}: {len(pit_total)} pitcher-game rows')
    except Exception as e:
        print(f'  {yr} pitcher-game failed: {e}')

pit_game_df = pd.concat(pit_out_rows, ignore_index=True) if pit_out_rows else pd.DataFrame()
print(f'Total pitcher-game rows: {len(pit_game_df):,}')

# ── Cell 6: Build Rolling Form Features ───────────────────────────────────────
print('Computing L7/L14 rolling hit features...')

if not game_df.empty:
    game_df = game_df.sort_values(['batter','game_date'])
    game_df['l7_hits']     = game_df.groupby('batter')['hits'].transform(
        lambda x: x.shift(1).rolling(7,  min_periods=1).sum()
    )
    game_df['l14_hits']    = game_df.groupby('batter')['hits'].transform(
        lambda x: x.shift(1).rolling(14, min_periods=1).sum()
    )
    game_df['l7_hit_rate'] = game_df.groupby('batter')['hit_binary'].transform(
        lambda x: x.shift(1).rolling(7,  min_periods=1).mean()
    )

print('Computing L5/L10 rolling K features for pitchers...')
if not pit_game_df.empty:
    pit_game_df = pit_game_df.sort_values(['pitcher','game_date'])
    pit_game_df['l5_ks']     = pit_game_df.groupby('pitcher')['total_ks'].transform(
        lambda x: x.shift(1).rolling(5,  min_periods=1).mean()
    )
    pit_game_df['l10_ks']    = pit_game_df.groupby('pitcher')['total_ks'].transform(
        lambda x: x.shift(1).rolling(10, min_periods=1).mean()
    )
    pit_game_df['l5_k_rate'] = pit_game_df.groupby('pitcher')['total_ks'].transform(
        lambda x: x.shift(1).rolling(5,  min_periods=1).mean()
        / pit_game_df.groupby('pitcher')['total_bf'].transform(
            lambda x: x.shift(1).rolling(5, min_periods=1).mean()
        ).clip(lower=1)
    )

print('Rolling features done.')

# ── Cell 7: Merge Season-Level Stats → Game Rows ─────────────────────────────
print('Merging FanGraphs season stats onto game rows...')

# Rename FG columns to match scorer keys
FG_BAT_MAP = {
    'xBA': 'sv_xba', 'xwOBA': 'sv_xwoba', 'xSLG': 'sv_xslg',
    'EV':  'sv_ev',  'Barrels': 'sv_brl_pct', 'HardHit%': 'sv_hh_pct',
    'SwStr%': 'sv_ss_pct', 'LA': 'sv_la',
    'K%': 'sv_k_pct', 'BB%': 'sv_bb_pct',
    'playerid': 'fg_playerid',
}
FG_PIT_MAP = {
    'xERA': 'opp_xera', 'ERA': 'opp_era',
    'K%': 'opp_k_pct', 'BB%': 'opp_bb_pct',
    'SwStr%': 'opp_whiff',
    'playerid': 'fg_pid',
}

if not fg_bat.empty:
    fg_bat_r = fg_bat.rename(columns={k: v for k, v in FG_BAT_MAP.items() if k in fg_bat.columns})
    fg_bat_r['sv_k_pct']  = fg_bat_r.get('sv_k_pct', pd.Series()).apply(
        lambda x: x * 100 if pd.notna(x) and x < 1 else x)
    fg_bat_r['sv_bb_pct'] = fg_bat_r.get('sv_bb_pct', pd.Series()).apply(
        lambda x: x * 100 if pd.notna(x) and x < 1 else x)
    # Merge by mlbam batter id + season if available, else by Name+season
    if 'IDfg' in fg_bat_r.columns:
        game_df = game_df.merge(
            fg_bat_r[['IDfg','season'] + [v for v in FG_BAT_MAP.values() if v in fg_bat_r.columns]],
            left_on=['batter','season'], right_on=['IDfg','season'], how='left'
        )

if not fg_pit.empty:
    fg_pit_r = fg_pit.rename(columns={k: v for k, v in FG_PIT_MAP.items() if k in fg_pit.columns})
    for pct_col in ('opp_k_pct','opp_bb_pct','opp_whiff'):
        if pct_col in fg_pit_r.columns:
            fg_pit_r[pct_col] = fg_pit_r[pct_col].apply(
                lambda x: x * 100 if pd.notna(x) and x < 1 else x)
    if 'IDfg' in fg_pit_r.columns:
        pit_game_df = pit_game_df.merge(
            fg_pit_r[['IDfg','season'] + [v for v in FG_PIT_MAP.values() if v in fg_pit_r.columns]],
            left_on=['pitcher','season'], right_on=['IDfg','season'], how='left'
        )

print(f'Hit df after merge: {game_df.shape}  |  K df after merge: {pit_game_df.shape}')

# ── Cell 8: Add Handedness + Platoon ─────────────────────────────────────────
print('Adding platoon flags...')
# p_throws is in game_df from the statcast pull
if 'p_throws' in game_df.columns:
    game_df['throws_R']   = (game_df['p_throws'] == 'R').astype(int)
else:
    game_df['throws_R']   = 1
# batter stand — need to pull from statcast or assume R
if 'stand' in game_df.columns:
    game_df['bats_L']     = (game_df['stand'] == 'L').astype(int)
else:
    game_df['bats_L']     = 0
game_df['platoon_adv'] = (
    ((game_df.get('bats_L', 0) == 1) & (game_df.get('throws_R', 1) == 1)) |
    ((game_df.get('bats_L', 0) == 0) & (game_df.get('throws_R', 1) == 0))
).astype(int)

# ── Cell 9: Fill missing + final dataset prep ─────────────────────────────────
print('Filling missing values with league medians...')

HIT_MEDIANS = {
    'sv_xba': 0.250, 'sv_xwoba': 0.320, 'sv_xslg': 0.400,
    'sv_ev': 88.0, 'sv_brl_pct': 4.0, 'sv_hh_pct': 35.0,
    'sv_ss_pct': 10.0, 'sv_la': 12.0, 'sv_k_pct': 22.0, 'sv_bb_pct': 8.0,
    'opp_xera': 4.50, 'opp_k_pct': 22.0, 'opp_bb_pct': 8.0, 'opp_whiff': 24.0,
    'bats_L': 0, 'throws_R': 1, 'platoon_adv': 0,
    'l7_hits': 1.5, 'l7_hit_rate': 0.50,
}
K_MEDIANS = {
    'sv_xera': 4.50, 'sv_era': 4.50, 'sv_k_pct': 22.0, 'sv_bb_pct': 8.0,
    'sv_whiff': 24.0, 'l5_ks': 4.5, 'l5_k_rate': 22.0, 'l10_ks': 4.5,
    'opp_lineup_k_pct': 22.0, 'opp_lineup_xwoba': 0.320,
}

for col, med in HIT_MEDIANS.items():
    if col not in game_df.columns:
        game_df[col] = med
    else:
        game_df[col].fillna(med, inplace=True)

for col, med in K_MEDIANS.items():
    if col not in pit_game_df.columns:
        pit_game_df[col] = med
    else:
        pit_game_df[col].fillna(med, inplace=True)

# K binary labels
for line in (3.5, 4.5, 5.5):
    pit_game_df[f'k_over_{line}'] = (pit_game_df['total_ks'] > line).astype(int)

print('Dataset ready.')
print(f"Hit label balance: {game_df['hit_binary'].mean():.3f} positive rate")
for line in (3.5, 4.5, 5.5):
    col = f'k_over_{line}'
    if col in pit_game_df.columns:
        print(f"K>{line} label balance: {pit_game_df[col].mean():.3f} positive rate")

# ── Cell 10: Train Hit Model ──────────────────────────────────────────────────
from xgboost import XGBClassifier
from sklearn.model_selection import StratifiedKFold, cross_val_score
from sklearn.calibration import CalibratedClassifierCV
from sklearn.metrics import roc_auc_score, log_loss, brier_score_loss

print('\n══ Training HIT model ══')

train_hit = game_df[game_df['season'] != TEST_YEAR].copy()
test_hit  = game_df[game_df['season'] == TEST_YEAR].copy()

X_train_h = train_hit[HITS_FEATURES].values.astype(np.float32)
y_train_h = train_hit['hit_binary'].values
X_test_h  = test_hit[HITS_FEATURES].values.astype(np.float32)
y_test_h  = test_hit['hit_binary'].values

# Class balance weight
pos_ratio_h = (y_train_h == 0).sum() / max(1, (y_train_h == 1).sum())

hit_model_raw = XGBClassifier(
    **XGB_PARAMS,
    scale_pos_weight=pos_ratio_h,
    use_label_encoder=False,
)

# Platt scaling calibration (sigmoid)
hit_model = CalibratedClassifierCV(hit_model_raw, method='sigmoid', cv=5)
hit_model.fit(X_train_h, y_train_h)

if len(y_test_h) > 0:
    p_test_h  = hit_model.predict_proba(X_test_h)[:, 1]
    auc_h     = roc_auc_score(y_test_h, p_test_h)
    ll_h      = log_loss(y_test_h, p_test_h)
    brier_h   = brier_score_loss(y_test_h, p_test_h)
    print(f'  HIT model  |  AUC: {auc_h:.4f}  |  LogLoss: {ll_h:.4f}  |  Brier: {brier_h:.4f}')
else:
    auc_h = ll_h = brier_h = None
    print('  HIT model trained (no 2025 test data yet)')

hit_path = os.path.join(OUTDIR, 'xgb_hits.pkl')
with open(hit_path, 'wb') as f:
    pickle.dump(hit_model, f)
print(f'  Saved → {hit_path}')

# ── Cell 11: Train K Models (3.5 / 4.5 / 5.5) ────────────────────────────────
print('\n══ Training K models ══')

k_metrics = {}
for line in (3.5, 4.5, 5.5):
    label_col = f'k_over_{line}'
    if label_col not in pit_game_df.columns:
        print(f'  K>{line}: label column missing, skipping')
        continue

    train_k = pit_game_df[pit_game_df['season'] != TEST_YEAR].copy()
    test_k  = pit_game_df[pit_game_df['season'] == TEST_YEAR].copy()

    X_train_k = train_k[K_FEATURES].values.astype(np.float32)
    y_train_k = train_k[label_col].values
    X_test_k  = test_k[K_FEATURES].values.astype(np.float32)
    y_test_k  = test_k[label_col].values

    pos_ratio_k = (y_train_k == 0).sum() / max(1, (y_train_k == 1).sum())

    k_model_raw = XGBClassifier(
        **XGB_PARAMS,
        scale_pos_weight=pos_ratio_k,
        use_label_encoder=False,
    )
    k_model = CalibratedClassifierCV(k_model_raw, method='sigmoid', cv=5)
    k_model.fit(X_train_k, y_train_k)

    if len(y_test_k) > 0 and y_test_k.sum() > 0:
        p_test_k  = k_model.predict_proba(X_test_k)[:, 1]
        auc_k     = roc_auc_score(y_test_k, p_test_k)
        ll_k      = log_loss(y_test_k, p_test_k)
        brier_k   = brier_score_loss(y_test_k, p_test_k)
        print(f'  K>{line}  |  AUC: {auc_k:.4f}  |  LogLoss: {ll_k:.4f}  |  Brier: {brier_k:.4f}')
        k_metrics[f'k_{line}'] = dict(auc=auc_k, logloss=ll_k, brier=brier_k)
    else:
        print(f'  K>{line} trained (no 2025 test data)')
        k_metrics[f'k_{line}'] = {}

    safe_line = str(line).replace('.', '_')
    kpath = os.path.join(OUTDIR, f'xgb_k_{safe_line}.pkl')
    with open(kpath, 'wb') as f:
        pickle.dump(k_model, f)
    print(f'  Saved → {kpath}')

# ── Cell 12: Feature Importance (SHAP) ───────────────────────────────────────
print('\n══ SHAP feature importance ══')
try:
    import shap
    # Use the base XGB model inside the calibrated wrapper
    base_hit = hit_model.calibrated_classifiers_[0].estimator
    explainer = shap.TreeExplainer(base_hit)
    # Sample 2000 rows for speed
    sample_idx = np.random.choice(len(X_train_h), min(2000, len(X_train_h)), replace=False)
    shap_vals  = explainer.shap_values(X_train_h[sample_idx])
    mean_shap  = np.abs(shap_vals).mean(axis=0)
    importance = sorted(zip(HITS_FEATURES, mean_shap), key=lambda x: x[1], reverse=True)
    print('  Hit model top features:')
    for feat, imp in importance[:10]:
        bar = '█' * int(imp / importance[0][1] * 20)
        print(f'    {feat:<22} {bar} {imp:.4f}')
except Exception as e:
    print(f'  SHAP skipped: {e}')

# ── Cell 13: Save metadata ────────────────────────────────────────────────────
print('\n══ Saving metadata ══')

feat_cols = {
    'hits':  HITS_FEATURES,
    'k_3.5': K_FEATURES,
    'k_4.5': K_FEATURES,
    'k_5.5': K_FEATURES,
}
with open(os.path.join(OUTDIR, 'xgb_feature_cols.json'), 'w') as f:
    json.dump(feat_cols, f, indent=2)
print(f'  Saved → {OUTDIR}/xgb_feature_cols.json')

metrics = {
    'trainedAt':  datetime.utcnow().isoformat() + 'Z',
    'seasons':    SEASONS,
    'testYear':   TEST_YEAR,
    'hits': dict(
        auc=round(auc_h, 4) if auc_h else None,
        logloss=round(ll_h, 4) if ll_h else None,
        brier=round(brier_h, 4) if brier_h else None,
        trainRows=int(len(X_train_h)),
        testRows=int(len(X_test_h)),
        features=HITS_FEATURES,
    ),
    'strikeouts': k_metrics,
}
with open(os.path.join(OUTDIR, 'model_metrics.json'), 'w') as f:
    json.dump(metrics, f, indent=2)
print(f'  Saved → {OUTDIR}/model_metrics.json')

print('\n✅  All models saved to', OUTDIR)
print('    Copy the models/ folder to your mlb-analytics-hub repo root.')
print('    App will auto-load on next restart (xgb_prop_scorer.py).')
