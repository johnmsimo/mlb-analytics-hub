from pathlib import Path

from product_hub import (
    PRODUCT_HUB_VERSION,
    VERIFIED_DECISION_RESUME_REVALIDATION_VERSION,
    product_hub_bp,
)


ROOT = Path(__file__).resolve().parents[1]


def test_resume_revalidation_contract_preserves_verified_save_boundaries():
    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(product_hub_bp)
    with app.test_client() as client:
        payload = client.get("/api/product/journey").get_json()

    contract = payload["verifiedDecisionResumeRevalidation"]
    assert payload["version"] == PRODUCT_HUB_VERSION == "4.64"
    assert contract["version"] == VERIFIED_DECISION_RESUME_REVALIDATION_VERSION == "4.82"
    assert contract["sourceContractVersion"] == "4.81"
    assert contract["draftContractVersion"] == "4.71"
    assert contract["resumeEvents"] == ["visibilitychange", "pageshow"]
    assert contract["hiddenTimerPaused"] is True
    assert contract["visibleTimerRestarted"] is True
    assert contract["absoluteExpiryRechecked"] is True
    assert contract["saveDisabledWhenExpired"] is True
    assert contract["clientPostSuppressedWhenExpired"] is True
    assert contract["manualPicksUnaffected"] is True
    assert contract["serverMutationOnResume"] is False
    assert contract["requiresExplicitSave"] is True
    assert contract["saveRequiresAdminAuth"] is True
    assert contract["canonicalRevalidationOnSave"] is True
    assert contract["failClosed"] is True


def test_hidden_tracker_pauses_the_verified_draft_timer():
    tracker = (ROOT / "tracker.html").read_text(encoding="utf-8")
    handler = tracker.split("function handleVerifiedDraftVisibilityChange()", 1)[1].split(
        "function isVerifiedDecisionDraft(draft)", 1
    )[0]

    assert "if (!pendingVerifiedDecisionDraft) return;" in handler
    assert "document.visibilityState === 'hidden'" in handler
    assert "stopVerifiedDraftExpiryTimer()" in handler
    assert "return;" in handler
    assert "api(" not in handler
    assert "fetch(" not in handler


def test_visible_and_pageshow_transitions_reconcile_absolute_expiry():
    tracker = (ROOT / "tracker.html").read_text(encoding="utf-8")
    reconcile = tracker.split("function reconcileVerifiedDraftExpiryAfterResume()", 1)[1].split(
        "function handleVerifiedDraftVisibilityChange()", 1
    )[0]
    wire = tracker.split("function wire()", 1)[1].split(
        "// ── Boot", 1
    )[0]

    assert "startVerifiedDraftExpiryTimer()" in reconcile
    assert "document.visibilityState === 'visible'" in tracker
    assert "reconcileVerifiedDraftExpiryAfterResume()" in tracker
    assert "document.addEventListener('visibilitychange', handleVerifiedDraftVisibilityChange)" in wire
    assert "window.addEventListener('pageshow', reconcileVerifiedDraftExpiryAfterResume)" in wire
    assert "Date.parse(String(pendingVerifiedDecisionDraft.expiresAt" in tracker
    assert "Date.now() > expiresAt" in tracker


def test_resume_reconciliation_retains_expired_save_lock_and_post_guard():
    tracker = (ROOT / "tracker.html").read_text(encoding="utf-8")
    update = tracker.split("function updateVerifiedDraftExpiryState()", 1)[1].split(
        "function startVerifiedDraftExpiryTimer()", 1
    )[0]
    save = tracker.split("async function saveManualPick()", 1)[1].split(
        "async function saveParlay()", 1
    )[0]

    assert "$('saveManualPickBtn').disabled = expired" in update
    assert "api(" not in update
    assert "!isVerifiedDecisionDraft(pendingVerifiedDecisionDraft)" in save
    assert save.index("!isVerifiedDecisionDraft") < save.index("await api('/api/tracker/pick'")
    assert "pendingVerifiedDecisionDraft ? 'my_hub_verified_decision_draft' : 'manual'" in save


def test_phase_482_is_documented_as_active():
    html = (ROOT / "product_hub.html").read_text(encoding="utf-8")
    roadmap = (ROOT / "docs" / "MLB_ANALYTICS_HUB_ROADMAP.md").read_text(
        encoding="utf-8"
    )

    assert "FEATURE 4.82" in html
    assert "Phase 4.82 is the active phase." in roadmap
    assert "### Phase 4.82 — Mobile resume freshness reconciliation" in roadmap
    assert "iPhone and background-tab throttling gap" in roadmap
