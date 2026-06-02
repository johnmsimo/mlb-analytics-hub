"""
lineup_loader.py  —  Confirmed MLB Lineup + PA Weighting
══════════════════════════════════════════════════════════
Pulls today's confirmed lineups from the MLB StatsAPI and computes
two game-day features for each batter:

  expected_pa      — estimated plate appearances given batting order position
                     and typical game length (uses historical PA-by-slot table)
  lineup_confirmed — 1 if lineup is official, 0 if projected/estimated

Also exposes get_lineup_features(player_name_or_id, game_pk) so the scorer
can inject confirmed PA exposure into the XGB feature vector.

API used: statsapi.mlb.com (no key required)
Cache:    data/lineups_{date}.json  — refreshed hourly by pipeline_scheduler
"""

from __future__ import annotations

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

_HERE      = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR  = os.path.join(_HERE, "data")
os.makedirs(_DATA_DIR, exist_ok=True)

# ── Historical average PA by batting order slot (MLB 2019-2024 average) ──────
# Slot 1 sees the most PAs; slot 9 the fewest.
# Source: Baseball Reference game-log aggregates.
_PA_BY_SLOT: dict[int, float] = {
    1: 4.60,
    2: 4.52,
    3: 4.44,
    4: 4.36,
    5: 4.28,
    6: 4.18,
    7: 4.08,
    8: 3.96,
    9: 3.84,
}
_DEFAULT_PA = 4.20  # fallback when slot unknown

# MLB StatsAPI base
_MLB_API = "https://statsapi.mlb.com/api/v1"
_TIMEOUT  = 10  # seconds


# ────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ────────────────────────────────────────────────────────────────────────────

def _cache_path(date_str: str) -> str:
    return os.path.join(_DATA_DIR, f"lineups_{date_str}.json")


def _cache_fresh(date_str: str, max_age_sec: int = 3600) -> bool:
    """True if cache file exists and is younger than max_age_sec."""
    path = _cache_path(date_str)
    if not os.path.exists(path):
        return False
    return (time.time() - os.path.getmtime(path)) < max_age_sec


