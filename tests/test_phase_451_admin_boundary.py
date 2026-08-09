import os
from unittest.mock import patch

from flask import Flask, jsonify

from security import install_security, is_admin_surface


_ADMIN_READ_PATHS = (
    "/settings",
    "/api/app-settings",
    "/api/admin/settings",
    "/api/brain-data/list",
    "/api/cache/status",
    "/api/memory/status",
    "/api/model-actual/daily-summary/stored",
    "/api/pipeline/status",
    "/api/training/status",
    "/api/tracker/settings",
)


def _test_app():
    app = Flask(__name__)

    @app.get("/settings")
    def settings_page():
        return "settings"

    @app.get("/api/app-settings")
    def app_settings():
        return jsonify({"success": True})

    @app.get("/api/admin/settings")
    def admin_settings():
        return jsonify({"success": True})

    @app.get("/api/picks/today")
    def public_picks():
        return jsonify({"success": True})

    @app.get("/<path:ignored>")
    def admin_surface_stub(ignored):
        return jsonify({"success": True})

    install_security(app)
    return app


def test_admin_surface_allowlist_covers_reads_and_excludes_public_apis():
    for path in _ADMIN_READ_PATHS:
        assert is_admin_surface(path), path
    assert is_admin_surface("/api/cache/status/")
    assert not is_admin_surface("/api/picks/today")
    assert not is_admin_surface("/api/games")


def test_admin_reads_and_settings_shell_require_authentication():
    with patch.dict(os.environ, {"APP_ENV": "production", "ADMIN_TOKEN": "secret"}, clear=True):
        client = _test_app().test_client()

        for path in _ADMIN_READ_PATHS:
            assert client.get(path).status_code == 401, path

        assert client.get("/api/picks/today").status_code == 200

        headers = {"X-Admin-Token": "secret"}
        for path in _ADMIN_READ_PATHS:
            response = client.get(path, headers=headers)
            assert response.status_code == 200, (path, response.status_code)


def test_admin_boundary_fails_closed_when_production_token_is_missing():
    with patch.dict(
        os.environ,
        {"APP_ENV": "production", "ADMIN_AUTH_REQUIRED": "1"},
        clear=True,
    ):
        response = _test_app().test_client().get("/api/admin/settings")
        assert response.status_code == 503
        assert response.get_json()["success"] is False
