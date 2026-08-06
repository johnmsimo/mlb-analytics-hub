"""Run web serving and durable jobs as isolated OS processes on one Fly VM.

Keeping both processes on the volume-owning Machine preserves the existing
file-backed tracker while preventing CPU-heavy Python work from running inside
Gunicorn.  The worker runs at lower OS priority and either child failing causes
Fly to restart the complete pair cleanly.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time


children: list[subprocess.Popen] = []
stopping = False


def _terminate(_signum=None, _frame=None):
    global stopping
    stopping = True
    for child in children:
        if child.poll() is None:
            child.terminate()


def main() -> int:
    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGINT, _terminate)

    web_env = dict(os.environ, PROCESS_ROLE="web")
    worker_env = dict(os.environ, PROCESS_ROLE="worker")
    web = subprocess.Popen(
        ["gunicorn", "--config", "gunicorn_conf.py", "wsgi:app"],
        env=web_env,
    )
    worker = subprocess.Popen(
        [sys.executable, "worker.py"],
        env=worker_env,
        preexec_fn=lambda: os.nice(5),
    )
    children.extend([web, worker])

    while not stopping:
        for child in children:
            code = child.poll()
            if code is not None:
                _terminate()
                for sibling in children:
                    if sibling is not child:
                        try:
                            sibling.wait(timeout=20)
                        except subprocess.TimeoutExpired:
                            sibling.kill()
                return int(code or 1)
        time.sleep(0.5)

    for child in children:
        try:
            child.wait(timeout=20)
        except subprocess.TimeoutExpired:
            child.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
