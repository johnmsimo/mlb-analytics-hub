"""
savant_bat_tracking.py — Statcast bat tracking + swing/take run value loader.

Two free leaderboards from Baseball Savant:
  • Bat Tracking: bat_speed, swing_length, fast_swing_pct, squared_up_pct
  • Swing/Take Run Value: per-zone (heart/shadow/chase/waste) swing RV

Used by _project_batter_batx to:
  • Boost HR/TB projection when bat_speed_pct > 75 and squared_up > 33%
  • Boost BB projection / reduce K when chase_zone_rv > 0 and heart_zone_rv > 0

Free sources:
  https://baseballsavant.mlb.com/leaderboard/bat-tracking?csv=true
  https://baseballsavant.mlb.com/leaderboard/swing-take?csv=true

Cached daily at data/savant_bat_tracking_<year>.csv and data/savant_swing_take_<year>.csv.
"""

import os
import re
import threading
from datetime import datetime
from urllib.request import Request, urlopen

import pandas as pd

from config import settings
from dataframe_lookup import DataFrameLookupIndex

DATA_DIR = settings.data_dir

_lock = threading.Lock()
_cache = {
    "bat": {
        "df": None,
        "loaded_at": None,
        "year": None,
        "index": None,
        "speed_percentiles": {},
    },
    "swt": {
        "df": None,
        "loaded_at": None,
        "year": None,
        "index": None,
        "speed_percentiles": {},
    },
}

REFRESH_HOURS = 24
USER_AGENT = "Mozilla/5.0 (compatible; mlb-analytics-hub/1.0)"

# Note: Savant CSV endpoints are sometimes inconsistent. These URLs are the
# ones that work as of Apr 2026; swing-take frequently returns header-only
# until filter params are tuned per season. Loader degrades gracefully.
BAT_TRACKING_URL = (
    "https://baseballsavant.mlb.com/leaderboard/bat-tracking"
    "?year={year}&team=&min_swings=q&type=batter&csv=true"
)
SWING_TAKE_URL = (
    "https://baseballsavant.mlb.com/leaderboard/swing-take"
    "?attackZone=&batSide=&gameType=R&groupBy=&min=q&pitchHand=&pitchType="
    "&season={year}&team=&type=batter&csv=true"
)


def _path(kind, year):
    fname = "savant_bat_tracking" if kind == "bat" else "savant_swing_take"
    return os.path.join(DATA_DIR, f"{fname}_{year}.csv")


def _read(path):
    try:
        df = pd.read_csv(path, low_memory=False)
    except Exception:
        return pd.DataFrame()
    return df


def _fetch(url, timeout=15):
    try:
        req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/csv"})
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"[savant_bat_tracking] fetch failed: {e}")
        return None
    if not raw or "," not in raw:
        return None
    return raw


def fetch_and_save(year=None):
    year = int(year or datetime.utcnow().year)
    written = {}
    for kind, url in (("bat", BAT_TRACKING_URL), ("swt", SWING_TAKE_URL)):
        raw = _fetch(url.format(year=year))
        if not raw:
            written[kind] = 0
            continue
        os.makedirs(DATA_DIR, exist_ok=True)
        path = _path(kind, year)
        with open(path, "w", encoding="utf-8") as f:
            f.write(raw)
        try:
            written[kind] = len(pd.read_csv(path, low_memory=False))
        except Exception:
            written[kind] = 0
    return written


def _load(kind, year=None):
    year = int(year or datetime.utcnow().year)
    now = datetime.utcnow()
    slot = _cache[kind]
    if (slot["df"] is not None and slot["year"] == year and slot["loaded_at"]
            and (now - slot["loaded_at"]).total_seconds() < REFRESH_HOURS * 3600):
        return slot["df"]
    with _lock:
        df = _read(_path(kind, year))
        if df.empty:
            df = _read(_path(kind, year - 1))
        slot["df"] = df
        slot["year"] = year
        slot["loaded_at"] = now
        slot["index"], slot["speed_percentiles"] = _build_lookup_snapshot(kind, df)
        return df


def _name_norm(s):
    if s is None:
        return ""
    s = re.sub(r"\s+", " ", str(s).lower().strip())
    if "," in s:
        last, first = [p.strip() for p in s.split(",", 1)]
        return f"{first} {last}".strip()
    return s


def _build_bat_speed_percentiles(df):
    """Precompute the legacy ``speed <= player speed`` percentile by value."""
    if df is None or df.empty or "avg_bat_speed" not in df.columns:
        return {}
    try:
        speeds = df["avg_bat_speed"].dropna().astype(float)
    except Exception:
        # Preserve the previous all-or-nothing conversion behavior: one invalid
        # leaderboard value disables percentiles until the next refresh.
        return {}
    if speeds.empty:
        return {}
    ranked = speeds.rank(method="max", pct=True) * 100.0
    return {
        float(speed): round(float(percentile), 1)
        for speed, percentile in zip(speeds, ranked)
    }


