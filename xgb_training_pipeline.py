"""
xgb_training_pipeline.py — XGBoost Prop Training Pipeline (Colab / local)

Outputs:
  models/xgb_hits.pkl
  models/xgb_k_3_5.pkl  models/xgb_k_4_5.pkl  models/xgb_k_5_5.pkl
  models/xgb_feature_cols.json
  models/model_metrics.json

Install cell:
  !pip install pybaseball xgboost scikit-learn shap imbalanced-learn -q
"""

import os, json, pickle, warnings
import numpy as np
import pandas as pd
from datetime import datetime
warnings.filterwarnings('ignore')

SEASONS   = [2021, 2022, 2023, 2024, 2025]
MIN_PA    = 50
MIN_BF    = 50
TEST_YEAR = 2025
N_FOLDS   = 5

XGB_PARAMS = dict(
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
)  # FIX: was missing closing )

OUTDIR = 'models'
os.makedirs(OUTDIR, exist_ok=True)

HITS_FEATURES = [
    'sv_xba', 'sv_xwoba', 'sv_xslg', 'sv_ev', 'sv_brl_pct', 'sv_hh_pct',
    'sv_ss_pct', 'sv_la', 'sv_k_pct', 'sv_bb_pct',
    'opp_xera', 'opp_k_pct', 'opp_bb_pct', 'opp_whiff',
    'bats_L', 'throws_R', 'platoon_adv',
    'l7_hits', 'l7_hit_rate',
]  # FIX: was missing closing ]

K_FEATURES = [
    'sv_xera', 'sv_era', 'sv_k_pct', 'sv_bb_pct', 'sv_whiff',
    'l5_ks', 'l5_k_rate', 'l10_ks',
    'opp_lineup_k_pct', 'opp_lineup_xwoba',
]  # FIX: was missing closing ]

# ── Pull FanGraphs leaderboards ───────────────────────────────────────────────
from pybaseball import pitching_stats, batting_stats, cache
cache.enable()

fg_bat_frames, fg_pit_frames = [], []
for yr in SEASONS:
    try:
        df = batting_stats(yr, qual=MIN_PA); df['season'] = yr; fg_bat_frames.append(df)
    except Exception as e:
        print(f'FG bat {yr}: {e}')
    try:
        df = pitching_stats(yr, qual=MIN_BF); df['season'] = yr; fg_pit_frames.append(df)
    except Exception as e:
        print(f'FG pit {yr}: {e}')

fg_bat = pd.concat(fg_bat_frames, ignore_index=True) if fg_bat_frames else pd.DataFrame()
fg_pit = pd.concat(fg_pit_frames, ignore_index=True) if fg_pit_frames else pd.DataFrame()

# ── Pull per-game Statcast ────────────────────────────────────────────────────
from pybaseball import statcast

out_rows = []
for yr in SEASONS:
    try:
        sc = statcast(f'{yr}-03-28', f'{yr}-10-05')
        sc = sc[sc['game_type'] == 'R'].copy()
        hit_events = {'single', 'double', 'triple', 'home_run'}
        sc['is_hit'] = sc['events'].isin(hit_events).astype(int)
        sc['is_ab']  = sc['events'].notna() & ~sc['events'].isin(
            {'walk', 'hit_by_pitch', 'sac_fly', 'sac_bunt', 'intent_walk'}
        )  # FIX: was missing closing ) on .isin(
        sc['is_k']   = sc['events'].isin({'strikeout', 'strikeout_double_play'}).astype(int)
        bat_game = (
            sc.groupby(['game_pk', 'game_date', 'batter', 'pitcher', 'p_throws'])
            .agg(hits=('is_hit', 'sum'), abs=('is_ab', 'sum'), k_batter=('is_k', 'sum'))
            .reset_index()
        )  # FIX: was missing closing )
        bat_game['season']     = yr
        bat_game['hit_binary'] = (bat_game['hits'] >= 1).astype(int)
        out_rows.append(bat_game)
    except Exception as e:
        print(f'{yr} Statcast: {e}')

game_df = pd.concat(out_rows, ignore_index=True) if out_rows else pd.DataFrame()

