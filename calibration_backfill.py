"""
calibration_backfill.py — un-gate the volume-gated calibration layers.

The live recalibration layer (`prop_calibration.py`) and the calibration
monitor need (model probability → graded outcome) pairs, and gathering them
from real tracker picks takes weeks (isotonic needs ≥80 graded picks per
market). But the CURRENT season's games are already played, the committed
models never trained on them (train 2021-24, test 2025), and every feature is
reconstructable exactly the way `regenerate_models.py` builds it — so scoring
the season-to-date against known outcomes yields thousands of genuinely
out-of-sample (prob, outcome) pairs per market TODAY.

Output: ``data/calibration_backfill.json``::

    {"generated_at_utc": ..., "season": 2026, "n_days": ...,
     "markets": {"batter_hits": [[p, y], ...], ...}}

keyed by TRACKER market key (all trained lines of a market pooled, mirroring
the mixed-line distribution the live calibrator sees).

Distribution caveat (why backfill pairs are TOPPED UP, not blended 1:1): the
production pre-calibration probability is the stacked fusion (XGB × analytic
× market blend), while backfill pairs are the XGB model's calibrated output
alone. Close — the fusion is anchored on the XGB prob for these markets — but
not identical, so `app._build_prop_calibrator` uses backfill only to fill the
gap below a target sample and retires it at a 4:1 rate as real graded picks
accumulate (at 150 tracker picks per market the backfill is fully displaced).

Run:  python calibration_backfill.py            # current season to date
      python calibration_backfill.py --season 2026
"""

from __future__ import annotations

import argparse
import json
import os
import warnings
from datetime import datetime, timedelta

import joblib
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

import regenerate_models as rm

_HERE = os.path.dirname(os.path.abspath(__file__))
_MODEL_DIR = os.path.join(_HERE, "models")
_DATA_DIR = os.path.join(_HERE, "data")
OUT_PATH = os.path.join(_DATA_DIR, "calibration_backfill.json")

# Cap probabilities the way the serve path does (xgb_prop_scorer._score_full).
P_LO, P_HI = 0.03, 0.97


def _season_windows(season: int):
    """regenerate_models windows, capped at yesterday (season to date)."""
    yesterday = datetime.utcnow() - timedelta(days=1)
    for ws, we in rm._windows(season):
        if datetime.strptime(ws, "%Y-%m-%d") > yesterday:
            break
        we = min(datetime.strptime(we, "%Y-%m-%d"), yesterday).strftime("%Y-%m-%d")
        yield ws, we


def fetch_season_logs(season: int):
    import pybaseball as pb
    pb.cache.enable()
    bf, pf = [], []
    windows = list(_season_windows(season))
    for i, (ws, we) in enumerate(windows, 1):
        print(f"  [{i}/{len(windows)}] Statcast {ws} → {we}", flush=True)
        try:
            sc = pb.statcast(start_dt=ws, end_dt=we)
        except Exception as e:
            print(f"    ⚠️ {ws}→{we} failed: {e}")
            continue
        if sc is None or len(sc) == 0:
            continue
        b, p, *_ = rm._agg_chunk(sc, season)
        if len(b):
            bf.append(b)
        if len(p):
            pf.append(p)
    bg = pd.concat(bf, ignore_index=True) if bf else pd.DataFrame()
    pg = pd.concat(pf, ignore_index=True) if pf else pd.DataFrame()
    print(f"  batter rows: {len(bg):,}  pitcher rows: {len(pg):,}")
    return bg, pg


def score_market(mkey: str, cfg: dict, df: pd.DataFrame):
    """Score one committed model over the season matrix → [(p, y), ...]."""
    path = os.path.join(_MODEL_DIR, f"{cfg['file_key']}.pkl")
    if not os.path.exists(path):
        return None
    art = joblib.load(path)
    model, feats = art["model"], art["features"]
    med = art.get("meta", {}).get("train_medians", {}) or {}
    target = cfg["target"]
    if target not in df.columns:
        return None
    sub = df.dropna(subset=[target]).copy()
    if sub.empty:
        return None
    X = pd.DataFrame(index=sub.index)
    for c in feats:
        X[c] = pd.to_numeric(sub.get(c), errors="coerce")
        m = med.get(c)
        # bt_* medians are recorded as None on purpose (never imputed).
        if m is not None:
            X[c] = X[c].fillna(m)
    probs = model.predict_proba(X.values.astype(np.float32))[:, 1]
    probs = np.clip(probs, P_LO, P_HI)
    ys = sub[target].values.astype(float)
    return [[round(float(p), 4), float(y)] for p, y in zip(probs, ys)]


def main(season: int):
    print(f"═══ Calibration backfill — season {season} to date ═══")
    bg, pg = fetch_season_logs(season)
    bat = rm.build_batter_matrix(bg) if len(bg) else pd.DataFrame()
    pit = rm.build_pitcher_matrix(pg) if len(pg) else pd.DataFrame()
    print(f"  batter matrix={bat.shape}  pitcher matrix={pit.shape}")

    markets: dict = {}
    for mkey, cfg in rm.MARKETS.items():
        tracker_mk = rm.TRACKER_MARKET.get(mkey)
        if not tracker_mk:
            continue
        df = bat if cfg["source"] == "batter" else pit
        if df.empty:
            continue
        pairs = score_market(mkey, cfg, df)
        if not pairs:
            print(f"  {mkey:<9} ✗ no model / no rows")
            continue
        mean_p = sum(p for p, _ in pairs) / len(pairs)
        mean_y = sum(y for _, y in pairs) / len(pairs)
        print(f"  {mkey:<9} n={len(pairs):>6,}  mean_pred={mean_p:.3f}  mean_actual={mean_y:.3f}")
        markets.setdefault(tracker_mk, []).extend(pairs)

    # Downsample per market (evenly strided → distribution preserved). The
    # consumer tops up to at most _PROP_CAL_BACKFILL_TARGET=600 pairs, so
    # 4,000 leaves ample headroom while keeping the JSON small.
    MAX_PAIRS = 4000
    for mk, pairs in markets.items():
        if len(pairs) > MAX_PAIRS:
            step = len(pairs) / MAX_PAIRS
            markets[mk] = [pairs[int(i * step)] for i in range(MAX_PAIRS)]

    out = {
        "generated_at_utc": datetime.utcnow().isoformat(),
        "season": season,
        "note": ("Out-of-sample (prob, outcome) pairs from scoring the committed "
                 "models over the current season to date. Consumed by "
                 "app._build_prop_calibrator as a top-up prior that real graded "
                 "tracker picks displace at 4:1."),
        "markets": {mk: pairs for mk, pairs in sorted(markets.items())},
    }
    with open(OUT_PATH, "w") as f:
        json.dump(out, f)
    total = sum(len(v) for v in markets.values())
    print(f"  → {OUT_PATH}  ({total:,} pairs across {len(markets)} markets)")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, default=datetime.utcnow().year)
    main(ap.parse_args().season)
