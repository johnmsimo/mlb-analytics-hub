from pathlib import Path

from flask import Flask

from security import _default_rate_limit_exempt, limiter


ROOT = Path(__file__).resolve().parents[1]


def test_health_and_readiness_are_explicitly_exempt_from_all_limits():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    health = source.split("@app.route('/health')", 1)[1].split(
        "@app.route('/ready')", 1
    )[0]
    ready = source.split("@app.route('/ready')", 1)[1].split(
        "@app.route('/api/jobs/<job_id>')", 1
    )[0]

    assert "@limiter.exempt" in health
    assert "@limiter.exempt" in ready
    assert "queue_health()" not in health
    assert "queue_health()" in ready


def test_default_limiter_only_consumes_storage_for_api_requests():
    app = Flask(__name__)
    expectations = {
        "/health": True,
        "/health/": True,
        "/ready": True,
        "/ready/": True,
        "/picks": True,
        "/static/app.js": True,
        "/api/picks/today": False,
        "/api/jobs/abc": False,
    }

    for path, expected in expectations.items():
        with app.test_request_context(path):
            assert _default_rate_limit_exempt() is expected


def test_redis_limiter_has_memory_failover_enabled():
    source = (ROOT / "security.py").read_text(encoding="utf-8")

    assert "in_memory_fallback_enabled=True" in source
    assert limiter._in_memory_fallback_enabled is True
