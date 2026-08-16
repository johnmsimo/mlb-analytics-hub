from pathlib import Path

from product_hub import (
    PRODUCT_HUB_VERSION,
    VERIFIED_DECISION_CROSS_TAB_INVALIDATION_VERSION,
    product_hub_bp,
)


ROOT = Path(__file__).resolve().parents[1]


def test_cross_tab_invalidation_contract_preserves_explicit_save_boundary():
    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(product_hub_bp)
    with app.test_client() as client:
        payload = client.get("/api/product/journey").get_json()

    contract = payload["verifiedDecisionCrossTabInvalidation"]
    assert payload["version"] == PRODUCT_HUB_VERSION == "4.64"
    assert contract["version"] == VERIFIED_DECISION_CROSS_TAB_INVALIDATION_VERSION == "4.83"
    assert contract["sourceContractVersion"] == "4.82"
    assert contract["draftContractVersion"] == "4.71"
    assert contract["storageEvent"] == "storage"
    assert contract["storageKey"] == "mlb_verified_decision_draft_v471"
    assert contract["replacementInvalidatesActiveReview"] is True
    assert contract["removalInvalidatesActiveReview"] is True
    assert contract["clearAllInvalidatesActiveReview"] is True
    assert contract["replacementPreserved"] is True
    assert contract["newDraftAutoOpened"] is False
    assert contract["clientPostOnInvalidation"] is False
    assert contract["serverMutationOnInvalidation"] is False
    assert contract["manualPicksUnaffected"] is True
    assert contract["requiresExplicitSave"] is True
    assert contract["saveRequiresAdminAuth"] is True
    assert contract["canonicalRevalidationOnSave"] is True
    assert contract["failClosed"] is True


def test_storage_change_only_invalidates_an_active_verified_review():
    tracker = (ROOT / "tracker.html").read_text(encoding="utf-8")
    handler = tracker.split("function handleVerifiedDecisionDraftStorage(event)", 1)[1].split(
        "function isVerifiedDecisionDraft(draft)", 1
    )[0]

    assert "if (!pendingVerifiedDecisionDraft || !event) return;" in handler
    assert "event.key !== VERIFIED_DECISION_DRAFT_KEY && event.key !== null" in handler
    assert "invalidateVerifiedDecisionReviewFromStorage()" in handler
    assert "api(" not in handler
    assert "fetch(" not in handler


def test_cross_tab_invalidation_closes_review_without_deleting_replacement():
    tracker = (ROOT / "tracker.html").read_text(encoding="utf-8")
    invalidate = tracker.split("function invalidateVerifiedDecisionReviewFromStorage()", 1)[1].split(
        "function handleVerifiedDecisionDraftStorage(event)", 1
    )[0]

    assert "stopVerifiedDraftExpiryTimer()" in invalidate
    assert "pendingVerifiedDecisionDraft = null" in invalidate
    assert "setVerifiedDraftFieldsLocked(false)" in invalidate
    assert "$('verifiedDraftNotice').hidden = true" in invalidate
    assert "$('saveManualPickBtn').disabled = false" in invalidate
    assert "closeManualPick()" in invalidate
    assert "changed in another tab; review closed and no pick was created" in invalidate
    assert "localStorage.removeItem" not in invalidate
    assert "api(" not in invalidate


def test_storage_event_is_wired_once_and_manual_entry_remains_available():
    tracker = (ROOT / "tracker.html").read_text(encoding="utf-8")
    wire = tracker.split("function wire()", 1)[1].split(
        "// ── Boot", 1
    )[0]

    assert "window.addEventListener('storage', handleVerifiedDecisionDraftStorage)" in wire
    assert tracker.count("window.addEventListener('storage', handleVerifiedDecisionDraftStorage)") == 1
    assert "pendingVerifiedDecisionDraft ? 'my_hub_verified_decision_draft' : 'manual'" in tracker
    assert "'Use this for books or markets you found outside the research board.'" in tracker


def test_save_time_guard_remains_before_tracker_post():
    tracker = (ROOT / "tracker.html").read_text(encoding="utf-8")
    save = tracker.split("async function saveManualPick()", 1)[1].split(
        "async function saveParlay()", 1
    )[0]

    guard = "!isVerifiedDecisionDraft(pendingVerifiedDecisionDraft)"
    assert guard in save
    assert save.index(guard) < save.index("const payload")
    assert save.index(guard) < save.index("await api('/api/tracker/pick'")


def test_phase_483_is_documented_as_active():
    html = (ROOT / "product_hub.html").read_text(encoding="utf-8")
    roadmap = (ROOT / "docs" / "MLB_ANALYTICS_HUB_ROADMAP.md").read_text(
        encoding="utf-8"
    )

    assert "FEATURE 4.84" in html
    assert "Phase 4.84 is the active phase." in roadmap
    assert "### Phase 4.83 — Cross-tab verified draft invalidation" in roadmap
    assert "does not delete a replacement written by another tab" in roadmap
