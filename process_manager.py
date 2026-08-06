"""Run web serving and durable jobs as isolated OS processes on one Fly VM.

Keeping both processes on the volume-owning Machine preserves the existing
file-backed tracker while preventing CPU-heavy Python work from running inside
Gunicorn.  The worker runs at lower OS priority and is intentionally
non-critical: Redis or worker failures must never take the web server (and its
constant-time liveness endpoint) down with them.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time


children: list[subprocess.Popen] = []
stopping = False
log = logging.getLogger(__name__)


def _terminate(_signum=None, _frame=None):
    global stopping
    stopping = True
    for child in children:
        if child.poll() is None:
            child.terminate()


def _start_web() -> subprocess.Popen:
    return subprocess.Popen(
        ["gunicorn", "--config", "gunicorn_conf.py", "wsgi:app"],
        env=dict(os.environ, PROCESS_ROLE="web"),
    )


def _start_worker() -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "worker.py"],
        env=dict(os.environ, PROCESS_ROLE="worker"),
        preexec_fn=lambda: os.nice(5),
    )


def _stop_child(child: subprocess.Popen | None) -> None:
    if child is None or child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=20)
    except subprocess.TimeoutExpired:
        child.kill()


def main() -> int:
    global stopping
    stopping = False
    children.clear()
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")
    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGINT, _terminate)

    web = _start_web()
    worker = _start_worker()
    children.extend([web, worker])
    worker_backoff = 1.0
    worker_started_at = time.monotonic()
    next_worker_start = 0.0

    while not stopping:
        web_code = web.poll()
        if web_code is not None:
            log.error("Gunicorn exited with code %s; stopping the Machine process", web_code)
            _stop_child(worker)
            return int(web_code or 1)

        worker_code = worker.poll() if worker is not None else None
        if worker_code is not None:
            ran_for = max(0.0, time.monotonic() - worker_started_at)
            if ran_for >= 60:
                worker_backoff = 1.0
            log.error(
                "Durable worker exited with code %s; web remains live and worker restarts in %.1fs",
                worker_code,
                worker_backoff,
            )
            if worker in children:
                children.remove(worker)
            worker = None
            next_worker_start = time.monotonic() + worker_backoff
            worker_backoff = min(worker_backoff * 2, 60.0)

        if worker is None and time.monotonic() >= next_worker_start:
            worker = _start_worker()
            worker_started_at = time.monotonic()
            children.append(worker)
        time.sleep(0.5)

    _stop_child(web)
    _stop_child(worker)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
