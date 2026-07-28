"""Typed environment-backed configuration for MLB Analytics Hub.

All production modules should read deployment settings through ``settings``
instead of parsing environment variables independently. Properties are
resolved on access so tests and management commands can safely override their
environment without reloading this module.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_BASE_DIR = Path(__file__).resolve().parent


def _string(name: str, default: str = "", *, strip: bool = True) -> str:
    value = os.environ.get(name)
    if value is None:
        value = default
    return value.strip() if strip else value


def _number(
    name: str,
    default: int | float,
    cast: type[int] | type[float],
    *,
    minimum: int | float | None = None,
    maximum: int | float | None = None,
) -> int | float:
    raw = os.environ.get(name)
    try:
        value = cast(raw) if raw is not None else default
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _boolean(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


@dataclass(frozen=True)
class Settings:
    """Single typed interface for runtime and deployment configuration."""

    @property
    def port(self) -> int:
        return int(_number("PORT", 8080, int, minimum=1, maximum=65535))

    @property
    def data_dir(self) -> str:
        return _string("DATA_DIR", str(_BASE_DIR / "data"), strip=False)

    @property
    def redis_url(self) -> str:
        return _string("REDIS_URL")

    @property
    def redis_health_interval(self) -> int:
        return int(_number("REDIS_HEALTH_INTERVAL", 30, int, minimum=1))

    @property
    def redis_failure_threshold(self) -> int:
        return int(_number("REDIS_FAILURE_THRESHOLD", 5, int, minimum=1))

    @property
    def redis_circuit_timeout(self) -> int:
        return int(_number("REDIS_CIRCUIT_TIMEOUT", 60, int, minimum=1))

    @property
    def cache_stale_ttl(self) -> int:
        return int(_number("CACHE_STALE_TTL", 300, int, minimum=0))

    @property
    def cache_allow_stale(self) -> bool:
        return _boolean("CACHE_ALLOW_STALE", True)

    @property
    def admin_token(self) -> str:
        return _string("ADMIN_TOKEN")

    @property
    def cache_admin_token(self) -> str:
        return _string("CACHE_ADMIN_TOKEN")

    @property
    def cache_ttls(self) -> dict[str, int]:
        return {
            "live": int(_number("CACHE_TTL_LIVE", 30, int, minimum=1)),
            "schedule": int(_number("CACHE_TTL_SCHEDULE", 300, int, minimum=1)),
            "stats": int(_number("CACHE_TTL_STATS", 3600, int, minimum=1)),
            "analytics": int(_number("CACHE_TTL_ANALYTICS", 900, int, minimum=1)),
            "static": int(_number("CACHE_TTL_STATIC", 21600, int, minimum=1)),
        }

    @property
    def http_retry_total(self) -> int:
        return int(_number("HTTP_RETRY_TOTAL", 3, int, minimum=0))

    @property
    def http_retry_backoff(self) -> float:
        return float(_number("HTTP_RETRY_BACKOFF", 0.5, float, minimum=0.0))

    @property
    def http_pool_connections(self) -> int:
        return int(_number("HTTP_POOL_CONNECTIONS", 16, int, minimum=1))

    @property
    def http_pool_maxsize(self) -> int:
        return int(_number("HTTP_POOL_MAXSIZE", 32, int, minimum=1))

    @property
    def http_pool_connections_http(self) -> int:
        return int(_number("HTTP_POOL_CONNECTIONS_HTTP", 8, int, minimum=1))

    @property
    def http_pool_maxsize_http(self) -> int:
        return int(_number("HTTP_POOL_MAXSIZE_HTTP", 16, int, minimum=1))

    @property
    def google_cloud_project(self) -> str:
        return _string("GOOGLE_CLOUD_PROJECT")

    @property
    def bq_dataset(self) -> str:
        return _string("BQ_DATASET", "mlb")

    @property
    def bq_location(self) -> str:
        return _string("BQ_LOCATION", "US")

    @property
    def bq_etl_hour_et(self) -> int:
        return int(_number("BQ_ETL_HOUR_ET", 9, int, minimum=0, maximum=23))

    @property
    def bq_etl_minute_et(self) -> int:
        return int(_number("BQ_ETL_MINUTE_ET", 30, int, minimum=0, maximum=59))

    @property
    def odds_api_key(self) -> str:
        return _string("ODDS_API_KEY")

    @property
    def odds_nrfi_ttl_seconds(self) -> int:
        return int(_number("ODDS_NRFI_TTL_SEC", 300, int, minimum=1))

    @property
    def draftkings_odds_ttl_seconds(self) -> int:
        return int(_number("DK_ODDS_TTL_SEC", 300, int, minimum=1))

    @property
    def draftkings_geo(self) -> str:
        return _string("DK_GEO", "dkusnj")

    @property
    def draftkings_mlb_event_group(self) -> int:
        return int(_number("DK_MLB_EVENT_GROUP", 84240, int, minimum=1))

    @property
    def mlb_base_url(self) -> str:
        return _string("MLB_BASE_URL", "https://mlb-analytics-hub.fly.dev")

    @property
    def mlb_admin_token(self) -> str:
        return _string("MLB_ADMIN_TOKEN") or self.admin_token

    def as_public_dict(self) -> dict[str, Any]:
        """Return non-secret operational settings for diagnostics."""
        return {
            "port": self.port,
            "data_dir": self.data_dir,
            "redis_configured": bool(self.redis_url),
            "redis_health_interval": self.redis_health_interval,
            "redis_failure_threshold": self.redis_failure_threshold,
            "redis_circuit_timeout": self.redis_circuit_timeout,
            "cache_allow_stale": self.cache_allow_stale,
            "cache_stale_ttl": self.cache_stale_ttl,
            "cache_ttls": self.cache_ttls,
            "http_retry_total": self.http_retry_total,
            "http_retry_backoff": self.http_retry_backoff,
            "bq_project_configured": bool(self.google_cloud_project),
            "bq_dataset": self.bq_dataset,
            "bq_location": self.bq_location,
        }


settings = Settings()
