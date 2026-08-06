"""
pipeline_routes.py — Flask Blueprint for the matchup pipeline.

Registers under /api/pipeline/* and wraps the three public accessors
exposed by pipeline_scheduler:
    get_pipeline_status()  →  GET  /api/pipeline/status
    get_matchup_df()       →  GET  /api/pipeline/matchup
    get_games_df()         →  GET  /api/pipeline/games
    run_pipeline()         →  POST /api/pipeline/run   (admin-token protected + rate limited)
"""

import logging

from flask import Blueprint, jsonify, request
from security import check_admin_auth, limiter
from task_queue import JobQueueUnavailable, enqueue_job

from pipeline_scheduler import (
    get_matchup_df,
    get_games_df,
    get_pipeline_status,
    run_pipeline,
)

pipeline_bp = Blueprint("pipeline", __name__, url_prefix="/api/pipeline")
log = logging.getLogger(__name__)

@pipeline_bp.route("/status")
def pipeline_status():
    """Return current pipeline state (idle | running | done | error)."""
    return jsonify(get_pipeline_status())


@pipeline_bp.route("/games")
def pipeline_games():
    """Return today's games DataFrame as a list of row dicts."""
    df = get_games_df()
    if df.empty:
        return jsonify({"success": True, "count": 0, "games": []})
    return jsonify({"success": True, "count": len(df), "games": df.to_dict(orient="records")})


@pipeline_bp.route("/matchup")
def pipeline_matchup():
    """
    Return the matchup DataFrame (all batter×pitcher rows for today).

    Optional query params:
        game_pk   — filter to a single game
        team      — filter by batting_team abbreviation
    """
    df = get_matchup_df()
    if df.empty:
        return jsonify({"success": True, "count": 0, "rows": []})

    game_pk = request.args.get("game_pk")
    team    = request.args.get("team", "").upper()

    if game_pk:
        try:
            df = df[df["game_pk"] == int(game_pk)]
        except (ValueError, KeyError):
            pass

    if team and "batting_team" in df.columns:
        df = df[df["batting_team"].str.upper() == team]

    return jsonify({
        "success": True,
        "count":   len(df),
        "rows":    df.to_dict(orient="records"),
    })


@pipeline_bp.route("/run", methods=["POST"])
@limiter.limit("3 per minute; 10 per hour")
def pipeline_run():
    """
    Manually trigger a pipeline run in a background thread.
    Protected by ADMIN_TOKEN when that env var is set.
    Rate-limited to 3/min and 10/hr per IP (via Flask-Limiter).
    Safe to call while a run is already in progress (noop if already running).
    """
    auth_err = check_admin_auth()
    if auth_err is not None:
        return auth_err

    status = get_pipeline_status()
    if status.get("status") == "running":
        return jsonify({"success": False, "error": "Pipeline already running"}), 409

    try:
        job = enqueue_job(
            "pipeline",
            {},
            dedupe_key="pipeline:manual",
            timeout_seconds=900,
            max_attempts=1,
        )
    except JobQueueUnavailable:
        return jsonify({"success": False, "error": "Worker unavailable"}), 503
    log.info("[pipeline] Manual run queued via POST /api/pipeline/run")
    return jsonify({"success": True, "message": "Pipeline run queued", "jobId": job["id"]}), 202
