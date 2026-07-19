from __future__ import annotations

from urllib.parse import urlparse

from flask import Blueprint, jsonify, request, session

from security import authenticate, establish_session, json_body, limiter

auth_bp = Blueprint("auth", __name__)


def _safe_next(value: str | None) -> str:
    if not value:
        return "/"
    parsed = urlparse(value)
    return value if not parsed.netloc and value.startswith("/") else "/"


@auth_bp.route("/api/auth/login", methods=["POST"])
@limiter.limit(5, 60)
def api_login():
    payload = json_body(allowed={"username", "password", "next"}, required={"username", "password"})
    username = str(payload["username"])[:128]
    password = str(payload["password"])[:512]
    if not authenticate(username, password):
        return jsonify({"error": "invalid credentials", "code": "INVALID_CREDENTIALS"}), 401
    user = establish_session(username)
    return jsonify({"ok": True, "user": user, "csrf_token": session["csrf_token"], "next": _safe_next(payload.get("next"))})


@auth_bp.route("/api/auth/session")
def api_session():
    return jsonify({"authenticated": True, "user": session["user"], "csrf_token": session["csrf_token"]})


@auth_bp.route("/api/auth/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})
