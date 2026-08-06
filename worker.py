"""Dedicated durable-job worker for simulations and data enrichment."""
from __future__ import annotations

import logging
import os
import signal
import threading


# Must be set before importing app.py; that module gates its periodic workers
# and startup behavior by process role.
os.environ["PROCESS_ROLE"] = "worker"

from task_queue import get_job_queue  # noqa: E402


log = logging.getLogger(__name__)
_stop = threading.Event()


def _handlers():
    import app as app_module
    from intelligence_integration import run_game_card_job
    from pipeline_scheduler import run_pipeline
    from training_routes import run_training_job

    def simulation(args):
        game_pk = int(args["gamePk"])
        sims = max(1500, min(5000, int(args.get("sims") or 1500)))
        payload = app_module._do_simulate(game_pk, sims)
        if not (
            payload
            and payload.get("success")
            and not payload.get("fallback")
            and payload.get("meta", {}).get("sims")
        ):
            raise RuntimeError(
                (payload or {}).get("error")
                or (payload or {}).get("warning")
                or "Matchup simulation did not complete."
            )
        app_module._write_simulation_snapshot(game_pk, sims, payload)

    def call_route(path, function, *, body=None):
        headers = {'X-Admin-Token': app_module.settings.admin_token}
        with app_module.app.test_request_context(
            path,
            method='POST',
            json=body,
            headers=headers,
        ):
            response = function()
            status = response[1] if isinstance(response, tuple) else getattr(response, 'status_code', 200)
            if int(status or 200) >= 400:
                raise RuntimeError(f'Worker route failed with HTTP {status}')

    def cache_warm(args):
        query = []
        if args.get('force'):
            query.append('force=1')
        if args.get('date'):
            query.append(f"date={args['date']}")
        path = '/api/cache/warm' + (('?' + '&'.join(query)) if query else '')
        call_route(path, app_module.api_cache_warm)

    def brain_ingest(args):
        call_route('/api/brain/ingest-trigger', app_module.api_brain_ingest_trigger, body=args)

    def odds_refresh(args):
        call_route('/api/odds/cache/refresh', app_module.api_odds_cache_refresh, body=args)

    return {
        "game_card": lambda args: run_game_card_job(app_module, args),
        "simulation": simulation,
        "pipeline": lambda _args: run_pipeline(),
        "cache_warm": cache_warm,
        "brain_ingest": brain_ingest,
        "odds_refresh": odds_refresh,
        "training": run_training_job,
    }, app_module


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")
    queue = get_job_queue()
    handlers, app_module = _handlers()
    app_module._preload_caches()

    def stop(_signum, _frame):
        _stop.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    queue.heartbeat()
    log.info("Phase 4.36 durable worker ready")
    while not _stop.is_set():
        queue.work_once(handlers, block_seconds=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
