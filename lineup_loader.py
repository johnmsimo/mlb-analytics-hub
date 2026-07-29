"""
lineup_loader.py  —  Confirmed MLB Lineup + PA Weighting + Game Aggregates
════════════════════════════════════════════════════════════════════════
Pulls today's confirmed lineups from the MLB StatsAPI and computes
two per-batter game-day features:

  expected_pa      — estimated plate appearances given batting order position
                     and typical game length (uses historical PA-by-slot table)
  lineup_confirmed — 1 if lineup is official, 0 if projected/estimated

Also exposes:
  get_lineup_features(player_name_or_id, game_pk)   — per-batter PA features
  get_today_lineups(date_str)                        — per-game aggregate stats
      returns {game_pk: {home_k_pct, home_xwoba, away_k_pct, away_xwoba, ...}}
      used by pipeline_scheduler._rescore_with_confirmed_lineups() to patch
      opp_lineup_k_pct_proxy and opp_lineup_xwoba_proxy in the live matchup_df.

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

from clients.mlb_client import mlb_client

_HERE     = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(_HERE, "data")
os.makedirs(_DATA_DIR, exist_ok=True)

# ── Historical average PA by batting order slot (MLB 2019-2024) ───────────────
_PA_BY_SLOT: dict[int, float] = {
    1: 4.60, 2: 4.52, 3: 4.44, 4: 4.36, 5: 4.28,
    6: 4.18, 7: 4.08, 8: 3.96, 9: 3.84,
}
_DEFAULT_PA = 4.20

# League-average fallbacks (2024 MLB season)
_LG_K_PCT  = 22.3
_LG_XWOBA  = 0.318

_TIMEOUT  = 10

# In-process LRU cache for player season stats (cleared each scheduler run)
_player_stats_cache: dict[int, dict] = {}


# ───────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ───────────────────────────────────────────────────────────────────────────────

def _cache_path(date_str: str) -> str:
    return os.path.join(_DATA_DIR, f"lineups_{date_str}.json")


def _cache_fresh(date_str: str, max_age_sec: int = 3600) -> bool:
    path = _cache_path(date_str)
    if not os.path.exists(path):
        return False
    return (time.time() - os.path.getmtime(path)) < max_age_sec


def _fetch_games_for_date(date_str: str) -> list[dict]:
    """Return raw game dicts from MLB schedule API."""
    try:
        return mlb_client.schedule(
            date_str=date_str,
            hydrate="lineups,probablePitcher,team",
            timeout=_TIMEOUT,
        )
    except Exception:
        print(f"[lineup_loader] schedule fetch failed — {traceback.format_exc()}")
        return []


def _fetch_player_season_stats(mlbam_id: int, season: Optional[int] = None) -> dict:
    """
    Fetch current-season hitting stats for one player from MLB StatsAPI.
    Returns {"k_pct": float, "xwoba": float} using league-average fallbacks.
    Results are cached in _player_stats_cache for the lifetime of this run.
    """
    if mlbam_id in _player_stats_cache:
        return _player_stats_cache[mlbam_id]

    fallback = {"k_pct": _LG_K_PCT, "xwoba": _LG_XWOBA}

    yr  = season or datetime.utcnow().year
    try:
        payload = mlb_client.person_stats(
            mlbam_id,
            stats="season",
            group="hitting",
            season=yr,
            timeout=_TIMEOUT,
        )
        stats_blocks = payload.get("stats", [])
        if not stats_blocks:
            _player_stats_cache[mlbam_id] = fallback
            return fallback

        splits = stats_blocks[0].get("splits", [])
        if not splits:
            _player_stats_cache[mlbam_id] = fallback
            return fallback

        s = splits[0].get("stat", {})

        # k_pct: MLB API returns strikeOutRate as a decimal (0.23 = 23%)
        # Multiply by 100 to normalise to match FanGraphs / our feature scale.
        raw_kpct  = s.get("strikeOutRate") or s.get("strikeoutRate")
        k_pct_val = float(raw_kpct) * 100.0 if raw_kpct is not None else _LG_K_PCT

        # xwoba is not in the standard MLB stats API; fall back to woba as proxy.
        woba_val  = float(s.get("wOba") or s.get("woba") or _LG_XWOBA)

        result = {"k_pct": round(k_pct_val, 2), "xwoba": round(woba_val, 4)}
        _player_stats_cache[mlbam_id] = result
        return result

    except Exception:
        _player_stats_cache[mlbam_id] = fallback
        return fallback


def _parse_lineups(games: list[dict]) -> dict:
    """
    Parse raw game dicts into a per-batter lookup:
      lineups[mlbam_id] = {
          game_pk, team, batting_order, expected_pa,
          lineup_confirmed, player_name
      }
    """
    lineups: dict[int, dict] = {}
    for game in games:
        game_pk = game.get("gamePk", 0)
        status  = (game.get("status") or {}).get("abstractGameState", "").lower()
        if status == "final":
            continue

        for side in ("home", "away"):
            team_name   = ((game.get("teams") or {}).get(side) or {}).get("team", {}).get("name", "")
            lineup_data = ((game.get("lineups") or {}).get(side + "Players") or [])
            confirmed   = 1 if lineup_data else 0

            for entry in lineup_data:
                player   = entry.get("person") or {}
                mlbam_id = player.get("id")
                name     = player.get("fullName", "")
                bat_order = entry.get("battingOrder")

                if not mlbam_id:
                    continue

                try:
                    raw_order = int(bat_order)
                    slot = raw_order // 100 if raw_order >= 100 else raw_order
                except (TypeError, ValueError):
                    slot = 0

                lineups[int(mlbam_id)] = {
                    "game_pk":          game_pk,
                    "team":             team_name,
                    "batting_order":    slot,
                    "expected_pa":      _PA_BY_SLOT.get(slot, _DEFAULT_PA),
                    "lineup_confirmed": confirmed,
                    "player_name":      name,
                }
    return lineups


def _compute_lineup_aggregates(games: list[dict]) -> dict[int, dict]:
    """
    For each game, compute average k_pct and xwoba across the confirmed
    home and away lineups by fetching each batter's season stats.

    Returns:
        {
          game_pk: {
            "home_k_pct":      float,   # avg k% for home lineup
            "home_xwoba":      float,   # avg xwoba (woba proxy) for home lineup
            "away_k_pct":      float,
            "away_xwoba":      float,
            "home_confirmed":  bool,    # True if lineup was official
            "away_confirmed":  bool,
          },
          ...
        }
    Falls back to league-average per side when lineup not yet posted.
    """
    result: dict[int, dict] = {}

    for game in games:
        game_pk = game.get("gamePk", 0)
        if not game_pk:
            continue
        status = (game.get("status") or {}).get("abstractGameState", "").lower()
        if status == "final":
            continue

        game_agg: dict = {
            "home_k_pct":     _LG_K_PCT,
            "home_xwoba":     _LG_XWOBA,
            "away_k_pct":     _LG_K_PCT,
            "away_xwoba":     _LG_XWOBA,
            "home_confirmed": False,
            "away_confirmed": False,
        }

        for side in ("home", "away"):
            lineup_data = ((game.get("lineups") or {}).get(side + "Players") or [])
            if not lineup_data:
                continue  # keep league-average fallback

            k_pcts, xwobas = [], []
            for entry in lineup_data:
                mid = (entry.get("person") or {}).get("id")
                if not mid:
                    continue
                stats = _fetch_player_season_stats(int(mid))
                k_pcts.append(stats["k_pct"])
                xwobas.append(stats["xwoba"])

            if k_pcts:
                game_agg[f"{side}_k_pct"]     = round(sum(k_pcts)  / len(k_pcts),  2)
                game_agg[f"{side}_xwoba"]     = round(sum(xwobas) / len(xwobas), 4)
                game_agg[f"{side}_confirmed"] = True

        result[game_pk] = game_agg

    return result


# ───────────────────────────────────────────────────────────────────────────────
# Public API
# ───────────────────────────────────────────────────────────────────────────────

def fetch_and_save(date_str: Optional[str] = None) -> dict:
    """
    Fetch today's lineups from MLB StatsAPI, save to data/lineups_{date}.json,
    and return the per-batter lineup dict.
    Called by pipeline_scheduler to keep the cache fresh.
    """
    if date_str is None:
        date_str = date.today().isoformat()

    games   = _fetch_games_for_date(date_str)
    lineups = _parse_lineups(games)

    path = _cache_path(date_str)
    with open(path, "w") as f:
        json.dump(
            {"fetched_at": datetime.utcnow().isoformat(), "lineups": lineups},
            f, indent=2,
        )

    n_confirmed = sum(1 for v in lineups.values() if v["lineup_confirmed"] == 1)
    print(
        f"[lineup_loader] {date_str} — {len(lineups)} batters across "
        f"{len(games)} games ({n_confirmed} confirmed)"
    )
    return lineups


def get_today_lineups(date_str: Optional[str] = None) -> dict[int, dict]:
    """
    Return per-game aggregate lineup stats for today (or date_str).

    Calls MLB StatsAPI to pull confirmed lineups and fetches each batter's
    season k_pct and xwoba (woba proxy), then averages by side.

    Return format:
        {
          game_pk (int): {
            "home_k_pct":     float,   # avg strikeout% for home lineup
            "home_xwoba":     float,   # avg woba proxy for home lineup
            "away_k_pct":     float,
            "away_xwoba":     float,
            "home_confirmed": bool,    # True = lineup officially posted
            "away_confirmed": bool,
          },
          ...
        }

    Falls back to league-average (_LG_K_PCT, _LG_XWOBA) per side when
    the lineup has not yet been posted for that game.

    Used by pipeline_scheduler._rescore_with_confirmed_lineups() to patch
    opp_lineup_k_pct_proxy / opp_lineup_xwoba_proxy in the live matchup_df.
    """
    global _player_stats_cache
    _player_stats_cache = {}  # clear per-run cache

    if date_str is None:
        date_str = date.today().isoformat()

    games = _fetch_games_for_date(date_str)
    if not games:
        print(f"[lineup_loader] get_today_lineups: no games found for {date_str}")
        return {}

    aggregates = _compute_lineup_aggregates(games)

    n_home = sum(1 for v in aggregates.values() if v["home_confirmed"])
    n_away = sum(1 for v in aggregates.values() if v["away_confirmed"])
    print(
        f"[lineup_loader] get_today_lineups {date_str} — {len(aggregates)} games, "
        f"{n_home} home lineups confirmed, {n_away} away lineups confirmed"
    )
    return aggregates


def get_lineup_features(
    mlbam_id: Optional[int] = None,
    player_name: Optional[str] = None,
    date_str: Optional[str] = None,
) -> dict:
    """
    Return per-batter lineup features for a player on the given date.
    Lookup priority: mlbam_id > player_name (case-insensitive substring).

    Returns:
        {
          "expected_pa":      float,
          "batting_order":    int,     # 1-9, 0 if unknown
          "lineup_confirmed": int,     # 1 confirmed / 0 projected
        }
    If player not found, returns league-average defaults.
    """
    if date_str is None:
        date_str = date.today().isoformat()

    path = _cache_path(date_str)
    if not _cache_fresh(date_str):
        try:
            fetch_and_save(date_str)
        except Exception:
            pass

    lineups: dict = {}
    if os.path.exists(path):
        try:
            with open(path) as f:
                lineups = json.load(f).get("lineups", {})
            lineups = {int(k): v for k, v in lineups.items()}
        except Exception:
            pass

    if mlbam_id is not None:
        entry = lineups.get(int(mlbam_id))
        if entry:
            return {
                "expected_pa":      float(entry.get("expected_pa",   _DEFAULT_PA)),
                "batting_order":    int(entry.get("batting_order",    0)),
                "lineup_confirmed": int(entry.get("lineup_confirmed", 0)),
            }

    if player_name:
        name_lower = player_name.lower()
        for entry in lineups.values():
            if name_lower in (entry.get("player_name") or "").lower():
                return {
                    "expected_pa":      float(entry.get("expected_pa",   _DEFAULT_PA)),
                    "batting_order":    int(entry.get("batting_order",    0)),
                    "lineup_confirmed": int(entry.get("lineup_confirmed", 0)),
                }

    return {
        "expected_pa":      _DEFAULT_PA,
        "batting_order":    0,
        "lineup_confirmed": 0,
    }


if __name__ == "__main__":
    today = date.today().isoformat()

    print("=== Per-batter lineup features ===")
    data = fetch_and_save(today)
    for mid, v in list(data.items())[:3]:
        print(f"  {v['player_name']} (#{v['batting_order']}) — "
              f"expected_pa={v['expected_pa']}  confirmed={v['lineup_confirmed']}")

    print("\n=== Per-game lineup aggregates ===")
    aggs = get_today_lineups(today)
    for gpk, v in list(aggs.items())[:3]:
        print(f"  game_pk={gpk}  "
              f"home k%={v['home_k_pct']} xwoba={v['home_xwoba']} confirmed={v['home_confirmed']}  |"
              f"  away k%={v['away_k_pct']} xwoba={v['away_xwoba']} confirmed={v['away_confirmed']}")
