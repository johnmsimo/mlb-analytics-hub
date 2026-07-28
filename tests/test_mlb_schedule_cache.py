from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

import cache_service
import mlb_schedule_cache
from redis_client import _MemoryClient


def _response(games):
    response = Mock()
    response.json.return_value = {"dates": [{"games": games}]} if games is not None else {
        "dates": []
    }
    return response


class MlbScheduleCacheTests(unittest.TestCase):
    def setUp(self):
        self.cache = _MemoryClient()
        cache_service._locks.clear()
        cache_service.reset_cache_metrics()
        self.cache_patch = patch("cache_service.get_redis", return_value=self.cache)
        self.cache_patch.start()

    def tearDown(self):
        self.cache_patch.stop()

    def test_date_schedule_is_reused(self):
        response = _response([{"gamePk": 13}])
        with patch("mlb_schedule_cache.requests.get", return_value=response) as get:
            first = mlb_schedule_cache.fetch_schedule("2026-07-28")
            second = mlb_schedule_cache.fetch_schedule("2026-07-28")

        self.assertEqual(first, second)
        self.assertEqual(get.call_count, 1)
        self.assertIn("date=2026-07-28", get.call_args.args[0])

    def test_game_schedule_is_reused(self):
        response = _response([{"gamePk": 99113, "status": "Preview"}])
        with patch("mlb_schedule_cache.requests.get", return_value=response) as get:
            first = mlb_schedule_cache.fetch_schedule_game(99113)
            second = mlb_schedule_cache.fetch_schedule_game(99113)

        self.assertEqual(first, second)
        self.assertEqual(first["gamePk"], 99113)
        self.assertEqual(get.call_count, 1)
        self.assertIn("gamePk=99113", get.call_args.args[0])

    def test_date_and_game_keys_do_not_collide(self):
        date_response = _response([{"gamePk": 1}])
        game_response = _response([{"gamePk": 2}])
        with patch(
            "mlb_schedule_cache.requests.get",
            side_effect=[date_response, game_response],
        ) as get:
            self.assertEqual(
                mlb_schedule_cache.fetch_schedule("99113"),
                [{"gamePk": 1}],
            )
            self.assertEqual(
                mlb_schedule_cache.fetch_schedule_game(99113),
                {"gamePk": 2},
            )

        self.assertEqual(get.call_count, 2)

    def test_not_found_game_is_cached(self):
        response = _response(None)
        with patch("mlb_schedule_cache.requests.get", return_value=response) as get:
            self.assertIsNone(mlb_schedule_cache.fetch_schedule_game(99113))
            self.assertIsNone(mlb_schedule_cache.fetch_schedule_game(99113))

        self.assertEqual(get.call_count, 1)

    def test_upstream_error_serves_stale_game(self):
        key = cache_service.normalize_cache_key("mlb_schedule_game", "99113")
        self.cache.set(
            f"{key}:stale",
            {"found": True, "game": {"gamePk": 99113, "status": "Preview"}},
            ttl=60,
        )
        response = _response([])
        response.raise_for_status.side_effect = ConnectionError("MLB unavailable")

        with patch("mlb_schedule_cache.requests.get", return_value=response):
            game = mlb_schedule_cache.fetch_schedule_game(99113)

        self.assertEqual(game["gamePk"], 99113)
        self.assertEqual(cache_service.cache_status()["metrics"]["stale_hits"], 1)


if __name__ == "__main__":
    unittest.main()
