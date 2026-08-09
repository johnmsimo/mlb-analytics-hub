from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_performance_blueprint_has_one_registration_owner():
    app_source = (ROOT / "app.py").read_text(encoding="utf-8")
    wsgi_source = (ROOT / "wsgi.py").read_text(encoding="utf-8")

    assert "app.register_blueprint(performance_bp)" in app_source
    assert "app.register_blueprint(performance_bp)" not in wsgi_source
    assert "from request_performance import" not in wsgi_source
    assert wsgi_source.count("app.register_blueprint(cache_warmup_bp)") == 1
