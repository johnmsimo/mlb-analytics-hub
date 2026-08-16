from pathlib import Path

from product_hub import (
    PRODUCT_HUB_VERSION,
    VERIFIED_DECISION_LIVE_EXPIRY_VERSION,
    product_hub_bp,
)


ROOT = Path(__file__).resolve().parents[1]


def test_live_expiry_contract_preserves_verified_save_boundaries():
    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(product_hub_bp)
    with app.test_client() as client:
        payload = client.get("/api/product/journey").get_json()

    contract = payload["verifiedDecisionLiveExpiry"]
    assert payload["version"] == PRODUCT_HUB_VERSION == "4.64"
    assert contract["version"] == VERIFIED_DECISION_LIVE_EXPIRY_VERSION == "4.81"
    assert contract["sourceContractVersion"] == "4.80"
    assert contract["draftContractVersion"] == "4.71"
    assert contract["freshnessStates"] == ["fresh", "expired"]
    assert contract["countdownIntervalMilliseconds"] == 1000
    assert contract["visibleCountdown"] is True
    assert contract["accessibleStateAnnouncement"] is True
    assert contract["saveDisabledWhenExpired"] is True
    assert contract["clientPostSuppressedWhenExpired"] is True
    assert contract["manualPicksUnaffected"] is True
    assert contract["serverMutationOnExpiry"] is False
    assert contract["requiresExplicitSave"] is True
    assert contract["saveRequiresAdminAuth"] is True
    assert contract["canonicalRevalidationOnSave"] is True
    assert contract["failClosed"] is True


def test_verified_review_exposes_live_accessible_freshness_state():
    tracker = (ROOT / "tracker.html").read_text(encoding="utf-8")

    assert 'data-freshness-state="idle"' in tracker
    assert 'id="verifiedDraftExpiry" aria-hidden="true"' in tracker
    assert 'id="verifiedDraftFreshnessStatus" role="status" aria-live="polite"' in tracker
    assert "remainingSeconds" in tracker
    assert "'m ' + seconds + 's remaining'" in tracker
    assert "freshnessState = expired ? 'expired' : 'fresh'" in tracker
    assert '.verified-draft-notice[data-freshness-state="expired"]' in tracker


def test_live_timer_disables_save_at_expiry_without_posting():
    tracker = (ROOT / "tracker.html").read_text(encoding="utf-8")
    update = tracker.split("function updateVerifiedDraftExpiryState()", 1)[1].split(
        "function startVerifiedDraftExpiryTimer()", 1
    )[0]

    assert "Date.now() > expiresAt" in update
    assert "$('saveManualPickBtn').disabled = expired" in update
    assert "stopVerifiedDraftExpiryTimer()" in update
    assert "Save is disabled and no pick was created." in update
    assert "api(" not in update
    assert "fetch(" not in update


def test_timer_starts_once_per_second_and_cleanup_restores_manual_controls():
    tracker = (ROOT / "tracker.html").read_text(encoding="utf-8")
    start = tracker.split("function startVerifiedDraftExpiryTimer()", 1)[1].split(
        "function isVerifiedDecisionDraft(draft)", 1
    )[0]
    clear = tracker.split("function clearVerifiedDecisionDraft(clearFields=false)", 1)[1].split(
        "function discardVerifiedDecisionDraft()", 1
    )[0]

    assert "stopVerifiedDraftExpiryTimer()" in start
    assert "window.setInterval(updateVerifiedDraftExpiryState, 1000)" in start
    assert "isVerifiedDecisionDraft(pendingVerifiedDecisionDraft)" in start
    assert "stopVerifiedDraftExpiryTimer()" in clear
    assert "$('saveManualPickBtn').disabled = false" in clear
    assert "freshnessState = 'idle'" in clear


def test_save_time_guard_remains_final_backstop_before_tracker_post():
    tracker = (ROOT / "tracker.html").read_text(encoding="utf-8")
    save = tracker.split("async function saveManualPick()", 1)[1].split(
        "async function saveParlay()", 1
    )[0]

    guard = "!isVerifiedDecisionDraft(pendingVerifiedDecisionDraft)"
    assert guard in save
    assert save.index(guard) < save.index("const payload")
    assert save.index(guard) < save.index("await api('/api/tracker/pick'")
    assert "pendingVerifiedDecisionDraft ? 'my_hub_verified_decision_draft' : 'manual'" in save


def test_phase_481_is_documented_as_active():
    html = (ROOT / "product_hub.html").read_text(encoding="utf-8")
    roadmap = (ROOT / "docs" / "MLB_ANALYTICS_HUB_ROADMAP.md").read_text(
        encoding="utf-8"
    )

    assert "FEATURE 4.86" in html
    assert "Phase 5.0 is the active phase." in roadmap
    assert "### Phase 4.81 — Live verified draft expiry state" in roadmap
    assert "assistive technology receives state-change announcements" in roadmap
