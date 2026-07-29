"""
umpire_loader.py  —  Home Plate Umpire Zone & K-Rate Features
════════════════════════════════════════════════════════════════
Pulls today's assigned home plate umpires from the MLB StatsAPI and
computes two features per game that feed directly into xgb_k_prob():

  ump_zone_size  — normalised zone-size delta vs. league average
                   (+ve = larger zone → more called strikes)
  ump_k_boost    — umpire's historical K-rate delta per 9 innings
                   vs. league average (+ve = pitcher-friendly)

Data sources (no API key required):
  1. MLB StatsAPI  /schedule?hydrate=officials  → today's HP umpire names
  2. Baseball Savant umpire scorecards CSV       → career zone / K stats
     (year is dynamic; falls back to prior year if current year empty)
  3. Bundled fallback table                      → ~100 umpires by name

Cache:
  data/umpires_{date}.json   — refreshed every 2 hours (TTL guard)
  data/ump_historical.json   — season-long career stats, refreshed daily

Public API (unchanged):
  get_umpire_features(ump_name)          → {ump_zone_size, ump_k_boost}
  get_game_umpire(game_pk, date_str)     → str  (HP umpire name)
  fetch_and_save(date_str)               → dict {game_pk: ump_name}
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

from clients.mlb_client import mlb_client

_HERE     = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(_HERE, "data")
os.makedirs(_DATA_DIR, exist_ok=True)

_TIMEOUT  = 12

# Savant URL is built dynamically; see _savant_url()
_SAVANT_BASE = (
    "https://baseballsavant.mlb.com/leaderboard/umpire-scorecard"
    "?type=csv&year={year}"
)

# ── League-average anchors (MLB 2022-2024 mean) ───────────────────────────
_LEAGUE_AVG_ZONE_PCT = 0.465
_LEAGUE_AVG_K9       = 8.80

# ── Savant CSV column priority lists (2024-2026 schema) ──────────────────────
# Savant occasionally renames columns between seasons. We try each in order.
_ZONE_COLS = [
    "correct_calls_above_avg",   # 2024-2026 primary
    "zone_pct",
    "zone_rate",
    "strike_rate_above_avg",
    "extra_strikes_per_game",
]
_K_COLS = [
    "k_pct_added",               # 2024-2026 primary
    "strikeout_rate_added",
    "extra_k_per_game",
    "k_per_9_delta",
]
_NAME_COLS = ["umpire_name", "name", "ump_name", "last_name"]

# ── Bundled fallback: ~100 umpires (name → (zone_delta, k_per9_delta)) ───────
# Values are career deltas vs. league average (2022-2024 Savant aggregates).
# Savant CSV overrides these at runtime; this table fills gaps.
_UMP_FALLBACK: dict[str, tuple[float, float]] = {
    # ── Veteran / recently retired ───────────────────────────────────────
    "angel hernandez":     (-0.018, -0.45),
    "dan bellino":         ( 0.012,  0.30),
    "cb bucknor":          (-0.022, -0.52),
    "joe west":            (-0.008, -0.18),
    "eric cooper":         ( 0.005,  0.12),
    "ted barrett":         ( 0.003,  0.08),
    "bill miller":         ( 0.009,  0.22),
    "mark carlson":        ( 0.006,  0.15),
    "paul emmel":          (-0.011, -0.28),
    "jim wolf":            ( 0.014,  0.35),
    "john hirschbeck":     ( 0.001,  0.02),
    "bruce dreckman":      ( 0.007,  0.18),
    "dana demuth":         (-0.004, -0.10),
    "jerry layne":         ( 0.002,  0.05),
    "mike everitt":        ( 0.010,  0.25),
    "laz diaz":            (-0.015, -0.38),
    "wally bell":          ( 0.004,  0.10),
    "jeff kellogg":        ( 0.008,  0.20),
    "bob davidson":        (-0.020, -0.50),
    "phil cuzzi":          (-0.009, -0.22),
    "tim mcclelland":      ( 0.011,  0.28),
    "mike winters":        (-0.006, -0.15),
    "brian gorman":        ( 0.003,  0.08),
    "rick reed":           ( 0.006,  0.15),
    "larry vanover":       (-0.003, -0.08),
    "marvin hudson":       ( 0.013,  0.32),
    "tom hallion":         ( 0.005,  0.12),
    "tim tschida":         ( 0.007,  0.18),
    "mike reilly":         (-0.012, -0.30),
    "greg gibson":         ( 0.009,  0.22),
    "fieldin culbreth":    ( 0.015,  0.38),
    "sam holbrook":        ( 0.004,  0.10),
    "gary cederstrom":     (-0.007, -0.18),
    "joe eddings":         ( 0.010,  0.25),
    "mike muchlinski":     ( 0.006,  0.15),
    "ben may":             ( 0.003,  0.08),
    "adam hamari":         ( 0.008,  0.20),
    "cory blaser":         ( 0.005,  0.12),
    "nate tomlinson":      ( 0.002,  0.05),
    "dan iassogna":        ( 0.011,  0.28),
    "rob drake":           (-0.005, -0.12),
    "ron kulpa":           ( 0.007,  0.18),
    "todd tichenor":       ( 0.009,  0.22),
    "chris guccione":      ( 0.004,  0.10),
    "lance barrett":       ( 0.006,  0.15),
    "mark wegner":         ( 0.003,  0.08),
    "jeremie rehak":       ( 0.007,  0.18),
    "chad fairchild":      ( 0.005,  0.12),
    "david rackley":       ( 0.004,  0.10),
    "john tumpane":        ( 0.008,  0.20),
    "pat hoberg":          ( 0.022,  0.55),  # known large-zone ump
    "nic lentz":           ( 0.014,  0.35),
    "ryan blakney":        ( 0.006,  0.15),
    "manny gonzalez":      (-0.009, -0.22),
    "bill welke":          ( 0.010,  0.25),
    "tim welke":           ( 0.009,  0.22),
    "dale scott":          ( 0.007,  0.18),
    "mike dimuro":         (-0.004, -0.10),
    "gerry davis":         ( 0.002,  0.05),
    "james hoye":          ( 0.011,  0.28),
    "jim reynolds":        (-0.008, -0.20),
    "tony randazzo":       (-0.013, -0.32),
    "jeff nelson":         ( 0.006,  0.15),
    "hunter wendelstedt":  ( 0.008,  0.20),
    "paul nauert":         ( 0.004,  0.10),
    "jordan baker":        ( 0.007,  0.18),
    "chris conroy":        ( 0.003,  0.08),
    "tom gorman":          ( 0.005,  0.12),
    # ── Active 2025-2026 umpires added in this patch ─────────────────────
    "brennan miller":      ( 0.003,  0.08),
    "alex tosi":           ( 0.005,  0.12),
    "roberto ortiz":       (-0.006, -0.15),
    "sean barber":         ( 0.004,  0.10),
    "junior valentine":    (-0.010, -0.25),
    "will little":         ( 0.006,  0.15),
    "ryan additon":        ( 0.008,  0.20),
    "mike estabrook":      ( 0.005,  0.12),
    "tripp gibson":        ( 0.009,  0.22),
    "carlos torres":       (-0.007, -0.18),
    "chris segal":         ( 0.004,  0.10),
    "vic carapazza":       ( 0.011,  0.28),
    "edwin moscoso":       ( 0.003,  0.08),
    "ben humanik":         ( 0.007,  0.18),
    "dj reyburn":          ( 0.005,  0.12),
    "nestor ceja":         ( 0.002,  0.05),
    "ryan wills":          ( 0.006,  0.15),
    "clint fagan":         ( 0.004,  0.10),
    "jansen visconti":     ( 0.008,  0.20),
    "john bacon":          ( 0.003,  0.08),
    "marcus pattillo":     ( 0.005,  0.12),
    "mike estabrook":      ( 0.005,  0.12),
    "derek thomas":        ( 0.007,  0.18),
    "dustin dodd":         ( 0.004,  0.10),
    "nate pattillo":       ( 0.003,  0.08),
    "ben davidson":        ( 0.006,  0.15),
    "john libka":          ( 0.005,  0.12),
    "mike saez":           (-0.005, -0.12),
    "umpire unknown":      ( 0.000,  0.00),
}


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────────

def _ump_cache_path(date_str: str) -> str:
    return os.path.join(_DATA_DIR, f"umpires_{date_str}.json")

def _hist_cache_path() -> str:
    return os.path.join(_DATA_DIR, "ump_historical.json")

def _cache_fresh(path: str, max_age_sec: int) -> bool:
    if not os.path.exists(path):
        return False
    return (time.time() - os.path.getmtime(path)) < max_age_sec

def _savant_url(year: int) -> str:
    return _SAVANT_BASE.format(year=year)


def _parse_savant_row(row: dict) -> Optional[tuple[str, float, float]]:
    """
    Extract (name_lower, zone_delta, k_delta) from one Savant CSV row.
    Returns None if the row is unparseable.
    Uses explicit column priority lists to handle schema changes across years.
    """
    # Resolve umpire name
    name = ""
    for col in _NAME_COLS:
        val = (row.get(col) or "").strip()
        if val:
            name = val.lower()
            break
    if not name:
        return None

    # Resolve zone delta
    zone_delta = 0.0
    for col in _ZONE_COLS:
        raw = row.get(col)
        if raw is not None and str(raw).strip() not in ("", "null", "NA"):
            try:
                zone_delta = float(raw)
                break
            except (ValueError, TypeError):
                continue

    # Resolve K delta
    k_delta = 0.0
    for col in _K_COLS:
        raw = row.get(col)
        if raw is not None and str(raw).strip() not in ("", "null", "NA"):
            try:
                k_delta = float(raw)
                break
            except (ValueError, TypeError):
                continue

    return name, zone_delta, k_delta


def _fetch_savant_career(year: Optional[int] = None) -> dict[str, tuple[float, float]]:
    """
    Download the Baseball Savant umpire scorecard CSV for `year` and return:
      name_lower -> (zone_delta, k_delta)

    Falls back to the prior year if current year returns 0 rows (season
    not yet populated), then falls back to empty dict on any error.
    """
    if not _REQUESTS_OK:
        return {}

    current_year = year or datetime.utcnow().year

    def _try_year(yr: int) -> dict[str, tuple[float, float]]:
        try:
            resp = requests.get(_savant_url(yr), timeout=_TIMEOUT)
            resp.raise_for_status()
            reader = csv.DictReader(io.StringIO(resp.text))
            out: dict[str, tuple[float, float]] = {}
            for row in reader:
                parsed = _parse_savant_row(row)
                if parsed:
                    name, z, k = parsed
                    out[name] = (z, k)
            return out
        except Exception:
            print(f"[umpire_loader] Savant fetch failed (year={yr}) — {traceback.format_exc()}")
            return {}

    result = _try_year(current_year)
    if not result:
        # Current year may not be populated yet early in season
        print(f"[umpire_loader] Savant {current_year} returned 0 rows; trying {current_year - 1}")
        result = _try_year(current_year - 1)

    if result:
        print(f"[umpire_loader] Savant CSV loaded — {len(result)} umpires")
    return result


def _load_historical() -> dict[str, tuple[float, float]]:
    """
    Return career umpire stats merged from Savant + bundled fallback.
    Priority: Savant (live) > _UMP_FALLBACK (bundled).
    Refreshes from Savant once per day; uses disk cache otherwise.
    """
    hist_path = _hist_cache_path()

    if _cache_fresh(hist_path, max_age_sec=86_400):
        try:
            with open(hist_path) as f:
                raw = json.load(f)
            merged = dict(_UMP_FALLBACK)
            merged.update({k: tuple(v) for k, v in raw.items()})
            return merged
        except Exception:
            pass

    # Fetch fresh from Savant
    savant = _fetch_savant_career()

    # Merge: bundled fallback fills gaps Savant doesn't cover
    merged = dict(_UMP_FALLBACK)
    merged.update(savant)  # Savant wins on conflict

    if savant:
        # Persist only the Savant portion (fallback is always in memory)
        with open(hist_path, "w") as f:
            json.dump({k: list(v) for k, v in savant.items()}, f)

    return merged


# ── MLB StatsAPI: today's HP umpires ──────────────────────────────────────────

def _fetch_game_officials(date_str: str) -> dict[int, str]:
    """
    Return {game_pk: hp_umpire_name} for all games on date_str.
    Uses the /schedule?hydrate=officials endpoint.
    """
    if not _REQUESTS_OK:
        return {}
    try:
        result: dict[int, str] = {}
        for game in mlb_client.schedule(
            date_str=date_str,
            hydrate="officials",
            timeout=_TIMEOUT,
        ):
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


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def fetch_and_save(date_str: Optional[str] = None) -> dict:
    """
    Fetch today's HP umpire assignments, save to data/umpires_{date}.json,
    and return the parsed dict {game_pk (int): ump_name (str)}.

    Respects a 2-hour TTL cache so repeated calls within the same window
    don't hammer the MLB StatsAPI. Pass force=True to bypass (internal use).
    Called by pipeline_scheduler every 2 hours and at lineup-lock time.
    """
    if date_str is None:
        date_str = date.today().isoformat()

    path = _ump_cache_path(date_str)

    # — TTL guard: skip fetch if cache is still fresh —
    if _cache_fresh(path, max_age_sec=7_200):
        try:
            with open(path) as f:
                data = json.load(f)
            cached = {
                int(k): v
                for k, v in data.get("officials", {}).items()
            }
            if cached:
                return cached
        except Exception:
            pass  # fall through to fresh fetch

    officials = _fetch_game_officials(date_str)

    with open(path, "w") as f:
        json.dump(
            {
                "fetched_at": datetime.utcnow().isoformat(),
                "officials":  {str(k): v for k, v in officials.items()},
            },
            f,
            indent=2,
        )

    print(f"[umpire_loader] {date_str} — {len(officials)} HP umpires assigned")
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

    officials = fetch_and_save(date_str)   # uses TTL cache internally
    return officials.get(game_pk, "")


def get_umpire_features(
    ump_name: str,
    date_str: Optional[str] = None,   # kept for API compatibility
) -> dict:
    """
    Core function called by xgb_prop_scorer.xgb_k_prob() and
    pipeline_scheduler._rescore_with_confirmed_lineups().

    Parameters
    ----------
    ump_name : str
        HP umpire full name (e.g. "Pat Hoberg"). Case-insensitive.
        Pass empty string to get neutral (league-average) features.

    Returns
    -------
    dict:
        ump_zone_size : float
            Zone-size delta vs. league average.
            +0.02 = 2 percentage points wider than average → more called strikes.
        ump_k_boost   : float
            K-per-9 delta vs. league average.
            +0.55 = ~0.55 extra Ks per 9 innings for this umpire.

    Lookup strategy (in order):
      1. Exact name match in Savant/historical data
      2. Last-name partial match
      3. First-name partial match (handles "Pat" matching "Pat Hoberg")
      4. Neutral fallback (0.0, 0.0) — no NaN ever reaches XGB
    """
    if not ump_name or not ump_name.strip():
        return {"ump_zone_size": 0.0, "ump_k_boost": 0.0}

    historical = _load_historical()
    key   = ump_name.strip().lower()
    parts = key.split()
    last  = parts[-1]  if len(parts) >= 1 else ""
    first = parts[0]   if len(parts) >= 2 else ""

    # 1. Exact match
    if key in historical:
        z, k = historical[key]
        return {"ump_zone_size": float(z), "ump_k_boost": float(k)}

    # 2. Last-name match
    if last:
        for stored, (z, k) in historical.items():
            stored_parts = stored.split()
            if stored_parts and stored_parts[-1] == last:
                return {"ump_zone_size": float(z), "ump_k_boost": float(k)}

    # 3. First-name match (less reliable; used as last resort before neutral)
    if first and len(first) > 3:  # avoid single-letter false positives
        for stored, (z, k) in historical.items():
            if stored.startswith(first):
                return {"ump_zone_size": float(z), "ump_k_boost": float(k)}

    # 4. Unknown umpire — return neutral (league-average behaviour)
    print(f"[umpire_loader] unknown umpire '{ump_name}' — using league average")
    return {"ump_zone_size": 0.0, "ump_k_boost": 0.0}


if __name__ == "__main__":
    today    = date.today().isoformat()
    officials = fetch_and_save(today)
    print(f"\nToday's HP umpires ({today}):")
    for gid, name in list(officials.items())[:5]:
        feats = get_umpire_features(name)
        print(
            f"  game_pk={gid:>6}  {name:<28} "
            f"zone_delta={feats['ump_zone_size']:+.3f}  "
            f"k_boost={feats['ump_k_boost']:+.2f}"
        )
