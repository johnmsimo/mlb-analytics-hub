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
]
K_FEATURES = [
    "sv_xera", "sv_era", "sv_k_pct", "sv_bb_pct", "sv_whiff_pct",
    "l3_ks", "l5_ks", "l5_k_rate", "l10_ks", "l3_ip", "l5_ip", "days_rest",
]

XGB_PARAMS_HITS = dict(n_estimators=300, max_depth=4, learning_rate=0.04,
                       subsample=0.80, colsample_bytree=0.80,
                       min_child_weight=3, gamma=0.05)
XGB_PARAMS_K = dict(n_estimators=350, max_depth=4, learning_rate=0.035,
                    subsample=0.78, colsample_bytree=0.78,
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
    sc["tb"] = sc["is_hit"] + sc["is_hr"] + sc["is_double"] + sc["is_triple"]

    pa = sc[sc["is_pa"] == 1].copy()
    if len(pa) == 0:
        return pd.DataFrame(), pd.DataFrame()

    # ── Starter per (game, side): pitcher with the smallest at_bat_number.
    #    A batter in inning_topbot=='Top' faces the home pitcher (Top-side
    #    starter); 'Bot' faces the away pitcher (Bot-side starter).
    side_first = (pa.sort_values("at_bat_number")
                    .groupby(["game_pk", "inning_topbot"])["pitcher"]
                    .first().reset_index().rename(columns={"pitcher": "opp_starter"}))

    bat = (pa.groupby(["game_pk", "game_date", "batter"])
             .agg(hits=("is_hit", "sum"), ab=("is_ab", "sum"), pa=("is_pa", "sum"),
                  tb=("tb", "sum"),
                  inning_topbot=("inning_topbot", "first"),
                  stand=("stand", "first") if "stand" in pa.columns else ("is_pa", "size"),
                  p_throws=("p_throws", "first") if "p_throws" in pa.columns else ("is_pa", "size"))
             .reset_index())
    bat = bat.merge(side_first, on=["game_pk", "inning_topbot"], how="left")
    bat["hit_over_0.5"] = (bat["hits"] >= 1).astype(int)
    bat["season"] = season

    pit = (pa.groupby(["game_pk", "game_date", "pitcher"])
             .agg(ks=("is_k", "sum"), bf=("is_pa", "sum"))
             .reset_index())
    pit["k_over_3.5"] = (pit["ks"] >= 4).astype(int)
    pit["k_over_4.5"] = (pit["ks"] >= 5).astype(int)
    pit["k_over_5.5"] = (pit["ks"] >= 6).astype(int)
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
    bats = df.get("Bats")
    out["bats_L"] = (bats == "L").astype(int) if bats is not None else 0
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
    bg["ab_safe"] = bg["ab"].clip(lower=1)
    bg["l7_hits"] = bg.groupby("batter")["hits"].transform(
        lambda x: x.shift(1).rolling(7, min_periods=1).mean())
    bg["l14_hits"] = bg.groupby("batter")["hits"].transform(
        lambda x: x.shift(1).rolling(14, min_periods=1).mean())
    bg["hit_rate_game"] = bg["hits"] / bg["ab_safe"]
    bg["l7_hit_rate"] = bg.groupby("batter")["hit_rate_game"].transform(
        lambda x: x.shift(1).rolling(7, min_periods=1).mean())

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
        # opponent starter skills
        if not fp.empty:
            opp = fp.rename(columns={
                "sv_xera": "opp_xera", "sv_k_pct": "opp_k_pct",
                "sv_bb_pct": "opp_bb_pct", "sv_whiff_pct": "opp_whiff",
            })[["mlbam", "opp_xera", "opp_k_pct", "opp_bb_pct", "opp_whiff", "throws_R"]]
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
    tr[feats] = tr[feats].fillna(med)
    te[feats] = te[feats].fillna(med)

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
            "xgboost_version": xgb.__version__,
            "exported_at_utc": datetime.utcnow().isoformat(),
            "model_type": "CalibratedClassifierCV(XGBClassifier)",
        },
    }
    return {"artifact": artifact, "auc_te": auc_te, "brier_te": brier_te,
            "brier_base": brier_base, "features": feats}


def main(markets: list[str]):
    print("═" * 70)
    print(f" Honest XGB regeneration — {datetime.utcnow():%Y-%m-%d %H:%M} UTC")
    print(f" Train seasons {TRAIN_SEASONS}  |  Test season {TEST_SEASON}")
    print("═" * 70)

    print("\n══ Fetching Statcast game logs ══")
    bg, pg = fetch_game_logs(ALL_SEASONS)
    bg.to_parquet(os.path.join(_DATA_DIR, "train_output_bg.parquet")) if len(bg) else None
    pg.to_parquet(os.path.join(_DATA_DIR, "train_output_pg.parquet")) if len(pg) else None

    print("\n══ Building feature matrices ══")
    bat = build_batter_matrix(bg) if len(bg) else pd.DataFrame()
    pit = build_pitcher_matrix(pg) if len(pg) else pd.DataFrame()
    print(f"  batter matrix={bat.shape}  pitcher matrix={pit.shape}")

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
        with open(os.path.join(_MODEL_DIR, "xgb_feature_cols.json"), "w") as f:
            json.dump(feat_map, f, indent=2)
    summary = {k: (v["artifact"]["meta"] if v else None) for k, v in results.items()}
    with open(os.path.join(_DATA_DIR, "regen_summary.json"), "w") as f:
        json.dump(summary, f, indent=2)
    print(f"\n  Shipped: {shipped or 'NONE'}")
    print(f"  Summary → data/regen_summary.json")
    return results


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--markets", nargs="+", default=list(MARKETS.keys()))
    args = ap.parse_args()
    main([m for m in args.markets if m in MARKETS])