def _build_lookup_snapshot(kind, df):
    index = DataFrameLookupIndex(
        df,
        id_columns=("player_id", "id", "batter", "mlbam_id"),
        name_columns=("last_name, first_name", "player_name", "name", "Name"),
        name_normalizer=_name_norm,
    )
    speed_percentiles = _build_bat_speed_percentiles(df) if kind == "bat" else {}
    return index, speed_percentiles


def _lookup_index(kind, df):
    slot = _cache[kind]
    index = slot.get("index")
    if (
        index is not None
        and slot.get("df") is df
        and "speed_percentiles" in slot
    ):
        return index
    with _lock:
        index = slot.get("index")
        if (
            index is None
            or slot.get("df") is not df
            or "speed_percentiles" not in slot
        ):
            slot["df"] = df
            index, speed_percentiles = _build_lookup_snapshot(kind, df)
            slot["index"] = index
            slot["speed_percentiles"] = speed_percentiles
    return index


def _find_row(df, name, player_id, kind="bat"):
    if df is None or df.empty:
        return None
    return _lookup_index(kind, df).find(
        player_id=player_id,
        name=name,
        last_name_fallback=True,
    )


def _f(row, *cols):
    for c in cols:
        if row is None or c not in row.index:
            continue
        v = row.get(c)
        try:
            f = float(v)
            if f == f:
                return f
        except (TypeError, ValueError):
            pass
    return None


def bat_tracking(name=None, player_id=None, year=None):
    """Look up bat tracking metrics for a batter.

    Savant CSV columns (verified): avg_bat_speed, swing_length, hard_swing_rate,
    squared_up_per_swing, squared_up_per_bat_contact, batter_run_value.
    Returns dict with floats / None.
    """
    row = _find_row(_load("bat", year), name, player_id, kind="bat")
    bs = _f(row, "avg_bat_speed", "bat_speed")
    pct = (
        _cache["bat"].get("speed_percentiles", {}).get(bs)
        if bs is not None
        else None
    )
    return {
        "bat_speed":            bs,
        "swing_length":         _f(row, "swing_length", "avg_swing_length"),
        "fast_swing_pct":       _f(row, "hard_swing_rate", "fast_swing_rate"),
        "squared_up_pct":       _f(row, "squared_up_per_swing", "squared_up_swing_rate"),
        "blast_pct":            _f(row, "blast_per_swing", "blast_swing_rate"),
        "bat_speed_percentile": pct,
        "batter_rv":            _f(row, "batter_run_value"),
    }


def swing_take(name=None, player_id=None, year=None):
    """Look up swing/take run value per zone for a batter.

    Savant CSV columns: runs_all, runs_heart, runs_shadow, runs_chase, runs_waste.
    Returns dict with floats / None.
    """
    row = _find_row(_load("swt", year), name, player_id, kind="swt")
    return {
        "heart_rv":  _f(row, "runs_heart",  "heart_runs"),
        "shadow_rv": _f(row, "runs_shadow", "shadow_runs"),
        "chase_rv":  _f(row, "runs_chase",  "chase_runs"),
        "waste_rv":  _f(row, "runs_waste",  "waste_runs"),
        "total_rv":  _f(row, "runs_all",    "swing_take_runs"),
    }


# ── Adjustment helpers ──────────────────────────────────────────────────────────

def power_boost(bt):
    """Convert bat-tracking dict into an HR/TB power multiplier in [0.95, 1.08].

    Bat speed percentile and squared-up rate together gate the boost.
    Elite (>= 90th pct + >= 35% squared-up) → +6-8%. Below average → -3-5%.
    """
    if not bt:
        return 1.0
    pct = bt.get("bat_speed_percentile")
    sup = bt.get("squared_up_pct")
    if pct is None and sup is None:
        return 1.0
    pct = pct if pct is not None else 50.0
    sup = sup if sup is not None else 0.33
    # Center: pct=50 + sup=0.33 → 1.0
    speed_delta = (pct - 50.0) / 50.0 * 0.05   # ±0.05
    sup_delta   = (sup - 0.33) * 0.20          # ±0.05 for ±25% squared-up
    mult = 1.0 + speed_delta + sup_delta
    return max(0.95, min(1.08, round(mult, 4)))


def discipline_boost(swt):
    """Convert swing/take dict into a discipline edge in [-0.10, +0.10].

    Positive total_rv (above-average swing decisions) boosts BB and reduces K.
    Heart-zone aggression matters more for power; chase-zone restraint matters
    more for K-under. Returns a fractional edge ready to add to disc_edge.
    """
    if not swt:
        return 0.0
    chase = swt.get("chase_rv")
    heart = swt.get("heart_rv")
    if chase is None and heart is None:
        return 0.0
    chase = chase if chase is not None else 0.0
    heart = heart if heart is not None else 0.0
    # Empirical scale: chase_rv ranges roughly ±15 over a full season
    chase_edge = (chase / 15.0) * 0.07
    heart_edge = (heart / 15.0) * 0.03
    edge = chase_edge + heart_edge
    return max(-0.10, min(0.10, round(edge, 4)))


if __name__ == "__main__":
    import sys
    year = int(sys.argv[1]) if len(sys.argv) > 1 else datetime.utcnow().year
    w = fetch_and_save(year)
    print(f"[savant_bat_tracking] wrote {w}")
