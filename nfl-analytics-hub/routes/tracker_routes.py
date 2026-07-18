"""
Tracker API: pick CRUD, performance summary (CLV-first), grading and
closing-capture triggers, bankroll settings.
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

import tracker

tracker_bp = Blueprint("tracker", __name__)


@tracker_bp.route("/api/tracker/pick", methods=["POST"])
def api_add_pick():
    payload = request.get_json(silent=True) or {}
    if not payload.get("marketKey"):
        return jsonify({"error": "marketKey required"}), 400
    return jsonify(tracker.add_pick(payload))


@tracker_bp.route("/api/tracker/picks")
def api_list_picks():
    return jsonify(tracker.list_picks(request.args.get("date")))


@tracker_bp.route("/api/tracker/pick/<date>/<pick_id>", methods=["PATCH"])
def api_update_pick(date, pick_id):
    patch = request.get_json(silent=True) or {}
    out = tracker.update_pick(date, pick_id, patch)
    if out is None:
        return jsonify({"error": "pick not found"}), 404
    return jsonify(out)


@tracker_bp.route("/api/tracker/pick/<date>/<pick_id>", methods=["DELETE"])
def api_delete_pick(date, pick_id):
    if not tracker.delete_pick(date, pick_id):
        return jsonify({"error": "pick not found"}), 404
    return jsonify({"deleted": True})


@tracker_bp.route("/api/tracker/performance")
def api_performance():
    return jsonify(tracker.performance_summary())


@tracker_bp.route("/api/tracker/grade", methods=["POST"])
def api_grade():
    return jsonify(tracker.grade_pending())


@tracker_bp.route("/api/tracker/closing-capture", methods=["POST"])
def api_closing_capture():
    return jsonify(tracker.closing_capture_once())


@tracker_bp.route("/api/tracker/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "POST":
        return jsonify(tracker.save_settings(request.get_json(silent=True) or {}))
    return jsonify(tracker.get_settings())
