import os
import unittest
from unittest.mock import patch

from config import settings


class ConfigurationTests(unittest.TestCase):
    def test_defaults_are_typed(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(settings.port, 8080)
            self.assertEqual(settings.http_retry_total, 3)
            self.assertEqual(settings.http_retry_backoff, 0.5)
            self.assertEqual(settings.cache_ttls["stats"], 3600)
            self.assertEqual(settings.bq_dataset, "mlb")
            self.assertEqual(settings.redis_health_interval, 30)
            self.assertEqual(settings.redis_failure_threshold, 5)
            self.assertEqual(settings.redis_circuit_timeout, 60)
            self.assertEqual(settings.cache_stale_ttl, 300)
            self.assertTrue(settings.cache_allow_stale)
            self.assertEqual(settings.mlb_schedule_cache_ttl, 120)
            self.assertEqual(
                settings.mlb_stats_api_base_url,
                "https://statsapi.mlb.com/api",
            )
            self.assertEqual(settings.mlb_http_timeout, 10)
            self.assertEqual(settings.mlb_bulk_http_timeout, 60)
            self.assertEqual(settings.mlb_slow_request_ms, 1000)
            self.assertTrue(settings.performance_monitor_enabled)
            self.assertFalse(settings.profile_requests)
            self.assertEqual(settings.performance_slow_ms, 1000)
            self.assertEqual(settings.performance_sample_size, 2048)
            self.assertEqual(settings.performance_route_limit, 256)
            self.assertEqual(settings.xgb_score_cache_ttl, 300)
            self.assertEqual(settings.xgb_score_cache_max_entries, 2048)

    def test_environment_overrides_are_resolved_on_access(self):
        with patch.dict(
            os.environ,
            {
                "PORT": "9090",
                "REDIS_URL": "redis://example:6379/0",
                "CACHE_TTL_LIVE": "45",
                "HTTP_RETRY_TOTAL": "6",
                "BQ_ETL_HOUR_ET": "7",
                "REDIS_FAILURE_THRESHOLD": "3",
                "CACHE_ALLOW_STALE": "false",
                "MLB_SCHEDULE_CACHE_TTL": "90",
                "MLB_STATS_API_BASE_URL": "https://mlb.example.test/api/",
                "MLB_HTTP_TIMEOUT": "15",
                "MLB_BULK_HTTP_TIMEOUT": "75",
                "MLB_SLOW_REQUEST_MS": "650",
                "PERFORMANCE_MONITOR_ENABLED": "false",
                "PERFORMANCE_SLOW_MS": "750",
                "XGB_SCORE_CACHE_TTL": "180",
                "XGB_SCORE_CACHE_MAX_ENTRIES": "512",
            },
            clear=True,
        ):
            self.assertEqual(settings.port, 9090)
            self.assertEqual(settings.redis_url, "redis://example:6379/0")
            self.assertEqual(settings.cache_ttls["live"], 45)
            self.assertEqual(settings.http_retry_total, 6)
            self.assertEqual(settings.bq_etl_hour_et, 7)
            self.assertEqual(settings.redis_failure_threshold, 3)
            self.assertFalse(settings.cache_allow_stale)
            self.assertEqual(settings.mlb_schedule_cache_ttl, 90)
            self.assertEqual(
                settings.mlb_stats_api_base_url,
                "https://mlb.example.test/api",
            )
            self.assertEqual(settings.mlb_http_timeout, 15)
            self.assertEqual(settings.mlb_bulk_http_timeout, 75)
            self.assertEqual(settings.mlb_slow_request_ms, 650)
            self.assertFalse(settings.performance_monitor_enabled)
            self.assertEqual(settings.performance_slow_ms, 750)
            self.assertEqual(settings.xgb_score_cache_ttl, 180)
            self.assertEqual(settings.xgb_score_cache_max_entries, 512)

    def test_invalid_numbers_fall_back_and_ranges_are_bounded(self):
        with patch.dict(
            os.environ,
            {
                "PORT": "invalid",
                "HTTP_RETRY_TOTAL": "-4",
                "BQ_ETL_HOUR_ET": "99",
                "BQ_ETL_MINUTE_ET": "-1",
                "REDIS_HEALTH_INTERVAL": "0",
                "CACHE_STALE_TTL": "-4",
                "MLB_SCHEDULE_CACHE_TTL": "0",
                "MLB_HTTP_TIMEOUT": "0",
                "MLB_BULK_HTTP_TIMEOUT": "9999",
                "PERFORMANCE_SAMPLE_SIZE": "999999",
                "PERFORMANCE_ROUTE_LIMIT": "0",
                "XGB_SCORE_CACHE_TTL": "-1",
                "XGB_SCORE_CACHE_MAX_ENTRIES": "999999",
            },
            clear=True,
        ):
            self.assertEqual(settings.port, 8080)
            self.assertEqual(settings.http_retry_total, 0)
            self.assertEqual(settings.bq_etl_hour_et, 23)
            self.assertEqual(settings.bq_etl_minute_et, 0)
            self.assertEqual(settings.redis_health_interval, 1)
            self.assertEqual(settings.cache_stale_ttl, 0)
            self.assertEqual(settings.mlb_schedule_cache_ttl, 1)
            self.assertEqual(settings.mlb_http_timeout, 1)
            self.assertEqual(settings.mlb_bulk_http_timeout, 300)
            self.assertEqual(settings.performance_sample_size, 10000)
            self.assertEqual(settings.performance_route_limit, 25)
            self.assertEqual(settings.xgb_score_cache_ttl, 0)
            self.assertEqual(settings.xgb_score_cache_max_entries, 20000)

    def test_public_snapshot_never_exposes_secrets(self):
        with patch.dict(
            os.environ,
            {
                "ADMIN_TOKEN": "admin-secret",
                "CACHE_ADMIN_TOKEN": "cache-secret",
                "ODDS_API_KEY": "api-secret",
                "REDIS_URL": "redis://user:redis-secret@example:6379/0",
            },
            clear=True,
        ):
            snapshot = settings.as_public_dict()
        serialized = repr(snapshot)
        self.assertNotIn("admin-secret", serialized)
        self.assertNotIn("cache-secret", serialized)
        self.assertNotIn("api-secret", serialized)
        self.assertNotIn("redis-secret", serialized)


if __name__ == "__main__":
    unittest.main()
