from __future__ import annotations

import unittest
from unittest.mock import patch

import cache_service
import mlb_schedule_cache
from redis_client import _MemoryClient


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
        games = [{"gamePk": 13}]
        with patch(
            "mlb_schedule_cache.mlb_client.schedule",
            return_value=games,
        ) as schedule:
            first = mlb_schedule_cache.fetch_schedule("2026-07-28")
            second = mlb_schedule_cache.fetch_schedule("2026-07-28")

        self.assertEqual(first, second)
        self.assertEqual(schedule.call_count, 1)
        self.assertEqual(schedule.call_args.kwargs["date_str"], "2026-07-28")

    def test_game_schedule_is_reused(self):
        games = [{"gamePk": 99113, "status": "Preview"}]
        with patch(
            "mlb_schedule_cache.mlb_client.schedule",
            return_value=games,
        ) as schedule:
            first = mlb_schedule_cache.fetch_schedule_game(99113)
            second = mlb_schedule_cache.fetch_schedule_game(99113)

        self.assertEqual(first, second)
        self.assertEqual(first["gamePk"], 99113)
        self.assertEqual(schedule.call_count, 1)
        self.assertEqual(schedule.call_args.kwargs["game_pk"], 99113)

    def test_date_and_game_keys_do_not_collide(self):
        date_response = [{"gamePk": 1}]
        game_response = [{"gamePk": 2}]
        with patch(
            "mlb_schedule_cache.mlb_client.schedule",
            side_effect=[date_response, game_response],
        ) as schedule:
            self.assertEqual(
                mlb_schedule_cache.fetch_schedule("99113"),
                [{"gamePk": 1}],
            )
            self.assertEqual(
                mlb_schedule_cache.fetch_schedule_game(99113),
                {"gamePk": 2},
            )

        self.assertEqual(schedule.call_count, 2)

    def test_not_found_game_is_cached(self):
        with patch(
            "mlb_schedule_cache.mlb_client.schedule",
            return_value=[],
        ) as schedule:
            self.assertIsNone(mlb_schedule_cache.fetch_schedule_game(99113))
            self.assertIsNone(mlb_schedule_cache.fetch_schedule_game(99113))

        self.assertEqual(schedule.call_count, 1)

    def test_upstream_error_serves_stale_game(self):
        key = cache_service.normalize_cache_key("mlb_schedule_game", "99113")
        self.cache.set(
            f"{key}:stale",
            {"found": True, "game": {"gamePk": 99113, "status": "Preview"}},
            ttl=60,
        )
        with patch(
            "mlb_schedule_cache.mlb_client.schedule",
            side_effect=ConnectionError("MLB unavailable"),
        ):
            game = mlb_schedule_cache.fetch_schedule_game(99113)

        self.assertEqual(game["gamePk"], 99113)
        self.assertEqual(cache_service.cache_status()["metrics"]["stale_hits"], 1)


if __name__ == "__main__":
    unittest.main()
