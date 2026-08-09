"""
Gunicorn config for mlb-analytics-hub on Fly.io.

Key goals:
  • Survive Fly.io 2GB memory limit cleanly
    - 1 worker  ->  caches live in a single process, no 2x RAM duplication
    - preload_app=False  -> avoids daemon thread fork-death on gunicorn master
  • Never get SIGKILL'd mid-cache-build
    - 120s request timeout (reduced from 600s — 600s caused the worker to
      appear frozen to Fly's watchdog, triggering health check failures)
    - 30s graceful shutdown (faster recovery if worker does need to restart)
  • Better latency for I/O-bound MLB API calls under load
    - gthread worker class with 8 threads -> serves 8 concurrent requests
      while the sync loaders run in background daemon threads
      (increased from 6 to ensure health check always gets a free thread
       even when Monte Carlo / Savant fetches are occupying several others)
  • No boot-storm
    - max_requests disabled (we DO NOT want to recycle and re-load caches)
  • Cache preload triggered AFTER the worker is bound (post_fork hook)
    - This ensures port 8080 is ready before any network I/O starts,
      so Fly.io health checks pass immediately from second 0.
  • auto_stop_machines=false in fly.toml keeps machine alive so caches
    are never lost to a suspend/resume cycle
"""
from config import settings

# Bind — Fly.io sets $PORT; fall back to 8080.
bind = f"0.0.0.0:{settings.port}"

# One worker only. pybaseball + Savant caches are large (~150MB) and
# 2 workers on a 2GB instance == unnecessary cache duplication.
workers = 1

# Threaded worker so the caches (loaded by background daemon threads)
# don't block request handling.
# 12 threads (raised from 8 after the 2026-07 CPU diet — vectorized MC,
# batched XGB — made requests I/O-dominated): the deepdive/dashboard pages
# fire 10+ parallel /api/* calls per navigation, which used to saturate all
# 8 threads and queue the rest. Most thread time is now spent waiting on
# statsapi.mlb.com, where extra threads are nearly free (small stacks, GIL
# released during I/O and numpy/XGB C calls). Health checks keep headroom.
worker_class = "gthread"
threads = 12

# Reduced from 600s. With gthread, a 600s timeout means Gunicorn waits
# 10 minutes before declaring the worker dead — but Fly.io's watchdog
# marks the machine unhealthy long before that. 120s is a safe ceiling
# for any single request while still allowing fast recovery on hangs.
timeout = 120
graceful_timeout = 30

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


def on_starting(server):
    """Install the pooled retrying HTTP session before workers import app.py."""
    try:
        from http_client import install_global_http_session

        session = install_global_http_session()
        retries = session.get_adapter("https://").max_retries.total
        server.log.info(
            "[on_starting] shared HTTP session installed with %s retries",
            retries,
        )
    except Exception as ex:
        server.log.warning(f"[on_starting] Could not install HTTP session: {ex}")


def post_fork(server, worker):
    """Install cache wrappers and start bounded, observable warmup."""
    try:
        from pipeline_cache_integration import install_pipeline_cache

        installed = install_pipeline_cache()
        server.log.info(
            "[post_fork] pipeline shared cache integration installed=%s",
            installed,
        )
    except Exception as ex:
        server.log.warning(
            f"[post_fork] Could not install pipeline cache integration: {ex}"
        )

    try:
        from app import _preload_caches
        from cache_warmup import warmup

        started = warmup.start(
            {"reference_snapshot": _preload_caches},
            timeout_seconds=30,
            max_workers=1,
        )
        server.log.info(
            "[post_fork] shared reference snapshot hydration started; upstream refresh remains worker-only; bounded warmup started=%s; status is available at /api/cache/warmup/status",
            started,
        )
    except Exception as ex:
        server.log.warning(f"[post_fork] Could not start reference preload: {ex}")
