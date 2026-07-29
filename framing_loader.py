"""
framing_loader.py — Statcast catcher framing leaderboard loader.

Provides per-catcher framing runs (Strike Zone Runs Saved) used to shade
K-prop projections when joined with HP umpire tendency.

Source: https://baseballsavant.mlb.com/leaderboard/catcher-framing?csv=true
Cached daily at data/savant_framing_<year>.csv.

NOTE: As of 2025 Savant's public CSV strips the `id` and `name` columns
(only the per-zone `rv_*` numbers populate). When that happens, every
lookup returns zeros and the merged framing×umpire multiplier defaults to
the umpire-only signal — graceful degradation. To enable framing edge:
drop a hand-curated CSV at data/savant_framing_<year>.csv with at least
two columns (`id` or `player_name`, plus `rv_tot` / `runs_extra_strikes`).
Until then the rest of the Best Bets pipeline still benefits from Stuff+,
bat tracking, Ballpark Pal, travel features, and calibrated tiers.

Graceful behavior: missing file or missing catcher → returns 0.0 framing runs.
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
_cache = {"df": None, "loaded_at": None, "year": None, "index": None}

REFRESH_HOURS = 24
SAVANT_URL = (
    "https://baseballsavant.mlb.com/leaderboard/catcher-framing"
    "?type=Cat&year={year}&team=&min=q&sort=4&sortDir=desc&csv=true"
)
USER_AGENT = "Mozilla/5.0 (compatible; mlb-analytics-hub/1.0)"


def _path_for_year(year):
    return os.path.join(DATA_DIR, f"savant_framing_{year}.csv")


def fetch_and_save(year=None, timeout=15):
    year = int(year or datetime.utcnow().year)
    url = SAVANT_URL.format(year=year)
    try:
        req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/csv"})
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"[framing_loader] fetch failed for {year}: {e}")
        return 0
    if not raw or "," not in raw:
        return 0
    os.makedirs(DATA_DIR, exist_ok=True)
    out = _path_for_year(year)
    with open(out, "w", encoding="utf-8") as f:
        f.write(raw)
    try:
        df = pd.read_csv(out, low_memory=False)
        return len(df)
    except Exception:
        return 0


def _read_csv(path):
    try:
        df = pd.read_csv(path, low_memory=False)
    except Exception:
        return pd.DataFrame()
    return df


def _load(year=None):
    year = int(year or datetime.utcnow().year)
    now = datetime.utcnow()
    if (_cache["df"] is not None and _cache["year"] == year and _cache["loaded_at"]
            and (now - _cache["loaded_at"]).total_seconds() < REFRESH_HOURS * 3600):
        return _cache["df"]
    with _lock:
        df = _read_csv(_path_for_year(year))
        if df.empty:
            df = _read_csv(_path_for_year(year - 1))
        _cache["df"] = df
        _cache["year"] = year
        _cache["loaded_at"] = now
        _cache["index"] = _build_index(df)
        return df


def _name_lower(s):
    return re.sub(r"\s+", " ", str(s or "").lower().strip())


def _first_first_last(name):
    """Convert 'Last, First' → 'first last'. Handles already-normalized too."""
    s = _name_lower(name)
    if "," in s:
        last, first = [p.strip() for p in s.split(",", 1)]
        return f"{first} {last}".strip()
    return s


def _build_index(df):
    return DataFrameLookupIndex(
        df,
        id_columns=("id", "player_id", "catcher", "mlbam_id"),
        name_columns=(
            "catcher_name",
            "player_name",
            "Name",
            "name",
            "last_name, first_name",
        ),
        name_normalizer=_first_first_last,
    )


def _lookup_index(df):
    index = _cache.get("index")
    if index is not None and _cache.get("df") is df:
        return index
    with _lock:
        index = _cache.get("index")
        if index is None or _cache.get("df") is not df:
            _cache["df"] = df
            index = _build_index(df)
            _cache["index"] = index
    return index


def framing_runs(name=None, player_id=None, year=None):
    """Return framing data dict for a catcher.

    Keys: framing_runs (season total), runs_extra_strikes (per-call),
    extra_strikes (raw count). Missing data → zeros.
    """
    out = {"framing_runs": 0.0, "runs_extra_strikes": 0.0, "extra_strikes": 0.0}
    df = _load(year)
    if df is None or df.empty:
        return out

    r = _lookup_index(df).find(player_id=player_id, name=name)
    if r is None:
        return out

    def _f(col):
        v = r.get(col) if col in r.index else None
        try:
            f = float(v)
            return f if f == f else 0.0
        except (TypeError, ValueError):
            return 0.0

    # `rv_tot` is the Savant catcher framing leaderboard's runs total. Older
    # column variants kept for compatibility.
    out["framing_runs"]       = _f("rv_tot") or _f("runs_extra_strikes") or _f("framing_runs") or _f("framing")
    out["runs_extra_strikes"] = _f("runs_extra_strikes") or _f("rv_tot")
    out["extra_strikes"]      = _f("strike_rate_above_avg") or _f("pct_tot") or _f("extra_strikes")
    return out


def framing_k_multiplier(framing_runs_value):
    """Convert framing runs to a K-rate multiplier in [0.94, 1.06].

    Range based on Statcast 2024-2025: elite framer ≈ +15 runs/season →
    +3.9 pp K%, equivalent to ~+1.05 K mult. Linear scale.
    """
    try:
        fr = float(framing_runs_value)
        if fr != fr:
            return 1.0
    except (TypeError, ValueError):
        return 1.0
    # 15 runs → +0.05 mult; -15 runs → -0.05 mult; linear in-between
    mult = 1.0 + (fr / 15.0) * 0.05
    return max(0.94, min(1.06, round(mult, 4)))


if __name__ == "__main__":
    import sys
    year = int(sys.argv[1]) if len(sys.argv) > 1 else datetime.utcnow().year
    n = fetch_and_save(year)
    print(f"[framing_loader] wrote {n} rows for {year}")
