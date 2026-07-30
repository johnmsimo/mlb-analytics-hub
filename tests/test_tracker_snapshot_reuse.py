import copy
import unittest
from unittest.mock import patch

import app as mlb_app


class TrackerSnapshotReuseTests(unittest.TestCase):
    def setUp(self):
        self.store = {
            "2026-07-29": {
                "capturedAt": "2026-07-29T12:00:00",
                "entries": [
                    {
                        "player": "Prior Batter",
                        "marketKey": "batter_hits",
                        "grade": "loss",
                        "edge": 0.03,
                        "stakeDollars": 10.0,
                        "profitDollars": -10.0,
                        "profitUnits": -1.0,
                        "clvEdge": -0.01,
                    }
                ],
            },
            "2026-07-30": {
                "capturedAt": "2026-07-30T12:00:00",
                "entries": [
                    {
                        "player": "Current Batter",
                        "marketKey": "batter_hits",
                        "grade": "win",
                        "edge": 0.08,
                        "stakeDollars": 10.0,
                        "profitDollars": 9.1,
                        "profitUnits": 0.91,
                        "clvEdge": 0.02,
                    }
                ],
            },
        }
        self.adjustments = mlb_app._default_adjustments()

    def test_helpers_use_supplied_tracker_snapshot(self):
        with patch.object(
            mlb_app,
            "_tracker_store",
            side_effect=AssertionError("unexpected tracker clone"),
        ):
            entries = mlb_app._collect_window_entries(
                "2026-07-30",
                2,
                store=self.store,
            )
            hit_series = mlb_app._daily_series(
                "2026-07-30",
                2,
                "batter_hits",
                store=self.store,
            )
            value_series = mlb_app._daily_value_series(
                "2026-07-30",
                2,
                "batter_hits",
                store=self.store,
            )

        self.assertEqual(len(entries), 2)
        self.assertEqual([row["graded"] for row in hit_series], [1, 1])
        self.assertEqual([row["units"] for row in value_series], [-1.0, 0.91])

    def test_performance_payload_clones_tracker_window_and_history_once(self):
        history = [
            {
                "timestamp": "2026-07-30T13:00:00",
                "eventType": "manual_save",
                "adjustments": {
                    "market_multipliers": {"batter_hits": 1.05}
                },
            }
        ]
        with (
            patch.object(
                mlb_app,
                "_tracker_store_for_dates",
                return_value=copy.deepcopy(self.store),
            ) as tracker_window,
            patch.object(
                mlb_app,
                "_history_in_window",
                return_value=history,
            ) as history_loader,
            patch.object(
                mlb_app,
                "_get_adjustments",
                return_value=copy.deepcopy(self.adjustments),
            ),
        ):
            payload = mlb_app._tracker_performance_payload("2026-07-30", 2)

        self.assertTrue(payload["success"])
        self.assertEqual(tracker_window.call_count, 1)
        self.assertEqual(history_loader.call_count, 1)
        self.assertEqual(
            payload["multiplierHistory"]["batter_hits"][0]["multiplier"],
            1.05,
        )

    def test_batch_recalculation_loads_adjustments_once(self):
        rows = [
            {
                "openingPrice": -110,
                "marketPrice": -110,
                "adjProb": 0.60,
                "grade": "pending",
                "stakeDollars": None,
            }
            for _ in range(200)
        ]
        expected = copy.deepcopy(rows)
        for row in expected:
            mlb_app._recalc_tracker_entry(
                row,
                adjustments=self.adjustments,
            )

        with patch.object(
            mlb_app,
            "_get_adjustments",
            return_value=copy.deepcopy(self.adjustments),
        ) as get_adjustments:
            actual = mlb_app._recalc_tracker_entries(rows)

        self.assertEqual(get_adjustments.call_count, 1)
        self.assertEqual(actual, expected)

    def test_tracker_entries_route_uses_cached_day(self):
        with (
            mlb_app.app.test_request_context(
                "/api/tracker/entries?date=2026-07-30"
            ),
            patch.object(
                mlb_app,
                "_tracker_store_for_dates",
                return_value=copy.deepcopy(self.store),
            ) as tracker_window,
        ):
            response = mlb_app.api_tracker_entries()

        payload = response.get_json()
        self.assertEqual(tracker_window.call_count, 1)
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["entries"][0]["player"], "Current Batter")

    def test_calibration_dashboard_reuses_store_and_history(self):
        with (
            mlb_app.app.test_request_context(
                "/api/tracker/calibration/dashboard/2026-07-30?window=2"
            ),
            patch.object(
                mlb_app,
                "_tracker_store_for_dates",
                return_value=copy.deepcopy(self.store),
            ) as tracker_window,
            patch.object(
                mlb_app,
                "_history_in_window",
                return_value=[],
            ) as history_loader,
            patch.object(
                mlb_app,
                "_get_adjustments",
                return_value=copy.deepcopy(self.adjustments),
            ),
        ):
            response = mlb_app.api_tracker_calibration_dashboard(
                "2026-07-30"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(tracker_window.call_count, 1)
        self.assertEqual(history_loader.call_count, 1)

    def test_value_dashboard_reuses_one_tracker_snapshot(self):
        with (
            mlb_app.app.test_request_context(
                "/api/tracker/value/dashboard/2026-07-30?window=2"
            ),
            patch.object(
                mlb_app,
                "_tracker_store_for_dates",
                return_value=copy.deepcopy(self.store),
            ) as tracker_window,
            patch.object(
                mlb_app,
                "_get_adjustments",
                return_value=copy.deepcopy(self.adjustments),
            ),
        ):
            response = mlb_app.api_tracker_value_dashboard("2026-07-30")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(tracker_window.call_count, 1)


if __name__ == "__main__":
    unittest.main()
