"""
training_routes.py — Flask Blueprint: 1-click XGB model training
═══════════════════════════════════════════════════════════════════════
Registers under /api/training/*

  POST /api/training/run
      Launches train_prop_models.train_all() in a background thread.
      Optional JSON body:
        { "markets": ["hits", "k_4.5"], "seasons": [2022, 2023, 2024, 2025] }
      Omit both keys to train all 7 markets on the default season window.

  GET  /api/training/status
      Returns current phase, per-market progress, live log tail,
      elapsed time, and a final results scorecard when done.

Protection:
  - Requires ADMIN_TOKEN (same env var as pipeline_routes.py) when set.
  - Rate-limited to 1 run / 10 min per IP (training is expensive).
  - Safe to poll /status at any cadence — it never blocks.
"""

from __future__ import annotations

import io
import json
import logging
import math
import sys
import threading
import time
import traceback
from contextlib import redirect_stdout
from typing import Optional

from flask import Blueprint, jsonify, request
from security import check_admin_auth, limiter
from task_queue import (
    JobQueueUnavailable,
    enqueue_job,
    get_job_queue,
    write_durable_json,
)

log = logging.getLogger(__name__)

training_bp = Blueprint("training", __name__, url_prefix="/api/training")

# ── Shared training state (in-memory; one run at a time) ──────────────────────
_VALID_MARKETS = ["hits", "hr", "tb", "rbi", "k_3.5", "k_4.5", "k_5.5"]

_state: dict = {
    "status":       "idle",          # idle | running | done | error
    "phase":        "",              # current human-readable phase
    "markets_total":   0,
    "markets_done":    0,
    "market_results":  {},           # {market_key: {auc, brier, status}}
    "log_lines":       [],           # rolling last-200 log lines
    "started_at":      None,
    "finished_at":     None,
    "error":           None,
    "requested_markets": [],
    "requested_seasons":  [],
}
_state_lock = threading.Lock()
_MAX_LOG_LINES = 200
_TRAINING_POINTER_KEY = "mlb:training:active-job:v436"
_TRAINING_RESULT_KEY = "mlb:training:last-result:v436"


def _set(key: str, value) -> None:
    with _state_lock:
        _state[key] = value


def _append_log(line: str) -> None:
    with _state_lock:
        _state["log_lines"].append(line)
        if len(_state["log_lines"]) > _MAX_LOG_LINES:
            _state["log_lines"] = _state["log_lines"][-_MAX_LOG_LINES:]


class _LogCapture(io.StringIO):
    """Tee stdout to the real console while recording ONLY the training thread's
    output into the UI log.

    `redirect_stdout` swaps `sys.stdout` process-wide, so without the thread
    filter every other request's `print()` (e.g. the props-projection debug
    lines) would pollute the training log — and the training output would be
    swallowed instead of reaching the real logs. Filtering by the owning thread
    ident fixes both."""
    def __init__(self, real_stream, owner_ident: int):
        super().__init__()
        self._real = real_stream
        self._owner = owner_ident

    def write(self, s: str) -> int:
        # Always pass through to the real stdout so nothing is lost.
        try:
            if self._real is not None:
                self._real.write(s)
        except Exception:
            pass
        # Only record lines emitted by the training thread itself.
        if threading.get_ident() == self._owner:
            for line in s.splitlines():
                stripped = line.strip()
                if stripped:
                    _append_log(stripped)
        return len(s)

    def flush(self) -> None:
        try:
            if self._real is not None:
                self._real.flush()
        except Exception:
            pass


def _json_safe(obj):
    """Recursively replace NaN/Infinity with None so the status payload is
    strict-JSON. jsonify emits bare `NaN`/`Infinity` tokens otherwise, which
    iOS Safari's JSON.parse rejects with 'The string did not match the
    expected pattern.'"""
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    return obj


# ── Background training worker ────────────────────────────────────────────────

