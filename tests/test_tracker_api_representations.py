import json
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import app as mlb_app


class TrackerApiRepresentationTests(unittest.TestCase):
    DATE = "2026-07-30"

    def setUp(self):
        self._clear_caches()

    def tearDown(self):
        self._clear_caches()

    @staticmethod
    def _clear_caches():
        with mlb_app._TRACKER_READ_LOCK:
            mlb_app._TRACKER_READ_CACHE.update({
                "sig": None,
                "pickled": None,
                "day_pickles": {},
                "day_json": {},
                "date_keys": (),
            })
        with mlb_app._TRACKER_RESPONSE_LOCK:
            mlb_app._TRACKER_RESPONSE_CACHE.update({
                "version": None,
                "representations": {},
            })
        with mlb_app._JSON_SNAPSHOT_LOCK:
            mlb_app._JSON_SNAPSHOT_CACHE.clear()

    @staticmethod
    def _entry(marker="first", game_pk=1, padding=""):
        return {
            "id": f"pick-{game_pk}",
            "player": f"Player {game_pk}",
            "gamePk": game_pk,
            "marketKey": "batter_hits",
            "recommendedSide": "Over",
            "line": 0.5,
            "rawProb": 0.61,
            "adjProb": 0.63,
            "edge": 0.07,
            "hubRating": 82,
            "stakeDollars": 12.5,
            "grade": "pending",
            "marker": marker,
            "padding": padding,
        }

    @classmethod
    def _store(cls, marker="first", padding=""):
        return {
            cls.DATE: {
                "capturedAt": f"{cls.DATE}T12:00:00",
                "gradedAt": None,
                "closingCapturedAt": None,
                "entries": [
                    cls._entry(marker, 1, padding),
                    cls._entry(marker, 2),
                ],
            }
        }

    @staticmethod
    def _write_json(path, payload):
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))

    def _paths(self, temp_dir):
        tracker = os.path.join(temp_dir, "daily_tracker.json")
        adjustments = os.path.join(temp_dir, "model_adjustments.json")
        return tracker, adjustments

    def test_today_route_reuses_json_and_gzip_representation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tracker, adjustments = self._paths(temp_dir)
            self._write_json(tracker, self._store(padding="x" * 5000))
            self._write_json(adjustments, {"bankroll": 1000})
            original_dumps = mlb_app.app.json.dumps
            dumps_calls = 0

            def counting_dumps(*args, **kwargs):
                nonlocal dumps_calls
                dumps_calls += 1
                return original_dumps(*args, **kwargs)

            with (
                patch.object(mlb_app, "TRACKER_STORE", tracker),
                patch.object(mlb_app, "ADJUST_STORE", adjustments),
                patch.object(
                    mlb_app.app.json,
                    "dumps",
                    side_effect=counting_dumps,
                ),
                mlb_app.app.test_client() as client,
            ):
                first = client.get(
                    f"/api/tracker/today?date={self.DATE}",
                    headers={"Accept-Encoding": "gzip"},
                )
                second = client.get(
                    f"/api/tracker/today?date={self.DATE}",
                    headers={"Accept-Encoding": "gzip"},
                )

            self.assertEqual(dumps_calls, 1)
            self.assertEqual(first.status_code, 200)
            self.assertEqual(second.status_code, 200)
            self.assertEqual(first.headers.get("Content-Encoding"), "gzip")
            self.assertEqual(first.data, second.data)
            self.assertEqual(first.headers.get("ETag"), second.headers.get("ETag"))

    def test_today_route_etag_revalidates_without_body(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tracker, adjustments = self._paths(temp_dir)
            self._write_json(tracker, self._store())
            self._write_json(adjustments, {"bankroll": 1000})

            with (
                patch.object(mlb_app, "TRACKER_STORE", tracker),
                patch.object(mlb_app, "ADJUST_STORE", adjustments),
                mlb_app.app.test_client() as client,
            ):
                first = client.get(f"/api/tracker/today?date={self.DATE}")
                second = client.get(
                    f"/api/tracker/today?date={self.DATE}",
                    headers={"If-None-Match": first.headers["ETag"]},
                )

            self.assertEqual(first.status_code, 200)
            self.assertEqual(second.status_code, 304)
            self.assertEqual(second.data, b"")

    def test_tracker_atomic_replacement_invalidates_same_size_version(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tracker, adjustments = self._paths(temp_dir)
            self._write_json(tracker, self._store("first"))
            self._write_json(adjustments, {"bankroll": 1000})
            original_stat = os.stat(tracker)

            with (
                patch.object(mlb_app, "TRACKER_STORE", tracker),
                patch.object(mlb_app, "ADJUST_STORE", adjustments),
                mlb_app.app.test_client() as client,
            ):
                first = client.get(f"/api/tracker/today?date={self.DATE}")
                replacement = f"{tracker}.new"
                self._write_json(replacement, self._store("other"))
                self.assertEqual(os.path.getsize(replacement), original_stat.st_size)
                os.utime(
                    replacement,
                    ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
                )
                os.replace(replacement, tracker)
                second = client.get(f"/api/tracker/today?date={self.DATE}")

            self.assertEqual(first.get_json()["entries"][0]["marker"], "first")
            self.assertEqual(second.get_json()["entries"][0]["marker"], "other")
            self.assertNotEqual(first.headers["ETag"], second.headers["ETag"])

    def test_adjustment_atomic_replacement_invalidates_response(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tracker, adjustments = self._paths(temp_dir)
            self._write_json(tracker, self._store())
            self._write_json(adjustments, {"bankroll": 1000})
            original_stat = os.stat(adjustments)

            with (
                patch.object(mlb_app, "TRACKER_STORE", tracker),
                patch.object(mlb_app, "ADJUST_STORE", adjustments),
                mlb_app.app.test_client() as client,
            ):
                first = client.get(f"/api/tracker/today?date={self.DATE}")
                replacement = f"{adjustments}.new"
                self._write_json(replacement, {"bankroll": 2000})
                self.assertEqual(os.path.getsize(replacement), original_stat.st_size)
                os.utime(
                    replacement,
                    ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
                )
                os.replace(replacement, adjustments)
                second = client.get(f"/api/tracker/today?date={self.DATE}")

            self.assertEqual(first.get_json()["settings"]["bankroll"], 1000)
            self.assertEqual(second.get_json()["settings"]["bankroll"], 2000)
            self.assertNotEqual(first.headers["ETag"], second.headers["ETag"])

    def test_successful_day_commit_invalidates_immediately(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tracker, adjustments = self._paths(temp_dir)
            self._write_json(tracker, self._store("before"))
            self._write_json(adjustments, {"bankroll": 1000})

            with (
                patch.object(mlb_app, "TRACKER_STORE", tracker),
                patch.object(mlb_app, "ADJUST_STORE", adjustments),
                mlb_app.app.test_client() as client,
            ):
                first = client.get(f"/api/tracker/today?date={self.DATE}")
                self.assertTrue(
                    mlb_app._tracker_commit_day(
                        self.DATE,
                        self._store("after")[self.DATE],
                    )
                )
                second = client.get(f"/api/tracker/today?date={self.DATE}")

            self.assertEqual(first.get_json()["entries"][0]["marker"], "before")
            self.assertEqual(second.get_json()["entries"][0]["marker"], "after")
            self.assertNotEqual(first.headers["ETag"], second.headers["ETag"])

    def test_entries_game_filters_keep_independent_representations(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tracker, adjustments = self._paths(temp_dir)
            self._write_json(tracker, self._store())
            self._write_json(adjustments, {"bankroll": 1000})

            with (
                patch.object(mlb_app, "TRACKER_STORE", tracker),
                patch.object(mlb_app, "ADJUST_STORE", adjustments),
                mlb_app.app.test_client() as client,
            ):
                first = client.get(
                    f"/api/tracker/entries?date={self.DATE}&gamePk=1"
                )
                second = client.get(
                    f"/api/tracker/entries?date={self.DATE}&gamePk=2"
                )

            self.assertEqual(first.get_json()["total"], 1)
            self.assertEqual(second.get_json()["total"], 1)
            self.assertEqual(first.get_json()["entries"][0]["gamePk"], 1)
            self.assertEqual(second.get_json()["entries"][0]["gamePk"], 2)
            self.assertNotEqual(first.headers["ETag"], second.headers["ETag"])

    def test_cached_today_keeps_decoded_payload_contract(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tracker, adjustments = self._paths(temp_dir)
            self._write_json(tracker, self._store())
            self._write_json(adjustments, {"bankroll": 1000})

            with (
                patch.object(mlb_app, "TRACKER_STORE", tracker),
                patch.object(mlb_app, "ADJUST_STORE", adjustments),
            ):
                expected = mlb_app._tracker_today_payload(self.DATE)
                with mlb_app.app.test_client() as client:
                    response = client.get(
                        f"/api/tracker/today?date={self.DATE}"
                    )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json(), expected)

    def test_legacy_date_route_keeps_response_shape(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tracker, adjustments = self._paths(temp_dir)
            self._write_json(tracker, {self.DATE: {"entries": []}})
            self._write_json(adjustments, {"bankroll": 1000})

            with (
                patch.object(mlb_app, "TRACKER_STORE", tracker),
                patch.object(mlb_app, "ADJUST_STORE", adjustments),
                mlb_app.app.test_client() as client,
            ):
                response = client.get(f"/api/tracker/date/{self.DATE}")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                set(response.get_json()),
                {
                    "success",
                    "date",
                    "adjustments",
                    "capturedAt",
                    "gradedAt",
                    "closingCapturedAt",
                    "entries",
                    "summary",
                },
            )

    def test_concurrent_cold_builds_encode_once(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tracker, adjustments = self._paths(temp_dir)
            self._write_json(tracker, self._store())
            self._write_json(adjustments, {"bankroll": 1000})
            calls = 0
            calls_lock = threading.Lock()

            def payload_factory():
                nonlocal calls
                with calls_lock:
                    calls += 1
                time.sleep(0.02)
                return {"success": True, "marker": "shared"}

            with (
                patch.object(mlb_app, "TRACKER_STORE", tracker),
                patch.object(mlb_app, "ADJUST_STORE", adjustments),
                ThreadPoolExecutor(max_workers=12) as pool,
            ):
                values = list(
                    pool.map(
                        lambda _: mlb_app._tracker_api_representation(
                            ("unit", self.DATE),
                            payload_factory,
                        ),
                        range(24),
                    )
                )

            self.assertEqual(calls, 1)
            self.assertEqual(
                {json.loads(value["body"])["marker"] for value in values},
                {"shared"},
            )


if __name__ == "__main__":
    unittest.main()
