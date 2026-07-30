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

    def test_append_serializes_once_and_advances_all_cached_views(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "mlb_memory_store.json")
            self._write_json(path, self._payload())
            new_snapshot = self._payload("appended")["latest"]
            original_dumps = json.dumps
            dumps_calls = 0

            def counting_dumps(*args, **kwargs):
                nonlocal dumps_calls
                dumps_calls += 1
                return original_dumps(*args, **kwargs)

            with patch.object(mlb_app, "MLB_MEMORY_STORE", path):
                mlb_app._mlb_memory_store_status_view()
                with (
                    patch.object(
                        mlb_app.json,
                        "dumps",
                        side_effect=counting_dumps,
                    ),
                    patch.object(
                        mlb_app.json,
                        "load",
                        side_effect=AssertionError(
                            "warm append should not reparse the store"
                        ),
                    ),
                ):
                    written = mlb_app._append_mlb_memory_snapshot(
                        new_snapshot,
                        keep=30,
                    )
                    status = mlb_app._mlb_memory_store_status_view()
                    summary = mlb_app._mlb_memory_latest_snapshot(
                        summary_only=True
                    )
                    latest = mlb_app._mlb_memory_latest_snapshot()
                    full = mlb_app._mlb_memory_store_payload()

            self.assertEqual(dumps_calls, 1)
            self.assertEqual(status["snapshotCount"], 3)
            self.assertEqual(latest["marker"], "appended")
            self.assertEqual(summary["createdAt"], latest["createdAt"])
            self.assertEqual(full, written)

    def test_append_preserves_retention_and_recent_detail_contract(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "mlb_memory_store.json")
            snapshots = []
            for idx in range(8):
                snap = self._payload(f"snapshot-{idx}")["latest"]
                snap["games"]["boxscores"] = [
                    {"gamePk": value}
                    for value in range(12)
                ]
                snapshots.append(snap)
            self._write_json(
                path,
                {
                    "latest": snapshots[-1],
                    "snapshots": snapshots,
                    "updatedAt": "before",
                },
            )
            appended = self._payload("snapshot-8")["latest"]
            appended["games"]["boxscores"] = [
                {"gamePk": value}
                for value in range(12)
            ]

            with patch.object(mlb_app, "MLB_MEMORY_STORE", path):
                written = mlb_app._append_mlb_memory_snapshot(
                    appended,
                    keep=6,
                )

            kept = written["snapshots"]
            self.assertEqual(len(kept), 6)
            self.assertEqual(
                [snap["marker"] for snap in kept],
                [f"snapshot-{idx}" for idx in range(3, 9)],
            )
            self.assertEqual(
                [snap["compact"] for snap in kept],
                [True, True, True, False, False, False],
            )
            self.assertNotIn("boxscores", kept[0]["games"])
            self.assertEqual(len(kept[-1]["games"]["boxscores"]), 12)
            self.assertIs(written["latest"], kept[-1])

    def test_append_prunes_to_byte_limit_before_atomic_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "mlb_memory_store.json")
            snapshots = []
            for idx in range(9):
                snap = self._payload(f"snapshot-{idx}")["latest"]
                snap["padding"] = "x" * 800
                snapshots.append(snap)
            self._write_json(
                path,
                {
                    "latest": snapshots[-1],
                    "snapshots": snapshots,
                    "updatedAt": "before",
                },
            )
            appended = self._payload("snapshot-9")["latest"]
            appended["padding"] = "x" * 800

            with (
                patch.object(mlb_app, "MLB_MEMORY_STORE", path),
                patch.object(mlb_app, "_MLB_MEMORY_MAX_BYTES", 7_000),
            ):
                written = mlb_app._append_mlb_memory_snapshot(
                    appended,
                    keep=30,
                )

            self.assertEqual(len(written["snapshots"]), 6)
            self.assertEqual(written["latest"]["marker"], "snapshot-9")
            with open(path, "rb") as handle:
                persisted = json.load(handle)
            self.assertEqual(persisted, written)

    def test_failed_append_preserves_previous_file_and_cached_views(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "mlb_memory_store.json")
            self._write_json(path, self._payload())

            with patch.object(mlb_app, "MLB_MEMORY_STORE", path):
                before = mlb_app._mlb_memory_latest_snapshot()
                with patch.object(
                    mlb_app.os,
                    "replace",
                    side_effect=OSError("replace failed"),
                ):
                    with self.assertRaises(OSError):
                        mlb_app._append_mlb_memory_snapshot(
                            self._payload("rejected")["latest"],
                            keep=30,
                        )
                after = mlb_app._mlb_memory_latest_snapshot()

            self.assertEqual(before["marker"], "first")
            self.assertEqual(after["marker"], "first")
            self.assertFalse(
                any(
                    name.endswith(".tmp")
                    for name in os.listdir(temp_dir)
                )
            )


if __name__ == "__main__":
    unittest.main()
