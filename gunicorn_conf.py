"""
Gunicorn config for mlb-analytics-hub on Fly.io.

Key goals:
  • Survive Fly.io 2GB memory limit cleanly
    - 1 worker  ->  caches live in a single process, no 2x RAM duplication
    - preload_app=False  -> avoids daemon thread fork-death on gunicorn master
  • Never get SIGKILL'd mid-cache-build
    - 120s request timeout — enough for heavy projections without freezing
      Fly's watchdog (which checks /health every 30s with a 10s timeout)
    - 30s graceful shutdown (faster recovery if worker does need to restart)
  • Better latency for I/O-bound MLB API calls under load
    - gthread worker class with 8 threads (was 4)
      ROOT CAUSE FIX: with only 4 threads, /api/props/projections (~2s) +
      3 other parallel requests consumed ALL threads simultaneously, leaving
      zero threads to respond to Fly.io's /health check → servicecheck
      failure → [PR04] load balancer crash loop.
      8 threads gives the health check guaranteed headroom even under burst.
  • No boot-storm
    - max_requests disabled (we DO NOT want to recycle and re-load caches)
  • auto_stop_machines=false in fly.toml keeps machine alive so caches
    are never lost to a suspend/resume cycle
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

# 8 threads (was 4). Key fix for the health check starvation bug:
# heavy endpoints like /api/props/projections hold a thread for ~2s;
# with 4 threads and a busy dashboard firing parallel requests, all
# threads could be consumed before Fly's health check gets one.
# 8 threads ensures /health always has a free thread to respond.
threads = 8

# Max simultaneous connections this worker will accept. Gives the OS-level
# accept queue enough room during request bursts.
worker_connections = 100

# 120s ceiling for any single request — safe for sims without making
# Fly's watchdog think the process is frozen.
timeout = 120
graceful_timeout = 30

# Do NOT preload the app. If we preload, gunicorn's master imports
# app.py once, kicks off the background cache-loader threads IN THE MASTER,
# then forks workers. On fork, daemon threads do NOT copy over, so the
# worker inherits _fg_loading=True / _sv_loading=True flags with no
# thread actually running — caches never populate and every request sees
# empty stats. With workers=1 preload is also meaningless.
preload_app = False

# Release idle keep-alive connections a bit faster than before (15s → 10s)
# so stale connections don't occupy thread slots unnecessarily.
keepalive = 10

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
