"""Shared, resilient caching for hydrated MLB schedule payloads."""
from __future__ import annotations

from typing import Any

import requests

from cache_service import get_or_compute, normalize_cache_key
from config import settings

MLB_API = "https://statsapi.mlb.com/api/v1"
_SCHEDULE_HYDRATE = (
    "team,probablePitcher,lineups,linescore,venue(location),weather"
)


def _schedule_games(*, date_str: str | None = None, game_pk: Any = None) -> list[dict]:
    if date_str is not None:
        url = (
            f"{MLB_API}/schedule?sportId=1&date={date_str}"
            f"&hydrate={_SCHEDULE_HYDRATE}"
        )
    else:
        url = (
            f"{MLB_API}/schedule?sportId=1&gamePk={game_pk}"
            f"&hydrate={_SCHEDULE_HYDRATE}"
        )

    response = requests.get(url, timeout=10)
    response.raise_for_status()
    payload = response.json()
    dates = payload.get("dates", []) if isinstance(payload, dict) else []
    games = dates[0].get("games", []) if dates else []
    return games if isinstance(games, list) else []


def fetch_schedule(date_str: str) -> list[dict]:
    """Return one date's hydrated games from the shared resilient cache."""
    normalized_date = str(date_str)
    key = normalize_cache_key("mlb_schedule_date", normalized_date)
    return get_or_compute(
        key,
        lambda: _schedule_games(date_str=normalized_date),
        ttl=settings.mlb_schedule_cache_ttl,
    )


def fetch_schedule_game(game_pk: Any) -> dict | None:
    """Return one hydrated game, including cached not-found responses."""
    key = normalize_cache_key("mlb_schedule_game", str(game_pk))

    def compute() -> dict[str, Any]:
        games = _schedule_games(game_pk=game_pk)
        return {
            "found": bool(games),
            "game": games[0] if games else None,
        }

    cached = get_or_compute(
        key,
        compute,
        ttl=settings.mlb_schedule_cache_ttl,
    )
    return cached.get("game") if isinstance(cached, dict) else None