def _run_training(markets: list[str], seasons: list[int]) -> None:
    """Runs in a daemon thread. Updates _state throughout."""
    with _state_lock:
        _state.update({
            "status":          "running",
            "phase":           "Initialising data fetch…",
            "markets_total":   len(markets),
            "markets_done":    0,
            "market_results":  {m: {"status": "pending"} for m in markets},
            "log_lines":       [],
            "started_at":      time.time(),
            "finished_at":     None,
            "error":           None,
        })

    cap = _LogCapture(sys.__stdout__ or sys.stdout, threading.get_ident())

    # Live phase/progress hook so the UI shows movement during the long
    # data-fetch stage (which can run for many minutes before market 1 starts).
    def _progress(phase: str, log_line: Optional[str] = None) -> None:
        if phase:
            _set("phase", phase)
        if log_line:
            _append_log(log_line)

    try:
        # Lazy import so the module isn't loaded until needed
        from train_prop_models import train_all, MODEL_CONFIGS

        _set("phase", "Fetching Statcast + FanGraphs season data…")
        _append_log(f"Starting training — markets: {markets}  seasons: {seasons}")

        # Monkey-patch train_one_market to intercept per-market results
        import train_prop_models as _tm
        _orig_train_one = _tm.train_one_market

        def _patched_train_one(market_key, df, config, seasons_arg):
            _set("phase", f"Training {market_key}…")
            _append_log(f"── {market_key} started")
            with _state_lock:
                _state["market_results"][market_key]["status"] = "training"

            result = _orig_train_one(market_key, df, config, seasons_arg)

            with _state_lock:
                if result:
                    m = result["meta"]
                    _state["market_results"][market_key] = {
                        "status":   "done",
                        "auc":      m.get("auc"),
                        "brier":    m.get("brier"),
                        "log_loss": m.get("log_loss"),
                        "n_rows":   m.get("n_rows"),
                        "pos_rate": m.get("pos_rate"),
                        "cv_auc":   m.get("cv_auc_mean"),
                    }
                    _append_log(
                        f"✓ {market_key}  AUC={m.get('auc'):.4f}  "
                        f"Brier={m.get('brier'):.4f}"
                    )
                else:
                    _state["market_results"][market_key]["status"] = "failed"
                    _append_log(f"✗ {market_key} FAILED")
                _state["markets_done"] += 1
            return result

        _tm.train_one_market = _patched_train_one

        with redirect_stdout(cap):
            results = train_all(markets=markets, seasons=seasons, progress_cb=_progress)

        # Restore original
        _tm.train_one_market = _orig_train_one

        # Mark any still-pending as failed (shouldn't happen, but safety net)
        with _state_lock:
            for mkey, mval in _state["market_results"].items():
                if mval.get("status") == "pending":
                    _state["market_results"][mkey]["status"] = "skipped"

        _append_log("Training complete — models saved to models/")
        _set("status", "done")
        _set("phase", "Complete")

    except Exception as exc:
        tb = traceback.format_exc()
        _append_log(f"ERROR: {exc}")
        for tbl in tb.splitlines():
            _append_log(tbl)
        _set("status", "error")
        _set("phase", "Error")
        _set("error", str(exc))
        log.error("[training] background run failed: %s", exc)
    finally:
        _set("finished_at", time.time())


def run_training_job(args) -> None:
    """Durable worker entry point for model training."""
    markets = list(args.get('markets') or _VALID_MARKETS)
    seasons = [int(value) for value in (args.get('seasons') or [2021, 2022, 2023, 2024, 2025])]
    with _state_lock:
        _state['requested_markets'] = markets
        _state['requested_seasons'] = seasons
    _run_training(markets, seasons)
    with _state_lock:
        result = _json_safe(dict(_state))
    result['success'] = result.get('status') == 'done'
    write_durable_json(_TRAINING_RESULT_KEY, result, ttl=86400)


