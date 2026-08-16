from pathlib import Path

from product_hub import (
    PRODUCT_HUB_VERSION,
    VERIFIED_DECISION_ORIGIN_PROVENANCE_VERSION,
    product_hub_bp,
)


ROOT = Path(__file__).resolve().parents[1]


def test_decision_origin_provenance_contract_is_explanatory_and_fail_closed():
    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(product_hub_bp)
    with app.test_client() as client:
        payload = client.get("/api/product/journey").get_json()

    contract = payload["verifiedDecisionOriginProvenance"]
    assert payload["version"] == PRODUCT_HUB_VERSION == "4.64"
    assert contract["version"] == VERIFIED_DECISION_ORIGIN_PROVENANCE_VERSION == "4.79"
    assert contract["sourceContractVersion"] == "4.78"
    assert contract["draftContractVersion"] == "4.71"
    assert contract["allowedOrigins"] == ["saved_player_digest", "eligible_alert"]
    assert contract["requiresOrigin"] is True
    assert contract["displayedBeforeSave"] is True
    assert contract["originAffectsRecommendation"] is False
    assert contract["originAffectsAuthorization"] is False
    assert contract["originAffectsCanonicalRevalidation"] is False
    assert contract["serverMutationOnReview"] is False
    assert contract["requiresExplicitSave"] is True
    assert contract["failClosed"] is True


def test_tracker_requires_an_exact_known_origin_before_accepting_draft():
    tracker = (ROOT / "tracker.html").read_text(encoding="utf-8")
    validation = tracker.split("function isVerifiedDecisionDraft(draft)", 1)[1].split(
        "function readVerifiedDecisionDraft()", 1
    )[0]

    assert "VERIFIED_DECISION_DRAFT_ORIGINS = Object.freeze" in tracker
    assert "saved_player_digest: 'Saved player digest'" in tracker
    assert "eligible_alert: 'Eligible alert'" in tracker
    assert "Boolean(verifiedDecisionOriginLabel(draft.preparedFrom))" in validation
    assert "Date.now() <= expiresAt" in validation
    assert "localStorage.removeItem(VERIFIED_DECISION_DRAFT_KEY)" in tracker


def test_tracker_displays_origin_in_review_notice_before_explicit_save():
    tracker = (ROOT / "tracker.html").read_text(encoding="utf-8")
    apply = tracker.split("function applyVerifiedDecisionDraft()", 1)[1].split(
        "function openManualPick()", 1
    )[0]

    assert 'id="verifiedDraftOrigin"' in tracker
    assert "Draft source unavailable" in tracker
    assert "'Draft source: ' + verifiedDecisionOriginLabel(draft.preparedFrom)" in apply
    assert apply.index("verifiedDraftOrigin") < apply.index("openManualPick()")
    assert "Review the canonical evidence, choose a stake, then explicitly save." in apply
    assert ".verified-draft-notice .verified-draft-origin" in tracker


def test_origin_provenance_does_not_change_save_authority_or_server_payload():
    tracker = (ROOT / "tracker.html").read_text(encoding="utf-8")
    save = tracker.split("async function saveManualPick()", 1)[1].split(
        "async function saveParlay()", 1
    )[0]

    assert "pendingVerifiedDecisionDraft ? 'my_hub_verified_decision_draft' : 'manual'" in save
    assert "canonicalCandidateId: pendingVerifiedDecisionDraft?.canonicalCandidateId" in save
    assert "await api('/api/tracker/pick'" in save
    assert "preparedFrom:" not in save
    assert "verifiedDecisionOriginLabel" not in save


def test_both_my_hub_handoff_paths_set_the_device_local_origin():
    source = (ROOT / "static" / "product-hub.js").read_text(encoding="utf-8")
    prepare = source.split("function prepareDecisionDraft(candidateId, origin)", 1)[1].split(
        "function savedOpportunityHtml(name)", 1
    )[0]

    assert "origin === 'eligible_alert' ? 'eligible_alert' : 'saved_player_digest'" in prepare
    assert "draft.preparedFrom = draftOrigin;" in prepare
    assert "writeVerifiedDecisionDraft(draft)" in prepare
    assert "fetch(" not in prepare


def test_phase_479_is_documented_as_active():
    html = (ROOT / "product_hub.html").read_text(encoding="utf-8")
    roadmap = (ROOT / "docs" / "MLB_ANALYTICS_HUB_ROADMAP.md").read_text(
        encoding="utf-8"
    )

    assert "FEATURE 4.81" in html
    assert "Phase 4.81 is the active phase." in roadmap
    assert "### Phase 4.79 — Decision draft origin provenance" in roadmap
    assert "existing admin authorization, canonical" in roadmap
