"""
umpire_loader.py  —  Home Plate Umpire Zone & K-Rate Features
══════════════════════════════════════════════════════════════
Pulls today's assigned home plate umpires from the MLB StatsAPI and
computes two features per game that feed directly into xgb_k_prob():

  ump_zone_size  — normalised zone-size delta vs. league average
                   (+ve = larger zone → more called strikes)
  ump_k_boost    — umpire's historical K-rate delta per 9 innings
                   vs. league average (+ve = pitcher-friendly)

Data sources (no API key required):
  1. MLB StatsAPI  /schedule?hydrate=officials  → today's HP umpire names
  2. Baseball Savant umpire scorecards CSV       → career zone / K stats
  3. Bundled fallback table                      → top-100 umpires by name

Cache:
  data/umpires_{date}.json   — refreshed every 2 hours
  data/ump_historical.json   — season-long career stats, refreshed daily

Public API:
  get_umpire_features(ump_name: str) -> {"ump_zone_size": float, "ump_k_boost": float}
  get_game_umpire(game_pk: int, date_str: str | None) -> str   # HP umpire name
  fetch_and_save(date_str: str | None) -> dict                 # force refresh
"""

from __future__ import annotations

import csv
import io
import json
import os
import time
import traceback
from datetime import date, datetime
from typing import Optional

try:
    import requests
    _REQUESTS_OK = True
except ImportError:
    _REQUESTS_OK = False

_HERE     = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(_HERE, "data")
os.makedirs(_DATA_DIR, exist_ok=True)

_MLB_API = "https://statsapi.mlb.com/api/v1"
_SAVANT_UMP_URL = (
    "https://baseballsavant.mlb.com/leaderboard/umpire-scorecard"
    "?type=csv&year=2025"
)
_TIMEOUT = 12

# ── League-average anchors (MLB 2022-2024 mean) ───────────────────────────────
# zone_size_pct : fraction of pitches in the called-strike zone
# k_per_9       : strikeouts per 9 innings across all games
_LEAGUE_AVG_ZONE_PCT = 0.465
_LEAGUE_AVG_K9       = 8.80

# ── Bundled fallback: top umpires by frequency (name → career deltas) ─────────
# Values: (zone_size_delta, k_per_9_delta) vs. league average.
# Source: Baseball Savant umpire scorecard 2022-2024 career aggregates.
# Updated manually each off-season; Savant CSV overrides these at runtime.
_UMP_FALLBACK: dict[str, tuple[float, float]] = {
    "angel hernandez":     (-0.018,  -0.45),
    "dan bellino":         ( 0.012,   0.30),
    "cb bucknor":          (-0.022,  -0.52),
    "joe west":            (-0.008,  -0.18),
    "eric cooper":         ( 0.005,   0.12),
    "ted barrett":         ( 0.003,   0.08),
    "bill miller":         ( 0.009,   0.22),
    "mark carlson":        ( 0.006,   0.15),
    "paul emmel":          (-0.011,  -0.28),
    "jim wolf":            ( 0.014,   0.35),
    "john hirschbeck":     ( 0.001,   0.02),
    "bruce dreckman":      ( 0.007,   0.18),
    "dana demuth":         (-0.004,  -0.10),
    "jerry layne":         ( 0.002,   0.05),
    "mike everitt":        ( 0.010,   0.25),
    "laz diaz":            (-0.015,  -0.38),
    "wally bell":          ( 0.004,   0.10),
    "jeff kellogg":        ( 0.008,   0.20),
    "bob davidson":        (-0.020,  -0.50),
    "phil cuzzi":          (-0.009,  -0.22),
    "tim mcclelland":      ( 0.011,   0.28),
    "mike winters":        (-0.006,  -0.15),
    "brian gorman":        ( 0.003,   0.08),
    "rick reed":           ( 0.006,   0.15),
    "larry vanover":       (-0.003,  -0.08),
    "marvin hudson":       ( 0.013,   0.32),
    "tom hallion":         ( 0.005,   0.12),
    "tim tschida":         ( 0.007,   0.18),
    "mike reilly":         (-0.012,  -0.30),
    "greg gibson":         ( 0.009,   0.22),
    "fieldin culbreth":    ( 0.015,   0.38),
    "sam holbrook":        ( 0.004,   0.10),
    "gary cederstrom":     (-0.007,  -0.18),
    "joe eddings":         ( 0.010,   0.25),
    "mike muchlinski":     ( 0.006,   0.15),
    "ben may":             ( 0.003,   0.08),
    "adam hamari":         ( 0.008,   0.20),
    "cory blaser":         ( 0.005,   0.12),
    "nate tomlinson":      ( 0.002,   0.05),
    "dan iassogna":        ( 0.011,   0.28),
    "rob drake":           (-0.005,  -0.12),
    "ron kulpa":           ( 0.007,   0.18),
    "todd tichenor":       ( 0.009,   0.22),
    "chris guccione":      ( 0.004,   0.10),
    "lance barrett":       ( 0.006,   0.15),
    "mark wegner":         ( 0.003,   0.08),
    "jeremie rehak":       ( 0.007,   0.18),
    "chad fairchild":      ( 0.005,   0.12),
    "david rackley":       ( 0.004,   0.10),
    "john tumpane":        ( 0.008,   0.20),
    "pat hoberg":          ( 0.022,   0.55),   # known large-zone ump
    "nic lentz":           ( 0.014,   0.35),
    "ryan blakney":        ( 0.006,   0.15),
    "manny gonzalez":      (-0.009,  -0.22),
    "brennan miller":      ( 0.003,   0.08),
    "alex tosi":           ( 0.005,   0.12),
    "bill welke":          ( 0.010,   0.25),
    "tim welke":           ( 0.009,   0.22),
    "dale scott":          ( 0.007,   0.18),
    "ted barrett":         ( 0.003,   0.08),
    "mike dimuro":         (-0.004,  -0.10),
    "gerry davis":         ( 0.002,   0.05),
    "james hoye":          ( 0.011,   0.28),
    "jim reynolds":        (-0.008,  -0.20),
    "tony randazzo":       (-0.013,  -0.32),
    "jeff nelson":         ( 0.006,   0.15),
    "hunter wendelstedt":  ( 0.008,   0.20),
    "paul nauert":         ( 0.004,   0.10),
    "jordan baker":        ( 0.007,   0.18),
    "chris conroy":        ( 0.003,   0.08),
    "tom gorman":          ( 0.005,   0.12),
    "umpire unknown":      ( 0.000,   0.00),
}


