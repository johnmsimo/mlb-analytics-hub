"""Shared cache primitives for MLB Analytics Hub.

Provides normalized keys, TTL policy, stampede protection, metrics, resilient
backend visibility, safe invalidation, and stale-if-error data.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from typing import Any, TypeVar

from config import settings
from redis_client import get_redis, is_redis_connected, redis_health_status

T = TypeVar("T")

TTL_SECONDS = settings.cache_ttls

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
_metrics_lock = threading.Lock()
_metrics: dict[str, float] = defaultdict(float)
_namespace_keys: dict[str, set[str]] = defaultdict(set)


def normalize_cache_key(namespace: str, *parts: Any, **params: Any) -> str:
    """Return a stable, compact key regardless of dict ordering or value types."""
    payload = {"parts": parts, "params": params}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    safe_namespace = "".join(c if c.isalnum() or c in "_-" else "_" for c in namespace)
    key = f"mlb:{safe_namespace}:{digest}"
    with _metrics_lock:
        _namespace_keys[safe_namespace].add(key)
    return key


def ttl_for(policy: str, default: int | None = None) -> int:
    if policy in TTL_SECONDS:
        return TTL_SECONDS[policy]
    if default is not None:
        return default
    raise KeyError(f"Unknown cache TTL policy: {policy}")


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock


def _record(name: str, value: float = 1.0) -> None:
    with _metrics_lock:
        _metrics[name] += value


def _stale_key(key: str) -> str:
    return f"{key}:stale"


def get_or_compute(
    key: str,
    compute: Callable[[], T],
    *,
    ttl: int | None = None,
    policy: str = "analytics",
    allow_stale: bool | None = None,
) -> T:
    """Return cached data, compute it once, or serve stale data on compute error."""
    cache = get_redis()
    lookup_started = time.perf_counter()
    cached = cache.get(key)
    _record("lookup_seconds", time.perf_counter() - lookup_started)
    _record("lookups")
    if cached is not None:
        _record("hits")
        return cached

    _record("misses")
    lock = _lock_for(key)
    with lock:
        cached = cache.get(key)
        if cached is not None:
            _record("hits_after_wait")
            return cached

        effective_ttl = ttl if ttl is not None else ttl_for(policy)
        stale_enabled = settings.cache_allow_stale if allow_stale is None else allow_stale
        compute_started = time.perf_counter()
        try:
            value = compute()
        except Exception:
            _record("compute_errors")
            if stale_enabled:
                stale = cache.get(_stale_key(key))
                if stale is not None:
                    _record("stale_hits")
                    return stale
            raise

        _record("compute_seconds", time.perf_counter() - compute_started)
        _record("computes")
        cache.set(key, value, ttl=effective_ttl)
        if stale_enabled and settings.cache_stale_ttl > 0:
            cache.set(
                _stale_key(key),
                value,
                ttl=effective_ttl + settings.cache_stale_ttl,
            )
            _record("stale_writes")
        _record("writes")
        return value


def invalidate(namespace: str, *parts: Any, **params: Any) -> None:
    """Invalidate one normalized cache key and its stale shadow."""
    cache = get_redis()
    key = normalize_cache_key(namespace, *parts, **params)
    cache.delete(key)
    cache.delete(_stale_key(key))
    _record("invalidations")


def invalidate_namespace(namespace: str) -> int:
    """Delete current-process keys for one namespace, including stale shadows."""
    safe_namespace = "".join(c if c.isalnum() or c in "_-" else "_" for c in namespace)
    with _metrics_lock:
        keys = tuple(_namespace_keys.get(safe_namespace, ()))
        _namespace_keys.pop(safe_namespace, None)
    cache = get_redis()
    for key in keys:
        cache.delete(key)
        cache.delete(_stale_key(key))
    _record("namespace_invalidations")
    _record("invalidated_keys", len(keys))
    return len(keys)


def cache_status() -> dict[str, Any]:
    """Return a JSON-safe operational snapshot for health/status endpoints."""
    with _metrics_lock:
        metrics = dict(_metrics)
        namespaces = {name: len(keys) for name, keys in _namespace_keys.items()}
    lookups = int(metrics.get("lookups", 0))
    hits = int(metrics.get("hits", 0)) + int(metrics.get("hits_after_wait", 0))
    computes = int(metrics.get("computes", 0))
    redis = redis_health_status()
    return {
        "backend": redis["backend"],
        "connected": redis["connected"],
        "redis": redis,
        "stale_if_error": {
            "enabled": settings.cache_allow_stale,
            "ttl_seconds": settings.cache_stale_ttl,
        },
        "metrics": {
            **metrics,
            "lookups": lookups,
            "hits_total": hits,
            "hit_rate": round(hits / lookups, 4) if lookups else 0.0,
            "average_compute_ms": round(
                (metrics.get("compute_seconds", 0.0) / computes) * 1000, 2
            ) if computes else 0.0,
        },
        "registered_namespaces": namespaces,
        "ttl_seconds": dict(TTL_SECONDS),
    }


def reset_cache_metrics() -> None:
    """Reset process-local counters without deleting cached data."""
    with _metrics_lock:
        _metrics.clear()
