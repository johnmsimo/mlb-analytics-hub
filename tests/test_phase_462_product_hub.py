from pathlib import Path

from product_hub import PRODUCT_HUB_VERSION, product_hub_bp


ROOT = Path(__file__).resolve().parents[1]


def test_product_journey_contract_is_complete_and_ordered():
    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(product_hub_bp)
    with app.test_client() as client:
        payload = client.get("/api/product/journey").get_json()

    assert payload["version"] == PRODUCT_HUB_VERSION == "4.64"
    assert [stage["key"] for stage in payload["stages"]] == [
        "discover", "validate", "track", "learn"
    ]
    assert payload["actionability"]["failClosed"] is True
    assert payload["personalization"]["alertDelivery"] == "in_app"
    assert payload["personalization"]["persistence"] == "device_private"
    assert payload["alerts"]["serverPersistence"] is False


def test_workspace_is_no_store_and_contains_growth_features():
    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(product_hub_bp)
    with app.test_client() as client:
        response = client.get("/workspace")

    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    for marker in ("Discover", "Validate", "Track", "Learn", "Saved players", "In-app alert threshold"):
        assert marker.lower() in html.lower()


def test_workspace_uses_canonical_fail_closed_sources_and_storage_keys():
    source = (ROOT / "static" / "product-hub.js").read_text(encoding="utf-8")
    assert "/api/edges/today?minEdge=0.03" in source
    assert "/api/calibration/markets" in source
    assert "/api/tracker/performance?window=30" in source
    assert "row.actionable === true" in source
    assert "price != null" in source
    assert "price !== 0" in source
    assert "Math.abs(price) >= 100" in source
    assert "Boolean(book)" in source
    assert "Boolean(row.canonicalFingerprint)" in source
    assert "mlb_watchlist" in source
    assert "mlb_market_preferences" in source
    assert "mlb_alert_edge_threshold" in source
    assert "mlb_alert_ledger" in source


def test_workspace_is_registered_once_and_available_in_shared_navigation():
    wsgi = (ROOT / "wsgi.py").read_text(encoding="utf-8")
    desktop = (ROOT / "static" / "global-nav.js").read_text(encoding="utf-8")
    mobile = (ROOT / "static" / "mobile-nav.js").read_text(encoding="utf-8")
    assert wsgi.count("app_module.app.register_blueprint(product_hub_bp)") == 1
    assert "href: '/workspace'" in desktop
    assert "href: '/workspace'" in mobile
    assert "return 'hub'" in mobile


def test_workspace_preserves_390px_touch_contract():
    css = (ROOT / "static" / "product-hub.css").read_text(encoding="utf-8")
    html = (ROOT / "product_hub.html").read_text(encoding="utf-8")
    assert "@media(max-width:480px)" in css
    assert "min-height:44px" in css
    assert 'href="/static/mobile.css"' in html
    assert 'name="viewport"' in html
