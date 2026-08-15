from pathlib import Path

from product_hub import (
    PRODUCT_HUB_VERSION,
    VERIFIED_DECISION_HANDOFF_VERSION,
    product_hub_bp,
)


ROOT = Path(__file__).resolve().parents[1]


def test_verified_decision_handoff_contract_is_explicit_and_fail_closed():
    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(product_hub_bp)
    with app.test_client() as client:
        payload = client.get("/api/product/journey").get_json()

    handoff = payload["verifiedDecisionHandoff"]
    assert payload["version"] == PRODUCT_HUB_VERSION == "4.64"
    assert handoff["version"] == VERIFIED_DECISION_HANDOFF_VERSION == "4.71"
    assert handoff["source"] == "saved_player_verified_opportunity"
    assert handoff["requiresEvidenceReceiptVersion"] == "4.69"
    assert handoff["storageKey"] == "mlb_verified_decision_draft_v471"
    assert handoff["destination"] == "/tracker"
    assert handoff["serverMutationOnPrepare"] is False
    assert handoff["requiresExplicitSave"] is True
    assert handoff["saveRequiresAdminAuth"] is True
    assert handoff["canonicalRevalidationOnSave"] is True
    assert handoff["expiresWithQuote"] is True
    assert handoff["failClosed"] is True


def test_workspace_only_prepares_drafts_from_current_receipted_rows():
    source = (ROOT / "static" / "product-hub.js").read_text(encoding="utf-8")

    for marker in (
        "function decisionDraftFrom(row)",
        "if (!isActionable(row)) return null;",
        "receiptAge + elapsedSeconds",
        "currentAge > MAX_ALERT_ODDS_AGE_SECONDS",
        "receiptVersion: receipt.contractVersion",
        "canonicalCandidateId: row.canonicalCandidateId",
        "canonicalFingerprint: row.canonicalFingerprint",
        "serverMutation: false",
        "function writeVerifiedDecisionDraft(draft)",
        "data-prepare-track=",
    ):
        assert marker in source


def test_workspace_handoff_is_device_private_and_never_posts():
    source = (ROOT / "static" / "product-hub.js").read_text(encoding="utf-8")

    assert "mlb_verified_decision_draft_v471" in source
    assert "window.localStorage.setItem(VERIFIED_DECISION_DRAFT_KEY" in source
    assert "window.location.assign('/tracker?decisionDraft=4.71')" in source
    handoff = source[source.index("function prepareDecisionDraft"):source.index("function savedOpportunityHtml")]
    assert "fetch(" not in handoff
    assert "/api/tracker/pick" not in handoff
    assert "no tracking draft was created" in handoff
    assert "no tracking action was taken" in handoff


def test_tracker_reviews_valid_draft_and_requires_explicit_save():
    tracker = (ROOT / "tracker.html").read_text(encoding="utf-8")

    for marker in (
        "function isVerifiedDecisionDraft(draft)",
        "draft.receiptVersion === '4.69'",
        "Date.now() <= expiresAt",
        "function applyVerifiedDecisionDraft()",
        "Review Verified Decision",
        "no pick has been created",
        "pendingVerifiedDecisionDraft ? 'my_hub_verified_decision_draft' : 'manual'",
        "canonicalCandidateId: pendingVerifiedDecisionDraft?.canonicalCandidateId",
        "await api('/api/tracker/pick'",
        "function discardVerifiedDecisionDraft()",
    ):
        assert marker in tracker

    assert tracker.index("applyVerifiedDecisionDraft()") < tracker.rindex("boot();")


def test_tracker_server_remains_admin_gated_and_canonically_revalidates():
    app_source = (ROOT / "app.py").read_text(encoding="utf-8")
    start = app_source.index("def api_tracker_pick():")
    end = app_source.index("@app.route('/api/tracker/pick/<pick_id>'", start)
    route = app_source[start:end]

    assert "denied = _check_admin_auth()" in route
    assert "_props_scan_today_payload(today)" in route
    assert "_evaluate_promotable_candidates" in route
    assert "row.get('canonicalCandidateId') == candidate_id" in route
    assert "Candidate does not pass the canonical betting" in route


def test_handoff_preserves_accessible_phone_review_controls():
    html = (ROOT / "product_hub.html").read_text(encoding="utf-8")
    css = (ROOT / "static" / "product-hub.css").read_text(encoding="utf-8")
    tracker = (ROOT / "tracker.html").read_text(encoding="utf-8")

    assert "FEATURE 4.78" in html
    assert "Review in Tracker prepares an expiring draft" in html
    assert ".saved-opportunity-track" in css
    assert ".watchlist-form button,.signal-save,.saved-opportunity-track,.market-chip" in css
    assert "min-height:44px" in css
    assert 'id="verifiedDraftNotice" hidden' in tracker
    assert "Saving explicitly revalidates the canonical candidate" in tracker


def test_phase_471_is_documented_as_active():
    roadmap = (ROOT / "docs" / "MLB_ANALYTICS_HUB_ROADMAP.md").read_text(
        encoding="utf-8"
    )

    assert "Phase 4.78 is the active phase." in roadmap
    assert "### Phase 4.71 — Verified decision handoff" in roadmap
