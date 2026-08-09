"""Route-level latency budgets for production readiness checks."""
from __future__ import annotations

from typing import Any


# Budgets describe the user-visible server response budget, not the upstream
# provider timeout.
DEFAULT_ROUTE_BUDGETS_MS: dict[str, int] = {
    "GET /health": 250,
    "GET /ready": 750,
    "GET /api/games/today": 2000,
    "GET /api/picks/today": 2500,
    "GET /api/game-projection/<game_pk>": 3000,
    "GET /api/pitchers/<game_pk>": 3000,
    "GET /api/pitcher-matchup/<game_pk>": 3500,
    "GET /api/props/projections/<game_pk>": 4000,
}


def _path_without_method(route_key: str) -> str:
    return str(route_key).split(" ", 1)[1] if " " in str(route_key) else str(route_key)


def route_budget_ms(route_key: str) -> int | None:
    """Return the configured budget for a normalized method/route key."""
    key = str(route_key)
    exact = DEFAULT_ROUTE_BUDGETS_MS.get(key)
    if exact is not None:
        return exact

    method = key.split(" ", 1)[0].upper() if " " in key else ""
    path = _path_without_method(key)
    for configured, budget in DEFAULT_ROUTE_BUDGETS_MS.items():
        configured_method, configured_path = configured.split(" ", 1)
        if method and method != configured_method:
            continue
        if configured_path.endswith("/<game_pk>") and path.startswith(
            configured_path.rsplit("/", 1)[0] + "/"
        ):
            return budget
    return None


def budget_result(route_key: str, duration_ms: float) -> dict[str, Any]:
    """Classify one measurement without turning a slow request into a 5xx."""
    budget = route_budget_ms(route_key)
    duration = round(max(0.0, float(duration_ms)), 2)
    if budget is None:
        return {
            "route": route_key,
            "duration_ms": duration,
            "budget_ms": None,
            "status": "unbudgeted",
            "breach_ms": 0.0,
        }
    breach = round(max(0.0, duration - budget), 2)
    return {
        "route": route_key,
        "duration_ms": duration,
        "budget_ms": budget,
        "status": "breached" if breach > 0 else "within_budget",
        "breach_ms": breach,
    }


def route_budget_snapshot() -> dict[str, int]:
    """Return a JSON-safe copy for diagnostics and deployment tooling."""
    return dict(DEFAULT_ROUTE_BUDGETS_MS)