# ── Pitcher-game K outcomes ───────────────────────────────────────────────────
pit_out_rows = []
for yr in SEASONS:
    try:
        sc = statcast(f'{yr}-03-28', f'{yr}-10-05')
        sc = sc[sc['game_type'] == 'R'].copy()
        sc['is_k'] = sc['events'].isin({'strikeout', 'strikeout_double_play'}).astype(int)
        pit_game = (
            sc.groupby(['game_pk', 'game_date', 'pitcher'])
            .agg(total_ks=('is_k', 'sum'), total_bf=('events', 'count'))
            .reset_index()
        )  # FIX: was missing closing )
        # opp context: FIX — opp_lineup_xwoba was never computed before
        opp_agg = (
            sc.groupby(['game_pk', 'pitcher'])
            .agg(
                opp_k_events=('is_k', 'sum'),
                opp_pa=('events', 'count'),
                opp_xwoba_sum=('estimated_woba_using_speedangle', 'sum'),
            )  # FIX: was missing closing )
            .reset_index()
        )
        opp_agg['opp_lineup_k_pct'] = opp_agg['opp_k_events'] / opp_agg['opp_pa'].clip(lower=1)
        opp_agg['opp_lineup_xwoba'] = opp_agg['opp_xwoba_sum'] / opp_agg['opp_pa'].clip(lower=1)
        pit_game = pit_game.merge(
            opp_agg[['game_pk', 'pitcher', 'opp_lineup_k_pct', 'opp_lineup_xwoba']],
            on=['game_pk', 'pitcher'], how='left',
        )
        pit_game['season'] = yr
        pit_out_rows.append(pit_game)
    except Exception as e:
        print(f'{yr} pit-game: {e}')

pit_game_df = pd.concat(pit_out_rows, ignore_index=True) if pit_out_rows else pd.DataFrame()

# ── Rolling form features ─────────────────────────────────────────────────────
if not game_df.empty:
    game_df = game_df.sort_values(['batter', 'game_date'])
    game_df['l7_hits'] = game_df.groupby('batter')['hits'].transform(
        lambda x: x.shift(1).rolling(7, min_periods=1).sum()
    )  # FIX: was missing closing )
    game_df['l7_hit_rate'] = game_df.groupby('batter')['hit_binary'].transform(
        lambda x: x.shift(1).rolling(7, min_periods=1).mean()
    )  # FIX: was missing closing )

if not pit_game_df.empty:
    pit_game_df = pit_game_df.sort_values(['pitcher', 'game_date'])
    pit_game_df['l5_ks'] = pit_game_df.groupby('pitcher')['total_ks'].transform(
        lambda x: x.shift(1).rolling(5, min_periods=1).mean()
    )  # FIX: was missing closing )
    pit_game_df['l10_ks'] = pit_game_df.groupby('pitcher')['total_ks'].transform(
        lambda x: x.shift(1).rolling(10, min_periods=1).mean()
    )  # FIX: was missing closing )
    _bf_roll = pit_game_df.groupby('pitcher')['total_bf'].transform(
        lambda x: x.shift(1).rolling(5, min_periods=1).mean()
    ).clip(lower=1)
    pit_game_df['l5_k_rate'] = (
        pit_game_df.groupby('pitcher')['total_ks'].transform(
            lambda x: x.shift(1).rolling(5, min_periods=1).mean()
        ) / _bf_roll
    )

# ── FanGraphs column rename map ───────────────────────────────────────────────
# FIX: 'Barrels' → 'Barrel%' (correct FanGraphs column name)
FG_BAT_MAP = {
    'xBA': 'sv_xba', 'xwOBA': 'sv_xwoba', 'xSLG': 'sv_xslg',
    'EV': 'sv_ev', 'Barrel%': 'sv_brl_pct', 'HardHit%': 'sv_hh_pct',
    'SwStr%': 'sv_ss_pct', 'LA': 'sv_la', 'K%': 'sv_k_pct', 'BB%': 'sv_bb_pct',
}  # FIX: was missing closing }

FG_PIT_MAP = {
    'xERA': 'opp_xera', 'ERA': 'opp_era', 'K%': 'opp_k_pct',
    'BB%': 'opp_bb_pct', 'SwStr%': 'opp_whiff',
}  # FIX: was missing closing }

# ── Handedness / platoon flags ────────────────────────────────────────────────
if 'p_throws' in game_df.columns:
    game_df['throws_R'] = (game_df['p_throws'] == 'R').astype(int)
else:
    game_df['throws_R'] = 1
if 'stand' in game_df.columns:
    game_df['bats_L'] = (game_df['stand'] == 'L').astype(int)
else:
    game_df['bats_L'] = 0
game_df['platoon_adv'] = (
    ((game_df.get('bats_L', 0) == 1) & (game_df.get('throws_R', 1) == 1)) |
    ((game_df.get('bats_L', 0) == 0) & (game_df.get('throws_R', 1) == 0))
).astype(int)

# ── Fill missing with league medians ─────────────────────────────────────────
HIT_MEDIANS = {
    'sv_xba': 0.250, 'sv_xwoba': 0.320, 'sv_xslg': 0.400,
    'sv_ev': 88.0, 'sv_brl_pct': 4.0, 'sv_hh_pct': 35.0,
    'sv_ss_pct': 10.0, 'sv_la': 12.0, 'sv_k_pct': 22.0, 'sv_bb_pct': 8.0,
    'opp_xera': 4.50, 'opp_k_pct': 22.0, 'opp_bb_pct': 8.0, 'opp_whiff': 24.0,
    'bats_L': 0, 'throws_R': 1, 'platoon_adv': 0,
    'l7_hits': 1.5, 'l7_hit_rate': 0.50,
}  # FIX: was missing closing }

