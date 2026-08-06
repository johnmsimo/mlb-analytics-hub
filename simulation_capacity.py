"""Process-wide capacity guard for CPU-heavy linked game simulations."""
from __future__ import annotations

import functools
import os
import threading


_SIMULATION_SLOT = threading.BoundedSemaphore(
    max(1, int(os.getenv("SIMULATION_CONCURRENCY", "1") or 1))
)


def serialized_simulation(function):
    """Run one linked simulation at a time on the shared Fly CPU.

    The application has several consumers of the same expensive matchup model
    (Quick Props, the props scan, and Deep Dive). Letting each launch its own
    trial loop made every job slower and could starve normal request threads.
    """

    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        timeout = max(
            30,
            int(os.getenv("SIMULATION_SLOT_WAIT_SECONDS", "240") or 240),
        )
        if not _SIMULATION_SLOT.acquire(timeout=timeout):
            raise TimeoutError(
                "Simulation capacity is busy; retry after the active matchup build completes."
            )
        try:
            return function(*args, **kwargs)
        finally:
            _SIMULATION_SLOT.release()

    return wrapped
