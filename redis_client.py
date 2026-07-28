"""Resilient Redis client with transparent in-memory failover.

The public client always mirrors writes to process memory. When Redis becomes
unavailable, reads continue from that mirror while a circuit breaker prevents
repeated slow connection attempts. A lightweight health monitor probes Redis
and automatically closes the circuit after recovery.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from config import settings

log = logging.getLogger(__name__)


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log_event(event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    payload = {"event": event, **fields}
    log.log(level, "[redis] %s", json.dumps(payload, sort_keys=True, default=str))


class _MemoryClient:
    """Thread-safe in-memory cache with per-key TTL support."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[Any, Optional[float]]] = {}
        self._lock = threading.Lock()

    def _evict(self) -> None:
        now = time.monotonic()
        expired = [k for k, (_, exp) in self._store.items() if exp is not None and now > exp]
        for key in expired:
            del self._store[key]

    def get(self, key: str) -> Any:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            value, expires_at = entry
            if expires_at is not None and time.monotonic() > expires_at:
                del self._store[key]
                return None
            return value

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        expires_at = (time.monotonic() + ttl) if ttl else None
        with self._lock:
            self._evict()
            self._store[key] = (value, expires_at)

    def delete(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def ping(self) -> bool:
        return True


class _RedisClient:
    """Raw Redis transport that raises failures to the resilience layer."""

    def __init__(self, url: str) -> None:
        import redis as redis_lib  # noqa: PLC0415

        self._r = redis_lib.from_url(
            url,
            decode_responses=True,
            socket_timeout=2,
            socket_connect_timeout=2,
        )

    def get(self, key: str) -> Any:
        raw = self._r.get(key)
        return None if raw is None else json.loads(raw)

    def ttl(self, key: str) -> Optional[int]:
        remaining = int(self._r.ttl(key))
        return remaining if remaining > 0 else None

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        raw = json.dumps(value, default=str)
        if ttl:
            self._r.setex(key, ttl, raw)
        else:
            self._r.set(key, raw)

    def delete(self, key: str) -> None:
        self._r.delete(key)

    def ping(self) -> bool:
        return bool(self._r.ping())


class _ResilientClient:
    """Redis-first cache with health monitoring, circuit breaking, and failover."""

    def __init__(
        self,
        url: str,
        *,
        health_interval: int,
        failure_threshold: int,
        circuit_timeout: int,
        redis_factory: Callable[[str], Any] = _RedisClient,
        start_monitor: bool = True,
    ) -> None:
        self._url = url
        self._health_interval = health_interval
        self._failure_threshold = failure_threshold
        self._circuit_timeout = circuit_timeout
        self._redis_factory = redis_factory
        self._memory = _MemoryClient()
        self._redis: Any = None
        self._lock = threading.RLock()
        self._state = "closed" if url else "disabled"
        self._opened_at: Optional[float] = None
        self._consecutive_failures = 0
        self._total_failures = 0
        self._successful_checks = 0
        self._latency_total_ms = 0.0
        self._last_success_at: Optional[str] = None
        self._last_failure_at: Optional[str] = None
        self._monitor_started = False
        if url and start_monitor:
            self._start_monitor()

    def _start_monitor(self) -> None:
        with self._lock:
            if self._monitor_started:
                return
            self._monitor_started = True
        thread = threading.Thread(
            target=self._monitor_loop,
            name="redis-health-monitor",
            daemon=True,
        )
        thread.start()

    def _monitor_loop(self) -> None:
        self.check_health(force=True)
        while True:
            time.sleep(self._health_interval)
            self.check_health()

    def _record_success(self, latency_ms: float) -> None:
        with self._lock:
            previous = self._state
            self._state = "closed"
            self._opened_at = None
            self._consecutive_failures = 0
            self._successful_checks += 1
            self._latency_total_ms += latency_ms
            self._last_success_at = _utc_timestamp()
        if previous in {"open", "half_open"}:
            _log_event("circuit_closed", latency_ms=round(latency_ms, 2))

    def _record_failure(self, operation: str, exc: Exception) -> None:
        opened = False
        with self._lock:
            self._consecutive_failures += 1
            self._total_failures += 1
            self._last_failure_at = _utc_timestamp()
            if self._consecutive_failures >= self._failure_threshold:
                if self._state != "open":
                    opened = True
                self._state = "open"
                self._opened_at = time.monotonic()
        _log_event(
            "operation_failed",
            level=logging.WARNING,
            operation=operation,
            error_type=type(exc).__name__,
            consecutive_failures=self._consecutive_failures,
        )
        if opened:
            _log_event(
                "circuit_opened",
                level=logging.WARNING,
                timeout_seconds=self._circuit_timeout,
            )

    def _redis_allowed(self, *, force: bool = False) -> bool:
        if not self._url:
            return False
        with self._lock:
            if force:
                if self._state == "open":
                    self._state = "half_open"
                return True
            if self._state != "open":
                return True
            if self._opened_at is None:
                return False
            if time.monotonic() - self._opened_at < self._circuit_timeout:
                return False
            self._state = "half_open"
            return True

    def _transport(self) -> Any:
        with self._lock:
            if self._redis is None:
                self._redis = self._redis_factory(self._url)
            return self._redis

    def check_health(self, *, force: bool = False) -> bool:
        if not self._redis_allowed(force=force):
            return False
        started = time.perf_counter()
        try:
            healthy = bool(self._transport().ping())
            if not healthy:
                raise ConnectionError("Redis ping returned false")
        except Exception as exc:
            with self._lock:
                self._redis = None
            self._record_failure("ping", exc)
            return False
        self._record_success((time.perf_counter() - started) * 1000)
        return True

    def get(self, key: str) -> Any:
        if self._redis_allowed():
            started = time.perf_counter()
            try:
                transport = self._transport()
                value = transport.get(key)
                self._record_success((time.perf_counter() - started) * 1000)
                if value is not None:
                    ttl = transport.ttl(key) if hasattr(transport, "ttl") else None
                    self._memory.set(key, value, ttl=ttl)
                return value
            except Exception as exc:
                with self._lock:
                    self._redis = None
                self._record_failure("get", exc)
        return self._memory.get(key)

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        self._memory.set(key, value, ttl=ttl)
        if not self._redis_allowed():
            return
        started = time.perf_counter()
        try:
            self._transport().set(key, value, ttl=ttl)
            self._record_success((time.perf_counter() - started) * 1000)
        except Exception as exc:
            with self._lock:
                self._redis = None
            self._record_failure("set", exc)

    def delete(self, key: str) -> None:
        self._memory.delete(key)
        if not self._redis_allowed():
            return
        started = time.perf_counter()
        try:
            self._transport().delete(key)
            self._record_success((time.perf_counter() - started) * 1000)
        except Exception as exc:
            with self._lock:
                self._redis = None
            self._record_failure("delete", exc)

    def ping(self) -> bool:
        return self.check_health()

    def status(self) -> dict[str, Any]:
        with self._lock:
            average_latency = (
                self._latency_total_ms / self._successful_checks
                if self._successful_checks
                else 0.0
            )
            connected = bool(self._url) and self._state == "closed" and self._last_success_at is not None
            return {
                "configured": bool(self._url),
                "connected": connected,
                "backend": "redis" if connected else "memory",
                "fallback_active": bool(self._url) and not connected,
                "circuit_state": self._state,
                "consecutive_failures": self._consecutive_failures,
                "total_failures": self._total_failures,
                "last_success_at": self._last_success_at,
                "last_failure_at": self._last_failure_at,
                "average_latency_ms": round(average_latency, 2),
                "health_interval_seconds": self._health_interval,
                "failure_threshold": self._failure_threshold,
                "circuit_timeout_seconds": self._circuit_timeout,
                "monitoring": self._monitor_started,
            }


_client: Optional[_ResilientClient] = None
_init_lock = threading.Lock()


def get_redis() -> _ResilientClient:
    """Return the process-wide resilient cache client."""
    global _client
    if _client is not None:
        return _client
    with _init_lock:
        if _client is None:
            _client = _ResilientClient(
                settings.redis_url,
                health_interval=settings.redis_health_interval,
                failure_threshold=settings.redis_failure_threshold,
                circuit_timeout=settings.redis_circuit_timeout,
            )
            if settings.redis_url:
                _log_event("client_initialized", monitoring=True)
            else:
                _log_event("memory_fallback_enabled", reason="redis_url_unset")
    return _client


def redis_health_status() -> dict[str, Any]:
    """Return a secret-safe Redis resilience snapshot."""
    return get_redis().status()


def is_redis_connected() -> bool:
    """True only while the live Redis backend is healthy."""
    return bool(redis_health_status()["connected"])
