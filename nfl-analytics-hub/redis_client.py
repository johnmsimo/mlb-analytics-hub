"""
redis_client.py — Thin Redis wrapper with transparent in-memory fallback.

When REDIS_URL is set the module connects to Redis and serialises all values
as JSON, so callers store and retrieve plain Python objects (dicts, lists,
strings, numbers) without caring about the transport.

When REDIS_URL is absent (local dev, Fly.io without a Redis add-on) the
module falls back to a thread-safe in-memory dict that honours TTLs.  The
public interface is identical in both modes, so callers never need to branch.

Usage
-----
    from redis_client import get_redis

    _redis = get_redis()

    cached = _redis.get("lineup:717465")
    if cached:
        return cached

    data = _build_lineup_data(...)
    _redis.set("lineup:717465", data, ttl=1800)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Optional

log = logging.getLogger(__name__)

_REDIS_URL: str = os.environ.get("REDIS_URL", "")


# ── In-memory fallback ────────────────────────────────────────────────────────

class _MemoryClient:
    """Thread-safe in-memory cache with per-key TTL support."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[Any, Optional[float]]] = {}
        self._lock = threading.Lock()

    # Purge expired entries lazily on every write (cheap enough at this scale).
    def _evict(self) -> None:
        now = time.monotonic()
        expired = [k for k, (_, exp) in self._store.items() if exp is not None and now > exp]
        for k in expired:
            del self._store[k]

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


# ── Redis client wrapper ──────────────────────────────────────────────────────

class _RedisClient:
    """Wraps redis.Redis; serialises/deserialises values as JSON automatically."""

    def __init__(self, url: str) -> None:
        import redis as _redis_lib  # noqa: PLC0415
        self._r = _redis_lib.from_url(
            url,
            decode_responses=True,
            socket_timeout=2,
            socket_connect_timeout=2,
        )

    def get(self, key: str) -> Any:
        try:
            raw = self._r.get(key)
            if raw is None:
                return None
            return json.loads(raw)
        except Exception as exc:
            log.warning("[redis] get(%s) failed: %s", key, exc)
            return None

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        try:
            raw = json.dumps(value, default=str)
            if ttl:
                self._r.setex(key, ttl, raw)
            else:
                self._r.set(key, raw)
        except Exception as exc:
            log.warning("[redis] set(%s) failed: %s", key, exc)

    def delete(self, key: str) -> None:
        try:
            self._r.delete(key)
        except Exception as exc:
            log.warning("[redis] delete(%s) failed: %s", key, exc)

    def ping(self) -> bool:
        return bool(self._r.ping())


# ── Singleton factory ─────────────────────────────────────────────────────────

_client: Optional[_RedisClient | _MemoryClient] = None
_init_lock = threading.Lock()


def get_redis() -> _RedisClient | _MemoryClient:
    """
    Return the singleton cache client.

    Tries Redis when REDIS_URL is set; silently falls back to the in-memory
    client if the connection fails or REDIS_URL is absent.  The returned object
    has identical .get() / .set() / .delete() semantics in both cases.
    """
    global _client
    if _client is not None:
        return _client

    with _init_lock:
        if _client is not None:
            return _client

        if _REDIS_URL:
            try:
                c: _RedisClient | _MemoryClient = _RedisClient(_REDIS_URL)
                c.ping()
                log.info("[redis] Connected → %s", _REDIS_URL[:40])
                _client = c
            except Exception as exc:
                log.warning(
                    "[redis] Could not connect to Redis (%s) — using in-memory fallback", exc
                )
                _client = _MemoryClient()
        else:
            log.info("[redis] REDIS_URL not set — using in-memory cache")
            _client = _MemoryClient()

    return _client


def is_redis_connected() -> bool:
    """True when the live Redis backend is being used (not the in-memory fallback)."""
    return isinstance(get_redis(), _RedisClient)
