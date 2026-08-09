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
    def process_role(self) -> str:
        value = _string("PROCESS_ROLE", "web").lower()
        return value if value in {"web", "worker", "test"} else "web"

    @property
    def production(self) -> bool:
        explicit = _string("APP_ENV").lower()
        if explicit:
            return explicit in {"production", "prod"}
        return bool(_string("FLY_APP_NAME"))

    @property
    def data_dir(self) -> str:
        return _string("DATA_DIR", str(_BASE_DIR / "data"), strip=False)

    @property
    def reference_snapshot_path(self) -> str:
        return _string(
            "REFERENCE_SNAPSHOT_PATH",
            str(Path(self.data_dir) / "reference_data.snapshot"),
            strip=False,
        )

    @property
    def reference_snapshot_poll_seconds(self) -> int:
        return int(
            _number(
                "REFERENCE_SNAPSHOT_POLL_SECONDS",
                15,
                int,
                minimum=1,
                maximum=300,
            )
        )

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
    def mlb_schedule_cache_ttl(self) -> int:
        return int(_number("MLB_SCHEDULE_CACHE_TTL", 120, int, minimum=1))

    @property
    def mlb_stats_api_base_url(self) -> str:
        return _string(
            "MLB_STATS_API_BASE_URL",
            "https://statsapi.mlb.com/api",
        ).rstrip("/")

    @property
    def mlb_http_timeout(self) -> int:
        return int(_number("MLB_HTTP_TIMEOUT", 10, int, minimum=1, maximum=120))

    @property
    def mlb_bulk_http_timeout(self) -> int:
        return int(
            _number("MLB_BULK_HTTP_TIMEOUT", 60, int, minimum=1, maximum=300)
        )

    @property
    def mlb_slow_request_ms(self) -> int:
        return int(_number("MLB_SLOW_REQUEST_MS", 1000, int, minimum=1))

    @property
    def performance_monitor_enabled(self) -> bool:
        return _boolean("PERFORMANCE_MONITOR_ENABLED", True)

    @property
    def profile_requests(self) -> bool:
        return _boolean("PROFILE_REQUESTS", False)

    @property
    def performance_slow_ms(self) -> int:
        return int(_number("PERFORMANCE_SLOW_MS", 1000, int, minimum=1))

    @property
    def performance_sample_size(self) -> int:
        return int(
            _number("PERFORMANCE_SAMPLE_SIZE", 2048, int, minimum=100, maximum=10000)
        )

    @property
    def performance_route_limit(self) -> int:
        return int(
            _number("PERFORMANCE_ROUTE_LIMIT", 256, int, minimum=25, maximum=2000)
        )

    @property
    def xgb_score_cache_ttl(self) -> int:
        return int(_number("XGB_SCORE_CACHE_TTL", 300, int, minimum=0, maximum=3600))

    @property
    def xgb_score_cache_max_entries(self) -> int:
        return int(
            _number(
                "XGB_SCORE_CACHE_MAX_ENTRIES",
                2048,
                int,
                minimum=0,
                maximum=20000,
            )
        )

    @property
    def admin_token(self) -> str:
        return _string("ADMIN_TOKEN")

    @property
    def admin_auth_required(self) -> bool:
        return _boolean("ADMIN_AUTH_REQUIRED", self.production)

    @property
    def allowed_origins(self) -> tuple[str, ...]:
        raw = _string(
            "ALLOWED_ORIGINS",
            "https://mlb-analytics-hub.fly.dev",
        )
        values = tuple(value.strip().rstrip("/") for value in raw.split(",") if value.strip())
        return values or ("https://mlb-analytics-hub.fly.dev",)

    @property
    def api_rate_limit(self) -> str:
        return _string("API_RATE_LIMIT", "180 per minute")

    @property
    def max_upload_bytes(self) -> int:
        return int(
            _number(
                "MAX_UPLOAD_BYTES",
                8 * 1024 * 1024,
                int,
                minimum=1024,
                maximum=32 * 1024 * 1024,
            )
        )

    @property
    def job_result_ttl(self) -> int:
        return int(_number("JOB_RESULT_TTL", 3600, int, minimum=300, maximum=86400))

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
            "process_role": self.process_role,
            "production": self.production,
            "data_dir": self.data_dir,
            "redis_configured": bool(self.redis_url),
            "redis_health_interval": self.redis_health_interval,
            "redis_failure_threshold": self.redis_failure_threshold,
            "redis_circuit_timeout": self.redis_circuit_timeout,
            "cache_allow_stale": self.cache_allow_stale,
            "cache_stale_ttl": self.cache_stale_ttl,
            "cache_ttls": self.cache_ttls,
            "mlb_schedule_cache_ttl": self.mlb_schedule_cache_ttl,
            "mlb_stats_api_base_url": self.mlb_stats_api_base_url,
            "mlb_http_timeout": self.mlb_http_timeout,
            "mlb_bulk_http_timeout": self.mlb_bulk_http_timeout,
            "mlb_slow_request_ms": self.mlb_slow_request_ms,
            "performance_monitor_enabled": self.performance_monitor_enabled,
            "performance_slow_ms": self.performance_slow_ms,
            "performance_sample_size": self.performance_sample_size,
            "performance_route_limit": self.performance_route_limit,
            "xgb_score_cache_ttl": self.xgb_score_cache_ttl,
            "xgb_score_cache_max_entries": self.xgb_score_cache_max_entries,
            "admin_auth_required": self.admin_auth_required,
            "allowed_origins": self.allowed_origins,
            "api_rate_limit": self.api_rate_limit,
            "max_upload_bytes": self.max_upload_bytes,
            "job_result_ttl": self.job_result_ttl,
            "http_retry_total": self.http_retry_total,
            "http_retry_backoff": self.http_retry_backoff,
            "bq_project_configured": bool(self.google_cloud_project),
            "bq_dataset": self.bq_dataset,
            "bq_location": self.bq_location,
        }


settings = Settings()
