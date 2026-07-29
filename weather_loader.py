"""
weather_loader.py  —  Game-Day Weather Features for MLB Prop Pipeline
═══════════════════════════════════════════════════════════════════════
Fetches temperature, wind speed, and wind direction for every MLB
ballpark on game day and converts them into three XGB features:

  temperature          — °F at first-pitch time
  wind_speed           — mph (0–40+ clipped at 50)
  wind_direction_factor — float in [-1.0, +1.0]
                          +1.0 = pure blowout directly to CF (best for HR/TB)
                          -1.0 = direct headwind from CF (worst for HR/TB)
                           0.0 = crosswind or neutral (no significant effect)

Data source: Open-Meteo API (https://open-meteo.com)
  • Free, no API key required
  • Hourly forecast: temperature_2m, wind_speed_10m, wind_direction_10m
  • Fetches the hour closest to 7 PM local park time as first-pitch proxy

Cache: data/weather_{date}.json — refreshed every 3 hours.
Fallback: park historical seasonal averages when API is unreachable.

Public API:
  get_game_weather(game_pk, date_str)  → {temperature, wind_speed, wind_direction_factor}
  get_all_weather(date_str)            → {game_pk: {...}, ...}
  fetch_and_save(date_str)             → dict (force refresh + save)
"""

from __future__ import annotations

import json
import math
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

_METEO_API     = "https://api.open-meteo.com/v1/forecast"
_TIMEOUT       = 12
_CACHE_TTL_SEC = 10_800   # 3 hours


