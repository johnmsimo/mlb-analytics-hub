from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from cache_warmup import WarmupCoordinator
from performance_budget import budget_result, route_budget_ms, route_budget_snapshot
from pipeline_scheduler import _run_refresh_tasks


class Phase459PerformanceTests(unittest.TestCase):
    def test_route_budgets_are_explicit_and_classify_breaches(self):
        self.assertEqual(route_budget_ms("GET /health"), 250)
        self.assertEqual(route_budget_ms("GET /api/game-projection/824263"), 3000)
        self.assertIsNone(route_budget_ms("GET /api/unknown"))
        self.assertEqual(
            budget_result("GET /health", 251)["status"],
            "breached",
        )
        self.assertEqual(
            budget_result("GET /health", 250)["status"],
            "within_budget",
        )
        self.assertIn("GET /ready", route_budget_snapshot())

    def test_warmup_runs_independent_tasks_concurrently_and_reports_partial(self):
        coordinator = WarmupCoordinator()
        calls = []

        def ready_task():
            calls.append("ready")

        def failed_task():
            calls.append("failed")
            raise RuntimeError("upstream unavailable")

        self.assertTrue(
            coordinator.start(
                {"ready": ready_task, "failed": failed_task},
                timeout_seconds=2,
                max_workers=2,
            )
        )
        deadline = time.monotonic() + 2
        while coordinator.snapshot()["status"] == "running" and time.monotonic() < deadline:
            time.sleep(0.01)

        snapshot = coordinator.snapshot()
        self.assertEqual(snapshot["status"], "partial")
        self.assertEqual(snapshot["tasks"]["ready"]["status"], "ready")
        self.assertEqual(snapshot["tasks"]["failed"]["status"], "failed")
        self.assertEqual(sorted(calls), ["failed", "ready"])

    def test_refresh_tasks_are_parallelized(self):
        def task(label):
            time.sleep(0.04)
            return label

        started = time.perf_counter()
        results = _run_refresh_tasks(
            [("a", lambda: task("a")), ("b", lambda: task("b"))],
            max_workers=2,
        )
        elapsed = time.perf_counter() - started

        self.assertEqual(results, {"a": "a", "b": "b"})
        self.assertLess(elapsed, 0.075)

    def test_warmup_is_idempotent_while_running(self):
        coordinator = WarmupCoordinator()

        with patch("cache_warmup.time.sleep"):
            self.assertTrue(
                coordinator.start(
                    {"slow": lambda: time.sleep(0.05)},
                    timeout_seconds=1,
                    max_workers=1,
                )
            )
            self.assertFalse(
                coordinator.start({"other": lambda: None}, timeout_seconds=1)
            )
