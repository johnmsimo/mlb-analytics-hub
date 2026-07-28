from unittest import TestCase
from unittest.mock import patch

import cache_service
from cache_routes import cache_ops_bp


class FakeCache:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, ttl=None):
        self.values[key] = value

    def delete(self, key):
        self.values.pop(key, None)


class CacheObservabilityTests(TestCase):
    def setUp(self):
        cache_service.reset_cache_metrics()
        with cache_service._metrics_lock:
            cache_service._namespace_keys.clear()
        self.cache = FakeCache()

    def test_status_reports_hits_misses_and_compute_timing(self):
        key = cache_service.normalize_cache_key("stats", 42)
        with patch("cache_service.get_redis", return_value=self.cache), patch(
            "cache_service.redis_health_status",
            return_value={
                "configured": True,
                "connected": False,
                "backend": "memory",
                "fallback_active": True,
                "circuit_state": "open",
                "consecutive_failures": 5,
                "total_failures": 5,
                "last_success_at": None,
                "last_failure_at": "2026-07-28T21:00:00+00:00",
                "average_latency_ms": 0.0,
                "health_interval_seconds": 30,
                "failure_threshold": 5,
                "circuit_timeout_seconds": 60,
                "monitoring": True,
            },
        ):
            self.assertEqual(cache_service.get_or_compute(key, lambda: {"x": 1}), {"x": 1})
            self.assertEqual(cache_service.get_or_compute(key, lambda: {"x": 2}), {"x": 1})
            status = cache_service.cache_status()

        self.assertEqual(status["backend"], "memory")
        self.assertEqual(status["metrics"]["lookups"], 2)
        self.assertEqual(status["metrics"]["hits_total"], 1)
        self.assertEqual(status["metrics"]["computes"], 1)
        self.assertEqual(status["metrics"]["hit_rate"], 0.5)
        self.assertEqual(status["redis"]["circuit_state"], "open")
        self.assertGreaterEqual(status["metrics"]["average_compute_ms"], 0.0)

    def test_namespace_invalidation_only_deletes_registered_namespace(self):
        stats_key = cache_service.normalize_cache_key("stats", 1)
        live_key = cache_service.normalize_cache_key("live", 1)
        self.cache.set(stats_key, "stats")
        self.cache.set(live_key, "live")

        with patch("cache_service.get_redis", return_value=self.cache):
            deleted = cache_service.invalidate_namespace("stats")

        self.assertEqual(deleted, 1)
        self.assertIsNone(self.cache.get(stats_key))
        self.assertEqual(self.cache.get(live_key), "live")

    def test_operational_writes_require_admin_token(self):
        from flask import Flask

        app = Flask(__name__)
        app.register_blueprint(cache_ops_bp)
        client = app.test_client()

        with patch.dict("os.environ", {"CACHE_ADMIN_TOKEN": "secret"}, clear=False):
            self.assertEqual(client.post("/api/cache/metrics/reset").status_code, 401)
            response = client.post(
                "/api/cache/metrics/reset",
                headers={"X-Cache-Admin-Token": "secret"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"reset": True})
