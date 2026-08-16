from pathlib import Path

from product_hub import (
    PRODUCT_HUB_VERSION,
    VERIFIED_DECISION_PRE_SAVE_IDENTITY_VERSION,
    product_hub_bp,
)


ROOT = Path(__file__).resolve().parents[1]


def test_pre_save_identity_contract_preserves_tracker_boundaries():
    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(product_hub_bp)
    with app.test_client() as client:
        payload = client.get("/api/product/journey").get_json()

    contract = payload["verifiedDecisionPreSaveIdentity"]
    assert payload["version"] == PRODUCT_HUB_VERSION == "4.64"
    assert contract["version"] == VERIFIED_DECISION_PRE_SAVE_IDENTITY_VERSION == "4.85"
    assert contract["sourceContractVersion"] == "4.84"
    assert contract["draftContractVersion"] == "4.71"
    assert contract["storageReReadBeforePost"] is True
    assert contract["fullDraftIdentityMatchRequired"] is True
    assert contract["mismatchInvalidatesActiveReview"] is True
    assert contract["replacementPreservedOnMismatch"] is True
    assert contract["replacementRequiresExplicitReview"] is True
    assert contract["clientPostOnMismatch"] is False
    assert contract["serverMutationOnMismatch"] is False
    assert contract["manualPicksUnaffected"] is True
    assert contract["requiresExplicitSave"] is True
    assert contract["saveRequiresAdminAuth"] is True
    assert contract["canonicalRevalidationOnSave"] is True
    assert contract["failClosed"] is True


def test_draft_identity_is_complete_and_key_order_independent():
    tracker = (ROOT / "tracker.html").read_text(encoding="utf-8")
    identity = tracker.split("function verifiedDecisionDraftIdentity(draft)", 1)[1].split(
        "function setVerifiedDraftFieldsLocked(locked)", 1
    )[0]

    assert "Object.keys(draft).sort()" in identity
    assert ".map(key => [key, draft[key]])" in identity
    assert "JSON.stringify" in identity


def test_verified_save_rereads_storage_and_matches_identity_before_payload():
    tracker = (ROOT / "tracker.html").read_text(encoding="utf-8")
    save = tracker.split("async function saveManualPick()", 1)[1].split(
        "async function saveParlay()", 1
    )[0]

    assert "const current = readVerifiedDecisionDraft()" in save
    assert "verifiedDecisionDraftIdentity(current.draft)" in save
    assert "verifiedDecisionDraftIdentity(pendingVerifiedDecisionDraft)" in save
    assert save.index("const current = readVerifiedDecisionDraft()") < save.index("const payload = {")
    assert save.index("const payload = {") < save.index("await api('/api/tracker/pick'")


def test_mismatch_fails_closed_and_preserves_explicit_recovery():
    tracker = (ROOT / "tracker.html").read_text(encoding="utf-8")
    save = tracker.split("async function saveManualPick()", 1)[1].split(
        "const payload = {", 1
    )[0]

    assert "if (pendingVerifiedDecisionDraft)" in save
    assert "latestCrossTabDraftAvailable = Boolean(current.draft)" in save
    assert "invalidateVerifiedDecisionReviewFromStorage()" in save
    assert "return;" in save
    assert "localStorage.removeItem" not in save
    assert "api(" not in save
    assert "fetch(" not in save


def test_manual_pick_path_remains_outside_identity_guard():
    tracker = (ROOT / "tracker.html").read_text(encoding="utf-8")
    save = tracker.split("async function saveManualPick()", 1)[1].split(
        "async function saveParlay()", 1
    )[0]
    guard = save.split("if (pendingVerifiedDecisionDraft)", 1)[1].split(
        "const payload = {", 1
    )[0]

    assert "readVerifiedDecisionDraft()" in guard
    assert "invalidateVerifiedDecisionReviewFromStorage()" in guard
    assert "const payload = {" not in guard
    assert "source: pendingVerifiedDecisionDraft ? 'my_hub_verified_decision_draft' : 'manual'" in save


def test_phase_485_is_documented_as_active():
    html = (ROOT / "product_hub.html").read_text(encoding="utf-8")
    roadmap = (ROOT / "docs" / "MLB_ANALYTICS_HUB_ROADMAP.md").read_text(
        encoding="utf-8"
    )

    assert "FEATURE 4.86" in html
    assert "Phase 4.86 is the final 4.x bridge." in roadmap
    assert "### Phase 4.85 — Pre-save verified draft identity guard" in roadmap
    assert "only an exact current draft identity" in roadmap
