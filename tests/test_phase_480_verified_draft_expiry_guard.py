from pathlib import Path

from product_hub import (
    PRODUCT_HUB_VERSION,
    VERIFIED_DECISION_REVIEW_FRESHNESS_VERSION,
    product_hub_bp,
)


ROOT = Path(__file__).resolve().parents[1]


def test_verified_review_freshness_contract_preserves_server_boundaries():
    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(product_hub_bp)
    with app.test_client() as client:
        payload = client.get("/api/product/journey").get_json()

    contract = payload["verifiedDecisionReviewFreshness"]
    assert payload["version"] == PRODUCT_HUB_VERSION == "4.64"
    assert contract["version"] == VERIFIED_DECISION_REVIEW_FRESHNESS_VERSION == "4.80"
    assert contract["sourceContractVersion"] == "4.79"
    assert contract["draftContractVersion"] == "4.71"
    assert contract["freshnessField"] == "expiresAt"
    assert contract["displayedBeforeSave"] is True
    assert contract["revalidatedOnSaveAttempt"] is True
    assert contract["clientPostSuppressedWhenExpired"] is True
    assert contract["serverMutationOnExpiry"] is False
    assert contract["recommendationChanged"] is False
    assert contract["requiresExplicitSave"] is True
    assert contract["saveRequiresAdminAuth"] is True
    assert contract["canonicalRevalidationOnSave"] is True
    assert contract["failClosed"] is True


def test_tracker_displays_quote_expiry_in_verified_review_notice():
    tracker = (ROOT / "tracker.html").read_text(encoding="utf-8")
    apply = tracker.split("function applyVerifiedDecisionDraft()", 1)[1].split(
        "function openManualPick()", 1
    )[0]

    assert 'id="verifiedDraftExpiry"' in tracker
    assert "Quote expiry unavailable" in tracker
    assert "function verifiedDecisionExpiryLabel(expiresAt)" in tracker
    assert "'Quote valid until ' + expiry.toLocaleTimeString" in tracker
    assert "verifiedDecisionExpiryLabel(draft.expiresAt)" in apply
    assert apply.index("verifiedDraftExpiry") < apply.index("openManualPick()")
    assert ".verified-draft-notice .verified-draft-expiry" in tracker


def test_verified_draft_is_revalidated_before_payload_or_post():
    tracker = (ROOT / "tracker.html").read_text(encoding="utf-8")
    save = tracker.split("async function saveManualPick()", 1)[1].split(
        "async function saveParlay()", 1
    )[0]

    guard = "!isVerifiedDecisionDraft(pendingVerifiedDecisionDraft)"
    assert guard in save
    assert "clearVerifiedDecisionDraft(true)" in save
    assert "Prepared My Hub draft expired during review; no pick was created." in save
    assert save.index(guard) < save.index("const payload")
    assert save.index(guard) < save.index("await api('/api/tracker/pick'")
    assert "return;" in save.split("const payload", 1)[0]


def test_expiry_guard_leaves_manual_pick_and_server_revalidation_contracts_intact():
    tracker = (ROOT / "tracker.html").read_text(encoding="utf-8")
    save = tracker.split("async function saveManualPick()", 1)[1].split(
        "async function saveParlay()", 1
    )[0]

    assert "if (pendingVerifiedDecisionDraft &&" in save
    assert "pendingVerifiedDecisionDraft ? 'my_hub_verified_decision_draft' : 'manual'" in save
    assert "canonicalCandidateId: pendingVerifiedDecisionDraft?.canonicalCandidateId" in save
    assert "evidenceReceiptVersion: pendingVerifiedDecisionDraft?.receiptVersion" in save
    assert "await api('/api/tracker/pick'" in save
    assert "preparedFrom:" not in save


def test_phase_480_is_documented_as_active():
    html = (ROOT / "product_hub.html").read_text(encoding="utf-8")
    roadmap = (ROOT / "docs" / "MLB_ANALYTICS_HUB_ROADMAP.md").read_text(
        encoding="utf-8"
    )

    assert "FEATURE 4.80" in html
    assert "Phase 4.80 is the active phase." in roadmap
    assert "### Phase 4.80 — Verified draft expiry guard" in roadmap
    assert "suppresses the POST" in roadmap
