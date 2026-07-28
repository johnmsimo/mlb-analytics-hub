import threading
import time
import unittest
from unittest.mock import patch

import cache_service
from redis_client import _MemoryClient


class CacheServiceTests(unittest.TestCase):
    def setUp(self):
        self.cache = _MemoryClient()
        cache_service._locks.clear()

    def test_normalized_key_is_stable_for_param_order(self):
        first = cache_service.normalize_cache_key("stats", player=13, season=2026)
        second = cache_service.normalize_cache_key("stats", season=2026, player=13)
        self.assertEqual(first, second)

    def test_get_or_compute_reuses_cached_value(self):
        calls = 0

        def compute():
            nonlocal calls
            calls += 1
            return {"value": 42}

        with patch("cache_service.get_redis", return_value=self.cache):
            first = cache_service.get_or_compute("mlb:test:key", compute, ttl=60)
            second = cache_service.get_or_compute("mlb:test:key", compute, ttl=60)

        self.assertEqual(first, second)
        self.assertEqual(calls, 1)

    def test_expired_value_is_recomputed(self):
        calls = 0

        def compute():
            nonlocal calls
            calls += 1
            return calls

        with patch("cache_service.get_redis", return_value=self.cache):
            self.assertEqual(cache_service.get_or_compute("mlb:test:expiry", compute, ttl=1), 1)
            time.sleep(1.05)
            self.assertEqual(cache_service.get_or_compute("mlb:test:expiry", compute, ttl=1), 2)

    def test_concurrent_misses_compute_once(self):
        calls = 0
        results = []
        barrier = threading.Barrier(5)

        def compute():
            nonlocal calls
            calls += 1
            time.sleep(0.05)
            return "ready"

        def worker():
            barrier.wait()
            with patch("cache_service.get_redis", return_value=self.cache):
                results.append(cache_service.get_or_compute("mlb:test:stampede", compute, ttl=60))

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(results, ["ready"] * 5)
        self.assertEqual(calls, 1)


if __name__ == "__main__":
    unittest.main()