def _fetch_games_for_date(date_str: str) -> list[dict]:
    """Return list of game dicts from MLB schedule API for the given date."""
    if not _REQUESTS_OK:
        return []
    url = f"{_MLB_API}/schedule"
    params = {
        "sportId": 1,
        "date": date_str,
        "hydrate": "lineups,probablePitcher,team",
    }
    try:
        resp = requests.get(url, params=params, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        games = []
        for date_block in data.get("dates", []):
            for g in date_block.get("games", []):
                games.append(g)
        return games
    except Exception:
        print(f"[lineup_loader] schedule fetch failed — {traceback.format_exc()}")
        return []


def _parse_lineups(games: list[dict]) -> dict:
    """
    Parse raw game dicts into a flat lookup:
      lineups[mlbam_id] = {
          "game_pk":           int,
          "team":              str,
          "batting_order":     int,   # 1-9
          "expected_pa":       float,
          "lineup_confirmed":  int,   # 1 = official, 0 = projected
          "player_name":       str,
      }
    """
    lineups: dict[int, dict] = {}

    for game in games:
        game_pk = game.get("gamePk", 0)
        status  = (game.get("status") or {}).get("abstractGameState", "").lower()
        if status == "final":
            continue

        for side in ("home", "away"):
            team_name = ((game.get("teams") or {}).get(side) or {}).get("team", {}).get("name", "")
            lineup_data = ((game.get("lineups") or {}).get(side + "Players") or [])

            confirmed = 1 if lineup_data else 0

            for entry in lineup_data:
                player     = entry.get("person") or {}
                mlbam_id   = player.get("id")
                name       = player.get("fullName", "")
                bat_order  = entry.get("battingOrder")

                if not mlbam_id:
                    continue

                slot: int
                try:
                    raw_order = int(bat_order)
                    slot = raw_order // 100 if raw_order >= 100 else raw_order
                except (TypeError, ValueError):
                    slot = 0

                exp_pa = _PA_BY_SLOT.get(slot, _DEFAULT_PA)

                lineups[int(mlbam_id)] = {
                    "game_pk":          game_pk,
                    "team":             team_name,
                    "batting_order":    slot,
                    "expected_pa":      exp_pa,
                    "lineup_confirmed": confirmed,
                    "player_name":      name,
                }

    return lineups


# ────────────────────────────────────────────────────────────────────────────
# Public API
# ────────────────────────────────────────────────────────────────────────────

def fetch_and_save(date_str: Optional[str] = None) -> dict:
    """
    Fetch today's lineups from MLB StatsAPI, save to data/lineups_{date}.json,
    and return the parsed lineup dict.

    Called by pipeline_scheduler to keep the cache fresh.
    """
    if date_str is None:
        date_str = date.today().isoformat()

    games   = _fetch_games_for_date(date_str)
    lineups = _parse_lineups(games)

    path = _cache_path(date_str)
    with open(path, "w") as f:
        json.dump({"fetched_at": datetime.utcnow().isoformat(), "lineups": lineups}, f, indent=2)

    n_confirmed = sum(1 for v in lineups.values() if v["lineup_confirmed"] == 1)
    print(
        f"[lineup_loader] {date_str} — {len(lineups)} batters across {len(games)} games "
        f"({n_confirmed} confirmed)"
    )
    return lineups


def get_lineup_features(
    mlbam_id: Optional[int] = None,
    player_name: Optional[str] = None,
    date_str: Optional[str] = None,
) -> dict:
    """
    Return lineup features for a given player on the given date.

    Lookup priority: mlbam_id > player_name (case-insensitive substring).

    Returns:
        {
          "expected_pa":      float,   # PA exposure from batting slot
          "batting_order":    int,     # 1-9, 0 if unknown
          "lineup_confirmed": int,     # 1 confirmed / 0 projected
        }
    If player not found, returns league-average defaults so the scorer
    always gets a valid float (no NaN propagation).
    """
    if date_str is None:
        date_str = date.today().isoformat()

    path = _cache_path(date_str)
    if not _cache_fresh(date_str):
        try:
            fetch_and_save(date_str)
        except Exception:
            pass  # use stale cache rather than crash

    lineups: dict = {}
    if os.path.exists(path):
        try:
            with open(path) as f:
                lineups = json.load(f).get("lineups", {})
            lineups = {int(k): v for k, v in lineups.items()}
        except Exception:
            pass

    # Lookup by MLBAM id
    if mlbam_id is not None:
        entry = lineups.get(int(mlbam_id))
        if entry:
            return {
                "expected_pa":      float(entry.get("expected_pa",  _DEFAULT_PA)),
                "batting_order":    int(entry.get("batting_order",   0)),
                "lineup_confirmed": int(entry.get("lineup_confirmed", 0)),
            }

    # Fallback: fuzzy name match
    if player_name:
        name_lower = player_name.lower()
        for entry in lineups.values():
            if name_lower in (entry.get("player_name") or "").lower():
                return {
                    "expected_pa":      float(entry.get("expected_pa",  _DEFAULT_PA)),
                    "batting_order":    int(entry.get("batting_order",   0)),
                    "lineup_confirmed": int(entry.get("lineup_confirmed", 0)),
                }

    # Player not in today's lineup or lineup not yet posted
    return {
        "expected_pa":      _DEFAULT_PA,
        "batting_order":    0,
        "lineup_confirmed": 0,
    }


if __name__ == "__main__":
    today = date.today().isoformat()
    data  = fetch_and_save(today)
    print(f"Loaded {len(data)} batters for {today}")
    for mid, v in list(data.items())[:3]:
        print(f"  {v['player_name']} (#{v['batting_order']}) — expected_pa={v['expected_pa']}  confirmed={v['lineup_confirmed']}")
