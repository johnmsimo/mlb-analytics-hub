import json
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import app as mlb_app


class MlbMemoryStoreSnapshotTests(unittest.TestCase):
    def setUp(self):
        self._clear_cache()

    def tearDown(self):
        self._clear_cache()

    @staticmethod
    def _clear_cache():
        with mlb_app._MLB_MEMORY_READ_LOCK:
            mlb_app._MLB_MEMORY_READ_CACHE.update(
                {
                    "sig": None,
                    "payload": None,
                    "latest": None,
                    "latest_summary": None,
                    "status": None,
                }
            )

    @staticmethod
    def _payload(marker="first"):
        latest = {
            "createdAt": "2026-07-30T12:00:00Z",
            "targetDateET": "2026-07-30",
            "meta": {
                "teamCount": 30,
                "gameCount": 15,
                "featuredPlayers": 160,
                "mode": "light",
            },
            "marker": marker,
            "games": {"boxscores": [{"gamePk": i} for i in range(50)]},
        }
        return {
            "latest": latest,
            "snapshots": [
                {"createdAt": "2026-07-29T12:00:00Z", "marker": "older"},
                latest,
            ],
            "updatedAt": "2026-07-30T12:00:01Z",
        }

    @staticmethod
    def _write_json(path, payload):
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))

    def test_all_views_share_one_parse_and_return_private_values(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "mlb_memory_store.json")
            self._write_json(path, self._payload())
            original_load = json.load
            calls = 0

            def counting_load(handle):
                nonlocal calls
                calls += 1
                return original_load(handle)

            with (
                patch.object(mlb_app, "MLB_MEMORY_STORE", path),
                patch.object(mlb_app.json, "load", side_effect=counting_load),
            ):
                status = mlb_app._mlb_memory_store_status_view()
                summary = mlb_app._mlb_memory_latest_snapshot(summary_only=True)
                latest = mlb_app._mlb_memory_latest_snapshot()
                full = mlb_app._mlb_memory_store_payload()

                status["latest"]["mode"] = "changed"
                summary["meta"]["teamCount"] = 1
                latest["games"]["boxscores"].clear()
                full["snapshots"].clear()

                status_again = mlb_app._mlb_memory_store_status_view()
                summary_again = mlb_app._mlb_memory_latest_snapshot(
                    summary_only=True
                )
                latest_again = mlb_app._mlb_memory_latest_snapshot()
                full_again = mlb_app._mlb_memory_store_payload()

            self.assertEqual(calls, 1)
            self.assertEqual(status_again["snapshotCount"], 2)
            self.assertEqual(status_again["latest"]["mode"], "light")
            self.assertEqual(summary_again["meta"]["teamCount"], 30)
            self.assertEqual(len(latest_again["games"]["boxscores"]), 50)
            self.assertEqual(len(full_again["snapshots"]), 2)

    def test_atomic_replacement_invalidates_even_with_same_size_and_mtime(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "mlb_memory_store.json")
            self._write_json(path, self._payload("first"))
            original_stat = os.stat(path)

            with patch.object(mlb_app, "MLB_MEMORY_STORE", path):
                first = mlb_app._mlb_memory_latest_snapshot()
                replacement = f"{path}.new"
                self._write_json(replacement, self._payload("other"))
                self.assertEqual(os.path.getsize(replacement), original_stat.st_size)
                os.utime(
                    replacement,
                    ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
                )
                os.replace(replacement, path)
                second = mlb_app._mlb_memory_latest_snapshot()

            self.assertEqual(first["marker"], "first")
            self.assertEqual(second["marker"], "other")

    def test_concurrent_cold_reads_collapse_to_one_parse(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "mlb_memory_store.json")
            self._write_json(path, self._payload())
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
                patch.object(mlb_app, "MLB_MEMORY_STORE", path),
                patch.object(
                    mlb_app.json,
                    "load",
                    side_effect=slow_counting_load,
                ),
                ThreadPoolExecutor(max_workers=16) as pool,
            ):
                values = list(
                    pool.map(
                        lambda _: mlb_app._mlb_memory_store_status_view(),
                        range(32),
                    )
                )

            self.assertEqual(calls, 1)
            self.assertEqual({value["snapshotCount"] for value in values}, {2})

    def test_status_and_summary_routes_bypass_full_store_copy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "mlb_memory_store.json")
            self._write_json(path, self._payload())

            with (
                patch.object(mlb_app, "MLB_MEMORY_STORE", path),
                patch.object(
                    mlb_app,
                    "_mlb_memory_store_payload",
                    side_effect=AssertionError("full store should not be copied"),
                ),
                mlb_app.app.test_client() as client,
            ):
                status_response = client.get("/api/memory/status")
                summary_response = client.get(
                    "/api/memory/latest?summaryOnly=true"
                )

            self.assertEqual(status_response.status_code, 200)
            self.assertEqual(summary_response.status_code, 200)
            self.assertEqual(
                status_response.get_json()["status"]["snapshotCount"], 2
            )
            self.assertEqual(
                summary_response.get_json()["snapshot"]["meta"]["gameCount"],
                15,
            )

    def test_invalid_file_does_not_poison_repaired_version(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "mlb_memory_store.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{invalid")

            with patch.object(mlb_app, "MLB_MEMORY_STORE", path):
                fallback = mlb_app._mlb_memory_store_payload()
                replacement = f"{path}.new"
                self._write_json(replacement, self._payload("repaired"))
                os.replace(replacement, path)
                repaired = mlb_app._mlb_memory_latest_snapshot()

            self.assertEqual(fallback, mlb_app._mlb_memory_store_default())
            self.assertEqual(repaired["marker"], "repaired")


if __name__ == "__main__":
    unittest.main()
