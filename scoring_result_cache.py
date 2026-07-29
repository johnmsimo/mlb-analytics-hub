"""Bounded process-local memoization for CPU-heavy scoring results.

The cache intentionally stores only deterministic, already-computed values. It
does not know about models or feature vectors; callers provide an immutable key
that identifies the complete scoring input.
"""
from __future__ import annotations

import copy
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any


class ScoringResultCache:
    """Thread-safe TTL/LRU cache with per-key singleflight computation."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.RLock()
        self._entries: OrderedDict[Any, tuple[float, Any]] = OrderedDict()
        self._inflight: dict[Any, threading.Event] = {}
        self._hits = 0
        self._misses = 0
        self._waits = 0
        self._evictions = 0
        self._expirations = 0

    def get_or_compute(
        self,
        key: Any,
        compute: Callable[[], Any],
        *,
        ttl_seconds: int,
        max_entries: int,
    ) -> Any:
        """Return a cached copy or compute one value for concurrent callers."""
        ttl_seconds = max(0, int(ttl_seconds))
        max_entries = max(0, int(max_entries))
        if ttl_seconds == 0 or max_entries == 0:
            return compute()

        while True:
            now = self._clock()
            with self._lock:
                entry = self._entries.get(key)
                if entry is not None:
                    expires_at, value = entry
                    if expires_at > now:
                        self._entries.move_to_end(key)
                        self._hits += 1
                        return copy.deepcopy(value)
                    self._entries.pop(key, None)
                    self._expirations += 1

                event = self._inflight.get(key)
                if event is None:
                    event = threading.Event()
                    self._inflight[key] = event
                    self._misses += 1
                    break
                self._waits += 1

            event.wait()

        try:
            value = compute()
        except BaseException:
            with self._lock:
                self._inflight.pop(key, None)
                event.set()
            raise

        with self._lock:
            self._entries[key] = (
                self._clock() + ttl_seconds,
                copy.deepcopy(value),
            )
            self._entries.move_to_end(key)
            while len(self._entries) > max_entries:
                self._entries.popitem(last=False)
                self._evictions += 1
            self._inflight.pop(key, None)
            event.set()
        return copy.deepcopy(value)

    def clear(self, *, reset_metrics: bool = False) -> None:
        """Clear completed results without disturbing active computations."""
        with self._lock:
            self._entries.clear()
            if reset_metrics:
                self._hits = 0
                self._misses = 0
                self._waits = 0
                self._evictions = 0
                self._expirations = 0

    def status(self) -> dict[str, int]:
        with self._lock:
            return {
                "entries": len(self._entries),
                "inflight": len(self._inflight),
                "hits": self._hits,
                "misses": self._misses,
                "waits": self._waits,
                "evictions": self._evictions,
                "expirations": self._expirations,
            }