# ── Stadium registry ─────────────────────────────────────────────────────────
# (team_abbrev, lat, lon, cf_bearing_deg, roof_type)
# cf_bearing_deg: compass bearing FROM home plate TOWARD center field.
#   Used to compute wind_direction_factor relative to prevailing wind.
# roof_type: "open" | "retractable" | "dome"
#   dome → weather features zeroed (controlled environment).
#   retractable → treat as open unless dome_closed flag set (not tracked here;
#                 default to open — conservative but safe).
_STADIUMS: dict[str, dict] = {
    # ── American League ───────────────────────────────────────────────────
    "BAL": {"name": "Camden Yards",            "lat": 39.2838, "lon": -76.6218, "cf_bearing": 335, "roof": "open"},
    "BOS": {"name": "Fenway Park",              "lat": 42.3467, "lon": -71.0972, "cf_bearing":  90, "roof": "open"},
    "NYY": {"name": "Yankee Stadium",           "lat": 40.8296, "lon": -73.9262, "cf_bearing": 320, "roof": "open"},
    "TB":  {"name": "Tropicana Field",          "lat": 27.7683, "lon": -82.6534, "cf_bearing": 290, "roof": "dome"},
    "TOR": {"name": "Rogers Centre",            "lat": 43.6415, "lon": -79.3892, "cf_bearing":  10, "roof": "retractable"},
    "CWS": {"name": "Guaranteed Rate Field",    "lat": 41.8300, "lon": -87.6339, "cf_bearing": 355, "roof": "open"},
    "CLE": {"name": "Progressive Field",        "lat": 41.4962, "lon": -81.6852, "cf_bearing": 325, "roof": "open"},
    "DET": {"name": "Comerica Park",            "lat": 42.3390, "lon": -83.0485, "cf_bearing":  10, "roof": "open"},
    "KC":  {"name": "Kauffman Stadium",         "lat": 39.0517, "lon": -94.4803, "cf_bearing": 350, "roof": "open"},
    "MIN": {"name": "Target Field",             "lat": 44.9817, "lon": -93.2781, "cf_bearing": 300, "roof": "open"},
    "HOU": {"name": "Minute Maid Park",         "lat": 29.7573, "lon": -95.3555, "cf_bearing":  35, "roof": "retractable"},
    "LAA": {"name": "Angel Stadium",            "lat": 33.8003, "lon": -117.8827,"cf_bearing": 345, "roof": "open"},
    "OAK": {"name": "Oakland Coliseum",         "lat": 37.7516, "lon": -122.2005,"cf_bearing": 320, "roof": "open"},
    "ATH": {"name": "Sutter Health Park",       "lat": 38.5798, "lon": -121.5056,"cf_bearing": 320, "roof": "open"},  # Sacramento (2025 temp)
    "SEA": {"name": "T-Mobile Park",            "lat": 47.5914, "lon": -122.3322,"cf_bearing": 325, "roof": "retractable"},
    "TEX": {"name": "Globe Life Field",         "lat": 32.7512, "lon": -97.0832, "cf_bearing":  10, "roof": "retractable"},
    # ── National League ───────────────────────────────────────────────────
    "ATL": {"name": "Truist Park",              "lat": 33.8908, "lon": -84.4678, "cf_bearing": 355, "roof": "open"},
    "MIA": {"name": "loanDepot park",           "lat": 25.7781, "lon": -80.2197, "cf_bearing":  10, "roof": "retractable"},
    "NYM": {"name": "Citi Field",               "lat": 40.7571, "lon": -73.8458, "cf_bearing": 325, "roof": "open"},
    "PHI": {"name": "Citizens Bank Park",       "lat": 39.9061, "lon": -75.1665, "cf_bearing":   5, "roof": "open"},
    "WSH": {"name": "Nationals Park",           "lat": 38.8730, "lon": -77.0074, "cf_bearing": 330, "roof": "open"},
    "CHC": {"name": "Wrigley Field",            "lat": 41.9484, "lon": -87.6553, "cf_bearing":  10, "roof": "open"},
    "CIN": {"name": "Great American Ball Park", "lat": 39.0979, "lon": -84.5082, "cf_bearing": 340, "roof": "open"},
    "MIL": {"name": "American Family Field",    "lat": 43.0280, "lon": -87.9712, "cf_bearing": 355, "roof": "retractable"},
    "PIT": {"name": "PNC Park",                "lat": 40.4469, "lon": -80.0057, "cf_bearing": 325, "roof": "open"},
    "STL": {"name": "Busch Stadium",            "lat": 38.6226, "lon": -90.1928, "cf_bearing": 330, "roof": "open"},
    "ARI": {"name": "Chase Field",              "lat": 33.4453, "lon": -112.0667,"cf_bearing": 335, "roof": "retractable"},
    "COL": {"name": "Coors Field",              "lat": 39.7559, "lon": -104.9942,"cf_bearing": 320, "roof": "open"},
    "LAD": {"name": "Dodger Stadium",           "lat": 34.0739, "lon": -118.2400,"cf_bearing": 335, "roof": "open"},
    "SD":  {"name": "Petco Park",               "lat": 32.7073, "lon": -117.1573,"cf_bearing": 310, "roof": "open"},
    "SF":  {"name": "Oracle Park",              "lat": 37.7786, "lon": -122.3893,"cf_bearing":  15, "roof": "open"},
}

# Alternate team code aliases (MLB StatsAPI sometimes returns full names or alt codes)
_TEAM_ALIASES: dict[str, str] = {
    "ALS": "LAA", "LOS": "LAD", "SDP": "SD",  "SFG": "SF",
    "WSN": "WSH", "KCR": "KC",  "TBR": "TB",  "CHW": "CWS",
    "ARI": "ARI", "OAK": "OAK", "ATH": "ATH",
}

