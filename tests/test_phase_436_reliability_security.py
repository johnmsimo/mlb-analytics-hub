from pathlib import Path
from unittest.mock import patch

from flask import Flask

from security import check_admin_auth


ROOT = Path(__file__).resolve().parents[1]


def test_web_delivery_never_spawns_simulation_threads():
    intelligence = (ROOT / 'intelligence_integration.py').read_text(encoding='utf-8')
    app = (ROOT / 'app.py').read_text(encoding='utf-8')
    training = (ROOT / 'training_routes.py').read_text(encoding='utf-8')
    simulation_route = app.split("def api_simulate(game_pk):", 1)[1].split(
        "# ── Phase 7 Odds", 1
    )[0]

    assert 'ThreadPoolExecutor' not in intelligence
    assert '_GAME_CARD_EXECUTOR' not in intelligence
    assert "enqueue_job(\n            'game_card'" in intelligence
    assert '_do_simulate(' not in simulation_route
    assert "enqueue_job(\n            'simulation'" in simulation_route
    training_route = training.split('def training_run():', 1)[1]
    assert 'threading.Thread(target=_run_training' not in training_route
    assert "enqueue_job(\n            'training'" in training_route


def test_process_roles_keep_startup_compute_out_of_gunicorn():
    gunicorn = (ROOT / 'gunicorn_conf.py').read_text(encoding='utf-8')
    app = (ROOT / 'app.py').read_text(encoding='utf-8')
    docker = (ROOT / 'Dockerfile').read_text(encoding='utf-8')

    assert 'shared reference snapshot hydration started; upstream refresh remains worker-only' in gunicorn
    assert "if settings.process_role == 'worker':" in app
    assert "if settings.process_role == 'web':" in app
    assert 'web shared reference snapshot watcher started' in app
    assert 'CMD ["python", "process_manager.py"]' in docker


def test_security_is_central_fail_closed_and_errors_are_sanitized():
    security = (ROOT / 'security.py').read_text(encoding='utf-8')
    app = (ROOT / 'app.py').read_text(encoding='utf-8')
    handler = app.split('def handle_exception(e):', 1)[1].split(
        '_HERE =', 1
    )[0]

    assert 'settings.admin_auth_required' in security
    assert 'hmac.compare_digest' in security
    assert 'request.method in {"POST", "PUT", "PATCH", "DELETE"}' in security
    assert 'X-Content-Type-Options' in security
    assert 'Content-Security-Policy' in security
    assert 'CORS(' in security
    assert '"error": "Internal server error"' in handler
    assert 'str(e)' not in handler


def test_health_is_constant_time_and_readiness_checks_worker():
    app = (ROOT / 'app.py').read_text(encoding='utf-8')
    health = app.split("def health_check():", 1)[1].split(
        "@app.route('/ready')", 1
    )[0]
    ready = app.split("def readiness_check():", 1)[1].split(
        "@app.route('/api/mc-upgrades/status')", 1
    )[0]

    assert '_fg_lock' not in health
    assert '_sv_lock' not in health
    assert 'queue_health()' in ready
    assert "200 if ready else 503" in ready


def test_admin_auth_fails_closed_and_uses_constant_time_token_check():
    flask_app = Flask(__name__)
    with patch.dict(
        'os.environ',
        {'APP_ENV': 'production', 'ADMIN_AUTH_REQUIRED': '1'},
        clear=True,
    ):
        with flask_app.test_request_context('/api/cache/warm', method='POST'):
            response, status = check_admin_auth()
            assert status == 503
            assert response.get_json()['success'] is False

    with patch.dict(
        'os.environ',
        {'APP_ENV': 'production', 'ADMIN_TOKEN': 'secret'},
        clear=True,
    ):
        with flask_app.test_request_context(
            '/api/cache/warm',
            method='POST',
            headers={'X-Admin-Token': 'secret'},
        ):
            assert check_admin_auth() is None
