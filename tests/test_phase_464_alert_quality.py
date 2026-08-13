from pathlib import Path

from product_hub import PRODUCT_HUB_VERSION, product_hub_bp


ROOT = Path(__file__).resolve().parents[1]


def test_phase_464_alert_quality_contract_is_explicit_and_fail_closed():
    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(product_hub_bp)
    with app.test_client() as client:
        payload = client.get("/api/product/journey").get_json()

    alerts = payload["alerts"]
    assert payload["version"] == PRODUCT_HUB_VERSION == "4.64"
    assert alerts["lifecycle"] == ["new", "seen", "dismissed", "superseded"]
    assert alerts["groupIdentity"] == "canonicalCandidateId"
    assert alerts["freshness"] == {
        "timestampField": "oddsUpdatedAt",
        "ageField": "oddsAgeSeconds",
        "maximumOddsAgeSeconds": 900,
    }
    assert alerts["materialChange"]["minimumEdgeDeltaPct"] == 1.0
    assert alerts["materialChange"]["minimumAmericanPriceDelta"] == 10
    assert alerts["materialChange"]["supersedePreviousSnapshot"] is True
    assert alerts["failClosed"] is True


def test_phase_464_alerts_independently_enforce_odds_freshness():
    source = (ROOT / "static" / "product-hub.js").read_text(encoding="utf-8")

    for marker in (
        "MAX_ALERT_ODDS_AGE_SECONDS = 900",
        "function isAlertFresh(row)",
        "row.oddsAgeSeconds",
        "row.oddsUpdatedAt",
        "Date.parse",
        "age <= MAX_ALERT_ODDS_AGE_SECONDS",
        "thresholdMatches().filter(isAlertFresh)",
        "stale suppressed",
    ):
        assert marker in source


def test_phase_464_groups_snapshots_and_suppresses_immaterial_noise():
    source = (ROOT / "static" / "product-hub.js").read_text(encoding="utf-8")

    for marker in (
        "ALERT_CANDIDATE_STATE_KEY",
        "MATERIAL_EDGE_DELTA_PCT = 1",
        "MATERIAL_PRICE_DELTA = 10",
        "function materialMovement(previous, row)",
        "function reconcileAlert(row, now)",
        "previous.fingerprint === String(row.canonicalFingerprint)",
        "priorAlert.status = 'superseded'",
        "suppressed = true",
        "activeAlertId",
        "persistAlertState()",
    ):
        assert marker in source


def test_phase_464_alert_state_remains_bounded_and_device_private():
    source = (ROOT / "static" / "product-hub.js").read_text(encoding="utf-8")
    html = (ROOT / "product_hub.html").read_text(encoding="utf-8")

    assert "cleanAlertCandidates" in source
    assert "while (candidateKeys.length > ALERT_LEDGER_LIMIT)" in source
    assert "while (ledgerKeys.length > ALERT_LEDGER_LIMIT" in source
    assert "mlb_alert_candidate_state" in source
    assert "PHASE 4.64" in html
    assert "Fresh market movement" in html
    assert "private to this device" in html