def _durable_training_status():
    try:
        queue = get_job_queue()
        pointer_raw = queue.client.get(_TRAINING_POINTER_KEY)
        if not pointer_raw:
            return None
        pointer = json.loads(pointer_raw)
        job = queue.get(pointer.get('jobId'))
        snapshot = queue.snapshot(job)
        if not snapshot:
            return None
        if snapshot['status'] in {'queued', 'running'}:
            return {
                'success': True,
                'status': snapshot['status'],
                'phase': 'Queued on durable worker' if snapshot['status'] == 'queued' else 'Training on durable worker',
                'progress_pct': 0,
                'markets_done': 0,
                'markets_total': len(pointer.get('markets') or []),
                'market_results': {},
                'log_tail': [],
                'elapsed_s': snapshot['elapsedSeconds'],
                'error': None,
                'job': snapshot,
            }
        if snapshot['status'] == 'error':
            return {
                'success': False,
                'status': 'error',
                'phase': 'Worker job failed',
                'progress_pct': 0,
                'markets_done': 0,
                'markets_total': len(pointer.get('markets') or []),
                'market_results': {},
                'log_tail': [],
                'elapsed_s': snapshot['elapsedSeconds'],
                'error': snapshot['error'],
                'job': snapshot,
            }
        result_raw = queue.client.get(_TRAINING_RESULT_KEY)
        return json.loads(result_raw) if result_raw else None
    except (JobQueueUnavailable, TypeError, ValueError):
        return None


# ── Auth helper ───────────────────────────────────────────────────────────────

# ── Routes ────────────────────────────────────────────────────────────────────

@training_bp.route("/status")
def training_status():
    """
    GET /api/training/status
    Returns:
      status        — idle | running | done | error
      phase         — human-readable current step
      progress_pct  — 0-100
      markets_done  — int
      markets_total — int
      market_results — {market: {status, auc, brier, ...}}
      log_tail      — last 50 log lines
      elapsed_s     — seconds since run started (null if idle)
      error         — error message string (null if none)
    """
    durable = _durable_training_status()
    if durable is not None:
        return jsonify(durable)

    with _state_lock:
        snap = dict(_state)

    total    = snap["markets_total"] or 1
    done     = snap["markets_done"]
    pct      = round(done / total * 100) if snap["status"] == "running" else (
        100 if snap["status"] == "done" else 0
    )
    elapsed  = None
    if snap["started_at"]:
        end  = snap["finished_at"] or time.time()
        elapsed = round(end - snap["started_at"], 1)

    return jsonify(_json_safe({
        "success":        True,
        "status":         snap["status"],
        "phase":          snap["phase"],
        "progress_pct":   pct,
        "markets_done":   done,
        "markets_total":  snap["markets_total"],
        "market_results": snap["market_results"],
        "log_tail":       snap["log_lines"][-50:],
        "elapsed_s":      elapsed,
        "error":          snap["error"],
    }))


@training_bp.route("/run", methods=["POST"])
@limiter.limit("1 per 10 minutes")
def training_run():
    """
    POST /api/training/run
    Optional JSON body:
      { "markets": ["hits","k_4.5"], "seasons": [2023,2024,2025] }
    Omit to train all 7 markets on default seasons (2021-2025).
    """
    auth_err = check_admin_auth()
    if auth_err is not None:
        return auth_err

    with _state_lock:
        if _state["status"] == "running":
            return jsonify({"success": False, "error": "Training already in progress"}), 409

    body    = request.get_json(silent=True) or {}
    markets = body.get("markets") or _VALID_MARKETS
    seasons = body.get("seasons") or [2021, 2022, 2023, 2024, 2025]

    # Validate
    bad_markets = [m for m in markets if m not in _VALID_MARKETS]
    if bad_markets:
        return jsonify({"success": False, "error": f"Unknown markets: {bad_markets}"}), 400

    with _state_lock:
        _state["requested_markets"] = markets
        _state["requested_seasons"] = seasons

    try:
        job = enqueue_job(
            'training',
            {'markets': markets, 'seasons': seasons},
            dedupe_key='model-training',
            timeout_seconds=7200,
            max_attempts=1,
        )
        write_durable_json(
            _TRAINING_POINTER_KEY,
            {'jobId': job['id'], 'markets': markets, 'seasons': seasons},
            ttl=86400,
        )
    except JobQueueUnavailable:
        return jsonify({'success': False, 'error': 'Worker unavailable'}), 503
    log.info("[training] run queued — markets=%s seasons=%s", markets, seasons)
    return jsonify({
        "success": True,
        "message": f"Training queued for {len(markets)} markets",
        "markets": markets,
        "seasons": seasons,
        "jobId": job['id'],
    }), 202
