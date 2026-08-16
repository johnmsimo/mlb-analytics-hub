from pathlib import Path

from product_hub import (
    PRODUCT_HUB_VERSION,
    VERIFIED_ALERT_REVIEW_HANDOFF_VERSION,
    product_hub_bp,
)


ROOT = Path(__file__).resolve().parents[1]


def test_alert_review_handoff_contract_reuses_verified_draft_boundary():
    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(product_hub_bp)
    with app.test_client() as client:
        payload = client.get("/api/product/journey").get_json()

    contract = payload["verifiedAlertReviewHandoff"]
    assert payload["version"] == PRODUCT_HUB_VERSION == "4.64"
    assert contract["version"] == VERIFIED_ALERT_REVIEW_HANDOFF_VERSION == "4.78"
    assert contract["sourceContractVersion"] == "4.77"
    assert contract["draftContractVersion"] == "4.71"
    assert contract["source"] == "eligible_alert"
    assert contract["destination"] == "/tracker"
    assert contract["storageKey"] == "mlb_verified_decision_draft_v471"
    assert contract["requiresAlertProvenance"] is True
    assert contract["requiresActionable"] is True
    assert contract["requiresFreshQuote"] is True
    assert contract["requiresActiveLedgerState"] is True
    assert contract["explicitUserActionRequired"] is True
    assert contract["expiresWithQuote"] is True
    assert contract["serverMutationOnPrepare"] is False
    assert contract["ledgerMutationOnPrepare"] is False
    assert contract["requiresExplicitSave"] is True
    assert contract["saveRequiresAdminAuth"] is True
    assert contract["canonicalRevalidationOnSave"] is True
    assert contract["failClosed"] is True


def test_eligible_alert_cards_expose_explicit_tracker_review_control():
    html = (ROOT / "product_hub.html").read_text(encoding="utf-8")
    source = (ROOT / "static" / "product-hub.js").read_text(encoding="utf-8")
    css = (ROOT / "static" / "product-hub.css").read_text(encoding="utf-8")

    assert "FEATURE 4.85" in html
    assert 'class="alert-review"' in source
    assert "data-prepare-alert-track" in source
    assert ">Review in Tracker</button>" in source
    assert ".alert-actions .alert-review" in css
    assert ".alert-actions button" in css
    assert "min-height:44px" in css


def test_alert_review_revalidates_provenance_before_preparing_draft():
    source = (ROOT / "static" / "product-hub.js").read_text(encoding="utf-8")
    handoff = source.split("function prepareAlertDecisionDraft(candidateId, alertId)", 1)[1].split(
        "function activeAlertFor(row)", 1
    )[0]

    assert "state.edges.find" in handoff
    assert "state.alertLedger[String(alertId || '')]" in handoff
    assert "alertEligibilityReasons(row, ledger).length" in handoff
    assert "no tracking draft was created." in handoff
    assert "prepareDecisionDraft(candidateId, 'eligible_alert');" in handoff
    assert "persistAlertState" not in handoff
    assert "fetch(" not in handoff


def test_alert_prepare_uses_existing_expiring_device_local_draft():
    source = (ROOT / "static" / "product-hub.js").read_text(encoding="utf-8")
    prepare = source.split("function prepareDecisionDraft(candidateId, origin)", 1)[1].split(
        "function savedOpportunityHtml(name)", 1
    )[0]

    assert "draftOrigin = origin === 'eligible_alert'" in prepare
    assert "draft.preparedFrom = draftOrigin;" in prepare
    assert "writeVerifiedDecisionDraft(draft)" in prepare
    assert "window.location.assign('/tracker?decisionDraft=4.71')" in prepare
    assert "fetch(" not in prepare
    assert "/api/tracker/pick" not in prepare


def test_alert_review_requires_explicit_click_and_preserves_lifecycle_actions():
    source = (ROOT / "static" / "product-hub.js").read_text(encoding="utf-8")
    wire = source.split("function wireAlertInbox()", 1)[1].split(
        "function marketEntries(payload)", 1
    )[0]

    assert "event.target.closest('[data-prepare-alert-track]')" in wire
    assert "prepareAlertDecisionDraft(" in wire
    assert "event.target.closest('[data-alert-action]')" in wire
    assert "item.status = button.getAttribute('data-alert-action')" in wire
    assert wire.index("if (review)") < wire.index("var button")


def test_missing_or_expired_alerts_fail_closed_without_server_or_ledger_write():
    source = (ROOT / "static" / "product-hub.js").read_text(encoding="utf-8")

    assert "if (!row || !ledger) return [];" in source
    assert "currentAge > MAX_ALERT_ODDS_AGE_SECONDS" in source
    assert "That alert is no longer eligible" in source
    assert "ledgerMutationOnPrepare" not in source


def test_phase_478_is_documented_as_active():
    roadmap = (ROOT / "docs" / "MLB_ANALYTICS_HUB_ROADMAP.md").read_text(
        encoding="utf-8"
    )

    assert "Phase 4.85 is the active phase." in roadmap
    assert "### Phase 4.78 — Verified alert review handoff" in roadmap
    assert "Tracker remains the only explicit server-save boundary." in roadmap