K_MEDIANS = {
    'sv_xera': 4.50, 'sv_era': 4.50, 'sv_k_pct': 22.0, 'sv_bb_pct': 8.0,
    'sv_whiff': 24.0, 'l5_ks': 4.5, 'l5_k_rate': 22.0, 'l10_ks': 4.5,
    'opp_lineup_k_pct': 22.0, 'opp_lineup_xwoba': 0.320,
}  # FIX: was missing closing }

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

for line in (3.5, 4.5, 5.5):
    pit_game_df[f'k_over_{line}'] = (pit_game_df['total_ks'] > line).astype(int)

# ── Train Hit Model ───────────────────────────────────────────────────────────
from xgboost import XGBClassifier
from sklearn.calibration import CalibratedClassifierCV
from sklearn.metrics import roc_auc_score, log_loss, brier_score_loss

train_hit = game_df[game_df['season'] != TEST_YEAR].copy()
test_hit  = game_df[game_df['season'] == TEST_YEAR].copy()

X_train_h = train_hit[HITS_FEATURES].values.astype(np.float32)
y_train_h = train_hit['hit_binary'].values
X_test_h  = test_hit[HITS_FEATURES].values.astype(np.float32)
y_test_h  = test_hit['hit_binary'].values

pos_ratio_h = (y_train_h == 0).sum() / max(1, (y_train_h == 1).sum())
hit_model = CalibratedClassifierCV(
    XGBClassifier(**XGB_PARAMS, scale_pos_weight=pos_ratio_h, use_label_encoder=False),
    method='sigmoid', cv=5,
)
hit_model.fit(X_train_h, y_train_h)

if len(y_test_h) > 0:
    p_h   = hit_model.predict_proba(X_test_h)[:, 1]
    auc_h = roc_auc_score(y_test_h, p_h)
    ll_h  = log_loss(y_test_h, p_h)
    br_h  = brier_score_loss(y_test_h, p_h)
    print(f'HIT | AUC {auc_h:.4f} | LL {ll_h:.4f} | Brier {br_h:.4f}')
else:
    auc_h = ll_h = br_h = None

with open(os.path.join(OUTDIR, 'xgb_hits.pkl'), 'wb') as f:
    pickle.dump(hit_model, f)

# ── Train K Models ────────────────────────────────────────────────────────────
k_metrics = {}
for line in (3.5, 4.5, 5.5):
    label_col = f'k_over_{line}'
    train_k = pit_game_df[pit_game_df['season'] != TEST_YEAR].copy()
    test_k  = pit_game_df[pit_game_df['season'] == TEST_YEAR].copy()
    X_train_k = train_k[K_FEATURES].values.astype(np.float32)
    y_train_k = train_k[label_col].values
    X_test_k  = test_k[K_FEATURES].values.astype(np.float32)
    y_test_k  = test_k[label_col].values
    pos_ratio_k = (y_train_k == 0).sum() / max(1, (y_train_k == 1).sum())
    k_model = CalibratedClassifierCV(
        XGBClassifier(**XGB_PARAMS, scale_pos_weight=pos_ratio_k, use_label_encoder=False),
        method='sigmoid', cv=5,
    )  # FIX: was missing closing )
    k_model.fit(X_train_k, y_train_k)
    if len(y_test_k) > 0 and y_test_k.sum() > 0:
        p_k   = k_model.predict_proba(X_test_k)[:, 1]
        auc_k = roc_auc_score(y_test_k, p_k)
        print(f'K>{line} | AUC {auc_k:.4f}')
        k_metrics[f'k_{line}'] = dict(auc=auc_k)
    safe = str(line).replace('.', '_')
    with open(os.path.join(OUTDIR, f'xgb_k_{safe}.pkl'), 'wb') as f:
        pickle.dump(k_model, f)

# ── Save metadata ─────────────────────────────────────────────────────────────
feat_cols = {'hits': HITS_FEATURES, 'k_3.5': K_FEATURES, 'k_4.5': K_FEATURES, 'k_5.5': K_FEATURES}  # FIX: was missing closing }
with open(os.path.join(OUTDIR, 'xgb_feature_cols.json'), 'w') as f:
    json.dump(feat_cols, f, indent=2)

metrics = {
    'trainedAt': datetime.utcnow().isoformat() + 'Z',
    'seasons': SEASONS, 'testYear': TEST_YEAR,
    'hits': dict(auc=round(auc_h, 4) if auc_h else None, features=HITS_FEATURES),
    'strikeouts': k_metrics,
}  # FIX: was missing closing }
with open(os.path.join(OUTDIR, 'model_metrics.json'), 'w') as f:
    json.dump(metrics, f, indent=2)

print('All models saved to', OUTDIR)
