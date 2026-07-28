import json
import logging
import unittest
from unittest.mock import patch

from flask import Flask, g

import request_performance
from request_performance import RequestPerformanceMonitor, performance_bp


class RequestPerformanceMonitorTests(unittest.TestCase):
    def setUp(self):
        self.monitor = RequestPerformanceMonitor(
            enabled=True,
            slow_ms=100,
            sample_size=100,
            route_limit=25,
        )
        self.app = Flask(__name__)

        @self.app.before_request
        def start_request():
            g.request_id = "test-13"
            self.monitor.begin_request()

        @self.app.after_request
        def finish_request(response):
            return self.monitor.finish_request(response)

        @self.app.get("/players/<int:player_id>")
        def player(player_id):
            return {"player_id": player_id}

    def test_response_headers_and_normalized_route_metrics(self):
        with patch("request_performance.time.perf_counter", side_effect=[1.0, 1.025]):
            response = self.app.test_client().get("/players/13")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Response-Time-Ms"], "25.00")
        self.assertEqual(response.headers["Server-Timing"], "app;dur=25.00")
        route = self.monitor.snapshot()["routes"][0]
        self.assertEqual(route["route"], "GET /players/<int:player_id>")
        self.assertEqual(route["requests"], 1)
        self.assertEqual(route["errors"], 0)

    def test_snapshot_reports_totals_p95_and_slowest_routes(self):
        self.monitor.record("GET", "/fast", 200, 10.0)
        self.monitor.record("GET", "/slow", 200, 150.0)
        self.monitor.record("GET", "/slow", 500, 250.0)

        snapshot = self.monitor.snapshot()
        self.assertEqual(snapshot["totals"]["requests"], 3)
        self.assertEqual(snapshot["totals"]["errors"], 1)
        self.assertEqual(snapshot["totals"]["slow_requests"], 2)
        self.assertEqual(snapshot["totals"]["p95_ms"], 250.0)
        self.assertEqual(snapshot["routes"][0]["route"], "GET /slow")

    def test_slow_log_omits_query_string(self):
        with self.app.test_request_context("/players/13?token=secret"):
            g.request_id = "test-13"
            with self.assertLogs("request_performance", level=logging.WARNING) as captured:
                self.monitor.record("GET", "/players/<int:player_id>", 200, 125.0)

        payload = json.loads(captured.output[0].split("[performance] ", 1)[1])
        self.assertEqual(payload["route"], "/players/<int:player_id>")
        self.assertNotIn("secret", captured.output[0])

    def test_route_cardinality_is_bounded(self):
        monitor = RequestPerformanceMonitor(
            enabled=True,
            slow_ms=100,
            sample_size=100,
            route_limit=25,
        )
        for index in range(30):
            monitor.record("GET", f"/route-{index}", 200, float(index))

        snapshot = monitor.snapshot()
        self.assertEqual(len(snapshot["routes"]), 25)
        other = next(item for item in snapshot["routes"] if item["route"] == "<other>")
        self.assertEqual(other["requests"], 6)

    def test_reset_clears_metrics(self):
        self.monitor.record("GET", "/players/<int:player_id>", 200, 25.0)
        self.monitor.reset()
        self.assertEqual(self.monitor.snapshot()["totals"]["requests"], 0)


class RequestPerformanceRoutesTests(unittest.TestCase):
    def setUp(self):
        request_performance.request_performance.reset()
        self.app = Flask(__name__)
        self.app.register_blueprint(performance_bp)
        self.client = self.app.test_client()

    def test_status_is_read_only_and_secret_safe(self):
        request_performance.request_performance.record("GET", "/players", 200, 25.0)
        response = self.client.get("/api/performance/status")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["totals"]["requests"], 1)

    def test_reset_requires_configured_admin_token(self):
        request_performance.request_performance.record("GET", "/players", 200, 25.0)
        with patch.dict("os.environ", {"ADMIN_TOKEN": "secret-13"}, clear=True):
            self.assertEqual(
                self.client.post("/api/performance/metrics/reset").status_code,
                401,
            )
            response = self.client.post(
                "/api/performance/metrics/reset",
                headers={"X-Admin-Token": "secret-13"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            request_performance.request_performance.snapshot()["totals"]["requests"],
            0,
        )


if __name__ == "__main__":
    unittest.main()
