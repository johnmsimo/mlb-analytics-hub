# ============================================================
# MLB XGBOOST PROP MODEL TRAINING PIPELINE
# Markets: Batter Hits OVER 0.5 | Pitcher Strikeouts
# Compatible with MLB Analytics Hub (app.py)
# ============================================================
# CELL 1 â€” Install & Imports
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

print("âœ… Imports done")
print(f"   XGBoost {xgb.__version__}  |  Seasons: {SEASONS[0]}â€“{SEASONS[-1]}")


# ============================================================
# CELL 2 â€” Data Fetch: Statcast Batter Season Agg + Pitcher
# ============================================================
def fetch_statcast_batters(season):
    """Pull season-level batter Statcast leaderboard via pybaseball."""
    print(f"  Fetching Statcast batters {season}...")
    try:
        df = pb.statcast_batter_expected_stats(season)
        df["season"] = season
        return df
    except Exception as e:
        print(f"  âš ï¸  statcast_batter_expected_stats({season}) failed: {e}")
        return pd.DataFrame()

def fetch_statcast_pitchers(season):
    """Pull season-level pitcher Statcast leaderboard."""
    print(f"  Fetching Statcast pitchers {season}...")
    try:
        df = pb.statcast_pitcher_expected_stats(season)
        df["season"] = season
        return df
    except Exception as e:
        print(f"  âš ï¸  statcast_pitcher_expected_stats({season}) failed: {e}")
        return pd.DataFrame()

def fetch_fangraphs_batters(season):
    """Pull FanGraphs batting leaderboard."""
    print(f"  Fetching FanGraphs batters {season}...")
    try:
        df = pb.batting_stats(season, qual=PA_MIN)
        df["season"] = season
        return df
    except Exception as e:
        print(f"  âš ï¸  batting_stats({season}) failed: {e}")
        return pd.DataFrame()

def fetch_fangraphs_pitchers(season):
    """Pull FanGraphs pitching leaderboard."""
    print(f"  Fetching FanGraphs pitchers {season}...")
    try:
        df = pb.pitching_stats(season, qual=IP_MIN)
        df["season"] = season
        return df
    except Exception as e:
        print(f"  âš ï¸  pitching_stats({season}) failed: {e}")
        return pd.DataFrame()

def fetch_game_logs_batters(season):
    """Pull FanGraphs batter game logs for ground truth outcomes."""
    print(f"  Fetching batter game logs {season}...")
    try:
        df = pb.batting_stats_bref(season)
        df["season"] = season
        return df
    except Exception as e:
        print(f"  âš ï¸  batting_stats_bref({season}) failed: {e}")
        return pd.DataFrame()

# Fetch all seasons
print("\nðŸ“¥ Fetching season data...")
sv_bat_all  = pd.concat([fetch_statcast_batters(s)  for s in SEASONS], ignore_index=True)
sv_pit_all  = pd.concat([fetch_statcast_pitchers(s) for s in SEASONS], ignore_index=True)
fg_bat_all  = pd.concat([fetch_fangraphs_batters(s) for s in SEASONS], ignore_index=True)
fg_bat_all = fg_bat_all.drop_duplicates(subset=["name","season"],keep="first")
fg_pit_all  = pd.concat([fetch_fangraphs_pitchers(s) for s in SEASONS], ignore_index=True)

print(f"\nâœ…!ata fetched:")
print(f"   Statcast batters: {len(sv_bat_all):,} rows")
print(f"   Statcast pitchers: {len(sv_pit_all):,} rows")
print(f"   FanGraphs batters: {len(fg_bat_all):,} rows")
print(f"   FanGraphs pitchers: {len(fg_pit_all):(} rows")