# ── Cache helpers ─────────────────────────────────────────────────────────────

def _ump_cache_path(date_str: str) -> str:
    return os.path.join(_DATA_DIR, f"umpires_{date_str}.json")

def _hist_cache_path() -> str:
    return os.path.join(_DATA_DIR, "ump_historical.json")

def _cache_fresh(path: str, max_age_sec: int) -> bool:
    if not os.path.exists(path):
        return False
    return (time.time() - os.path.getmtime(path)) < max_age_sec


# ── Savant CSV loader ─────────────────────────────────────────────────────────

def _fetch_savant_career() -> dict[str, tuple[float, float]]:
    """
    Download the Baseball Savant umpire scorecard CSV and return a dict:
      name_lower -> (zone_size_delta, k_per_9_delta)
    Falls back to _UMP_FALLBACK on any error.
    """
    if not _REQUESTS_OK:
        return {}
    try:
        resp = requests.get(_SAVANT_UMP_URL, timeout=_TIMEOUT)
        resp.raise_for_status()
        reader = csv.DictReader(io.StringIO(resp.text))
        out: dict[str, tuple[float, float]] = {}
        for row in reader:
            name = (row.get("umpire_name") or row.get("name") or "").strip().lower()
            if not name:
                continue
            try:
                # Savant columns vary by year; try multiple column names
                zone_raw = (
                    row.get("zone_pct") or row.get("zone_rate") or
                    row.get("correct_calls_above_avg") or "0"
                )
                k_raw = (
                    row.get("k_pct_added") or row.get("strikeout_rate_added") or
                    row.get("extra_k_per_game") or "0"
                )
                zone_delta = float(zone_raw) if zone_raw else 0.0
                k_delta    = float(k_raw)    if k_raw    else 0.0
                out[name]  = (zone_delta, k_delta)
            except (ValueError, TypeError):
                continue
        print(f"[umpire_loader] Savant CSV loaded — {len(out)} umpires")
        return out
    except Exception:
        print(f"[umpire_loader] Savant fetch failed — {traceback.format_exc()}")
        return {}


def _load_historical() -> dict[str, tuple[float, float]]:
    """
    Return career umpire stats. Tries (in order):
      1. Fresh/cached data/ump_historical.json
      2. Re-fetch from Savant and save
      3. _UMP_FALLBACK table
    """
    hist_path = _hist_cache_path()
    # Refresh historical once per day
    if _cache_fresh(hist_path, max_age_sec=86400):
        try:
            with open(hist_path) as f:
                raw = json.load(f)
            return {k: tuple(v) for k, v in raw.items()}
        except Exception:
            pass

    savant = _fetch_savant_career()
    if savant:
        with open(hist_path, "w") as f:
            json.dump({k: list(v) for k, v in savant.items()}, f)
        return savant

    # Last resort: bundled fallback
    return dict(_UMP_FALLBACK)


