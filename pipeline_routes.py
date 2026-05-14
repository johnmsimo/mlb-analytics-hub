"""
pipeline_routes.py — Flask Blueprint for the matchup pipeline.

Registers under /api/pipeline/* and wraps the three public accessors
exposed by pipeline_scheduler:
    get_pipeline_status()  →  GET  /api/pipeline/status
    get_matchup_df()       →  GET  /api/pipeline/matchup
    get_games_df()         →  GET  /api/pipeline/games
    run_pipeline()         →  POST /api/pipeline/run   (admin-token protected)
"""

import os
import threading
import logging

from flask import Blueprint, jsonify, request

from pipeline_scheduler import (
    get_matchup_df,
    get_games_df,
    get_pipeline_status,
    run_pipeline,
)

pipeline_bp = Blueprint("pipeline", __name__, url_prefix="/api/pipeline")
log = logging.getLogger(__name__)

# Read once at import time; same variable the rest of app.py uses.
_ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()


def _check_admin_auth():
    """Return None if auth passes (or ADMIN_TOKEN is unset), else a 401 Response.

    Accepts the token via:
      Authorization: Bearer <token>
      X-Admin-Token: <token>
    """
    if not _ADMIN_TOKEN:
        return None  # auth disabled — open access
    auth_header  = request.headers.get("Authorization", "")
    token_header = request.headers.get("X-Admin-Token", "")
    bearer = (
        auth_header.removeprefix("Bearer ").strip()
        if auth_header.startswith("Bearer ")
        else ""
    )
    provided = bearer or token_header.strip()
    if provided == _ADMIN_TOKEN:
        return None
    return jsonify({"success": False, "error": "Unauthorized"}), 401


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
def pipeline_run():
    """
    Manually trigger a pipeline run in a background thread.
    Protected by ADMIN_TOKEN when that env var is set.
    Safe to call while a run is already in progress (noop if already running).
    """
    auth_err = _check_admin_auth()
    if auth_err is not None:
        return auth_err

    status = get_pipeline_status()
    if status.get("status") == "running":
        return jsonify({"success": False, "error": "Pipeline already running"}), 409

    threading.Thread(target=run_pipeline, daemon=True).start()
    log.info("[pipeline] Manual run triggered via POST /api/pipeline/run")
    return jsonify({"success": True, "message": "Pipeline run started"})
