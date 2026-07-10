"""
stuff_model.py — physics-based pitch-quality ("Stuff") model
═══════════════════════════════════════════════════════════════════════════════
Why this exists
---------------
The K models' swing-and-miss inputs (season SwStr%, arsenal whiff%) are all
OUTCOME-based: they need a meaningful sample of results before they say
anything, and they can't tell "got lucky vs bad swings" from "unhittable
pitch". The professional systems that moved the needle recently (Stuff+ /
StuffPro / THE BATcast) model expected outcome from the PHYSICS of the pitch —
velocity, movement, spin, extension — which stabilizes in a few hundred
pitches and leads outcome stats when a pitcher adds velo or a new pitch.

This module is that model, sized to this repo:

  • A pitch-TYPE-level XGB regressor: predict a pitch type's whiff rate
    (whiffs / swings) from its physics — velocity, arm-side-normalized
    horizontal break, vertical break, spin rate, extension, and the velocity
    gap off the pitcher's primary fastball. No outcome features.
  • A pitcher's "stuff" = his arsenal's usage-weighted expected whiff, scaled
    to a Stuff+ style index (league mean 100, +10 per league SD).

TRAIN/SERVE PARITY IS THE POINT OF THIS FILE. regenerate_models.py builds
training aggregates with the SAME functions (physics_sums → finalize_features
→ score) that app.py uses at serve time on a live statcast_pitcher pull, so a
feature the model learned is exactly the feature it is fed.

Leakage policy: the physics→whiff mapping is fit on TRAIN seasons only (the
caller passes the fit mask); pitcher-season scores are season aggregates —
same convention as every FG season stat the models already train on.

Pure training/inference module: no Flask, no app imports.
Self-tested: `python stuff_model.py` runs a synthetic-data sanity check.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd

# ── Pitch classification (shared with regenerate_models._agg_chunk's whiff
#    definitions — foul tips count as whiffs, matching the batter g_whiff). ──
SWING_DESCRIPTIONS = (
    "swinging_strike", "swinging_strike_blocked", "foul_tip",
    "foul", "hit_into_play",
)
WHIFF_DESCRIPTIONS = ("swinging_strike", "swinging_strike_blocked", "foul_tip")

# Fastball family used to anchor the velocity gap (primary fastball = the
# highest-usage member present; fallback: the pitcher's fastest pitch type).
FASTBALL_TYPES = ("FF", "SI", "FC")

# Physics features the stuff model trains on — nothing outcome-based.
STUFF_FEATURES = ["velo", "pfx_x_arm", "pfx_z", "spin", "ext", "velo_gap"]

MIN_SWINGS_FIT = 30       # a pitch-type row needs this many swings to train on
MIN_PITCHES_SCORE = 150   # a pitcher needs this many pitches for a stuff score

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ARTIFACT_PATH = os.path.join(_HERE, "models", "stuff_whiff.pkl")

_XGB_PARAMS = dict(n_estimators=250, max_depth=4, learning_rate=0.05,
                   subsample=0.80, colsample_bytree=0.80,
                   min_child_weight=5, random_state=42, n_jobs=-1)


# ════════════════════════════════════════════════════════════════════════
# 1. Aggregation (chunk-safe sums → finalized per-pitch-type features)
# ════════════════════════════════════════════════════════════════════════

def physics_sums(sc: pd.DataFrame, keys: tuple = ("pitcher",)) -> pd.DataFrame:
    """Per (keys…, pitch_type) physics SUMS from raw Statcast pitch rows.

    Returns sums + counts (not means) so chunked pulls combine exactly: concat
    the outputs and let finalize_features() group-sum before dividing. The
    horizontal break is sign-normalized so arm-side run is positive for both
    hands (pfx_x is catcher-view: positive = arm side for LHP only).
    """
    need = {"pitch_type", "description", "release_speed"}
    if sc is None or len(sc) == 0 or not need.issubset(sc.columns):
        return pd.DataFrame()
    d = sc.loc[sc["pitch_type"].notna() & (sc["pitch_type"] != "")].copy()
    if len(d) == 0:
        return pd.DataFrame()

    desc = d["description"].astype(str)
    d["_swing"] = desc.isin(SWING_DESCRIPTIONS).astype(int)
    d["_whiff"] = desc.isin(WHIFF_DESCRIPTIONS).astype(int)

    throws = d["p_throws"].astype(str) if "p_throws" in d.columns else pd.Series("R", index=d.index)
    pfx_x = pd.to_numeric(d.get("pfx_x"), errors="coerce")
    d["_pfx_x_arm"] = np.where(throws == "L", pfx_x, -pfx_x)
    d["_pfx_z"] = pd.to_numeric(d.get("pfx_z"), errors="coerce")
    d["_velo"] = pd.to_numeric(d["release_speed"], errors="coerce")
    d["_spin"] = pd.to_numeric(d.get("release_spin_rate"), errors="coerce")
    d["_ext"] = pd.to_numeric(d.get("release_extension"), errors="coerce")

    gcols = list(keys) + ["pitch_type"]
    agg = d.groupby(gcols).agg(
        n=("pitch_type", "size"),
        swings=("_swing", "sum"),
        whiffs=("_whiff", "sum"),
        sum_velo=("_velo", "sum"),      n_velo=("_velo", "count"),
        sum_pfx_x=("_pfx_x_arm", "sum"), n_pfx=("_pfx_x_arm", "count"),
        sum_pfx_z=("_pfx_z", "sum"),
        sum_spin=("_spin", "sum"),      n_spin=("_spin", "count"),
        sum_ext=("_ext", "sum"),        n_ext=("_ext", "count"),
    ).reset_index()
    return agg


def finalize_features(sums: pd.DataFrame, keys: tuple = ("pitcher",)) -> pd.DataFrame:
    """Combine chunked physics_sums frames and derive the model features.

    Returns one row per (keys…, pitch_type) with STUFF_FEATURES + usage,
    n/swings/whiffs and whiff_rate. Missing physics stay NaN (XGB missing
    branch) — never imputed.
    """
    if sums is None or len(sums) == 0:
        return pd.DataFrame()
    gcols = list(keys) + ["pitch_type"]
    s = sums.groupby(gcols, as_index=False).sum(numeric_only=True)

    def _mean(sum_col, n_col):
        n = s[n_col].replace(0, np.nan)
        return s[sum_col] / n

    s["velo"] = _mean("sum_velo", "n_velo")
    s["pfx_x_arm"] = _mean("sum_pfx_x", "n_pfx")
    s["pfx_z"] = s["sum_pfx_z"] / s["n_pfx"].replace(0, np.nan)
    s["spin"] = _mean("sum_spin", "n_spin")
    s["ext"] = _mean("sum_ext", "n_ext")
    s["whiff_rate"] = (s["whiffs"] / s["swings"].replace(0, np.nan)).clip(0, 0.8)

    # Primary fastball velocity per pitcher(-season): highest-usage fastball
    # type present; fallback to the pitcher's fastest pitch type. velo_gap =
    # fastball velo − pitch velo (positive for offspeed).
    kl = list(keys)
    fb = s[s["pitch_type"].isin(FASTBALL_TYPES)]
    if len(fb):
        fb_pick = fb.sort_values("n", ascending=False).groupby(kl, as_index=False).first()
        fb_velo = fb_pick[kl + ["velo"]].rename(columns={"velo": "_fb_velo"})
    else:
        fb_velo = pd.DataFrame(columns=kl + ["_fb_velo"])
    fastest = (s.sort_values("velo", ascending=False)
                 .groupby(kl, as_index=False).first())[kl + ["velo"]] \
        .rename(columns={"velo": "_max_velo"})
    s = s.merge(fb_velo, on=kl, how="left").merge(fastest, on=kl, how="left")
    s["_fb_velo"] = s["_fb_velo"].fillna(s["_max_velo"])
    s["velo_gap"] = s["_fb_velo"] - s["velo"]

    total_n = s.groupby(kl)["n"].transform("sum")
    s["usage"] = s["n"] / total_n.replace(0, np.nan)

    keep = gcols + STUFF_FEATURES + ["usage", "n", "swings", "whiffs", "whiff_rate"]
    return s[keep].copy()


def _as_float32(df: pd.DataFrame) -> np.ndarray:
    """Nullable-dtype-safe float32 matrix: raw Statcast pulls come back with
    pandas NAType values that a bare .astype(np.float32) rejects."""
    return df.apply(pd.to_numeric, errors="coerce").to_numpy(
        dtype=np.float32, na_value=np.nan)


# ════════════════════════════════════════════════════════════════════════
# 2. Fit + score
# ════════════════════════════════════════════════════════════════════════

def fit(rows: pd.DataFrame, keys: tuple = ("pitcher",),
        fit_seasons: Optional[list] = None) -> Optional[dict]:
    """Fit the physics→whiff regressor on finalized pitch-type rows and return
    the artifact dict {model, features, meta}. `rows` should already be
    restricted to the fit population (e.g. train seasons only) — this function
    does not filter by season itself; `fit_seasons` is recorded in meta only.

    League normalization (mean/SD of pitcher expected whiff) is computed from
    the SAME fit rows and stored in meta, so serve-time Stuff+ is anchored to
    the training league, not whatever slice is being scored.
    """
    import xgboost as xgb

    if rows is None or len(rows) == 0:
        return None
    tr = rows[(rows["swings"] >= MIN_SWINGS_FIT) & rows["whiff_rate"].notna()].copy()
    if len(tr) < 200:
        return None

    X = _as_float32(tr[STUFF_FEATURES])
    y = _as_float32(tr[["whiff_rate"]]).ravel()
    w = _as_float32(tr[["swings"]]).ravel()

    model = xgb.XGBRegressor(**_XGB_PARAMS)
    model.fit(X, y, sample_weight=w)

    artifact = {
        "model": model,
        "features": list(STUFF_FEATURES),
        "meta": {
            "target": "pitch_type_whiff_rate",
            "min_swings_fit": MIN_SWINGS_FIT,
            "min_pitches_score": MIN_PITCHES_SCORE,
            "n_fit_rows": int(len(tr)),
            "fit_seasons": list(fit_seasons or []),
            "xgboost_version": xgb.__version__,
            "exported_at_utc": datetime.now(timezone.utc).isoformat(),
        },
    }
    # League anchor from the fit population's pitcher-level expected whiff.
    league = score_groups(artifact, tr, keys=keys, _normalize=False)
    league = league[league["n_pitches"] >= MIN_PITCHES_SCORE]
    if len(league) < 50:
        return None
    artifact["meta"]["league_mean"] = round(float(league["xwhiff"].mean()), 5)
    artifact["meta"]["league_sd"] = round(float(league["xwhiff"].std(ddof=0)), 5)
    return artifact


def score_groups(artifact: dict, rows: pd.DataFrame,
                 keys: tuple = ("pitcher",), _normalize: bool = True) -> pd.DataFrame:
    """Score finalized pitch-type rows → one row per (keys…) with the
    usage-weighted expected whiff (`xwhiff`), `n_pitches`, and (when the
    artifact carries league params and _normalize) `stuff_plus`."""
    if rows is None or len(rows) == 0:
        return pd.DataFrame(columns=list(keys) + ["xwhiff", "n_pitches", "stuff_plus"])
    d = rows.copy()
    X = _as_float32(d[artifact["features"]])
    d["_xw"] = np.clip(artifact["model"].predict(X), 0.0, 0.8)
    d["_wxw"] = d["_xw"] * d["n"]
    g = (d.groupby(list(keys), as_index=False)
           .agg(_wxw=("_wxw", "sum"), n_pitches=("n", "sum")))
    g["xwhiff"] = g["_wxw"] / g["n_pitches"].replace(0, np.nan)
    g = g.drop(columns=["_wxw"])
    if _normalize:
        meta = artifact.get("meta", {})
        mu, sd = meta.get("league_mean"), meta.get("league_sd")
        if mu is not None and sd:
            g["stuff_plus"] = (100.0 + 10.0 * (g["xwhiff"] - mu) / sd).round(1)
        else:
            g["stuff_plus"] = np.nan
    return g


# ════════════════════════════════════════════════════════════════════════
# 3. Artifact IO + serve entry point
# ════════════════════════════════════════════════════════════════════════

def save(artifact: dict, path: str = DEFAULT_ARTIFACT_PATH) -> str:
    import joblib
    os.makedirs(os.path.dirname(path), exist_ok=True)
    joblib.dump(artifact, path)
    return path


def load(path: str = DEFAULT_ARTIFACT_PATH) -> Optional[dict]:
    import joblib
    if not os.path.exists(path):
        return None
    try:
        art = joblib.load(path)
        if isinstance(art, dict) and "model" in art and "features" in art:
            return art
    except Exception:
        pass
    return None


def pitcher_stuff_plus(artifact: dict, raw_pitches: pd.DataFrame) -> Optional[float]:
    """Serve entry: Stuff+ for ONE pitcher from his raw Statcast pitch rows
    (statcast_pitcher output). Returns None below MIN_PITCHES_SCORE — callers
    fall back (e.g. to a prior-season pull) or serve NaN, matching training
    where a thin pitcher-season row is dropped rather than imputed."""
    if artifact is None or raw_pitches is None or len(raw_pitches) == 0:
        return None
    raw = raw_pitches.copy()
    raw["_pid"] = 1
    sums = physics_sums(raw, keys=("_pid",))
    rows = finalize_features(sums, keys=("_pid",))
    if rows.empty or rows["n"].sum() < MIN_PITCHES_SCORE:
        return None
    scored = score_groups(artifact, rows, keys=("_pid",))
    if scored.empty:
        return None
    v = scored["stuff_plus"].iloc[0]
    return round(float(v), 1) if pd.notna(v) else None


# ════════════════════════════════════════════════════════════════════════
# 4. Self-test (synthetic)
# ════════════════════════════════════════════════════════════════════════

def _self_test():
    rng = np.random.default_rng(7)
    n_pitchers = 240
    frames = []
    truth = {}
    for pid in range(n_pitchers):
        fb_velo = rng.normal(93.5, 2.4)
        spin = rng.normal(2250, 150)
        ride = rng.normal(1.3, 0.25)          # pfx_z feet on the fastball
        hand = "L" if rng.random() < 0.28 else "R"
        # ground truth: velo + ride + spin drive whiffs
        base_whiff = 0.16 + 0.020 * (fb_velo - 93.5) + 0.05 * (ride - 1.3) \
            + 0.00005 * (spin - 2250)
        truth[pid] = base_whiff
        for ptype, share, dv, dz in (("FF", 0.55, 0.0, 0.0),
                                     ("SL", 0.30, -7.5, -1.2),
                                     ("CH", 0.15, -8.5, -0.6)):
            n = int(1400 * share)
            whiff_p = np.clip(base_whiff + (0.10 if ptype != "FF" else 0.0), 0.02, 0.7)
            swings = int(n * 0.47)
            whiffs = rng.binomial(swings, whiff_p)
            velo = fb_velo + dv
            frames.append(pd.DataFrame({
                "pitcher": pid, "pitch_type": ptype, "p_throws": hand,
                "release_speed": rng.normal(velo, 0.8, n),
                "pfx_x": rng.normal(-0.6 if hand == "R" else 0.6, 0.2, n),
                "pfx_z": rng.normal(ride + dz, 0.15, n),
                "release_spin_rate": rng.normal(spin, 40, n),
                "release_extension": rng.normal(6.4, 0.2, n),
                "description": (["swinging_strike"] * whiffs
                                + ["foul"] * (swings - whiffs)
                                + ["ball"] * (n - swings)),
            }))
    sc = pd.concat(frames, ignore_index=True)

    sums = physics_sums(sc, keys=("pitcher",))
    rows = finalize_features(sums, keys=("pitcher",))
    assert len(rows) == n_pitchers * 3, f"rows={len(rows)}"
    assert rows["velo_gap"].max() > 5, "velo_gap should be positive for offspeed"

    art = fit(rows, keys=("pitcher",), fit_seasons=["synthetic"])
    assert art is not None, "fit failed"
    scored = score_groups(art, rows, keys=("pitcher",))
    assert abs(scored["stuff_plus"].mean() - 100.0) < 2.0, scored["stuff_plus"].mean()

    # Physics ordering must survive: pitchers in the top-decile of TRUE stuff
    # must out-score the bottom decile on the model's Stuff+.
    scored["truth"] = scored["pitcher"].map(truth)
    top = scored.nlargest(24, "truth")["stuff_plus"].mean()
    bot = scored.nsmallest(24, "truth")["stuff_plus"].mean()
    assert top - bot > 10, f"ordering weak: top={top:.1f} bot={bot:.1f}"

    # Serve path: single-pitcher raw pitches → Stuff+ close to the batch score.
    best_pid = int(scored.nlargest(1, "truth")["pitcher"].iloc[0])
    one = sc[sc["pitcher"] == best_pid]
    sp = pitcher_stuff_plus(art, one)
    batch = float(scored.loc[scored["pitcher"] == best_pid, "stuff_plus"].iloc[0])
    assert sp is not None and abs(sp - batch) < 1.0, f"serve={sp} batch={batch}"

    # Thin sample → None (never a fabricated score).
    assert pitcher_stuff_plus(art, one.head(40)) is None

    print(f"stuff_model self-test OK — league mean 100±2 (got "
          f"{scored['stuff_plus'].mean():.1f}), top-vs-bottom decile "
          f"gap {top - bot:.1f} Stuff+ pts, serve≈batch ({sp} vs {batch:.1f})")


if __name__ == "__main__":
    _self_test()
