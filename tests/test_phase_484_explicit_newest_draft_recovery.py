from pathlib import Path

from product_hub import (
    PRODUCT_HUB_VERSION,
    VERIFIED_DECISION_EXPLICIT_RECOVERY_VERSION,
    product_hub_bp,
)


ROOT = Path(__file__).resolve().parents[1]


def test_explicit_recovery_contract_preserves_tracker_save_boundary():
    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(product_hub_bp)
    with app.test_client() as client:
        payload = client.get("/api/product/journey").get_json()

    contract = payload["verifiedDecisionExplicitRecovery"]
    assert payload["version"] == PRODUCT_HUB_VERSION == "4.64"
    assert contract["version"] == VERIFIED_DECISION_EXPLICIT_RECOVERY_VERSION == "4.84"
    assert contract["sourceContractVersion"] == "4.83"
    assert contract["draftContractVersion"] == "4.71"
    assert contract["controlLabel"] == "Review newest draft"
    assert contract["minimumTouchTargetPixels"] == 44
    assert contract["replacementValidatedBeforeOffer"] is True
    assert contract["storageReReadOnTap"] is True
    assert contract["explicitUserActionRequired"] is True
    assert contract["newDraftAutoOpened"] is False
    assert contract["clientPostOnReview"] is False
    assert contract["serverMutationOnReview"] is False
    assert contract["manualPicksUnaffected"] is True
    assert contract["requiresExplicitSave"] is True
    assert contract["saveRequiresAdminAuth"] is True
    assert contract["canonicalRevalidationOnSave"] is True
    assert contract["failClosed"] is True


def test_recovery_control_is_accessible_and_phone_sized():
    tracker = (ROOT / "tracker.html").read_text(encoding="utf-8")

    assert 'id="reviewLatestDraftBtn" type="button" hidden' in tracker
    assert ">Review newest draft</button>" in tracker
    assert ".review-latest-draft{min-height:44px" in tracker
    assert ".review-latest-draft[hidden]{display:none}" in tracker


def test_only_a_valid_replacement_offers_explicit_recovery():
    tracker = (ROOT / "tracker.html").read_text(encoding="utf-8")
    handler = tracker.split("function handleVerifiedDecisionDraftStorage(event)", 1)[1].split(
        "function reviewLatestVerifiedDecisionDraft()", 1
    )[0]
    invalidate = tracker.split("function invalidateVerifiedDecisionReviewFromStorage()", 1)[1].split(
        "function handleVerifiedDecisionDraftStorage(event)", 1
    )[0]

    assert "latestCrossTabDraftAvailable = false" in handler
    assert "typeof event.newValue === 'string'" in handler
    assert "isVerifiedDecisionDraft(JSON.parse(event.newValue))" in handler
    assert "$('reviewLatestDraftBtn').hidden = !latestCrossTabDraftAvailable" in invalidate
    assert "localStorage.removeItem" not in invalidate
    assert "api(" not in handler
    assert "fetch(" not in handler


def test_recovery_tap_rereads_storage_and_uses_normal_review_path():
    tracker = (ROOT / "tracker.html").read_text(encoding="utf-8")
    review = tracker.split("function reviewLatestVerifiedDecisionDraft()", 1)[1].split(
        "function isVerifiedDecisionDraft(draft)", 1
    )[0]
    wire = tracker.split("function wire()", 1)[1].split(
        "// ── Boot", 1
    )[0]

    assert "const result = readVerifiedDecisionDraft()" in review
    assert "if (!result.present || !result.draft)" in review
    assert "no longer available; no pick was created" in review
    assert "applyVerifiedDecisionDraft()" in review
    assert "api(" not in review
    assert "fetch(" not in review
    assert "['reviewLatestDraftBtn', reviewLatestVerifiedDecisionDraft]" in wire


def test_cleanup_hides_stale_recovery_and_save_guard_remains():
    tracker = (ROOT / "tracker.html").read_text(encoding="utf-8")
    clear = tracker.split("function clearVerifiedDecisionDraft(clearFields=false)", 1)[1].split(
        "function discardVerifiedDecisionDraft()", 1
    )[0]
    save = tracker.split("async function saveManualPick()", 1)[1].split(
        "async function saveParlay()", 1
    )[0]

    assert "latestCrossTabDraftAvailable = false" in clear
    assert "$('reviewLatestDraftBtn').hidden = true" in clear
    assert "!isVerifiedDecisionDraft(pendingVerifiedDecisionDraft)" in save
    assert save.index("!isVerifiedDecisionDraft") < save.index("await api('/api/tracker/pick'")


def test_phase_484_is_documented_as_active():
    html = (ROOT / "product_hub.html").read_text(encoding="utf-8")
    roadmap = (ROOT / "docs" / "MLB_ANALYTICS_HUB_ROADMAP.md").read_text(
        encoding="utf-8"
    )

    assert "FEATURE 4.84" in html
    assert "Phase 4.84 is the active phase." in roadmap
    assert "### Phase 4.84 — Explicit newest-draft recovery" in roadmap
    assert "never auto-opens a draft" in roadmap
