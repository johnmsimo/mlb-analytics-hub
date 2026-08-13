from pathlib import Path

from product_hub import PRODUCT_HUB_VERSION, product_hub_bp


ROOT = Path(__file__).resolve().parents[1]


def test_phase_463_alert_contract_is_device_private_and_fail_closed():
    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(product_hub_bp)
    with app.test_client() as client:
        payload = client.get("/api/product/journey").get_json()

    alerts = payload["alerts"]
    assert payload["version"] == PRODUCT_HUB_VERSION == "4.64"
    assert alerts["lifecycle"][:3] == ["new", "seen", "dismissed"]
    assert alerts["dedupeIdentity"] == [
        "canonicalCandidateId",
        "canonicalFingerprint",
    ]
    assert alerts["requiresPreferredMarket"] is True
    assert alerts["requiresThresholdMatch"] is True
    assert alerts["serverPersistence"] is False
    assert alerts["failClosed"] is True


def test_phase_463_alert_inbox_has_bounded_dedupe_and_lifecycle_controls():
    source = (ROOT / "static" / "product-hub.js").read_text(encoding="utf-8")

    for marker in (
        "ALERT_LEDGER_KEY",
        "ALERT_LEDGER_LIMIT = 200",
        "alertIdentity(row)",
        "canonicalCandidateId",
        "canonicalFingerprint",
        "['new', 'seen', 'dismissed']",
        "data-alert-action=\"seen\"",
        "data-alert-action=\"dismiss\"",
        "markAllAlertsSeen",
        "persistAlertState()",
    ):
        assert marker in source


def test_phase_463_alerts_only_use_canonical_actionable_threshold_matches():
    source = (ROOT / "static" / "product-hub.js").read_text(encoding="utf-8")

    assert "row.actionable === true" in source
    assert "Boolean(row.player && row.playerId && row.canonicalCandidateId)" in source
    assert "Boolean(row.canonicalFingerprint)" in source
    assert "Math.abs(price) >= 100" in source
    assert "invalidBooks.indexOf(book) === -1" in source
    assert "edge != null && edge > 0" in source
    assert "state.preferred.has(marketKeyOf(row))" in source
    assert "(edgeValue(row) || 0) >= state.threshold" in source


def test_phase_463_alert_inbox_preserves_mobile_and_privacy_contract():
    html = (ROOT / "product_hub.html").read_text(encoding="utf-8")
    css = (ROOT / "static" / "product-hub.css").read_text(encoding="utf-8")

    assert "PHASE 4.64" in html
    assert 'id="alertList"' in html
    assert 'id="markAllAlertsSeen"' in html
    assert "private to this device" in html
    assert ".signal-card,.alert-card{grid-template-columns:1fr}" in css
    assert ".alert-actions{justify-content:flex-start}" in css
    assert "min-height:44px" in css
