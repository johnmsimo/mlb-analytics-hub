import json
import os
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import umpire_loader


class UmpireSnapshotCacheTests(unittest.TestCase):
    def setUp(self):
        umpire_loader._clear_snapshot_caches()
        self.tempdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        umpire_loader._clear_snapshot_caches()
        self.tempdir.cleanup()

    def _path(self, name):
        return os.path.join(self.tempdir.name, name)

    def _write_json(self, path, payload):
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        stat = os.stat(path)
        os.utime(
            path,
            ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000),
        )

    def test_historical_file_is_parsed_once_for_repeated_lookups(self):
        path = self._path("ump_historical.json")
        self._write_json(path, {"test umpire": [0.123, 0.456]})
        real_reader = umpire_loader._read_historical_file

        with (
            patch.object(umpire_loader, "_hist_cache_path", return_value=path),
            patch.object(
                umpire_loader,
                "_read_historical_file",
                wraps=real_reader,
            ) as read_file,
        ):
            exact = umpire_loader.get_umpire_features("Test Umpire")
            by_last = umpire_loader.get_umpire_features("Someone Umpire")
            exact_again = umpire_loader.get_umpire_features("Test Umpire")

        self.assertEqual(exact, {"ump_zone_size": 0.123, "ump_k_boost": 0.456})
        self.assertEqual(by_last, exact)
        self.assertEqual(exact_again, exact)
        self.assertEqual(read_file.call_count, 1)

    def test_historical_snapshot_rebuilds_after_file_replacement(self):
        path = self._path("ump_historical.json")
        self._write_json(path, {"test umpire": [0.1, 0.2]})

        with patch.object(umpire_loader, "_hist_cache_path", return_value=path):
            first = umpire_loader.get_umpire_features("Test Umpire")
            self._write_json(path, {"test umpire": [0.333, 0.444]})
            second = umpire_loader.get_umpire_features("Test Umpire")

        self.assertEqual(first, {"ump_zone_size": 0.1, "ump_k_boost": 0.2})
        self.assertEqual(second, {"ump_zone_size": 0.333, "ump_k_boost": 0.444})

    def test_exact_last_and_first_prefix_matching_preserve_order(self):
        snapshot = umpire_loader._build_historical_snapshot(
            {
                "alexander shared": (0.11, 0.22),
                "albert shared": (0.33, 0.44),
            }
        )

        with patch.object(
            umpire_loader,
            "_load_historical_snapshot",
            return_value=snapshot,
        ):
            exact = umpire_loader.get_umpire_features("Albert Shared")
            by_last = umpire_loader.get_umpire_features("Someone Shared")
            by_first_prefix = umpire_loader.get_umpire_features("Alexa Missing")

        self.assertEqual(exact, {"ump_zone_size": 0.33, "ump_k_boost": 0.44})
        self.assertEqual(by_last, {"ump_zone_size": 0.11, "ump_k_boost": 0.22})
        self.assertEqual(
            by_first_prefix,
            {"ump_zone_size": 0.11, "ump_k_boost": 0.22},
        )

    def test_concurrent_historical_lookups_build_one_snapshot(self):
        path = self._path("ump_historical.json")
        self._write_json(path, {"test umpire": [0.123, 0.456]})
        real_reader = umpire_loader._read_historical_file

        def slow_reader(file_path):
            time.sleep(0.03)
            return real_reader(file_path)

        with (
            patch.object(umpire_loader, "_hist_cache_path", return_value=path),
            patch.object(
                umpire_loader,
                "_read_historical_file",
                side_effect=slow_reader,
            ) as read_file,
            ThreadPoolExecutor(max_workers=8) as pool,
        ):
            results = list(
                pool.map(
                    lambda _: umpire_loader.get_umpire_features("Test Umpire"),
                    range(16),
                )
            )

        self.assertEqual(read_file.call_count, 1)
        self.assertEqual(
            results,
            [{"ump_zone_size": 0.123, "ump_k_boost": 0.456}] * 16,
        )

    def test_failed_historical_refresh_has_bounded_retry_cooldown(self):
        path = self._path("missing_historical.json")

        with (
            patch.object(umpire_loader, "_hist_cache_path", return_value=path),
            patch.object(
                umpire_loader,
                "_fetch_savant_career",
                return_value={},
            ) as fetch,
        ):
            first = umpire_loader.get_umpire_features("Pat Hoberg")
            second = umpire_loader.get_umpire_features("Pat Hoberg")

        self.assertEqual(first, second)
        self.assertEqual(fetch.call_count, 1)

    def test_daily_officials_file_is_parsed_once_and_results_are_isolated(self):
        path = self._path("umpires_2026-07-30.json")
        self._write_json(
            path,
            {"officials": {"13": "Test Umpire"}},
        )
        real_reader = umpire_loader._read_officials_file

        with (
            patch.object(umpire_loader, "_ump_cache_path", return_value=path),
            patch.object(
                umpire_loader,
                "_read_officials_file",
                wraps=real_reader,
            ) as read_file,
            patch.object(umpire_loader, "_fetch_game_officials") as fetch,
        ):
            first = umpire_loader.fetch_and_save("2026-07-30")
            first[13] = "Mutated"
            second = umpire_loader.fetch_and_save("2026-07-30")

        self.assertEqual(second, {13: "Test Umpire"})
        self.assertEqual(read_file.call_count, 1)
        fetch.assert_not_called()

    def test_daily_officials_snapshot_reloads_after_file_replacement(self):
        path = self._path("umpires_2026-07-30.json")
        self._write_json(path, {"officials": {"13": "First Umpire"}})

        with patch.object(umpire_loader, "_ump_cache_path", return_value=path):
            first = umpire_loader.get_game_umpire(13, "2026-07-30")
            self._write_json(path, {"officials": {"13": "Second Umpire"}})
            second = umpire_loader.get_game_umpire(13, "2026-07-30")

        self.assertEqual(first, "First Umpire")
        self.assertEqual(second, "Second Umpire")

    def test_concurrent_daily_misses_share_one_schedule_fetch(self):
        path = self._path("umpires_2026-07-30.json")

        def slow_fetch(_date_str):
            time.sleep(0.03)
            return {13: "Test Umpire"}

        with (
            patch.object(umpire_loader, "_ump_cache_path", return_value=path),
            patch.object(
                umpire_loader,
                "_fetch_game_officials",
                side_effect=slow_fetch,
            ) as fetch,
            ThreadPoolExecutor(max_workers=8) as pool,
        ):
            results = list(
                pool.map(
                    lambda _: umpire_loader.fetch_and_save("2026-07-30"),
                    range(8),
                )
            )

        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(results, [{13: "Test Umpire"}] * 8)


if __name__ == "__main__":
    unittest.main()