# Park-level historical seasonal averages (°F, mph) used as fallback
# when Open-Meteo is unreachable. Values are approximate April-October means.
_PARK_FALLBACK: dict[str, dict] = {
    "BAL": {"temperature": 72, "wind_speed": 9,  "wind_direction_factor":  0.0},
    "BOS": {"temperature": 68, "wind_speed": 13, "wind_direction_factor":  0.1},
    "NYY": {"temperature": 72, "wind_speed": 11, "wind_direction_factor":  0.0},
    "TB":  {"temperature": 72, "wind_speed":  0, "wind_direction_factor":  0.0},  # dome
    "TOR": {"temperature": 72, "wind_speed":  6, "wind_direction_factor":  0.0},  # retractable
    "CWS": {"temperature": 70, "wind_speed": 11, "wind_direction_factor":  0.0},
    "CLE": {"temperature": 68, "wind_speed": 13, "wind_direction_factor":  0.0},
    "DET": {"temperature": 70, "wind_speed": 10, "wind_direction_factor":  0.0},
    "KC":  {"temperature": 74, "wind_speed": 13, "wind_direction_factor":  0.1},
    "MIN": {"temperature": 68, "wind_speed": 11, "wind_direction_factor":  0.0},
    "HOU": {"temperature": 72, "wind_speed":  6, "wind_direction_factor":  0.0},  # retractable
    "LAA": {"temperature": 75, "wind_speed":  8, "wind_direction_factor":  0.0},
    "OAK": {"temperature": 62, "wind_speed": 14, "wind_direction_factor": -0.1},  # notoriously windy/cold
    "ATH": {"temperature": 75, "wind_speed": 10, "wind_direction_factor":  0.0},
    "SEA": {"temperature": 63, "wind_speed":  9, "wind_direction_factor":  0.0},  # retractable
    "TEX": {"temperature": 78, "wind_speed":  9, "wind_direction_factor":  0.0},  # retractable
    "ATL": {"temperature": 78, "wind_speed":  8, "wind_direction_factor":  0.0},
    "MIA": {"temperature": 82, "wind_speed":  6, "wind_direction_factor":  0.0},  # retractable
    "NYM": {"temperature": 72, "wind_speed": 12, "wind_direction_factor":  0.1},
    "PHI": {"temperature": 73, "wind_speed": 10, "wind_direction_factor":  0.0},
    "WSH": {"temperature": 75, "wind_speed":  9, "wind_direction_factor":  0.0},
    "CHC": {"temperature": 68, "wind_speed": 15, "wind_direction_factor":  0.2},  # famous Lake Michigan wind
    "CIN": {"temperature": 74, "wind_speed": 10, "wind_direction_factor":  0.0},
    "MIL": {"temperature": 67, "wind_speed": 11, "wind_direction_factor":  0.0},  # retractable
    "PIT": {"temperature": 70, "wind_speed": 10, "wind_direction_factor":  0.0},
    "STL": {"temperature": 76, "wind_speed": 12, "wind_direction_factor":  0.1},
    "ARI": {"temperature": 80, "wind_speed":  7, "wind_direction_factor":  0.0},  # retractable
    "COL": {"temperature": 70, "wind_speed": 13, "wind_direction_factor":  0.1},  # thin air + wind matters
    "LAD": {"temperature": 73, "wind_speed":  8, "wind_direction_factor":  0.0},
    "SD":  {"temperature": 68, "wind_speed": 10, "wind_direction_factor":  0.0},
    "SF":  {"temperature": 60, "wind_speed": 16, "wind_direction_factor": -0.2},  # notoriously cold/windy
}

_NEUTRAL_WEATHER = {"temperature": 72.0, "wind_speed": 8.0, "wind_direction_factor": 0.0}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _cache_path(date_str: str) -> str:
    return os.path.join(_DATA_DIR, f"weather_{date_str}.json")

def _cache_fresh(path: str) -> bool:
    if not os.path.exists(path):
        return False
    return (time.time() - os.path.getmtime(path)) < _CACHE_TTL_SEC

def _resolve_team(raw: str) -> Optional[str]:
    """Normalize a team code to our _STADIUMS key."""
    t = (raw or "").strip().upper()
    return _TEAM_ALIASES.get(t, t) if t in _STADIUMS or t in _TEAM_ALIASES else None

def _celsius_to_f(c: float) -> float:
    return c * 9 / 5 + 32

def _ms_to_mph(ms: float) -> float:
    return ms * 2.23694

def _kmh_to_mph(kmh: float) -> float:
    return kmh * 0.621371

