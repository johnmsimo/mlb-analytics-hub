import json
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import app as mlb_app


class ControlFileSnapshotTests(unittest.TestCase):
    def setUp(self):
        with mlb_app._JSON_SNAPSHOT_LOCK:
            mlb_app._JSON_SNAPSHOT_CACHE.clear()

    def tearDown(self):
        with mlb_app._JSON_SNAPSHOT_LOCK:
            mlb_app._JSON_SNAPSHOT_CACHE.clear()

    @staticmethod
    def _write_json(path, payload):
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)

    def test_adjustments_parse_once_and_return_private_values(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "model_adjustments.json")
            self._write_json(
                path,
                {
                    "bankroll": 2500.0,
                    "market_multipliers": {"batter_hits": 1.08},
                },
            )
            original_load = json.load
            calls = 0

            def counting_load(handle):
                nonlocal calls
                calls += 1
                return original_load(handle)

            with (
                patch.object(mlb_app, "ADJUST_STORE", path),
                patch.object(mlb_app.json, "load", side_effect=counting_load),
            ):
                first = mlb_app._get_adjustments()
                first["bankroll"] = 1.0
                first["market_multipliers"]["batter_hits"] = 9.0
                second = mlb_app._get_adjustments()

            self.assertEqual(calls, 1)
            self.assertEqual(second["bankroll"], 2500.0)
            self.assertEqual(second["market_multipliers"]["batter_hits"], 1.08)

    def test_adjustments_rebuild_after_file_replacement(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "model_adjustments.json")
            self._write_json(path, {"bankroll": 1000.0})

            with patch.object(mlb_app, "ADJUST_STORE", path):
                first = mlb_app._get_adjustments()
                replacement = f"{path}.new"
                self._write_json(replacement, {"bankroll": 3200.0})
                os.replace(replacement, path)
                second = mlb_app._get_adjustments()

            self.assertEqual(first["bankroll"], 1000.0)
            self.assertEqual(second["bankroll"], 3200.0)

    def test_concurrent_cold_reads_collapse_to_one_parse(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "model_adjustments.json")
            self._write_json(path, {"bankroll": 1750.0})
            original_load = json.load
            calls = 0
            call_lock = threading.Lock()

            def slow_counting_load(handle):
                nonlocal calls
                with call_lock:
                    calls += 1
                time.sleep(0.02)
                return original_load(handle)

            with (
                patch.object(mlb_app, "ADJUST_STORE", path),
                patch.object(
                    mlb_app.json,
                    "load",
                    side_effect=slow_counting_load,
                ),
                ThreadPoolExecutor(max_workers=16) as pool,
            ):
                values = list(pool.map(lambda _: mlb_app._get_adjustments(), range(32)))

            self.assertEqual(calls, 1)
            self.assertEqual({value["bankroll"] for value in values}, {1750.0})

    def test_calibration_history_reuses_snapshot_and_isolates_mutation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "calibration_history.json")
            self._write_json(
                path,
                [
                    {
                        "timestamp": "2026-07-30T12:00:00",
                        "adjustments": {
                            "market_multipliers": {"batter_hits": 1.04}
                        },
                    }
                ],
            )
            original_load = json.load
            calls = 0

            def counting_load(handle):
                nonlocal calls
                calls += 1
                return original_load(handle)

            with (
                patch.object(mlb_app, "CAL_HISTORY_STORE", path),
                patch.object(mlb_app.json, "load", side_effect=counting_load),
            ):
                first = mlb_app._history_in_window("2026-07-30", 1)
                first[0]["adjustments"]["market_multipliers"]["batter_hits"] = 5.0
                second = mlb_app._history_in_window("2026-07-30", 1)

            self.assertEqual(calls, 1)
            self.assertEqual(
                second[0]["adjustments"]["market_multipliers"]["batter_hits"],
                1.04,
            )

    def test_invalid_file_falls_back_without_poisoning_repaired_version(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "model_adjustments.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{invalid")

            with patch.object(mlb_app, "ADJUST_STORE", path):
                fallback = mlb_app._get_adjustments()
                replacement = f"{path}.new"
                self._write_json(replacement, {"bankroll": 4100.0})
                os.replace(replacement, path)
                repaired = mlb_app._get_adjustments()

            self.assertEqual(fallback["bankroll"], 1000.0)
            self.assertEqual(repaired["bankroll"], 4100.0)


if __name__ == "__main__":
    unittest.main()
