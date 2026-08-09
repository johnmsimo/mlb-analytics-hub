"""Bounded, observable cache warmup coordination."""
from __future__ import annotations

import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from flask import Blueprint, jsonify


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class WarmupCoordinator:
    """Run independent warmup tasks concurrently with a hard wall-clock cap."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state: dict[str, Any] = {
            "status": "idle",
            "startedAt": None,
            "finishedAt": None,
            "timeoutSeconds": None,
            "tasks": {},
        }

    def start(
        self,
        tasks: Mapping[str, Callable[[], Any]],
        *,
        timeout_seconds: float = 30.0,
        max_workers: int | None = None,
    ) -> bool:
        with self._lock:
            if self._state["status"] == "running":
                return False
            self._state = {
                "status": "running",
                "startedAt": _utc_timestamp(),
                "finishedAt": None,
                "timeoutSeconds": float(timeout_seconds),
                "tasks": {
                    name: {"status": "queued", "durationMs": None, "error": None}
                    for name in tasks
                },
            }

        thread = threading.Thread(
            target=self._run,
            args=(dict(tasks), float(timeout_seconds), max_workers),
            name="cache-warmup",
            daemon=True,
        )
        thread.start()
        return True

    def _run(
        self,
        tasks: dict[str, Callable[[], Any]],
        timeout_seconds: float,
        max_workers: int | None,
    ) -> None:
        started = time.monotonic()
        worker_count = max_workers or max(1, min(4, len(tasks) or 1))
        pool = ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="cache-warmup-task",
        )
        futures = {
            pool.submit(task): name for name, task in tasks.items()
        }
        pending = set(futures)
        deadline = started + timeout_seconds
        try:
            while pending:
                remaining = max(0.0, deadline - time.monotonic())
                done, pending = wait(
                    pending,
                    timeout=remaining,
                    return_when=FIRST_COMPLETED,
                )
                if not done:
                    elapsed_ms = round((time.monotonic() - started) * 1000, 2)
                    with self._lock:
                        for future in pending:
                            name = futures[future]
                            self._state["tasks"][name] = {
                                "status": "timed_out",
                                "durationMs": elapsed_ms,
                                "error": "warmup wall-clock budget exceeded",
                            }
                            future.cancel()
                    break

                for future in done:
                    name = futures[future]
                    elapsed_ms = round((time.monotonic() - started) * 1000, 2)
                    try:
                        future.result()
                    except Exception as exc:
                        with self._lock:
                            self._state["tasks"][name] = {
                                "status": "failed",
                                "durationMs": elapsed_ms,
                                "error": type(exc).__name__,
                            }
                    else:
                        with self._lock:
                            self._state["tasks"][name] = {
                                "status": "ready",
                                "durationMs": elapsed_ms,
                                "error": None,
                            }
        finally:
            # Do not wait for a stuck upstream call after publishing readiness.
            pool.shutdown(wait=False, cancel_futures=True)

        with self._lock:
            statuses = [item["status"] for item in self._state["tasks"].values()]
            if statuses and all(status == "ready" for status in statuses):
                status = "ready"
            elif any(status == "ready" for status in statuses):
                status = "partial"
            else:
                status = "failed"
            self._state["status"] = status
            self._state["finishedAt"] = _utc_timestamp()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                **self._state,
                "tasks": {
                    name: dict(value) for name, value in self._state["tasks"].items()
                },
            }


warmup = WarmupCoordinator()
cache_warmup_bp = Blueprint("cache_warmup", __name__)


@cache_warmup_bp.get("/api/cache/warmup/status")
def warmup_status():
    """Expose cold-start warmup state without requiring admin credentials."""
    return jsonify(warmup.snapshot())
