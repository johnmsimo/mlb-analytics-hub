"""
Tracker API: pick CRUD, performance summary (CLV-first), grading and
closing-capture triggers, bankroll settings.
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

import tracker
from security import bounded_number, json_body, limiter

tracker_bp = Blueprint("tracker", __name__)


@tracker_bp.route("/api/tracker/pick", methods=["POST"])
@limiter.limit(30, 60, key="user")
def api_add_pick():
    # Must accept the FULL row the bet slip posts (see props.html/_build_game_rows
    # and app.js confirmSlip) — season/week/gameday are also what grade_pending
    # and the CLV date-keying read back. Trimming this list breaks pick saving.
    allowed = {
        "gameId", "season", "week", "gameday", "playerId", "player", "team",
        "opponent", "position", "marketKey", "marketLabel", "line", "side",
        "price", "book", "stakeDollars", "stakeUnits", "kellyPct", "modelProb",
        "impliedProb", "fairProb", "edge", "evPct", "modelSource", "source",
        "eventDate", "commenceTime", "projection", "probability",
    }
    payload = json_body(allowed=allowed, required={"marketKey", "side"})
    payload["marketKey"] = str(payload["marketKey"])[:80]
    payload["side"] = str(payload["side"]).lower()[:16]
    if payload["side"] not in {"over", "under", "home", "away", "yes", "no"}:
        return jsonify({"error": "invalid side"}), 400
    if "stakeDollars" in payload:
        payload["stakeDollars"] = bounded_number(payload, "stakeDollars", 0, 1_000_000)
    return jsonify(tracker.add_pick(payload))


@tracker_bp.route("/api/tracker/picks")
def api_list_picks():
    return jsonify(tracker.list_picks(request.args.get("date")))


@tracker_bp.route("/api/tracker/pick/<date>/<pick_id>", methods=["PATCH"])
@limiter.limit(60, 60, key="user")
def api_update_pick(date, pick_id):
    # Field names must match the store schema tracker.py reads/writes.
    patch = json_body(allowed={"grade", "profitDollars", "actual", "closingPrice",
                               "closingImplied", "clvEdge", "stakeDollars",
                               "notes", "price", "line", "book"})
    out = tracker.update_pick(date, pick_id, patch)
    if out is None:
        return jsonify({"error": "pick not found"}), 404
    return jsonify(out)


@tracker_bp.route("/api/tracker/pick/<date>/<pick_id>", methods=["DELETE"])
@limiter.limit(60, 60, key="user")
def api_delete_pick(date, pick_id):
    if not tracker.delete_pick(date, pick_id):
        return jsonify({"error": "pick not found"}), 404
    return jsonify({"deleted": True})


@tracker_bp.route("/api/tracker/performance")
def api_performance():
    return jsonify(tracker.performance_summary())


@tracker_bp.route("/api/tracker/grade", methods=["POST"])
@limiter.limit(10, 60, key="user")
def api_grade():
    return jsonify(tracker.grade_pending())


@tracker_bp.route("/api/tracker/closing-capture", methods=["POST"])
@limiter.limit(10, 60, key="user")
def api_closing_capture():
    return jsonify(tracker.closing_capture_once())


@tracker_bp.route("/api/tracker/live")
def api_live():
    return jsonify(tracker.live_status())


@tracker_bp.route("/api/tracker/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "POST":
        # tracker.html posts all four settings, including max_bet_pct.
        payload = json_body(allowed={"bankroll", "unit_pct", "kelly_fraction",
                                     "max_bet_pct"})
        if "bankroll" in payload:
            payload["bankroll"] = bounded_number(payload, "bankroll", 0, 100_000_000)
        if "unit_pct" in payload:
            payload["unit_pct"] = bounded_number(payload, "unit_pct", 0.001, 0.1)
        if "kelly_fraction" in payload:
            payload["kelly_fraction"] = bounded_number(payload, "kelly_fraction", 0, 1)
        if "max_bet_pct" in payload:
            payload["max_bet_pct"] = bounded_number(payload, "max_bet_pct", 0.001, 1)
        return jsonify(tracker.save_settings(payload))
    return jsonify(tracker.get_settings())
