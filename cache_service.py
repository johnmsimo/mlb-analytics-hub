"""Shared cache primitives for MLB Analytics Hub.

Provides normalized keys, TTL policy, and per-key stampede protection on top
of redis_client's Redis/in-memory backend.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Callable
from typing import Any, TypeVar

from redis_client import get_redis

T = TypeVar("T")

TTL_SECONDS = {
    "live": int(os.getenv("CACHE_TTL_LIVE", "30")),
    "schedule": int(os.getenv("CACHE_TTL_SCHEDULE", "300")),
    "stats": int(os.getenv("CACHE_TTL_STATS", "3600")),
    "analytics": int(os.getenv("CACHE_TTL_ANALYTICS", "900")),
    "static": int(os.getenv("CACHE_TTL_STATIC", "21600")),
}

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def normalize_cache_key(namespace: str, *parts: Any, **params: Any) -> str:
    """Return a stable, compact key regardless of dict ordering or value types."""
    payload = {
        "parts": parts,
        "params": params,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    safe_namespace = "".join(c if c.isalnum() or c in "_-" else "_" for c in namespace)
    return f"mlb:{safe_namespace}:{digest}"


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


def get_or_compute(
    key: str,
    compute: Callable[[], T],
    *,
    ttl: int | None = None,
    policy: str = "analytics",
) -> T:
    """Return cached data or compute it once per key under concurrent load."""
    cache = get_redis()
    cached = cache.get(key)
    if cached is not None:
        return cached

    lock = _lock_for(key)
    with lock:
        cached = cache.get(key)
        if cached is not None:
            return cached
        value = compute()
        cache.set(key, value, ttl=ttl if ttl is not None else ttl_for(policy))
        return value


def invalidate(namespace: str, *parts: Any, **params: Any) -> None:
    get_redis().delete(normalize_cache_key(namespace, *parts, **params))
