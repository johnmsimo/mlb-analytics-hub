import json
import os
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import weather_loader


class WeatherSnapshotCacheTests(unittest.TestCase):
    def setUp(self):
        weather_loader._clear_snapshot_cache()
        self.tempdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        weather_loader._clear_snapshot_cache()
        self.tempdir.cleanup()

    def _path(self, date_str):
        return os.path.join(self.tempdir.name, f"weather_{date_str}.json")

    def _write_weather(self, date_str, games):
        path = self._path(date_str)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "date": date_str,
                    "games": {str(game_pk): row for game_pk, row in games.items()},
                },
                handle,
            )
        stat = os.stat(path)
        os.utime(
            path,
            ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000),
        )
        return path

    def test_fresh_file_is_parsed_once_for_repeated_lookups(self):
        date_str = "2026-07-30"
        path = self._write_weather(
            date_str,
            {13: {"temperature": 75.0, "wind_speed": 9.0}},
        )
        real_reader = weather_loader._read_weather_file

        with (
            patch.object(weather_loader, "_cache_path", return_value=path),
            patch.object(
                weather_loader,
                "_read_weather_file",
                wraps=real_reader,
            ) as read_file,
        ):
            first = weather_loader.get_game_weather(13, date_str)
            second = weather_loader.get_game_weather(13, date_str)

        self.assertEqual(first, second)
        self.assertEqual(read_file.call_count, 1)

    def test_snapshot_reloads_after_file_replacement(self):
        date_str = "2026-07-30"
        path = self._write_weather(
            date_str,
            {13: {"temperature": 70.0}},
        )

        with patch.object(weather_loader, "_cache_path", return_value=path):
            first = weather_loader.get_game_weather(13, date_str)
            self._write_weather(
                date_str,
                {13: {"temperature": 82.0, "wind_speed": 12.0}},
            )
            weather_loader._snapshots[date_str]["check_after"] = 0.0
            second = weather_loader.get_game_weather(13, date_str)

        self.assertEqual(first["temperature"], 70.0)
        self.assertEqual(second["temperature"], 82.0)

    def test_returned_weather_is_mutation_isolated(self):
        date_str = "2026-07-30"
        path = self._write_weather(
            date_str,
            {13: {"temperature": 75.0, "wind_speed": 9.0}},
        )

        with patch.object(weather_loader, "_cache_path", return_value=path):
            first = weather_loader.get_all_weather(date_str)
            first[13]["temperature"] = -100.0
            first[99] = {"temperature": 99.0}
            second = weather_loader.get_all_weather(date_str)

        self.assertEqual(second[13]["temperature"], 75.0)
        self.assertNotIn(99, second)

    def test_hot_lookup_avoids_repeated_filesystem_checks(self):
        date_str = "2026-07-30"
        path = self._write_weather(
            date_str,
            {13: {"temperature": 75.0}},
        )

        with patch.object(weather_loader, "_cache_path", return_value=path):
            weather_loader.get_game_weather(13, date_str)
            with patch.object(
                weather_loader,
                "_file_signature",
                wraps=weather_loader._file_signature,
            ) as signature:
                for _ in range(50):
                    weather_loader.get_game_weather(13, date_str)

        signature.assert_not_called()

    def test_concurrent_fresh_reads_parse_once(self):
        date_str = "2026-07-30"
        path = self._write_weather(
            date_str,
            {13: {"temperature": 75.0}},
        )
        real_reader = weather_loader._read_weather_file

        def slow_reader(file_path):
            time.sleep(0.03)
            return real_reader(file_path)

        with (
            patch.object(weather_loader, "_cache_path", return_value=path),
            patch.object(
                weather_loader,
                "_read_weather_file",
                side_effect=slow_reader,
            ) as read_file,
            ThreadPoolExecutor(max_workers=8) as pool,
        ):
            results = list(
                pool.map(
                    lambda _: weather_loader.get_game_weather(13, date_str),
                    range(16),
                )
            )

        self.assertEqual(read_file.call_count, 1)
        self.assertEqual(results, [{"temperature": 75.0}] * 16)

    def test_concurrent_cache_misses_share_one_weather_refresh(self):
        date_str = "2026-07-30"
        path = self._path(date_str)
        home_teams = {13: "NYY", 14: "NYY", 15: "BOS"}

        def slow_weather(team, _date_str):
            time.sleep(0.02)
            return {
                "temperature": 80.0 if team == "NYY" else 70.0,
                "wind_speed": 10.0,
                "wind_direction_factor": 0.0,
            }

        with (
            patch.object(weather_loader, "_cache_path", return_value=path),
            patch.object(
                weather_loader,
                "_fetch_home_teams",
                return_value=home_teams,
            ) as fetch_teams,
            patch.object(
                weather_loader,
                "_weather_for_team",
                side_effect=slow_weather,
            ) as fetch_weather,
            ThreadPoolExecutor(max_workers=8) as pool,
        ):
            results = list(
                pool.map(
                    lambda _: weather_loader.get_all_weather(date_str),
                    range(8),
                )
            )

        self.assertEqual(fetch_teams.call_count, 1)
        self.assertEqual(fetch_weather.call_count, 2)
        self.assertEqual(results, [results[0]] * 8)

    def test_stale_file_triggers_refresh_instead_of_snapshot_reuse(self):
        date_str = "2026-07-30"
        path = self._write_weather(
            date_str,
            {13: {"temperature": 65.0}},
        )
        stale = time.time() - weather_loader._CACHE_TTL_SEC - 10
        os.utime(path, (stale, stale))

        with (
            patch.object(weather_loader, "_cache_path", return_value=path),
            patch.object(
                weather_loader,
                "_fetch_home_teams",
                return_value={13: "NYY"},
            ) as fetch_teams,
            patch.object(
                weather_loader,
                "_weather_for_team",
                return_value={"temperature": 81.0},
            ),
        ):
            weather = weather_loader.get_game_weather(13, date_str)

        self.assertEqual(weather["temperature"], 81.0)
        self.assertEqual(fetch_teams.call_count, 1)

    def test_snapshot_date_cache_is_bounded(self):
        with patch.object(
            weather_loader,
            "_SNAPSHOT_MAX_DATES",
            2,
        ):
            for day in ("2026-07-28", "2026-07-29", "2026-07-30"):
                path = self._write_weather(day, {13: {"temperature": 75.0}})
                weather_loader._cache_snapshot(
                    day,
                    path,
                    {13: {"temperature": 75.0}},
                )

        self.assertEqual(
            list(weather_loader._snapshots),
            ["2026-07-29", "2026-07-30"],
        )


if __name__ == "__main__":
    unittest.main()
