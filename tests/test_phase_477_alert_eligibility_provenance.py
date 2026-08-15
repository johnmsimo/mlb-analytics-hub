from pathlib import Path

from product_hub import (
    ALERT_ELIGIBILITY_PROVENANCE_VERSION,
    PRODUCT_HUB_VERSION,
    product_hub_bp,
)


ROOT = Path(__file__).resolve().parents[1]


def test_alert_provenance_contract_is_explanatory_and_fail_closed():
    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(product_hub_bp)
    with app.test_client() as client:
        payload = client.get("/api/product/journey").get_json()

    contract = payload["alertEligibilityProvenance"]
    assert payload["version"] == PRODUCT_HUB_VERSION == "4.64"
    assert contract["version"] == ALERT_ELIGIBILITY_PROVENANCE_VERSION == "4.77"
    assert contract["sourceContractVersion"] == "4.76"
    assert contract["reasonOrder"] == [
        "preferred_market",
        "threshold_match",
        "fresh_quote",
        "eligible_event",
    ]
    assert contract["eligibleEventKinds"] == [
        "new_opportunity",
        "edge_up",
        "edge_down",
        "price_move",
    ]
    assert contract["requiresActionable"] is True
    assert contract["requiresActiveLedgerState"] is True
    assert contract["provenanceOnly"] is True
    assert contract["eligibilityChanged"] is False
    assert contract["ledgerMutation"] is False
    assert contract["learningPerformanceUsed"] is False
    assert contract["recommendation"] is False
    assert contract["serverPersistence"] is False
    assert contract["failClosed"] is True


def test_alert_reasons_reuse_canonical_eligibility_checks():
    source = (ROOT / "static" / "product-hub.js").read_text(encoding="utf-8")
    reasons = source.split("function alertEligibilityReasons(row, ledger)", 1)[1].split(
        "function alertExplanation(row, ledger)", 1
    )[0]

    for marker in (
        "isActionable(row)",
        "isSupportedMarketKey(marketKey)",
        "state.preferred.has(marketKeyOf(row))",
        "edge < state.threshold",
        "isAlertFresh(row)",
        "activeStates.indexOf(ledger.status)",
        "validKinds.indexOf(ledger.kind)",
        "return [];",
    ):
        assert marker in reasons


def test_alert_reason_order_and_event_labels_are_stable():
    source = (ROOT / "static" / "product-hub.js").read_text(encoding="utf-8")
    reasons = source.split("function alertEligibilityReasons(row, ledger)", 1)[1].split(
        "function alertExplanation(row, ledger)", 1
    )[0]

    ordered = [
        reasons.index("key: 'preferred_market'"),
        reasons.index("key: 'threshold_match'"),
        reasons.index("key: 'fresh_quote'"),
        reasons.index("key: 'eligible_event'"),
    ]
    assert ordered == sorted(ordered)
    assert "Threshold ' + state.threshold + '%+'" in reasons
    assert "Fresh quote ≤15m" in reasons
    assert "New opportunity" in reasons
    assert "Material change" in reasons


def test_alert_cards_expose_accessible_machine_readable_provenance():
    html = (ROOT / "product_hub.html").read_text(encoding="utf-8")
    source = (ROOT / "static" / "product-hub.js").read_text(encoding="utf-8")
    css = (ROOT / "static" / "product-hub.css").read_text(encoding="utf-8")

    assert "FEATURE 4.80" in html
    assert "data-alert-provenance" in source
    assert "data-alert-provenance-reason" in source
    assert "Alert eligibility reasons:" in source
    assert "<strong>Alert because</strong>" in source
    assert ".alert-provenance" in css
    assert '[data-alert-provenance-reason="eligible_event"]' in css


def test_alert_rendering_fails_closed_without_complete_reasons():
    source = (ROOT / "static" / "product-hub.js").read_text(encoding="utf-8")
    alert = source.split("function alertHtml(record)", 1)[1].split(
        "function renderAlerts()", 1
    )[0]
    render = source.split("function renderAlerts()", 1)[1].split(
        "function activeAlertFor(row)", 1
    )[0]

    assert "var reasons = alertEligibilityReasons(row, ledger);" in alert
    assert "if (!reasons.length) return '';" in alert
    assert "reasonKeys.join(',')" in alert
    assert "reasonLabels.join(', ')" in alert
    assert ".map(alertHtml).filter(Boolean)" in render
    assert "if (!cards.length)" in render


def test_alert_provenance_does_not_mutate_ledger_or_use_learning_performance():
    source = (ROOT / "static" / "product-hub.js").read_text(encoding="utf-8")
    reasons = source.split("function alertEligibilityReasons(row, ledger)", 1)[1].split(
        "function alertExplanation(row, ledger)", 1
    )[0]

    for forbidden in (
        "state.alertLedger[",
        "persistAlertState",
        "tracker",
        "learning",
        "performance",
        "roi",
        "clv",
        "winRate",
        "fetch(",
    ):
        assert forbidden not in reasons


def test_phase_477_is_documented_as_active():
    roadmap = (ROOT / "docs" / "MLB_ANALYTICS_HUB_ROADMAP.md").read_text(
        encoding="utf-8"
    )

    assert "Phase 4.80 is the active phase." in roadmap
    assert "### Phase 4.77 — Alert eligibility provenance" in roadmap
    assert "Provenance is explanatory only." in roadmap