def _wind_direction_factor(wind_bearing_deg: float, cf_bearing_deg: float) -> float:
    """
    Compute how much the wind blows OUT toward center field.

    wind_bearing_deg : direction the wind is blowing TOWARD (meteorological
                       standard: 0 = wind blowing north, 90 = east, etc.)
    cf_bearing_deg   : compass bearing from home plate to center field.

    Returns a float in [-1.0, +1.0]:
      +1.0 → wind blows directly from home plate toward CF (blowout)
      -1.0 → wind blows directly from CF toward home plate (suppressor)
       0.0 → crosswind (90° off CF axis)

    Uses cosine of the angle difference — smooth, continuous, symmetric.
    """
    diff_rad = math.radians(wind_bearing_deg - cf_bearing_deg)
    return round(math.cos(diff_rad), 4)


# ── Open-Meteo fetcher ────────────────────────────────────────────────────────

def _fetch_meteo(lat: float, lon: float, date_str: str) -> Optional[dict]:
    """
    Call Open-Meteo hourly forecast API and extract the 7 PM local hour values
    (first-pitch proxy) for temperature_2m, wind_speed_10m, wind_direction_10m.

    Open-Meteo returns wind speed in km/h by default; we convert to mph.
    Temperature is in °C; we convert to °F.

    Returns None on any error.
    """
    if not _REQUESTS_OK:
        return None
    params = {
        "latitude":             lat,
        "longitude":            lon,
        "hourly":               "temperature_2m,wind_speed_10m,wind_direction_10m",
        "temperature_unit":     "celsius",
        "wind_speed_unit":      "kmh",
        "timezone":             "auto",
        "start_date":           date_str,
        "end_date":             date_str,
        "forecast_days":        1,
    }
    try:
        resp = requests.get(_METEO_API, params=params, timeout=_TIMEOUT)
        resp.raise_for_status()
        data    = resp.json()
        hourly  = data.get("hourly", {})
        times   = hourly.get("time", [])
        temps   = hourly.get("temperature_2m", [])
        speeds  = hourly.get("wind_speed_10m", [])
        dirs    = hourly.get("wind_direction_10m", [])

        # Find the 19:00 (7 PM) local hour index; fall back to 18:00 then 20:00
        target_hours = ["T19:00", "T18:00", "T20:00", "T17:00", "T21:00"]
        idx = None
        for th in target_hours:
            for i, t in enumerate(times):
                if t.endswith(th):
                    idx = i
                    break
            if idx is not None:
                break

        if idx is None or idx >= len(temps):
            # Fall back to daily mean if no evening hour found
            valid_temps  = [v for v in temps  if v is not None]
            valid_speeds = [v for v in speeds if v is not None]
            valid_dirs   = [v for v in dirs   if v is not None]
            if not valid_temps:
                return None
            return {
                "temp_c":        sum(valid_temps)  / len(valid_temps),
                "wind_kmh":      sum(valid_speeds) / max(len(valid_speeds), 1),
                "wind_dir_deg":  sum(valid_dirs)   / max(len(valid_dirs),   1),
            }

        return {
            "temp_c":       temps[idx],
            "wind_kmh":     speeds[idx] if idx < len(speeds) else 0.0,
            "wind_dir_deg": dirs[idx]   if idx < len(dirs)   else 180.0,
        }
    except Exception:
        print(f"[weather_loader] Open-Meteo fetch failed ({lat},{lon}) — {traceback.format_exc()}")
        return None


def _weather_for_team(team_code: str, date_str: str) -> dict:
    """
    Return weather feature dict for a team's home ballpark.
    Falls back to park historical averages on any fetch failure.
    Domed stadiums return neutral weather regardless.
    """
    key     = _resolve_team(team_code)
    stadium = _STADIUMS.get(key or "")
    fallback = _PARK_FALLBACK.get(key or "", _NEUTRAL_WEATHER)

    if not stadium:
        print(f"[weather_loader] unknown team '{team_code}' — using neutral weather")
        return dict(_NEUTRAL_WEATHER)

    # Domed stadiums: controlled environment, no weather effect
    if stadium["roof"] == "dome":
        return {"temperature": 72.0, "wind_speed": 0.0, "wind_direction_factor": 0.0}

    raw = _fetch_meteo(stadium["lat"], stadium["lon"], date_str)
    if not raw:
        print(f"[weather_loader] using fallback for {key}")
        return dict(fallback)

    temp_f      = round(_celsius_to_f(raw["temp_c"]), 1)
    wind_mph    = round(min(_kmh_to_mph(raw["wind_kmh"]), 50.0), 1)  # clip at 50 mph
    wdf         = _wind_direction_factor(raw["wind_dir_deg"], stadium["cf_bearing"])

    return {
        "temperature":           temp_f,
        "wind_speed":            wind_mph,
        "wind_direction_factor": wdf,
    }


