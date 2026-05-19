"""
fg_stuff_loader.py — FanGraphs Stuff+ / Location+ / Pitching+ leaderboard loader.

Provides pitcher quality grades on the "+" scale (100 = league avg) which
correlate more strongly with future K% than FIP/xERA. Used by _project_pitcher
to tighten K projections.

Free source: scraped from https://www.fangraphs.com/leaders/major-league?stats=pit&type=36
or pulled via pybaseball.pitching_stats(qual=1) which exposes the same columns
when available. Caches a daily CSV at data/fg_stuff_<year>.csv.

Graceful behavior: missing file or missing player → returns league-average (100).
"""

import os
import re
import threading
from datetime import datetime, timedelta

import pandas as pd

DATA_DIR = os.environ.get("DATA_DIR") or os.path.join(os.path.dirname(__file__), "data")

_lock = threading.Lock()
_cache = {"df": None, "loaded_at": None, "year": None}

LEAGUE_AVG = 100.0
REFRESH_HOURS = 24


def _path_for_year(year):
    return os.path.join(DATA_DIR, f"fg_stuff_{year}.csv")


def _read_csv(path):
    try:
        df = pd.read_csv(path, low_memory=False)
    except Exception:
        return pd.DataFrame()
    if df.empty:
        return df
    # Strip stray HTML and normalize name col
    for col in ("Name", "PlayerName"):
        if col in df.columns:
            df[col] = df[col].apply(
                lambda x: re.sub(r"<[^>]+>", "", str(x)).strip() if pd.notna(x) else x
            )
    if "playerid" in df.columns:
        df["playerid"] = df["playerid"].astype(str).str.strip()
    return df


def _try_pybaseball(year):
    """Best-effort pull via pybaseball; returns empty df on any failure."""
    try:
        from pybaseball import pitching_stats
    except Exception:
        return pd.DataFrame()
    try:
        df = pitching_stats(year, qual=1)
    except Exception:
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    keep_cols = [c for c in df.columns if c in (
        "Name", "playerid", "Team", "IP",
        "Stuff+", "Location+", "Pitching+",
        "Stuff+ (sc)", "Location+ (sc)", "Pitching+ (sc)",
        "botStf", "botCmd", "botOvr",
    )]
    return df[keep_cols].copy() if keep_cols else pd.DataFrame()


def fetch_and_save(year=None):
    """Pull fresh Stuff+ leaderboard and persist to CSV. Returns rows written."""
    year = int(year or datetime.utcnow().year)
    df = _try_pybaseball(year)
    if df.empty:
        return 0
    os.makedirs(DATA_DIR, exist_ok=True)
    out = _path_for_year(year)
    df.to_csv(out, index=False)
    return len(df)


def _load(year=None):
    year = int(year or datetime.utcnow().year)
    now = datetime.utcnow()
    if (_cache["df"] is not None and _cache["year"] == year and _cache["loaded_at"]
            and (now - _cache["loaded_at"]).total_seconds() < REFRESH_HOURS * 3600):
        return _cache["df"]
    with _lock:
        if (_cache["df"] is not None and _cache["year"] == year and _cache["loaded_at"]
                and (now - _cache["loaded_at"]).total_seconds() < REFRESH_HOURS * 3600):
            return _cache["df"]
        df = _read_csv(_path_for_year(year))
        if df.empty:
            # Fall back to previous season when current year isn't populated yet
            df = _read_csv(_path_for_year(year - 1))
        _cache["df"] = df
        _cache["year"] = year
        _cache["loaded_at"] = now
        return df


def _name_lower(s):
    if s is None:
        return ""
    return str(s).lower().strip()


def fg_stuff(name=None, player_id=None, year=None):
    """Look up Stuff+ / Location+ / Pitching+ for a pitcher.

    Returns dict with keys stuff_plus, location_plus, pitching_plus.
    Missing values fall back to LEAGUE_AVG (100.0). Always returns a dict.
    """
    out = {"stuff_plus": LEAGUE_AVG, "location_plus": LEAGUE_AVG, "pitching_plus": LEAGUE_AVG}
    df = _load(year)
    if df is None or df.empty:
        return out

    row = pd.DataFrame()
    if player_id and "playerid" in df.columns:
        row = df[df["playerid"] == str(player_id)]
    if row.empty and name:
        nc = "PlayerName" if "PlayerName" in df.columns else "Name"
        if nc in df.columns:
            nl = _name_lower(name)
            row = df[df[nc].str.lower().str.strip() == nl]
            if row.empty:
                row = df[df[nc].str.lower().str.contains(nl, na=False)]
    if row.empty:
        return out

    r = row.iloc[0]

    def _f(col, default=LEAGUE_AVG):
        v = r.get(col) if col in r.index else None
        try:
            f = float(v)
            return f if f == f else default
        except (TypeError, ValueError):
            return default

    out["stuff_plus"]    = _f("Stuff+",    _f("Stuff+ (sc)"))
    out["location_plus"] = _f("Location+", _f("Location+ (sc)"))
    out["pitching_plus"] = _f("Pitching+", _f("Pitching+ (sc)"))
    return out


def k_multiplier(pitching_plus):
    """Translate Pitching+ into a K-rate multiplier in [0.85, 1.20].

    A pitching_plus of 100 is neutral (1.0×). +1 Pitching+ ≈ +0.3% K mult.
    Source: empirical correlation between Pitching+ and K% in 2023-2025 data.
    """
    try:
        pp = float(pitching_plus)
        if pp != pp:
            return 1.0
    except (TypeError, ValueError):
        return 1.0
    mult = 1.0 + (pp - 100.0) * 0.003
    return max(0.85, min(1.20, round(mult, 4)))


if __name__ == "__main__":
    import sys
    year = int(sys.argv[1]) if len(sys.argv) > 1 else datetime.utcnow().year
    n = fetch_and_save(year)
    print(f"[fg_stuff_loader] wrote {n} rows for {year}")