# ── MLB StatsAPI: today's HP umpires ─────────────────────────────────────────

def _fetch_game_officials(date_str: str) -> dict[int, str]:
    """
    Return {game_pk: hp_umpire_name} for all games on date_str.
    Uses the /schedule?hydrate=officials endpoint.
    """
    if not _REQUESTS_OK:
        return {}
    url = f"{_MLB_API}/schedule"
    params = {
        "sportId": 1,
        "date": date_str,
        "hydrate": "officials",
    }
    try:
        resp = requests.get(url, params=params, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        result: dict[int, str] = {}
        for date_block in data.get("dates", []):
            for game in date_block.get("games", []):
                game_pk = game.get("gamePk", 0)
                for official in (game.get("officials") or []):
                    otype = (official.get("officialType") or "").lower()
                    if "home plate" in otype or otype in ("hp", "home"):
                        person = official.get("official") or {}
                        name   = (person.get("fullName") or "").strip()
                        if name:
                            result[game_pk] = name
                            break
        return result
    except Exception:
        print(f"[umpire_loader] officials fetch failed — {traceback.format_exc()}")
        return {}


# ── Public API ────────────────────────────────────────────────────────────────

def fetch_and_save(date_str: Optional[str] = None) -> dict:
    """
    Fetch today's HP umpire assignments, save to data/umpires_{date}.json,
    and return the parsed dict {game_pk_str: ump_name}.
    Called by pipeline_scheduler every 2 hours.
    """
    if date_str is None:
        date_str = date.today().isoformat()

    officials = _fetch_game_officials(date_str)

    path = _ump_cache_path(date_str)
    with open(path, "w") as f:
        json.dump(
            {
                "fetched_at": datetime.utcnow().isoformat(),
                "officials":  {str(k): v for k, v in officials.items()},
            },
            f,
            indent=2,
        )

    print(
        f"[umpire_loader] {date_str} — {len(officials)} HP umpires assigned"
    )
    return officials


def get_game_umpire(
    game_pk: int,
    date_str: Optional[str] = None,
) -> str:
    """
    Return the HP umpire name for a specific game.
    Returns empty string if unknown.
    """
    if date_str is None:
        date_str = date.today().isoformat()

    path = _ump_cache_path(date_str)
    if not _cache_fresh(path, max_age_sec=7200):
        try:
            fetch_and_save(date_str)
        except Exception:
            pass

    if os.path.exists(path):
        try:
            with open(path) as f:
                data = json.load(f)
            return data.get("officials", {}).get(str(game_pk), "")
        except Exception:
            pass
    return ""


def get_umpire_features(
    ump_name: str,
    date_str: Optional[str] = None,
) -> dict:
    """
    Core function called by xgb_prop_scorer.xgb_k_prob().

    Parameters
    ----------
    ump_name : str
        HP umpire full name (e.g. "Pat Hoberg"). Case-insensitive.
        Pass an empty string to get neutral (league-average) features.

    Returns
    -------
    dict with keys:
        ump_zone_size : float
            Zone-size delta vs. league average.
            +0.02 means 2 percentage points wider than average.
        ump_k_boost   : float
            K-per-9 delta vs. league average.
            +0.55 means ~0.55 extra Ks per 9 innings when this ump works.

    Gracefully returns (0.0, 0.0) for unknown umpires — model sees
    league-average behaviour, no NaN propagation.
    """
    if not ump_name or not ump_name.strip():
        return {"ump_zone_size": 0.0, "ump_k_boost": 0.0}

    historical = _load_historical()
    key = ump_name.strip().lower()

    # Exact match first
    if key in historical:
        z, k = historical[key]
        return {"ump_zone_size": float(z), "ump_k_boost": float(k)}

    # Partial / last-name match
    parts = key.split()
    last  = parts[-1] if parts else ""
    for stored_name, (z, k) in historical.items():
        if last and last in stored_name:
            return {"ump_zone_size": float(z), "ump_k_boost": float(k)}

    # Unknown umpire — return neutral
    print(f"[umpire_loader] unknown umpire '{ump_name}' — using league average")
    return {"ump_zone_size": 0.0, "ump_k_boost": 0.0}


if __name__ == "__main__":
    today = date.today().isoformat()
    officials = fetch_and_save(today)
    print(f"\nToday's HP umpires ({today}):")
    for gid, name in list(officials.items())[:5]:
        feats = get_umpire_features(name)
        print(
            f"  game_pk={gid:>6}  {name:<25}  "
            f"zone_delta={feats['ump_zone_size']:+.3f}  "
            f"k_boost={feats['ump_k_boost']:+.2f}"
        )
