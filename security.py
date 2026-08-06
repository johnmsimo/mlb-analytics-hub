"""Central fail-closed API security and response hardening."""
from __future__ import annotations

import hmac

from flask import g, jsonify, request
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from config import settings


limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=settings.redis_url or "memory://",
    default_limits=[settings.api_rate_limit],
    headers_enabled=True,
)


def check_admin_auth():
    """Authenticate an administrative request and fail closed in production."""
    expected = settings.admin_token
    if not expected:
        if settings.admin_auth_required:
            return jsonify({
                "success": False,
                "error": "Administrative API is unavailable until ADMIN_TOKEN is configured.",
            }), 503
        return None
    auth = request.headers.get("Authorization", "")
    token = request.headers.get("X-Admin-Token", "")
    bearer = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
    supplied = bearer or token.strip()
    if supplied and hmac.compare_digest(expected, supplied):
        return None
    return jsonify({"success": False, "error": "Unauthorized"}), 401


def install_security(app) -> None:
    app.config["MAX_CONTENT_LENGTH"] = settings.max_upload_bytes
    CORS(
        app,
        resources={r"/api/*": {"origins": list(settings.allowed_origins)}},
        supports_credentials=False,
        methods=["GET", "HEAD", "OPTIONS", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "Authorization", "X-Admin-Token"],
        max_age=600,
    )
    limiter.init_app(app)

    @app.before_request
    def _protect_api_mutations():
        if (
            request.path.startswith("/api/")
            and request.method in {"POST", "PUT", "PATCH", "DELETE"}
        ):
            return check_admin_auth()
        return None

    @app.after_request
    def _security_headers(response):
        if (
            settings.production
            and response.status_code >= 500
            and (response.mimetype or '').startswith('application/json')
        ):
            message = (
                'Service temporarily unavailable'
                if response.status_code == 503
                else 'Internal server error'
            )
            response.set_data(app.json.dumps({
                'success': False,
                'error': message,
                'requestId': getattr(g, 'request_id', None),
            }))
            response.headers['Content-Type'] = 'application/json'
            response.headers['Content-Length'] = len(response.get_data())
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.headers.setdefault(
            "Content-Security-Policy",
            "frame-ancestors 'none'; object-src 'none'; base-uri 'self'",
        )
        if request.is_secure:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response
