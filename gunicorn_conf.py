"""
Gunicorn config for mlb-analytics-hub on Fly.io.

Key goals:
  • Survive Fly.io 2GB memory limit cleanly
    - 1 worker  ->  caches live in a single process, no 2x RAM duplication
    - preload_app=False  -> avoids daemon thread fork-death on gunicorn master
  • Never get SIGKILL'd mid-cache-build
    - 600s request timeout (the MLB-API-derived build can take 2-4 minutes)
    - 300s graceful shutdown
  • Better latency for I/O-bound MLB API calls under load
    - gthread worker class with 4 threads -> serves 4 concurrent requests
      while the sync loaders run in background daemon threads
  • No boot-storm
    - max_requests disabled (we DO NOT want to recycle and re-load caches)
"""
import os

# Bind — Fly.io sets $PORT; fall back to 8080.
bind = f"0.0.0.0:{os.environ.get('PORT', '8080')}"

# One worker only. pybaseball + Savant caches are large (~150MB) and
# 2 workers on a 2GB instance == unnecessary cache duplication.
workers = 1

# Threaded worker so the caches (loaded by background daemon threads)
# don't block request handling.
worker_class = "gthread"
threads = 4

# The MLB-API-derived cache build hits 900+ players and can take 120-240s
# on a cold boot; anything less and gunicorn kills the worker.
timeout = 600
graceful_timeout = 300

# Do NOT preload the app. If we preload, gunicorn's master imports
# app.py once, kicks off the background cache-loader threads IN THE MASTER,
# then forks workers. On fork, daemon threads do NOT copy over, so the
# worker inherits _fg_loading=True / _sv_loading=True flags with no
# thread actually running — caches never populate and every request sees
# empty stats. With workers=1 preload is also meaningless.
preload_app = False

# Keep connections open a bit longer than the default (2s) so the CDN
# can reuse them for dashboards that fire many parallel /api/* calls.
keepalive = 15

# Do NOT recycle workers based on request count — recycling triggers a
# full pybaseball/Savant re-load which blows the memory/time budget.
max_requests = 0

# Logging — send access/error to stdout/stderr so Fly.io captures them.
accesslog = "-"
errorlog = "-"
loglevel = "info"
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s %(L)ss'

# Keep the master process's control socket quiet on shutdown.
capture_output = True