# ── MLB schedule → home team lookup ──────────────────────────────────────────

def _fetch_home_teams(date_str: str) -> dict[int, str]:
    """
    Return {game_pk: home_team_code} for all games on date_str via MLB StatsAPI.
    """
    if not _REQUESTS_OK:
        return {}
    try:
        result: dict[int, str] = {}
        for game in mlb_client.schedule(date_str=date_str, timeout=_TIMEOUT):
            pk   = game.get("gamePk", 0)
            code = (
                game.get("teams", {})
                    .get("home", {})
                    .get("team", {})
                    .get("abbreviation", "")
            )
            if pk and code:
                result[pk] = code
        return result
    except Exception:
        print(f"[weather_loader] schedule fetch failed — {traceback.format_exc()}")
        return {}


# ── Public API ────────────────────────────────────────────────────────────────

def fetch_and_save(date_str: Optional[str] = None) -> dict:
    """
    Fetch weather for all games on date_str, save to data/weather_{date}.json,
    and return {game_pk (int): weather_dict}.

    Respects 3-hour TTL cache — safe to call repeatedly from the scheduler.
    """
    if date_str is None:
        date_str = date.today().isoformat()

    path = _cache_path(date_str)

    # TTL guard
    if _cache_fresh(path):
        try:
            with open(path) as f:
                raw = json.load(f)
            cached = {int(k): v for k, v in raw.get("games", {}).items()}
            if cached:
                return cached
        except Exception:
            pass

    home_teams = _fetch_home_teams(date_str)
    # Deduplicate teams so we only hit Open-Meteo once per unique park
    unique_teams: dict[str, dict] = {}
    for gid, team in home_teams.items():
        key = _resolve_team(team) or team
        if key not in unique_teams:
            unique_teams[key] = _weather_for_team(team, date_str)

    result: dict[int, dict] = {}
    for gid, team in home_teams.items():
        key = _resolve_team(team) or team
        result[gid] = unique_teams.get(key, dict(_NEUTRAL_WEATHER))

    with open(path, "w") as f:
        json.dump(
            {
                "fetched_at": datetime.utcnow().isoformat(),
                "date":       date_str,
                "games":      {str(k): v for k, v in result.items()},
            },
            f,
            indent=2,
        )

    print(f"[weather_loader] {date_str} — weather fetched for {len(result)} games")
    return result


def get_all_weather(date_str: Optional[str] = None) -> dict:
    """
    Return {game_pk (int): {temperature, wind_speed, wind_direction_factor}}
    for all games on date_str. Triggers fetch_and_save if cache is stale.
    """
    if date_str is None:
        date_str = date.today().isoformat()
    return fetch_and_save(date_str)


def get_game_weather(
    game_pk: int,
    date_str: Optional[str] = None,
) -> dict:
    """
    Return weather features for a single game.
    Returns neutral weather dict if game_pk not found.

    Called by pipeline_scheduler._rescore_with_confirmed_lineups()
    and optionally by xgb_prop_scorer.xgb_hits_prob() for HR/TB props.
    """
    if date_str is None:
        date_str = date.today().isoformat()
    all_wx = get_all_weather(date_str)
    return all_wx.get(game_pk, dict(_NEUTRAL_WEATHER))


if __name__ == "__main__":
    today = date.today().isoformat()
    print(f"Fetching weather for {today}...\n")
    games = fetch_and_save(today)
    for gid, wx in list(games.items())[:6]:
        print(
            f"  game_pk={gid:>7}  "
            f"temp={wx['temperature']:>5.1f}°F  "
            f"wind={wx['wind_speed']:>5.1f}mph  "
            f"wdf={wx['wind_direction_factor']:>+.3f}"
        )
