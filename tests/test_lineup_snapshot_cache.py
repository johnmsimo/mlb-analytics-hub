from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import lineup_loader


class LineupSnapshotCacheTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.data_dir_patch = patch.object(
            lineup_loader,
            "_DATA_DIR",
            self.tempdir.name,
        )
        self.data_dir_patch.start()
        lineup_loader._clear_lineup_snapshot_cache()

    def tearDown(self):
        lineup_loader._clear_lineup_snapshot_cache()
        self.data_dir_patch.stop()
        self.tempdir.cleanup()

    def _write_lineups(self, date_str: str, lineups: dict) -> str:
        path = lineup_loader._cache_path(date_str)
        with open(path, "w") as handle:
            json.dump({"lineups": lineups}, handle)
        return path

    def test_repeated_id_and_exact_name_lookups_parse_file_once(self):
        date_str = "2026-07-29"
        self._write_lineups(
            date_str,
            {
                "13": {
                    "player_name": "John Simo",
                    "expected_pa": 4.44,
                    "batting_order": 3,
                    "lineup_confirmed": 1,
                }
            },
        )
        real_json_load = json.load

        with (
            patch.object(lineup_loader, "_cache_fresh", return_value=True),
            patch.object(
                lineup_loader.json,
                "load",
                side_effect=real_json_load,
            ) as load,
        ):
            by_id = lineup_loader.get_lineup_features(
                mlbam_id=13,
                date_str=date_str,
            )
            by_name = lineup_loader.get_lineup_features(
                player_name="John Simo",
                date_str=date_str,
            )

        self.assertEqual(by_id, by_name)
        self.assertEqual(by_id["batting_order"], 3)
        self.assertEqual(load.call_count, 1)

    def test_changed_file_signature_rebuilds_snapshot(self):
        date_str = "2026-07-29"
        path = self._write_lineups(
            date_str,
            {
                "13": {
                    "player_name": "John Simo",
                    "batting_order": 2,
                    "lineup_confirmed": 1,
                }
            },
        )

        with patch.object(lineup_loader, "_cache_fresh", return_value=True):
            first = lineup_loader.get_lineup_features(
                mlbam_id=13,
                date_str=date_str,
            )
            self._write_lineups(
                date_str,
                {
                    "13": {
                        "player_name": "John Simo",
                        "batting_order": 5,
                        "lineup_confirmed": 1,
                    }
                },
            )
            stat = os.stat(path)
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
            second = lineup_loader.get_lineup_features(
                mlbam_id=13,
                date_str=date_str,
            )

        self.assertEqual(first["batting_order"], 2)
        self.assertEqual(second["batting_order"], 5)

    def test_partial_name_fallback_preserves_first_match_and_is_memoized(self):
        date_str = "2026-07-29"
        self._write_lineups(
            date_str,
            {
                "13": {"player_name": "John Simo", "batting_order": 3},
                "14": {"player_name": "Johnny Simo", "batting_order": 7},
            },
        )

        with patch.object(lineup_loader, "_cache_fresh", return_value=True):
            first = lineup_loader.get_lineup_features(
                player_name="Simo",
                date_str=date_str,
            )
            second = lineup_loader.get_lineup_features(
                player_name="Simo",
                date_str=date_str,
            )

        snapshot = lineup_loader._lineup_snapshot_cache[date_str]
        self.assertEqual(first["batting_order"], 3)
        self.assertEqual(second, first)
        self.assertEqual(list(snapshot["partial_name_memo"]), ["simo"])

    def test_missing_file_retains_default_contract(self):
        with (
            patch.object(lineup_loader, "_cache_fresh", return_value=False),
            patch.object(lineup_loader, "fetch_and_save", side_effect=OSError),
        ):
            result = lineup_loader.get_lineup_features(
                player_name="Missing Player",
                date_str="2026-07-29",
            )

        self.assertEqual(
            result,
            {
                "expected_pa": 4.20,
                "batting_order": 0,
                "lineup_confirmed": 0,
                "rbi_traffic_obp": 0.320,
            },
        )

    def test_concurrent_stale_requests_collapse_to_one_refresh(self):
        date_str = "2026-07-29"
        refreshed = threading.Event()
        calls = {"fetch": 0}

        def cache_fresh(_):
            return refreshed.is_set()

        def fetch(_):
            calls["fetch"] += 1
            self._write_lineups(
                date_str,
                {
                    "13": {
                        "player_name": "John Simo",
                        "batting_order": 3,
                    }
                },
            )
            refreshed.set()
            return {}

        with (
            patch.object(lineup_loader, "_cache_fresh", side_effect=cache_fresh),
            patch.object(lineup_loader, "fetch_and_save", side_effect=fetch),
            ThreadPoolExecutor(max_workers=6) as pool,
        ):
            results = list(
                pool.map(
                    lambda _: lineup_loader.get_lineup_features(
                        mlbam_id=13,
                        date_str=date_str,
                    ),
                    range(12),
                )
            )

        self.assertEqual(calls["fetch"], 1)
        self.assertTrue(all(row["batting_order"] == 3 for row in results))

    def test_snapshot_cache_is_bounded_by_date(self):
        with patch.object(lineup_loader, "_cache_fresh", return_value=True):
            for day in range(1, 6):
                date_str = f"2026-07-{day:02d}"
                self._write_lineups(
                    date_str,
                    {
                        str(day): {
                            "player_name": f"Player {day}",
                            "batting_order": day,
                        }
                    },
                )
                lineup_loader.get_lineup_features(
                    mlbam_id=day,
                    date_str=date_str,
                )

        self.assertEqual(
            len(lineup_loader._lineup_snapshot_cache),
            lineup_loader._LINEUP_SNAPSHOT_MAX_DATES,
        )
        self.assertNotIn("2026-07-01", lineup_loader._lineup_snapshot_cache)
        self.assertNotIn("2026-07-02", lineup_loader._lineup_snapshot_cache)


if __name__ == "__main__":
    unittest.main()
