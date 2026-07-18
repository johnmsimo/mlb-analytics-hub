"""
Gunicorn config for nfl-analytics-hub (Fly.io).

Same proven shape as the MLB hub, lighter caches:
  - 1 worker: in-memory caches live in a single process.
  - gthread x 8: I/O-bound ESPN/Odds API calls; /health always has a thread.
  - preload_app=False: preload would start cache-loader daemon threads in the
    gunicorn master, which do not survive fork — workers would inherit
    "loading" flags with no loader running. Do not change.
  - post_fork: kick cache preload AFTER the worker binds, so /health returns
    200 from second 0 while caches build in the background.
"""
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8080')}"
workers = 1
worker_class = "gthread"
threads = 8
timeout = 120
graceful_timeout = 30
keepalive = 15
max_requests = 0
preload_app = False


def post_fork(server, worker):
    from app import _preload_caches
    _preload_caches()
