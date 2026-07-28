"""Bounded request-latency observability for MLB Analytics Hub."""
from __future__ import annotations

import json
import logging
import math
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from flask import Blueprint, g, has_request_context, jsonify, request

from config import settings

log = logging.getLogger(__name__)


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class _RouteStats:
    requests: int = 0
    errors: int = 0
    slow_requests: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0


class RequestPerformanceMonitor:
    """Collect aggregate route latency without retaining request data."""

    def __init__(
        self,
        *,
        enabled: bool | None = None,
        slow_ms: int | None = None,
        sample_size: int | None = None,
        route_limit: int | None = None,
    ) -> None:
        self.enabled = settings.performance_monitor_enabled if enabled is None else enabled
        self.slow_ms = settings.performance_slow_ms if slow_ms is None else slow_ms
        self.sample_size = (
            settings.performance_sample_size if sample_size is None else sample_size
        )
        self.route_limit = (
            settings.performance_route_limit if route_limit is None else route_limit
        )
        self._lock = threading.Lock()
        self._started_at = _utc_timestamp()
        self._samples: deque[float] = deque(maxlen=self.sample_size)
        self._routes: dict[str, _RouteStats] = {}

    def begin_request(self) -> None:
        """Start timing the current Flask request."""
        if self.enabled:
            g.request_performance_started_at = time.perf_counter()

    def finish_request(self, response: Any) -> Any:
        """Record the current request and attach secret-safe timing headers."""
        if not self.enabled:
            return response

        started_at = getattr(g, "request_performance_started_at", None)
        if started_at is None:
            return response

        duration_ms = max(0.0, (time.perf_counter() - started_at) * 1000)
        route_rule = request.url_rule.rule if request.url_rule is not None else "<unmatched>"
        method = request.method.upper()
        status_code = int(response.status_code)
        self.record(method, route_rule, status_code, duration_ms)

        response.headers["X-Response-Time-Ms"] = f"{duration_ms:.2f}"
        server_timing = f"app;dur={duration_ms:.2f}"
        if response.headers.get("Server-Timing"):
            response.headers["Server-Timing"] = (
                f'{response.headers["Server-Timing"]}, {server_timing}'
            )
        else:
            response.headers["Server-Timing"] = server_timing
        return response

    def record(
        self,
        method: str,
        route_rule: str,
        status_code: int,
        duration_ms: float,
    ) -> None:
        """Record one normalized request measurement."""
        route_key = f"{method.upper()} {route_rule}"
        with self._lock:
            if route_key not in self._routes and len(self._routes) >= self.route_limit - 1:
                route_key = "<other>"
            stats = self._routes.setdefault(route_key, _RouteStats())
            stats.requests += 1
            stats.errors += int(status_code >= 500)
            stats.slow_requests += int(duration_ms >= self.slow_ms)
            stats.total_ms += duration_ms
            stats.max_ms = max(stats.max_ms, duration_ms)
            self._samples.append(duration_ms)

        if duration_ms >= self.slow_ms:
            log.warning(
                "[performance] %s",
                json.dumps(
                    {
                        "event": "slow_request",
                        "method": method.upper(),
                        "route": route_rule,
                        "status": status_code,
                        "duration_ms": round(duration_ms, 2),
                        "request_id": (
                            getattr(g, "request_id", None)
                            if has_request_context()
                            else None
                        ),
                        "threshold_ms": self.slow_ms,
                    },
                    sort_keys=True,
                ),
            )

    def snapshot(self) -> dict[str, Any]:
        """Return aggregate metrics for the bounded in-process sample window."""
        with self._lock:
            samples = list(self._samples)
            route_items = [
                {
                    "route": route,
                    "requests": stats.requests,
                    "errors": stats.errors,
                    "slow_requests": stats.slow_requests,
                    "average_ms": round(stats.total_ms / stats.requests, 2),
                    "max_ms": round(stats.max_ms, 2),
                }
                for route, stats in self._routes.items()
            ]
            started_at = self._started_at

        route_items.sort(
            key=lambda item: (item["average_ms"], item["requests"]),
            reverse=True,
        )
        total_requests = sum(item["requests"] for item in route_items)
        error_count = sum(item["errors"] for item in route_items)
        slow_requests = sum(item["slow_requests"] for item in route_items)
        sorted_samples = sorted(samples)

        return {
            "enabled": self.enabled,
            "started_at": started_at,
            "generated_at": _utc_timestamp(),
            "slow_threshold_ms": self.slow_ms,
            "sample_capacity": self.sample_size,
            "route_capacity": self.route_limit,
            "totals": {
                "requests": total_requests,
                "errors": error_count,
                "slow_requests": slow_requests,
                "sampled_requests": len(samples),
                "average_ms": round(sum(samples) / len(samples), 2) if samples else 0.0,
                "p95_ms": self._percentile(sorted_samples, 0.95),
                "max_ms": round(max(samples), 2) if samples else 0.0,
            },
            "routes": route_items,
        }

    def reset(self) -> None:
        """Clear all collected metrics without changing configuration."""
        with self._lock:
            self._samples.clear()
            self._routes.clear()
            self._started_at = _utc_timestamp()

    @staticmethod
    def _percentile(sorted_values: list[float], percentile: float) -> float:
        if not sorted_values:
            return 0.0
        index = max(0, math.ceil(percentile * len(sorted_values)) - 1)
        return round(sorted_values[index], 2)


request_performance = RequestPerformanceMonitor()
performance_bp = Blueprint("performance", __name__)


@performance_bp.get("/api/performance/status")
def performance_status():
    """Return a secret-safe in-process request performance snapshot."""
    return jsonify(request_performance.snapshot())


@performance_bp.post("/api/performance/metrics/reset")
def reset_performance_metrics():
    """Reset performance metrics when the application admin token is supplied."""
    expected = settings.admin_token
    provided = request.headers.get("X-Admin-Token", "")
    if not expected:
        return jsonify({"error": "performance metrics reset is disabled"}), 503
    if not provided or not secrets.compare_digest(provided, expected):
        return jsonify({"error": "unauthorized"}), 401
    request_performance.reset()
    return jsonify({"ok": True})
