import json
import os
import pickle
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import app as mlb_app


class TrackerDaySnapshotTests(unittest.TestCase):
    def setUp(self):
        self._clear_cache()

    def tearDown(self):
        self._clear_cache()

    @staticmethod
    def _clear_cache():
        with mlb_app._TRACKER_READ_LOCK:
            mlb_app._TRACKER_READ_CACHE.update({
                "sig": None,
                "pickled": None,
                "day_pickles": {},
                "date_keys": (),
            })

    @staticmethod
    def _store(marker="first"):
        return {
            "2026-07-28": {
                "capturedAt": "2026-07-28T12:00:00",
                "entries": [{"player": "Older", "marker": marker}],
            },
            "2026-07-29": {
                "capturedAt": "2026-07-29T12:00:00",
                "entries": [{"player": "Prior", "marker": marker}],
            },
            "2026-07-30": {
                "capturedAt": "2026-07-30T12:00:00",
                "entries": [{"player": "Current", "marker": marker}],
            },
        }

    @staticmethod
    def _write_json(path, payload):
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))

    def test_full_and_partial_reads_share_one_parse_and_isolate_mutation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "daily_tracker.json")
            self._write_json(path, self._store())
            original_load = json.load
            calls = 0

            def counting_load(handle):
                nonlocal calls
                calls += 1
                return original_load(handle)

            with (
                patch.object(mlb_app, "TRACKER_STORE", path),
                patch.object(mlb_app.json, "load", side_effect=counting_load),
            ):
                partial = mlb_app._tracker_store_for_dates(
                    ["2026-07-30", "2026-07-28"]
                )
                full = mlb_app._tracker_store()
                partial["2026-07-30"]["entries"].clear()
                full["2026-07-28"]["entries"][0]["player"] = "Changed"

                partial_again = mlb_app._tracker_store_for_dates(
                    ["2026-07-30", "2026-07-28"]
                )
                full_again = mlb_app._tracker_store()

            self.assertEqual(calls, 1)
            self.assertEqual(set(partial_again), {"2026-07-28", "2026-07-30"})
            self.assertEqual(
                partial_again["2026-07-30"]["entries"][0]["player"],
                "Current",
            )
            self.assertEqual(
                full_again["2026-07-28"]["entries"][0]["player"],
                "Older",
            )

    def test_atomic_replacement_invalidates_with_same_size_and_mtime(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "daily_tracker.json")
            self._write_json(path, self._store("first"))
            original_stat = os.stat(path)

            with patch.object(mlb_app, "TRACKER_STORE", path):
                first = mlb_app._tracker_store_for_dates(["2026-07-30"])
                replacement = f"{path}.new"
                self._write_json(replacement, self._store("other"))
                self.assertEqual(os.path.getsize(replacement), original_stat.st_size)
                os.utime(
                    replacement,
                    ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
                )
                os.replace(replacement, path)
                second = mlb_app._tracker_store_for_dates(["2026-07-30"])

            self.assertEqual(
                first["2026-07-30"]["entries"][0]["marker"],
                "first",
            )
            self.assertEqual(
                second["2026-07-30"]["entries"][0]["marker"],
                "other",
            )

    def test_concurrent_cold_reads_collapse_to_one_parse(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "daily_tracker.json")
            self._write_json(path, self._store())
            original_load = json.load
            calls = 0
            calls_lock = threading.Lock()

            def slow_counting_load(handle):
                nonlocal calls
                with calls_lock:
                    calls += 1
                time.sleep(0.02)
                return original_load(handle)

            with (
                patch.object(mlb_app, "TRACKER_STORE", path),
                patch.object(
                    mlb_app.json,
                    "load",
                    side_effect=slow_counting_load,
                ),
                ThreadPoolExecutor(max_workers=16) as pool,
            ):
                results = list(
                    pool.map(
                        lambda _: mlb_app._tracker_store_for_dates(
                            ["2026-07-30"]
                        ),
                        range(32),
                    )
                )

            self.assertEqual(calls, 1)
            self.assertEqual(
                {
                    result["2026-07-30"]["entries"][0]["player"]
                    for result in results
                },
                {"Current"},
            )

    def test_partial_read_does_not_deserialize_full_store_blob(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "daily_tracker.json")
            payload = self._store()
            payload["2026-07-01"] = {
                "entries": [{"notes": "x" * 100_000}]
            }
            self._write_json(path, payload)

            with patch.object(mlb_app, "TRACKER_STORE", path):
                snapshot = mlb_app._tracker_cached_snapshot()
                full_blob = snapshot["pickled"]
                original_loads = pickle.loads

                def guarded_loads(blob):
                    if blob is full_blob:
                        raise AssertionError("full tracker blob was deserialized")
                    return original_loads(blob)

                with patch.object(pickle, "loads", side_effect=guarded_loads):
                    partial = mlb_app._tracker_store_for_dates(
                        ["2026-07-30"]
                    )

            self.assertEqual(set(partial), {"2026-07-30"})
            self.assertEqual(
                partial["2026-07-30"]["entries"][0]["player"],
                "Current",
            )

    def test_invalid_file_does_not_poison_repaired_version(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "daily_tracker.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{invalid")

            with patch.object(mlb_app, "TRACKER_STORE", path):
                self.assertEqual(mlb_app._tracker_store(), {})
                replacement = f"{path}.new"
                self._write_json(replacement, self._store("fixed"))
                os.replace(replacement, path)
                repaired = mlb_app._tracker_store_for_dates(
                    ["2026-07-30"]
                )

            self.assertEqual(
                repaired["2026-07-30"]["entries"][0]["marker"],
                "fixed",
            )


if __name__ == "__main__":
    unittest.main()
