"""Unified integration control plane.

Exposes safe configuration status, validation and orchestration without ever
returning secrets. Provider adapters remain in external_providers.py and
commercial_integrations.py.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

PROVIDERS = {
    "nflverse": {
        "label": "nflverse",
        "datasets": ["pbp", "rosters", "injuries", "depth_charts", "snap_counts"],
        "required_env": [],
        "kind": "public",
    },
    "nws": {
        "label": "National Weather Service",
        "datasets": ["weather"],
        "required_env": ["NWS_USER_AGENT"],
        "kind": "public",
    },
    "openweather": {
        "label": "OpenWeather",
        "datasets": ["weather"],
        "required_env": ["OPENWEATHER_API_KEY"],
        "kind": "credentialed",
    },
    "the-odds-api": {
        "label": "The Odds API",
        "datasets": ["odds"],
        "required_env": ["ODDS_API_KEY"],
        "kind": "credentialed",
    },
    "sportsdataio": {
        "label": "SportsDataIO",
        "datasets": ["live_games", "coaches", "transactions"],
        "required_env": ["SPORTSDATAIO_API_KEY"],
        "kind": "credentialed",
    },
}


def _configured(required_env: list[str]) -> tuple[bool, list[str]]:
    missing = [key for key in required_env if not os.getenv(key)]
    return not missing, missing


def integration_status() -> dict:
    providers = []
    for key, cfg in PROVIDERS.items():
        configured, missing = _configured(cfg["required_env"])
        providers.append({
            "key": key,
            "label": cfg["label"],
            "kind": cfg["kind"],
            "datasets": cfg["datasets"],
            "configured": configured,
            "missing_env": missing,
            "enabled": _provider_enabled(key),
        })
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "providers": providers,
        "summary": {
            "configured": sum(1 for p in providers if p["configured"]),
            "enabled": sum(1 for p in providers if p["enabled"]),
            "total": len(providers),
        },
    }


def _provider_enabled(key: str) -> bool:
    raw = os.getenv("ENABLED_PROVIDERS", "nflverse,nws")
    return key in {x.strip() for x in raw.split(",") if x.strip()}


def run_integrations(season: int, datasets: list[str], week: int | None = None) -> dict:
    """Route each dataset to the selected adapter.

    WEATHER_PROVIDER controls weather selection: nws (default) or openweather.
    """
    from external_providers import sync_external
    from commercial_integrations import (
        sync_coaches,
        sync_live_games,
        sync_odds,
        sync_transactions,
        sync_weather,
    )

    public = [d for d in datasets if d in {"pbp", "rosters", "injuries", "depth_charts", "snap_counts"}]
    result: dict = {"season": season, "week": week, "results": {}, "errors": {}}
    if public:
        try:
            result["results"].update(sync_external(season, public))
        except Exception as exc:  # caller receives partial result
            result["errors"]["nflverse"] = str(exc)

    actions = {
        "weather": lambda: sync_weather(season, week),
        "odds": lambda: sync_odds(season, week),
        "coaches": lambda: sync_coaches(season),
        "transactions": lambda: sync_transactions(season),
        "live_games": lambda: sync_live_games(season, week),
    }
    for dataset in datasets:
        if dataset in public:
            continue
        action = actions.get(dataset)
        if not action:
            result["errors"][dataset] = "unsupported dataset"
            continue
        try:
            result["results"][dataset] = action()
        except Exception as exc:
            result["errors"][dataset] = str(exc)
    result["ok"] = not result["errors"]
    return result
