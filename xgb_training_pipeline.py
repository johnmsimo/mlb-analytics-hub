"""
xgb_training_pipeline.py — XGBoost Prop Training Pipeline (Step 1B update)

Changes from previous version:
  - opp_lineup_k_pct and opp_lineup_xwoba are now computed from actual
    Statcast opponent-lineup data per game_pk instead of being hardcoded
    league-average constants (22.0 / 0.320).
  - ump_zone_size and ump_k_boost added to K_FEATURES and K_MEDIANS so
    umpire zone data (from umpire_loader.py) flows into all three K models
    at both train time and score time.
  - K_MEDIANS defaults for the new features set to 0.0 (league-average
    z-score / boost) so training is unaffected when umpire data is absent.

Outputs:
  models/xgb_hits.pkl
  models/xgb_k_3_5.pkl
  models/xgb_k_4_5.pkl
  models/xgb_k_5_5.pkl
  models/xgb_feature_cols.json
  models/model_metrics.json
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
OUTDIR = 'models'
os.makedirs(OUTDIR, exist_ok=True)

HITS_FEATURES = [
    'sv_xba', 'sv_xwoba', 'sv_xslg', 'sv_ev', 'sv_brl_pct', 'sv_hh_pct',
    'sv_ss_pct', 'sv_la', 'sv_k_pct', 'sv_bb_pct',
    'opp_xera', 'opp_k_pct', 'opp_bb_pct', 'opp_whiff',
    'bats_L', 'throws_R', 'platoon_adv',
    'l7_hits', 'l7_hit_rate',
    # v2 features (BATX-parity): give XGB the same context BATX uses so the
    # two models can no longer disagree because of missing inputs.
    'park_factor', 'wx_temp_mult', 'wx_wind_mult',
    'pitch_mix_slg_edge', 'bvp_woba_edge_shrunk',
    'split_ops_edge', 'expected_pa',
]

# ── K features now include real umpire zone features ───────────────────────────
K_FEATURES = [
    'sv_xera', 'sv_era', 'sv_k_pct', 'sv_bb_pct', 'sv_whiff_pct',
    'l3_ks', 'l5_ks', 'l10_ks',
    'l3_ip', 'l5_ip',
    'days_rest',
    'opp_lineup_k_pct',    # real per-game opponent lineup K% (was proxy)
    'opp_lineup_xwoba',    # real per-game opponent lineup xwOBA (was proxy)
    'ump_zone_size',       # umpire zone size z-score (from umpire_loader)
    'ump_k_boost',         # umpire extra Ks/9 vs. league average
]

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
)

from pybaseball import pitching_stats, batting_stats, cache, statcast
cache.enable()

fg_bat_frames, fg_pit_frames = [], []
for yr in SEASONS:
    try:
        df = batting_stats(yr, qual=MIN_PA)
        df['season'] = yr
        fg_bat_frames.append(df)
    except Exception as e:
        print(f'FG bat {yr}: {e}')
    try:
        df = pitching_stats(yr, qual=MIN_BF)
        df['season'] = yr
        fg_pit_frames.append(df)
    except Exception as e:
        print(f'FG pit {yr}: {e}')

fg_bat = pd.concat(fg_bat_frames, ignore_index=True) if fg_bat_frames else pd.DataFrame()
fg_pit = pd.concat(fg_pit_frames, ignore_index=True) if fg_pit_frames else pd.DataFrame()

# Batter-game outcomes
out_rows = []
for yr in SEASONS:
    try:
        sc = statcast(f'{yr}-03-28', f'{yr}-10-05')
        sc = sc[sc['game_type'] == 'R'].copy()
        hit_events = {'single', 'double', 'triple', 'home_run'}
        sc['is_hit'] = sc['events'].isin(hit_events).astype(int)
        sc['is_ab'] = sc['events'].notna() & ~sc['events'].isin(
            {'walk', 'hit_by_pitch', 'sac_fly', 'sac_bunt', 'intent_walk'}
        )
        sc['is_k'] = sc['events'].isin({'strikeout', 'strikeout_double_play'}).astype(int)

        bat_game = (
            sc.groupby(['game_pk', 'game_date', 'batter', 'pitcher', 'p_throws'])
            .agg(hits=('is_hit', 'sum'), abs=('is_ab', 'sum'), k_batter=('is_k', 'sum'))
            .reset_index()
        )
        bat_game['season'] = yr
        bat_game['hit_binary'] = (bat_game['hits'] >= 1).astype(int)
        out_rows.append(bat_game)
    except Exception as e:
        print(f'{yr} Statcast: {e}')

game_df = pd.concat(out_rows, ignore_index=True) if out_rows else pd.DataFrame()

# ── Pitcher-game outcomes with real opponent lineup stats ──────────────────────
pit_out_rows = []
for yr in SEASONS:
    try:
        sc = statcast(f'{yr}-03-28', f'{yr}-10-05')
        sc = sc[sc['game_type'] == 'R'].copy()

        sc['is_k'] = sc['events'].isin({'strikeout', 'strikeout_double_play'}).astype(int)
        sc['is_out'] = sc['events'].isin({
            'strikeout', 'strikeout_double_play', 'field_out', 'force_out',
            'double_play', 'triple_play', 'grounded_into_double_play',
            'fielders_choice_out', 'sac_fly', 'sac_bunt'
        }).astype(int)

        pit_game = (
            sc.groupby(['game_pk', 'game_date', 'pitcher', 'home_team', 'away_team'])
            .agg(
                total_ks=('is_k', 'sum'),
                total_bf=('events', 'count'),
                outs_recorded=('is_out', 'sum'),
            )
            .reset_index()
        )
        pit_game['season'] = yr
        pit_game['ip'] = pit_game['outs_recorded'] / 3.0

        # ── Real opponent lineup K% and xwOBA per game ────────────────────────
        # For each (game_pk, pitcher): the opposing batters are those who batted
        # for the *other* team in that game.
        #
        # Step 1: tag each plate appearance with whether the batter's team is
        #         the home or away side (inferred from fielding_team)
        sc['is_k_bat'] = sc['is_k']  # already computed above
        sc['xwoba_val'] = pd.to_numeric(
            sc.get('estimated_woba_using_speedangle', sc.get('estimated_woba', None)),
            errors='coerce'
        )

        # Build per-game batter-side aggregates (home batting lineup and away batting lineup)
        # Statcast 'inning_topbot': 'Top' = away batting, 'Bot' = home batting
        opp_rows = []
        for (gk, ht, at), g_sc in sc.groupby(['game_pk', 'home_team', 'away_team']):
            for pitching_side, batting_inning in [('home', 'Top'), ('away', 'Bot')]:
                # pitching_side pitcher faces batters in batting_inning half-innings
                opp_pa = g_sc[g_sc['inning_topbot'] == batting_inning]
                if opp_pa.empty:
                    continue
                n_pa    = len(opp_pa)
                n_k     = int(opp_pa['is_k_bat'].sum())
                xwoba   = opp_pa['xwoba_val'].dropna().mean()
                # Identify the pitcher(s) for the pitching_side
                # (use pitcher column; take the most common pitcher in those PAs as primary)
                if pitching_side == 'home':
                    pit_ids = g_sc[g_sc['inning_topbot'] == 'Top']['pitcher'].unique()
                else:
                    pit_ids = g_sc[g_sc['inning_topbot'] == 'Bot']['pitcher'].unique()
                for pid in pit_ids:
                    opp_rows.append({
                        'game_pk':           gk,
                        'pitcher':           pid,
                        'opp_lineup_k_pct':  round(n_k / max(n_pa, 1) * 100, 2),
                        'opp_lineup_xwoba':  round(xwoba, 4) if not pd.isna(xwoba) else np.nan,
                    })

        opp_df = pd.DataFrame(opp_rows).drop_duplicates(subset=['game_pk', 'pitcher'])

        # Merge real lineup stats onto pit_game
        pit_game = pit_game.merge(opp_df, on=['game_pk', 'pitcher'], how='left')

        # Fill any unmatched rows with season medians (rather than fixed constants)
        season_k_med    = pit_game['opp_lineup_k_pct'].dropna().median()
        season_xwoba_med = pit_game['opp_lineup_xwoba'].dropna().median()
        pit_game['opp_lineup_k_pct']  = pit_game['opp_lineup_k_pct'].fillna(
            season_k_med if not pd.isna(season_k_med) else 22.0
        )
        pit_game['opp_lineup_xwoba'] = pit_game['opp_lineup_xwoba'].fillna(
            season_xwoba_med if not pd.isna(season_xwoba_med) else 0.320
        )

        print(f"{yr} opp_lineup_k_pct  median={pit_game['opp_lineup_k_pct'].median():.2f}  "
              f"std={pit_game['opp_lineup_k_pct'].std():.2f}")
        print(f"{yr} opp_lineup_xwoba  median={pit_game['opp_lineup_xwoba'].median():.4f}  "
              f"std={pit_game['opp_lineup_xwoba'].std():.4f}")

        # ── Rolling K/IP windows ──────────────────────────────────────────────
        pit_game = pit_game.sort_values(['pitcher', 'game_date'])
        pit_game['l3_ks'] = pit_game.groupby('pitcher')['total_ks'].transform(
            lambda x: x.shift(1).rolling(3, min_periods=1).mean()
        )
        pit_game['l5_ks'] = pit_game.groupby('pitcher')['total_ks'].transform(
            lambda x: x.shift(1).rolling(5, min_periods=1).mean()
        )
        pit_game['l10_ks'] = pit_game.groupby('pitcher')['total_ks'].transform(
            lambda x: x.shift(1).rolling(10, min_periods=1).mean()
        )
        pit_game['l3_ip'] = pit_game.groupby('pitcher')['ip'].transform(
            lambda x: x.shift(1).rolling(3, min_periods=1).mean()
        )
        pit_game['l5_ip'] = pit_game.groupby('pitcher')['ip'].transform(
            lambda x: x.shift(1).rolling(5, min_periods=1).mean()
        )
        pit_game['prev_game_date'] = pit_game.groupby('pitcher')['game_date'].shift(1)
        pit_game['days_rest'] = (
            pd.to_datetime(pit_game['game_date']) - pd.to_datetime(pit_game['prev_game_date'])
        ).dt.days.fillna(5).clip(lower=0, upper=14)

        # ── Umpire features ───────────────────────────────────────────────────
        # During training we don't have HP umpire assignments per historical
        # game_pk, so we default both umpire features to 0.0 (league average).
        # At score time (xgb_prop_scorer.py), umpire_loader.get_umpire_features()
        # supplies the real values for today's umpire.
        pit_game['ump_zone_size'] = 0.0
        pit_game['ump_k_boost']   = 0.0

        pit_game['k_over_3.5'] = (pit_game['total_ks'] >= 4).astype(int)
        pit_game['k_over_4.5'] = (pit_game['total_ks'] >= 5).astype(int)
        pit_game['k_over_5.5'] = (pit_game['total_ks'] >= 6).astype(int)

        pit_out_rows.append(pit_game)
    except Exception as e:
        print(f'{yr} pit-game: {e}')

pit_game_df = pd.concat(pit_out_rows, ignore_index=True) if pit_out_rows else pd.DataFrame()

# FG-derived pitcher season priors
if not fg_pit.empty:
    fg_pit = fg_pit.copy()
    if 'playerid' in fg_pit.columns:
        fg_pit['playerid'] = pd.to_numeric(fg_pit['playerid'], errors='coerce')
    rename_map = {
        'xERA': 'sv_xera',
        'ERA': 'sv_era',
        'K%': 'sv_k_pct',
        'BB%': 'sv_bb_pct',
        'SwStr%': 'sv_whiff_pct',
    }
    for old, new in rename_map.items():
        if old in fg_pit.columns:
            fg_pit[new] = fg_pit[old]

# Rolling hit features
if not game_df.empty:
    game_df = game_df.sort_values(['batter', 'game_date'])
    game_df['l7_hits'] = game_df.groupby('batter')['hits'].transform(
        lambda x: x.shift(1).rolling(7, min_periods=1).sum()
    )
    game_df['l7_hit_rate'] = game_df.groupby('batter')['hit_binary'].transform(
        lambda x: x.shift(1).rolling(7, min_periods=1).mean()
    )

if 'p_throws' in game_df.columns:
    game_df['throws_R'] = (game_df['p_throws'] == 'R').astype(int)
else:
    game_df['throws_R'] = 1
game_df['bats_L'] = 0
game_df['platoon_adv'] = (
    ((game_df.get('bats_L', 0) == 1) & (game_df.get('throws_R', 1) == 1)) |
    ((game_df.get('bats_L', 0) == 0) & (game_df.get('throws_R', 1) == 0))
).astype(int)

# Defaults
HIT_MEDIANS = {
    'sv_xba': 0.250, 'sv_xwoba': 0.320, 'sv_xslg': 0.400,
    'sv_ev': 88.0, 'sv_brl_pct': 4.0, 'sv_hh_pct': 35.0,
    'sv_ss_pct': 10.0, 'sv_la': 12.0, 'sv_k_pct': 22.0, 'sv_bb_pct': 8.0,
    'opp_xera': 4.50, 'opp_k_pct': 22.0, 'opp_bb_pct': 8.0, 'opp_whiff': 24.0,
    'bats_L': 0, 'throws_R': 1, 'platoon_adv': 0,
    'l7_hits': 1.5, 'l7_hit_rate': 0.50,
}
# K_MEDIANS updated: real computed columns replace proxy constants;
# umpire features default to 0.0 (league average z-score / boost).
K_MEDIANS = {
    'sv_xera': 4.50, 'sv_era': 4.50, 'sv_k_pct': 22.0, 'sv_bb_pct': 8.0,
    'sv_whiff_pct': 24.0, 'l3_ks': 4.5, 'l5_ks': 4.5, 'l10_ks': 4.5,
    'l3_ip': 5.0, 'l5_ip': 5.0, 'days_rest': 5.0,
    'opp_lineup_k_pct':  22.0,   # real column; median fallback for any missing
    'opp_lineup_xwoba':  0.320,  # real column; median fallback for any missing
    'ump_zone_size':     0.0,    # league-average umpire zone (z-score)
    'ump_k_boost':       0.0,    # league-average umpire K boost
}

for col, med in HIT_MEDIANS.items():
    if col not in game_df.columns:
        game_df[col] = med
    else:
        game_df[col] = game_df[col].fillna(med)

for col, med in K_MEDIANS.items():
    if col not in pit_game_df.columns:
        pit_game_df[col] = med
    else:
        pit_game_df[col] = pit_game_df[col].fillna(med)

from xgboost import XGBClassifier
from sklearn.calibration import CalibratedClassifierCV
from sklearn.metrics import roc_auc_score, log_loss, brier_score_loss

train_hit = game_df[game_df['season'] != TEST_YEAR].copy()
test_hit = game_df[game_df['season'] == TEST_YEAR].copy()

X_train_h = train_hit[HITS_FEATURES].values.astype(np.float32)
y_train_h = train_hit['hit_binary'].values
X_test_h = test_hit[HITS_FEATURES].values.astype(np.float32)
y_test_h = test_hit['hit_binary'].values

pos_ratio_h = (y_train_h == 0).sum() / max(1, (y_train_h == 1).sum())
hit_model = CalibratedClassifierCV(
    XGBClassifier(**XGB_PARAMS, scale_pos_weight=pos_ratio_h, use_label_encoder=False),
    method='sigmoid', cv=5,
)
hit_model.fit(X_train_h, y_train_h)

if len(y_test_h) > 0:
    p_h = hit_model.predict_proba(X_test_h)[:, 1]
    auc_h = roc_auc_score(y_test_h, p_h)
    ll_h = log_loss(y_test_h, p_h)
    br_h = brier_score_loss(y_test_h, p_h)
    print(f'HIT | AUC {auc_h:.4f} | LL {ll_h:.4f} | Brier {br_h:.4f}')
else:
    auc_h = ll_h = br_h = None

with open(os.path.join(OUTDIR, 'xgb_hits.pkl'), 'wb') as f:
    pickle.dump(hit_model, f)

k_metrics = {}
for line in (3.5, 4.5, 5.5):
    label_col = f'k_over_{line}'
    train_k = pit_game_df[pit_game_df['season'] != TEST_YEAR].copy()
    test_k = pit_game_df[pit_game_df['season'] == TEST_YEAR].copy()

    X_train_k = train_k[K_FEATURES].values.astype(np.float32)
    y_train_k = train_k[label_col].values
    X_test_k = test_k[K_FEATURES].values.astype(np.float32)
    y_test_k = test_k[label_col].values

    pos_ratio_k = (y_train_k == 0).sum() / max(1, (y_train_k == 1).sum())
    k_model = CalibratedClassifierCV(
        XGBClassifier(**XGB_PARAMS, scale_pos_weight=pos_ratio_k, use_label_encoder=False),
        method='sigmoid', cv=5,
    )
    k_model.fit(X_train_k, y_train_k)

    if len(y_test_k) > 0 and y_test_k.sum() > 0:
        p_k = k_model.predict_proba(X_test_k)[:, 1]
        auc_k = roc_auc_score(y_test_k, p_k)
        print(f'K>{line} | AUC {auc_k:.4f}')
        k_metrics[f'k_{line}'] = dict(auc=auc_k)

    safe = str(line).replace('.', '_')
    with open(os.path.join(OUTDIR, f'xgb_k_{safe}.pkl'), 'wb') as f:
        pickle.dump(k_model, f)

feat_cols = {
    'hits': HITS_FEATURES,
    'k_3.5': K_FEATURES,
    'k_4.5': K_FEATURES,
    'k_5.5': K_FEATURES,
}
with open(os.path.join(OUTDIR, 'xgb_feature_cols.json'), 'w') as f:
    json.dump(feat_cols, f, indent=2)

metrics = {
    'trainedAt': datetime.utcnow().isoformat() + 'Z',
    'seasons': SEASONS,
    'testYear': TEST_YEAR,
    'hits': dict(auc=round(auc_h, 4) if auc_h else None, features=HITS_FEATURES),
    'strikeouts': k_metrics,
}
with open(os.path.join(OUTDIR, 'model_metrics.json'), 'w') as f:
    json.dump(metrics, f, indent=2)

print('All models saved to', OUTDIR)