# ============================================================
# CELL 3 â€” Game-Log Ground Truth (individual game outcomes)
# ============================================================
def fetch_statcast_game_logs(start_dt, end_dt):
    """
    Pull raw pitch-by-pitch Statcast for a date range, aggregate to
    per-game per-player outcome rows.
    Returns two DataFrames: batter_game_rows, pitcher_game_rows
    """
    print(f"  Pulling Statcast {start_dt} â†¢ {end_dt}...")
    try:
        sc = pb.statcast(start_dt=start_dt, end_dt=end_dt)
        sc = sc.dropna(subset=["batter", "pitcher", "game_pk"])
    except Exception as e:
        print(f"  âš ï¸  statcast pull failed: {b}")
        return pd.DataFrame(), pd.]Qœ˜[YJ
B‚ˆÈ8¥ 8¥ ˜]\ˆØ[YHYÙÜ™YØ][Ûˆ8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ ˆØÖÈš\×Ú]—HHØÖÈ™]™[È—Kš\Ú[ŠÊÈœÚ[™ÛH‹™ÝX›H‹š\H‹šÛYWÜ[ˆ—JK˜\Ý\J[
BˆØÖÈš\×ØXˆ—HHØÖÈ™]™[È—Kš\Ú[ŠˆÈœÚ[™ÛH‹™ÝX›H‹š\H‹šÛYWÜ[ˆ‹œÝšZÙ[Ý]‹™šY[ÛÝ]‹ˆ™Ü›Ý[™YÚ[×ÙÝX›WÜ^H‹™ÝX›WÜ^H‹™›Ü˜ÙWÛÝ]‹ˆ™šY[\œ×ØÚÚXÙH‹™šY[\œ×ØÚÚXÙWÛÝ]‹œÝšZÙ[Ý]ÙÝX›WÜ^H—JK˜\Ý\J[
BˆØÖÈš\×ÜH—HH
œØÖÈ™]™[È—Kš\Û˜J
JK˜\Ý\J[
BˆØÖÈš\×ÚÈ—HHØÖÈ™]™[È—Kš\Ú[ŠÈœÝšZÙ[Ý]‹œÝšZÙ[Ý]ÙÝX›WÜ^H—JK˜\Ý\J[
BˆØÖÈš\×Ø˜ˆ—HHØÖÈ™]™[È—Kš\Ú[ŠÈØ[È‹š[[ÝØ[È—JK˜\Ý\J[
BˆØÖÈš\×Úˆ—HH
ØÖÈ™]™[È—HOHšÛYWÜ[ˆŠK˜\Ý\J[
B‚ˆ˜]\—ÙØ[YHH
ØÖÜØÖÈš\×ÜH—HOHWBˆ™Ü›Ý\žJÈ™Ø[YWÜÈ‹™Ø[YWÙ]H‹˜˜]\ˆ—JBˆ˜YÙÊˆ]ÏJš\×Ú]‹œÝ[HŠKˆXJš\×ØXˆ‹œÝ[HŠKˆOJš\×ÜH‹œÝ[HŠKˆÏJš\×ÚÈ‹œÝ[HŠKˆ˜Jš\×Ø˜ˆ‹œÝ[HŠKˆJš\×Úˆ‹œÝ[HŠKˆÛYWÝX[OJšÛYWÝX[H‹™š\œÝŠKˆ]Ø^WÝX[OJ˜]Ø^WÝX[H‹™š\œÝŠKˆÝ›ÝÜÏJœÝ›ÝÜÈ‹™š\œÝŠKˆÝ[™JœÝ[™‹™š\œÝŠKˆ
Kœ™\Ù]Ú[™^

JBˆ˜]\—ÙØ[YVÈš]ÛÝ™\—ÌH—HH
˜]-\—ÙØ[YVÈš]È—HHJK˜\Ý\J[
Bˆ˜]-\—ÙØ[YVÈš]ÛÝ™\—ÌKH—HH
˜]\—ÙØ[YVÈš]È—HHŠK˜\Ý\J[
B‚ˆÈ8¥ 8¥ ]Ú\ˆØ[YHYÙÜ™YØ][Ûˆ8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ —ÜØÖÈš\×Ú×Ü—HHØÖÈ™]™[È—Kš\Ú[ŠÈœÝšZÙ[Ý]‹œÝšZÙ[Ý]ÙÝX›WÜ^H—JK˜\Ý\J[
BˆØÖÈš\×Ø™ˆ—HH
œØÖÈ™]™[È—Kš\Û˜J
JK˜\Ý\J[
B‚ˆ]Ú\—ÙØ[YHH
ØÖÜØÖÈš\×Ø™ˆ—HOHWBˆ™Ü›Ý\žJÈ™Ø[YWÜÈ‹™Ø[YWÙ]H‹œ]Ú\ˆ—JBˆ˜YÙÊˆÜÏJš\×Ú×Ü‹œÝ[HŠKˆ™Jš\×Ø™ˆ‹œÝ[HŠKˆÛYWÝX[OJšÛYWÝX[H‹™š\œÝŠKˆ]Ø^WÝX[OJ˜]Ø^WÝX[H‹™š\œÝŠKˆÝ[™ÛZ^JœÝ[™‹™š\œÝŠKˆ
Kœ™\Ù]Ú[™^

JBˆ]Ú\—ÙØ[YVÈš×ÛÝ™\—ÌËH—HH
]Ú\—ÙØ[YVÈšÜÈ—HH
K˜\Ý\J[
Bˆ]Ú\—ÙØ[YVÈš×ÛÝ™\—ÍH—HH
]Ú\—ÙØ[YVÈšÜÈ—HHJK˜\Ý\J[
Bˆ]Ú\—ÙØ[YVÈš×ÛÝ™\—ÍKH—HH
]Ú\—ÙØ[YVÈšÜÈ—HHŠK˜\Ý\J[
B‚ˆ™]\›ˆ˜]\—ÙØ[YK]Ú\—ÙØ[YB‚ˆÈ™]ÚÛ™HÙX\ÛÛˆÙˆØ[YHÙÜÈ
Œ\È^[\NÈ^[™›Üˆ[˜Z[š[™ÊBˆÈ›Üˆ[\[[™H[˜ÛÛ[Y[[ÙX\ÛÛœÈ[™ÛÛ˜Ø][˜]Bœš[
—¼'äéH™]Ú[™ÈØ[YHÙÜÈ
Œ8 %^[™È[ÙX\ÛÛœÈ›Üˆ[˜Z[š[™ÊK‹‹ˆŠB˜™×ÌŒ×ÌŒH™]ÚÜÝ]Ø\ÝÙØ[YWÛÙÜÊŒŒLËLŒ‹ŒŒLLLHŠBœš[
ˆˆ˜]\ˆØ[YH›ÝÜÎˆÛ[Š™×ÌŒ
N‹HŠBœš[
ˆˆ]Ú\ˆØ[YH›ÝÜÎˆÛ[Š×ÌŒ
N‹HŠB‚ˆÈØ]™H˜]ÈØ[YHÙÜÂ˜™×ÌŒ×ØÜÝŠˆžÓÕUUÑTŸKÜ˜]×Ø˜]\—ÙØ[YWÛÙÜ×ÌŒ˜ÜÝˆ‹[™^Q˜[ÙJBœ×ÌŒ×ØÜÝŠˆžÓÕUUÑTŸKÜ˜]×Ü]Ú\—ÙØ[YWÛÙÜ×ÌŒ˜ÜÝˆ‹[™^Q˜[ÙJBœš[
¸§!H˜]ÈØ[YHÙÜÈØ]™YŠB‚‚ˆÈOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOBˆÈÑS8 %™X]\™H[™Ú[™Y\š[™ÎˆUÈSÑSˆÈOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOB™YˆZ[Ø˜]\—Ù™X]\™\Ê™ËÝ—Ø˜]™×Ø˜]Ý—Ü]™×Ü]ÙX\ÛÛŠN‚ˆˆˆ‚ˆY\™ÙH\‹YØ[YH˜]\ˆÝ]ÛÛY\ÈÚ]ÙX\ÛÛ‹[]™[Ý]Ø\Ýˆ[™˜[‘Ü˜\È™X]\™\È›Üˆ›ÝH˜]\ˆ[™ÜÜÚ[™È]Ú\‹‚ˆˆˆ‚ˆÈ›Ü›X[^™HQÈÈ[ˆ™ÈH™Ë˜ÛÜJ
Bˆ™ÖÈ˜˜]\ˆ—HH™ÖÈ˜˜]\ˆ—K˜\Ý\J[
B‚ˆÈ8¥ 8¥ ˜]\ˆÙX\ÛÛˆÝ]È8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ ˆÝˆHÝ—Ø˜]ÜÝ—Ø˜]ÈœÙX\ÛÛˆ—HOHÙX\ÛÛ—K˜ÛÜJ
BˆÝ–Èœ^Y\—ÚY—HHÝ–Èœ^Y\—ÚY—K˜\Ý\J[
BˆÝˆHÝ‹œ™[˜[YJÛÛ[[œÏ^Âˆ™\ÝØ˜HŽˆœÝ—Þ˜H‹ˆ™\ÝÝÛØ˜HŽˆœÝ—ÞÛØ˜H‹ˆ™\ÝÜÛÈŽˆœÝ—ÞÛÈ‹ˆš×Ü\˜Ù[ŽˆœÝ—Ú×ÜÝ‹ˆ˜˜—Ü\˜Ù[ŽˆœÝ—Ø˜—ÜÝ‹ˆ˜]™×Ú]ÜÜYYŽˆœÝ—Ù]ˆ‹ˆ˜œ›Ü\˜Ù[ŽˆœÝ—Øœ›ÜÝ‹ˆ˜[™Û\ÝÙY]ÜÝ\˜Ù[ŽˆœÝ—ÜÜ×ÜÝ‹ˆ™]ŽM\\˜Ù[ŽˆœÝ—ÚÜÝ‹ˆ˜]™×Ú]Ø[™ÛHŽˆœÝ—ÛH‹ˆJBˆ˜]\—ÜÝ—ØÛÛÈHÈœ^Y\—ÚY‹œÝ—Þ˜H‹œÝ—ÞÛØ˜H‹œÝ—ÞÛÈ‹ˆœÝ—Ú×ÜÝ‹œÝ—Ø˜—ÜÝ‹œÝ—Ù]ˆ‹œÝ—Øœ›ÜÝ‹ˆœÝ—ÜÜ×ÜÝ‹œÝ—ÚÜÝ‹œÝ—ÛH—BˆÝˆHÝ–ÖØÈ›ÜˆÈ[ˆ˜]\—ÜÝ—ØÛÛÈYˆÈ[ˆÝ‹˜ÛÛ[[œ×WB‚ˆ™ÈH™×Ø˜]Ù™×Ø˜]ÈœÙX\ÛÛˆ—HOHÙX\ÛÛ—K˜ÛÜJ
Bˆ™×ÚYØÛÛH’Q™ÈˆYˆ’Q™Èˆ[ˆ™Ë˜ÛÛ[[œÈ[ÙHœ^Y\šY‚ˆYˆ™×ÚYØÛÛ[ˆ™Ë˜ÛÛ[[œÎ‚ˆ™ÖÙ™×ÚYØÛÛHH™ÖÙ™×ÚYØÛÛK˜\Ý\JÝŠBˆ™×Ø˜]ØÛÛÈHÙ™×ÚYØÛÛ“˜[YH‹U‘È‹“Ð”‹”ÓÈ‹ÓÐH‹ÔÊÈ‹”H‹ˆ’ÉH‹‰H‹’TÓÈ‹P’T‹”Ü‹•ÐTˆ—Bˆ™ÈH™ÖÖØÈ›ÜˆÈ[ˆ™×Ø˜]ØÛÛÈYˆÈ[ˆ™Ë˜ÛÛ[[œ×WBˆ™Ë˜ÛÛ[[œÈHØË›ÝÙ\Š
Kœ™\XÙJ‰H‹—ÜÝŠKœ™\XÙJŠÈ‹—Ü\ÈŠKœ™\XÙJˆ‹—ÈŠBˆ›ÜˆÈ[ˆ™Ë˜ÛÛ[[œ×B‚ˆÈY\™ÙH˜]\ˆÝ]ÈÛÈØ[YHÙÂˆˆH™Ë›Y\™ÙJÝ‹YÛÛH˜˜]\ˆ‹šYÚÛÛHœ^Y\—ÚY‹ÝÏH›YŠB‚ˆÈ8¥ 8¥ ]Ú\ˆÙX\ÛÛˆÝ]È8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ ˆÝ—ÜHÝ—Ü]ÜÝ—Ü]ÈœÙX\ÛÛˆ—HOHÙX\ÛÛ—K˜ÛÜJ
BˆÝ—ÜÈœ^Y\—ÚY—HHÝ—ÜÈœ^Y\—ÚY—K˜\Ý\J[
BˆÝ—ÜHÝ—Üœ™[˜[YJÛÛ[[œÏ^Âˆž\˜HŽˆœÝ—Ù\˜WÜ‹ˆš×Ü\˜Ù[ŽˆœÝ—Ú×ÜÝÜ‹ˆ™\ÝÝÛØ˜HŽˆœÝ—ÞÛØ˜WÜ‹ˆÚY™—Ü\˜Ù[ŽˆœÝ—ÝÚY™—Ü‹ˆJBˆ]ÜÝ—ØÛÛÈHÈœ^Y\—ÚY‹œÝ—Ù\˜WÜ‹œÝ—Ú×ÜÝÜ‹œÝ—ÞÛØ˜WÜ‹œÝ—ÝÚY™—Ü—BˆÝ—ÜHÝ—ÜÖØÈ›ÜˆÈ[ˆ]ÜÝ—ØÛÛÈYˆÈ[ˆÝ—Ü˜ÛÛ[[œ×WB‚ˆÈY]Ú\ˆ›Û[™ÈØ[YK[ÙÈ™X]\™\È
œ›ÛH˜]\ˆØ[YHÙÈÛÛ^
BˆÈ›Üˆ›ÝÈ\ÙHHÝ]Ø\ÝYÙÈ8 %[˜KYØ[YH™X]\™\ÈYY[ˆÙ[B‚ˆÈ8¥ 8¥ ›Û[™È˜]\ˆ›Ü›H8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ ˆˆH‹œÛÜÝ˜[Y\ÊÈ˜˜]\ˆ‹™Ø[YWÙ]H—JBˆÈ8¥ 8¥ ˜]\ˆ›Ü›H[ÛY[[H
Ý\JH8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ ˆˆH‹œÛÜÝ˜[Y\ÊÈ˜˜]\ˆ‹™Ø[YWÙ]H—JBˆÈØ[Ý[]HMKY^H›Û[™È]™\˜YÙHÈÛÛ\\™HœÈÙX\ÛÛ˜[˜\Ù[[™Bˆ–È›MWÚ]È—HH‹™Ü›Ý\žJ˜˜]\ˆŠVÈš]È—K˜[œÙ›Ü›Jˆ[X™HˆœÚY
JKœ›Û[™ÊMKZ[—Ü\š[ÙÏLÊK›YX[Š
JBˆÈ[ÛY[[H™X]\™NˆMY]™\˜YÙHÈÙX\ÛÛˆ]™\˜YÙBˆÈ\È]XÝÈYˆH^Y\ˆ\È	ÚX][™È\	È™[]]™HÈZ\ˆÙX\ÛÛˆ[[ˆ–Èš]Û[ÛY[[H—HH–È›MWÚ]È—HÈ
–È˜]™È—H
ÈYKMŠBˆˆ–È›WÚ]È—HH‹™Ü›Ý\žJ˜˜]\ˆŠVÈš]È—K˜[œÙ›Ü›Jˆ[X™HˆœÚY
JKœ›Û[™ÊKZ[—Ü\š[ÙÏLJK›YX[Š
JBˆ–È›LÚ]È—HH‹™Ü›Ý\žJ˜˜]\ˆŠVÈš]È—K˜[œÙ›Ü›Jˆ[X™HˆœÚY
JKœ›Û[™ÊLZ[—Ü\š[ÙÏLJK›YX[Š
JBˆ–È›WØXˆ—HH‹™Ü›Ý\žJ˜˜]\ˆŠVÈ˜Xˆ—K˜[œÙ›Ü›Jˆ[X™HˆœÚY
JKœ›Û[™ÊKZ[—Ü\š[ÙÏLJK›YX[Š
JBˆ–È›WÜH—HH‹™Ü›Ý\žJ˜˜]\ˆŠVÈœH—K˜[œÙ›Ü›Jˆ[X™HˆœÚY
JKœ›Û[™ÊKZ[—Ü\š[ÙÏLJK›YX[Š
JB‚ˆ–ÈœÙX\ÛÛˆ—HHÙX\ÛÛ‚ˆ™]\›ˆ‚‚‚œš[
—¼'å)ÈZ[[™È˜]\ˆ™X]\™HX]š^
Œ
K‹‹ˆŠB˜˜]Ù™X]\™\ÈHZ[Ø˜]\—Ù™X]\™\Êˆ™×ÌŒÝ—Ø˜]Ø[™×Ø˜]Ø[Ý—Ü]Ø[™×Ü]Ø[ÙX\ÛÛLŒŠBœš[
ˆˆÚ\NˆØ˜]Ù™X]\™\ËœÚ\_HŠB˜˜]Ù™X]\™\Ë×ØÜÝŠˆžÓÕUUÑTŸKØ˜]Ù™X]\™\×ÌŒ˜ÜÝˆ‹[™^Q˜[ÙJBœš[
¸§!H˜]Ù™X]\™\×ÌŒ˜ÜÝˆØ]™YŠB‚‚ˆÈOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOBˆÈÑSH8 %™X]\™H[™Ú[™Y\š[™ÎˆÕ’RÑSÕUÈSÑSˆÈOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOB™YˆZ[Ü]Ú\—Ù™X]\™\ÊËÝ—Ü]™×Ü]Ý—Ø˜]Û[™]\™×Ø˜]ÙX\ÛÛ‹[\\™WÙ™X]\™\ÏS›Û™K[™]\ÜÝ]ÏS›Û™JN‚ˆÈHË˜ÛÜJ
BˆÖÈœ]Ú\ˆ—HHÖÈœ]Ú\ˆ—K˜\Ý\J[
B‚ˆÝ—ÜHÝ—Ü]ÜÝ—Ü]ÈœÙX\ÛÛˆ—HOHÙX\ÛÛ—K˜ÛÜJ
BˆÝ—ÜÈœ^Y\—ÚY—HHÝ—ÜÈœ^Y\—ÚY—K˜\Ý\J[
BˆÝ—ÜHÝ—Üœ™[˜[YJÛÛ[[œÏ^Âˆž\˜HŽˆœÝ—Þ\˜H‹ˆ™\˜HŽˆœÝ—Ù\˜H‹ˆš×Ü\˜Ù[ŽˆœÝ—Ú×ÜÝ‹ˆ˜˜—Ü\˜Ù[ŽˆœÝ—Ø˜—ÜÝ‹ˆÚY™—Ü\˜Ù[ŽˆœÝ—ÝÚY™—ÜÝ‹ˆJBˆ”]ØÛÛÈHÈœ^Y\—ÚY‹œÝ—Þ\˜H‹œÝ—Ù\˜H‹œÝ—Ú×ÜÝ‹œÝ—Ø˜—ÜÝ‹œÝ—ÝÚY™—ÜÝ—BˆÝ—ÜHÝ—ÜÖØÈ›ÜˆÈ[ˆ]ØÛÛÈYˆÈ[ˆÝ—Ü˜ÛÛ[[œ×WB‚ˆˆHYË›Y\™ÙJÝ—ÜYÛÛHœ]Ú\ˆ‹šYÚÛÛHœ^Y\—ÚY‹ÝÏH›YŠBˆˆH‹œÛÜÝ˜[Y\ÊÈœ]Ú\ˆ‹™Ø[YWÙ]H—JB‚ˆYˆ˜™ˆˆ›Ý[ˆ‹˜ÛÛ[[œÈ[™šÜÈˆ[ˆ‹˜ÛÛ[[œÎ‚ˆ–È˜™ˆ—HHN‚ˆYˆ›Ý]×Ü™XÛÜ™Yˆ[ˆ‹˜ÛÛ[[œÎ‚ˆ–Èš\—HH–È›Ý]×Ü™XÛÜ™Y—HÈËŒˆ[Yˆ˜™ˆˆ[ˆ‹˜ÛÛ[[œÎ‚ˆ–Èš\—HHœ˜Û\
–È˜™ˆ—HÈŒ‹JBˆ[ÙN‚ˆ–Èš\—HHKŒ‚ˆÈ8¥ 8¥ ]Ú\ˆÕ’RÑSÕU[ÛY[[H
Ý\JH8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ ˆ–Èš×Û[ÛY[[H—HH–È›WÚÜÈ—HÈ
–ÈœÝ—Ú×ÜÝ—HÈLŒ
ˆŒ
ÈYKMŠHÈ›ÞBˆˆ–È›×ÚÜÈ—HH‹™Ü›Ý\žJœ]Ú\ˆŠVÈšÜÈ—K˜[œÙ›Ü›Jˆ[X™HˆœÚY
JKœ›Û[™ÊËZ[—Ü\š[ÙÏLJK›YX[Š
Bˆ
Bˆ–È›WÚÜÈ—HH‹™Ü›Ý\žJœ]Ú\ˆŠVÈšÜÈ—K˜[œÙ›Ü›Jˆ[X™HˆœÚY
JKœ›Û[™ÊKZ[—Ü\š[ÙÏLJK›YX[Š
Bˆ
Bˆ–È›LÚÜÈ—HH‹™Ü›Ý\žJœ]Ú\ˆŠVÈšÜÈ—K˜[œÙ›Ü›Jˆ[X™HˆœÚY
JKœ›Û[™ÊLZ[—Ü\š[ÙÏLJK›YX[Š
Bˆ
B‚ˆ–È›×Ú\—HH‹™Ü›Ý\žJœ]Ú\ˆŠVÈš\—K˜[œÙ›Ü›Jˆ[X™HˆœÚY
JKœ›Û[™ÊËZ[—Ü\š[ÙÏLJK›YX[Š
Bˆ
Bˆ–È›WÚ\—HH‹™Ü›Ý\žJœ]Ú\ˆŠVÈš\—K˜[œÙ›Ü›Jˆ[X™HˆœÚY
JKœ›Û[™ÊKZ[—Ü\š[ÙÏLJK›YX[Š
Bˆ
B‚ˆ–Èœ™]—ÙØ[YWÙ]H—HH‹™Ü›Ý\žJœ]Ú\ˆŠVÈ™Ø[YWÙ]H—KœÚY
JBˆ–È™^\×Ü™\Ý—HH
ˆ×Ù]][YJ–È™Ø[YWÙ]H—JHH×Ù]][YJ–Èœ™]—ÙØ[YWÙ]H—JBˆ
K™™^\Ë™š[˜JJK˜Û\
ÝÙ\L\\LM
B‚ˆÈKKH™X[[™]\Ý]È
[š™XÝYœ›ÛH[™]\ÛØY\ŠHKKBˆYˆ[™]\ÜÝ]È[™š×ÜÝˆ[ˆ[™]\ÜÝ]Î‚ˆ–È›ÜÛ[™]\Ú×ÜÝÜ›ÞH—HH›Ø]
[™]\ÜÝ]ÖÈš×ÜÝ—JBˆ[ÙN‚ˆ–È›ÜÛ[™]\Ú×ÜÝÜ›ÞH—HHŒ‹ŒˆYˆ[™]\ÜÝ]È[™žÛØ˜Hˆ[ˆ[™]\ÜÝ]Î‚ˆ–È›ÜÛ[™]\ÞÛØ˜WÜ›ÞH—HH›Ø]
[™]\ÜÝ]ÖÈžÛØ˜H—JBˆ[ÙN‚ˆ–È›ÜÛ[™]\ÞÛØ˜WÜ›ÞH—HHŒÌŒ‚ˆÈKKH[\\™H™X]\™\È
œ›ÛH[\\™WÛØY\ŠHKKBˆ–È[\Þ›Û™WÜÚ^™H—HH›Ø]

[\\™WÙ™X]\™\ÈÜˆßJK™Ù]
[\Þ›Û™WÜÚ^™H‹Œ
JBˆ–È[\Ú×Ø›ÛÜÝ—HH›Ø]

[\\™WÙ™X]\™\ÈÜˆßJK™Ù]
[\Ú×Ø›ÛÜÝ‹Œ
JB‚ˆ–ÈœÙX\ÛÛˆ—HHÙX\ÛÛ‚ˆ™]\›ˆ‚‚‚‚œš[
—¼'å)ÈZ[[™È]Ú\ˆ™X]\™HX]š^
Œ
K‹‹ˆŠBœ]Ù™X]\™\ÈHZ[Ü]Ú\—Ù™X]\™\Êˆ×ÌŒÝ—Ü]Ø[™×Ü]Ø[Ý—Ø˜]Ø[™×Ø˜]Ø[ÙX\ÛÛLŒŠBœš[
ˆˆÚ\NˆÜ]Ù™X]\™\ËœÚ\_HŠBœ]Ù™X]\™\Ë×ØÜÝŠiÛÞžÛ«zÉí=UQAUQ}%Iô½Á¥Ñ}™•…ÑÕÉ•Í|ÈÀÈÐ¹ÍØˆ°¥¹‘•àõ…±Í”¤)ÁÉ¥¹Ð ‹ŠrÁ¥Ñ}™•…ÑÕÉ•Í|ÈÀÈÐ¹ÍØÍ…Ù•ˆ¤(((Œ€ôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôô(Œ10€ØƒŠP5½‘•°QÉ…¥¹¥¹œè!%QLa	½½ÍÐ(Œ€ôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôô)!%QM}QUIM}	M€ôl(€€€€Œ	…ÑÑ•ÈÅÕ…±¥Ñä(€€€€‰ÍÙ}á‰„ˆ°€‰ÍÙ}áÝ½‰„ˆ°€‰ÍÙ}áÍ±œˆ°(€€€€‰ÍÙ}­}ÁÐˆ°€‰ÍÙ}‰‰}ÁÐˆ°€‰ÍÙ}•Øˆ°€‰ÍÙ}‰É±}ÁÐˆ°€‰ÍÙ}ÍÍ}ÁÐˆ°(€€€€ŒA¥Ñ¡•ÈÅÕ…±¥Ñä€¡½ÁÁ½¹•¹Ð¤(€€€€‰ÍÙ}•É…}Àˆ°€‰ÍÙ}­}ÁÑ}Àˆ°€‰ÍÙ}áÝ½‰…}Àˆ°€‰ÍÙ}Ý¡¥™™}Àˆ°(€€€€ŒI••¹Ð™½É´(€€€€‰°Õ}¡¥ÑÌˆ°€‰°ÄÁ}¡¥ÑÌˆ°€‰°Õ}…ˆˆ°€‰°Õ}Á„ˆ°(€€€€Œ½¹Ñ•áÐ(€€€€‰Á}Ñ¡É½ÝÌˆ°)t()¥˜ÑÉ…¥¹}á‰}¡¥ÑÌ¡‘˜°Ñ…É•Ðô‰¡¥Ñ}½Ù•É|À¸Ôˆ¤è(€€€™•…Ñ}½±Ì€ôm˜™½È˜¥¸!%QM}QUIM}	M¥˜˜¥¸‘˜¹½±Õµ¹Ít((€€€¥˜€‰Á}Ñ¡É½ÝÌˆ¥¸™•…Ñ}½±Ìè(€€€€€€€‘˜€ô‘˜¹½Áä ¤(€€€€€€€‘™l‰Á}Ñ¡É½ÝÌ‰t€ô1…‰•±¹½‘•È ¤¹™¥Ñ}ÑÉ…¹Í™½É´¡‘™l‰Á}Ñ¡É½ÝÌ‰t¹™¥±±¹„ ‰Hˆ¤¤((€€€­••Á}½±Ì€ô™•…Ñ}½±Ì€¬mÑ…É•Ñt(€€€‘™}µ½‘•°€ô‘™m­••Á}½±Ít¹‘É½Á¹„¡ÍÕ‰Í•ÐõmÑ…É•Ñt¤¹½Áä ¤(€€€‘™}µ½‘•±m™•…Ñ}½±Ít€ô‘™}µ½‘•±m™•…Ñ}½±Ít¹™¥±±¹„¡‘™}µ½‘•±m™•…Ñ}½±Ít¹µ•‘¥…¸ ¤¤(€€€‘™}µ½‘•°€ô‘™}µ½‘•±m‘™}µ½‘•±l‰°Õ}…ˆ‰t€øô€Ét((€€€`€ô‘™}µ½‘•±m™•…Ñ}½±Ít¹Ù…±Õ•Ì(€€€ä€ô‘™}µ½‘•±mÑ…É•Ñt¹Ù…±Õ•Ì((€€€Í­˜€ôMÑÉ…Ñ¥™¥•‘-½±¡¹}ÍÁ±¥ÑÌôÔ°Í¡Õ™™±”õQÉÕ”°É…¹‘½µ}ÍÑ…Ñ”õM¤(€€€µ½‘•°€ôáˆ¹a	±…ÍÍ¥™¥•È (€€€€€€€¹}•ÍÑ¥µ…Ñ½ÉÌôÌÀÀ°(€€€€€€€µ…æk•ÁÑ ôÐ°(€€€€€€€±•…É¹¥¹}É…Ñ”ôÀ¸ÀÔ°(€€€€€€€ÍÕ‰Í…µÁ±”ôÀ¸à°(€€€€€€€½±Í…µÁ±•}‰åÑÉ•”ôÀ¸à°(€€€€€€€ÕÍ•}±…‰•±}•¹½‘•Èõ…±Í”°(€€€€€€€•Ù…±}µ•ÑÉ¥Œô‰±½±½ÍÌˆ°(€€€€€€€É…¹‘½µ}ÍÑ…Ñ”õM°(€€€€¤(€€€Ù}Í½É•Ì€ôÉ½ÍÍ}Ù…±}Í½É”¡µ½‘•°°`°ä°ØõÍ­˜°Í½É¥¹œô‰É½}…ÕŒˆ¤(€€€ÁÉ¥¹Ð¡˜ˆ€XUèíÙ}Í½É•Ì¹µ•…¸ ¤è¸Ñ™ôƒ
ÄíÙ}Í½É•Ì¹ÍÑ ¤è¸Ñ™ôˆ¤((€€€µ½‘•°¹™¥Ð¡`°ä¤(€€€…±¥ˆ€ô…±¥‰É…Ñ•‘±…ÍÍ¥™¥•ÉX¡µ½‘•°°ØôÌ°µ•Ñ¡½ô‰Í¥µ½¥ˆ¤(€€€…±¥ˆ¹™¥Ð¡`°ä¤((€€€É•ÑÕÉ¸…±¥ˆ°™•…Ñ}½±Ì°‘™}µ½‘•°()ÁÉ¥¹Ð ‰q»Â~>/¾â<€QÉ…¥¹¥¹œ!%QLa	½½ÍÐµ½‘•°¸¸¸ˆ¤)¡¥ÑÍ}µ½‘•°°¡¥ÑÍ}™•…ÑÕÉ•Ì°¡¥ÑÍ}‘˜€ôÑÉ…¥¹}á‰}¡¥ÑÌ¡‰…Ñ}™•…ÑÕÉ•Ì¤(((Œ€ôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôô(Œ10€ÜƒŠP5½‘•°QÉ…¥¹¥¹œèMQI%-=UQLa	½½ÍÐ(Œ€ôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôô)-}QUIM}	M€ôl(€€€€ŒA¥Ñ¡•ÈÅÕ…±¥Ñä(€€€€‰ÍÙ}á•É„ˆ°€‰ÍÙ}•É„ˆ°€‰ÍÙ}­}ÁÐˆ°€‰ÍÙ}‰‰}ÁÐˆ°€‰ÍÙ}Ý¡¥™™}ÁÐˆ°(€€€€ŒI••¹Ð™½É´(€€€€‰°Õ}­Ìˆ°€‰°Õ}­}É…Ñ”ˆ°€‰°ÄÁ}­Ìˆ°(€€€€Œ=ÁÁ½¹•¹Ð±¥¹•ÕÀ(€€€€‰½ÁÁ}±¥¹•ÕÁ}­}ÁÑ}ÁÉ½áäˆ°€‰½ÁÁ}±¥¹•ÕÁ}áÝ½‰…}ÁÉ½áäˆ°(€€€€ŒUµÁ¥É”é½¹”Ñ•¹‘•¹¥•Ì(€€€€‰ÕµÁ}é½¹•}Í¥é”ˆ°€‰ÕµÁ}­}‰½½ÍÐˆ°)t()‘•˜ÑÉ…¥¹}á‰}­Ì¡‘˜°Ñ…É•Ð°±…‰•°¤è(€€€™•…Ñ}½±Ì€ôm˜™½È˜¥¸-}QUIM}	M¥˜˜¥¸‘˜¹½±Õµ¹Ít((€€€­••Á}½±Ì€ô™•…Ñ}½±Ì€¬mÑ…É•Ñt(€€€™½È•áÑÉ…}½°¥¸l‰¥Àˆ°€‰‰˜‰tè€€€€€€€¥˜•áÑÉ…}½°¥¸‘˜¹½±Õµ¹Ì…¹•áÑÉ…}½°¹½Ð¥¸­••Á}½±Ìè(€€€€€€€€€€€­••Á}½±Ì¹…ÁÁ•¹¡•áÑÉ…}½°¤((€€€‘™_model = df[keep_cols].dropna(subset=[target]).copy()
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
    print(f"  [{label}] CV AUC: {cv_scores.mean():.4f} Â± {cv_scores.std():.4f}")

    model.fit(X, y)
    calib = CalibratedClassifierCV(model, cv=3, method="sigmoid")
    calib.fit(X, y)

    return calib, feat_cols, df_model

print("\nðŸ‹ï¸  Training K OVER 3.5 model...")
k35_model, k35_features, k35_df = train_xgb_ks(pit_features, "k_over_3.5", "K>3.5")
print("\nðŸ‹ï¸  Training K OVER 4.5 model...")
k45_model, k45_features, k45_df = train_xgb_ks(pit_features, "k_over_4.5", "K>4.5")
print("\nðŸ‹ï¸  Training K OVER 5.5 model...")
k55_model, k55_features, k55_df = train_xgb_ks(pit_features, "k_over_5.5", "K>5.5")


# ============================================================
# CELL 8 â€” Model Evaluation
# ============================================================
def evaluate_model(model, feat_cols, df_model, target, label):
    X = df_model[feat_cols].values
    { = df_model[target].values
    probs = model.predict_proba(X)[:, 1]
    preds = (probs >= 0.5).astype(int)

    auc   = roc_auc_score(y, probs)
    brier = brier_score_loss(y, probs)
    ll    = log_loss(y, probs)

    print(f"\nâ”€â”€ {label} â”€â”€")
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

print("\nðŸ“Š Evaluating models...")
hits_eval = evaluate_model(hits_model, hits_features, hits_df, "hit_over_0.5", "Hits>0.5")
k35_eval  = evaluate_model(k35_model,  k35_features,  k35_df,  "k_over_3.5",   "K>3.5")
k45_eval  = evaluate_model(k45_model,  k45_features,  k45_df,  "k_over_4.5",   "K>4.5")
k55_eval  = evaluate_model(k55_model,  k55_features,  k55_df,  "k_over_5.5",   "K>5.5")


# ============================================================
# CELL 9 â€” SHAP Explainability
# ============================================================
def plot_shap(model, feat_cols, df_model, label, n=500):
    try:
        raw = (model.estimators_[0]
               if hasattr(model, "estimators_") else model)
        explainer = shap.TreeExplainer(raw)
        sample = df_model[feat_cols].sample(min(n, len(df_model)), random_state=SEED)
        shap_vals = explainer.shap_values(sample)
        shap.summary_plot(shap_vals, sample, show=False)
        plt.title(f"SHAP â€” {label}")
        plt.tight_layout()
        plt.savefig(f"{OUTPUT_DIR}/shap_{label.replace('>','_over_').replace(' ','_')}.png", dpi=120)
        plt.close()
        print(f"  SHAP plot saved for {label}")
    except Exception as e:
        print(f"  SHAP failed for {label}: {e}")

print("\nðŸ” Generating SHAP plots...")
plot_shap(hits_model, hits_features, hits_df, "Hits_0.5")
plot_shap(k35_model,  k35_features,  k35_df,  "K_3.5")
plot_shap(k45_model,  k45_features,  k45_df,  "K_4.5")


# ============================================================
# CELL 10 â€” Save Models
# ============================================================
_REGISTRY: dict[str, dict] = {}

def save_model(model, feat_cols, line_key, meta=None):
    path = os.path.join(OUTPUT_DIR, f"xgb_{line_key}.pkl")
    _REGISTRY[line_key] = {"model": model, "features": feat_cols}
    joblib.dump({"model": model, "features": feat_cols, "meta": meta or {}}, path)
    print(f"  Saved â†’ {path}")

results = {
    "hits_over_0.5": hits_eval,
    "k_over_3.5":    k35_eval,
    "k_over_4.5":    k45_eval,
    "k_over_5.5":    k55_eval,
}

print("\nðŸ’¾ Saving models...")
save_model(hits_model, hits_features, "hits_over_0.5", {"target": "hits", "line": 0.5})
save_model(k35_model,  k35_features,  "k_over_3.5",   {"target": "ks",   "line": 3.5})
save_model(k45_model,  k45_features,  "k_over_4.5",   {"target": "ks",   "line": 4.5})
save_model(k55_model,  k55_features,  "k_over_5.5",   {"target": "ks",   "line": 5.5})

with open(f"{OUTPUT_DIR}/eval_results.json", "w") as f:
    json.dump(results, f, indent=2)
print("âœ… eval_results.json saved")
print("\nâœ… All models saved to", OUTPUT_DIR)


# ============================================================
# CELL 11 â€” xgb_prop_scorer.py (app.py integration snippet)
# ============================================================
APP_INTEGRATION_CODE = '''
"""
xgb_prop_scorer.py â€” XGBoost Prop Probability Scorer
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
    Probability of batter recording â‰¥1 hit.
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
        "sv_era":                  _safe(pitcher_stats.get("sv_era") or 
                                         pitcher_stats.get("fg_era"),    4.20),
        "sv_k_pct":                _safe(pitcher_stats.get("sv_k_pct"), 22.0),
        "sv_bb_pct":               _safe(pitcher_stats.get("sv_bb_pct"), 8.5),
        "sv_whiff_pct":           _safe(pitcher_stats.get("sv_whiff_pct"), 22.0),
        "l5_ks":                  _safe(pitcher_stats.get("l5_ks"), 5.0),
        "l5_k_rate":              _safe(pitcher_stats.get("sv_k_pct"), 22.0) / 100.0,
        "l10_ks":                 _safe(pitcher_stats.get("l10_ks"), 5.0),
        "opp_lineup_k_pct_proxy": _safe(pitcher_stats.get("opp_lineup_k_boost"), 22.0),
        "opp_lineup_xwoba_proxy": _safe(pitcher_stats.get("sv_xwoba_p"), 0.310),
        "ump_zone_size":          _safe(pitcher_stats.get("ump_zone_size"), 0.0),
        "ump_k_boost":           _safe(pitcher_stats.get("ump_k_boost"), 0.0),
    }
    X = np.array([[row.get(f, 0.0) for f in feat_cols]], dtype=float)
    try:
        return float(reg["model"].predict_proba(X)[0, 1])
    except Exception :
        return None
'''
