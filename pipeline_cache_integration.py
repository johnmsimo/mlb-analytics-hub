"""Cache integration for high-cost pipeline scheduler lookups.

Installs cache-backed replacements for schedule, roster, BvP, and platoon
lookups while preserving the scheduler's existing return types and fallbacks.
Also registers cache operational routes once per worker.
"""
from __future__ import annotations

import logging
from datetime import datetime
from types import ModuleType

import pandas as pd

from cache_service import get_or_compute, normalize_cache_key

log = logging.getLogger(__name__)
_INSTALLED_ATTR = "_shared_cache_integration_installed"
_ROUTES_ATTR = "_cache_operations_routes_installed"


def _install_cache_routes() -> bool:
    """Register the cache operations blueprint on the main Flask app once."""
    import app as app_module
    from cache_routes import cache_ops_bp

    flask_app = getattr(app_module, "app", None)
    if flask_app is None:
        log.warning("[pipeline.cache] app module has no Flask 'app' object")
        return False
    if getattr(flask_app, _ROUTES_ATTR, False):
        return False
    flask_app.register_blueprint(cache_ops_bp)
    setattr(flask_app, _ROUTES_ATTR, True)
    log.info("[pipeline.cache] cache operations routes registered")
    return True


def install_pipeline_cache(module: ModuleType | None = None) -> bool:
    """Install cache-backed pipeline helpers once per process."""
    if module is None:
        import pipeline_scheduler as module

    if getattr(module, _INSTALLED_ATTR, False):
        try:
            _install_cache_routes()
        except Exception as exc:
            log.warning("[pipeline.cache] route registration failed: %s", exc)
        return False

    original_position_ids = module._get_position_player_ids
    original_bvp = module._get_bvp
    original_splits = module._get_platoon_splits

    def cached_build_games_df(fetch_schedule, target_date=None):
        date_str = target_date or datetime.now(module.ET).strftime("%Y-%m-%d")
        key = normalize_cache_key("schedule", date_str)

        def compute():
            try:
                return fetch_schedule(date_str) or []
            except Exception as exc:
                log.error("[pipeline.cache] fetch_schedule failed: %s", exc)
                return []

        games_raw = get_or_compute(key, compute, policy="schedule")
        return pd.DataFrame(games_raw) if games_raw else pd.DataFrame()

    def cached_position_ids(team_id):
        key = normalize_cache_key("active_roster", team_id)
        return get_or_compute(
            key,
            lambda: original_position_ids(team_id),
            policy="live",
        )

    def cached_bvp(batter_id, pitcher_id):
        key = normalize_cache_key("bvp", batter_id, pitcher_id)
        return get_or_compute(
            key,
            lambda: original_bvp(batter_id, pitcher_id),
            policy="stats",
        )

    def cached_splits(player_id, group="hitting"):
        key = normalize_cache_key("platoon_splits", player_id, group=group)
        return get_or_compute(
            key,
            lambda: original_splits(player_id, group),
            policy="stats",
        )

    module._build_games_df = cached_build_games_df
    module._get_position_player_ids = cached_position_ids
    module._get_bvp = cached_bvp
    module._get_platoon_splits = cached_splits
    setattr(module, _INSTALLED_ATTR, True)

    try:
        _install_cache_routes()
    except Exception as exc:
        log.warning("[pipeline.cache] route registration failed: %s", exc)

    log.info("[pipeline.cache] shared cache installed for schedule and MLB loaders")
    return True
