import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import app as mlb_app


class TrackerCopyOnWriteTests(unittest.TestCase):
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
                "day_json": {},
                "date_keys": (),
            })

    @staticmethod
    def _store():
        return {
            "2026-07-28": {
                "capturedAt": "2026-07-28T12:00:00",
                "entries": [{"id": "older", "marker": "keep"}],
            },
            "2026-07-29": [
                {"id": "legacy", "marker": "legacy-list"},
            ],
            "2026-07-30": {
                "capturedAt": "2026-07-30T12:00:00",
                "entries": [{"id": "today", "marker": "before"}],
            },
        }

    @staticmethod
    def _write_json(path, payload):
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))

    def test_warm_single_day_commit_reuses_json_and_refreshes_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "daily_tracker.json")
            original = self._store()
            self._write_json(path, original)
            updated = {
                "capturedAt": "2026-07-30T13:00:00",
                "entries": [{"id": "today", "marker": "after"}],
            }

            with patch.object(mlb_app, "TRACKER_STORE", path):
                mlb_app._tracker_cached_snapshot()
                with (
                    patch.object(
                        mlb_app.json,
                        "load",
                        side_effect=AssertionError("tracker file was reparsed"),
                    ),
                    patch.object(
                        mlb_app,
                        "_tracker_store",
                        side_effect=AssertionError("full store was cloned"),
                    ),
                ):
                    self.assertTrue(
                        mlb_app._tracker_commit_day("2026-07-30", updated)
                    )
                    reread = mlb_app._tracker_store_for_dates(
                        ["2026-07-28", "2026-07-30"]
                    )

            with open(path, encoding="utf-8") as handle:
                saved = json.load(handle)

            self.assertEqual(saved["2026-07-28"], original["2026-07-28"])
            self.assertEqual(saved["2026-07-29"], original["2026-07-29"])
            self.assertEqual(saved["2026-07-30"], updated)
            self.assertEqual(
                reread["2026-07-30"]["entries"][0]["marker"],
                "after",
            )

    def test_new_day_appends_without_changing_existing_day_shapes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "daily_tracker.json")
            self._write_json(path, self._store())
            new_day = {
                "capturedAt": "2026-07-31T12:00:00",
                "entries": [{"id": "new"}],
            }

            with patch.object(mlb_app, "TRACKER_STORE", path):
                self.assertTrue(
                    mlb_app._tracker_commit_day("2026-07-31", new_day)
                )
                self.assertEqual(
                    mlb_app._tracker_date_keys(),
                    (
                        "2026-07-28",
                        "2026-07-29",
                        "2026-07-30",
                        "2026-07-31",
                    ),
                )

            with open(path, encoding="utf-8") as handle:
                saved = json.load(handle)

            self.assertIsInstance(saved["2026-07-29"], list)
            self.assertEqual(saved["2026-07-31"], new_day)

    def test_concurrent_day_commits_preserve_both_updates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "daily_tracker.json")
            self._write_json(path, self._store())
            barrier = threading.Barrier(2)

            def commit(date_key):
                barrier.wait()
                return mlb_app._tracker_commit_day(
                    date_key,
                    {
                        "capturedAt": f"{date_key}T15:00:00",
                        "entries": [{"id": date_key, "marker": "updated"}],
                    },
                )

            with (
                patch.object(mlb_app, "TRACKER_STORE", path),
                ThreadPoolExecutor(max_workers=2) as pool,
            ):
                results = list(
                    pool.map(commit, ["2026-07-28", "2026-07-30"])
                )

            with open(path, encoding="utf-8") as handle:
                saved = json.load(handle)

            self.assertEqual(results, [True, True])
            self.assertEqual(
                saved["2026-07-28"]["entries"][0]["marker"],
                "updated",
            )
            self.assertEqual(
                saved["2026-07-30"]["entries"][0]["marker"],
                "updated",
            )
            self.assertEqual(
                saved["2026-07-29"][0]["marker"],
                "legacy-list",
            )

    def test_failed_replace_preserves_file_and_cached_day(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "daily_tracker.json")
            original = self._store()
            self._write_json(path, original)

            with patch.object(mlb_app, "TRACKER_STORE", path):
                mlb_app._tracker_cached_snapshot()
                with patch.object(
                    mlb_app.os,
                    "replace",
                    side_effect=OSError("simulated replace failure"),
                ):
                    self.assertFalse(
                        mlb_app._tracker_commit_day(
                            "2026-07-30",
                            {"entries": [{"marker": "lost"}]},
                        )
                    )
                cached = mlb_app._tracker_store_for_dates(["2026-07-30"])

            with open(path, encoding="utf-8") as handle:
                saved = json.load(handle)

            self.assertEqual(saved, original)
            self.assertEqual(
                cached["2026-07-30"]["entries"][0]["marker"],
                "before",
            )
            self.assertFalse(
                any(name.endswith(".tmp") for name in os.listdir(temp_dir))
            )

    def test_full_store_rebuilds_lazily_after_partial_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "daily_tracker.json")
            self._write_json(path, self._store())
            updated = {
                "capturedAt": "2026-07-30T16:00:00",
                "entries": [{"id": "today", "marker": "lazy"}],
            }

            with patch.object(mlb_app, "TRACKER_STORE", path):
                self.assertTrue(
                    mlb_app._tracker_commit_day("2026-07-30", updated)
                )
                self.assertIsNone(mlb_app._TRACKER_READ_CACHE["pickled"])
                full = mlb_app._tracker_store()
                self.assertIsNotNone(mlb_app._TRACKER_READ_CACHE["pickled"])
                full["2026-07-30"]["entries"].clear()
                isolated = mlb_app._tracker_store()

            self.assertEqual(set(full), set(self._store()))
            self.assertEqual(
                isolated["2026-07-30"]["entries"][0]["marker"],
                "lazy",
            )


if __name__ == "__main__":
    unittest.main()
