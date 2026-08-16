from pathlib import Path

from product_hub import (
    PRODUCT_HUB_VERSION,
    SAVED_PLAYER_DIGEST_VERSION,
    product_hub_bp,
)


ROOT = Path(__file__).resolve().parents[1]


def test_saved_player_digest_contract_is_device_private_and_fail_closed():
    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(product_hub_bp)
    with app.test_client() as client:
        payload = client.get("/api/product/journey").get_json()

    digest = payload["savedPlayerDigest"]
    assert payload["version"] == PRODUCT_HUB_VERSION == "4.64"
    assert digest["version"] == SAVED_PLAYER_DIGEST_VERSION == "4.70"
    assert digest["source"] == "/api/edges/today"
    assert digest["watchlistStorageKey"] == "mlb_watchlist"
    assert digest["requiresEvidenceReceiptVersion"] == "4.69"
    assert digest["states"] == [
        "loading",
        "verified_opportunity",
        "no_verified_opportunity",
        "unavailable",
    ]
    assert digest["oneTapSignalControls"] is True
    assert digest["serverPersistence"] is False
    assert digest["failClosed"] is True


def test_workspace_contains_saved_player_digest_accessibility_contract():
    html = (ROOT / "product_hub.html").read_text(encoding="utf-8")

    assert "PHASE 4.64 · MY HUB" in html
    assert "FEATURE 4.81" in html
    assert 'id="savedOpportunitySummary"' in html
    assert 'id="savedOpportunityList"' in html
    assert 'aria-live="polite"' in html
    assert "Only 4.69-receipted opportunities appear" in html


def test_digest_uses_only_receipted_actionable_rows_and_explicit_empty_states():
    source = (ROOT / "static" / "product-hub.js").read_text(encoding="utf-8")

    for marker in (
        "function savedPlayerRows(name)",
        "playerKey(row.player) === key && isActionable(row)",
        "function renderSavedOpportunityDigest()",
        "No verified opportunity right now.",
        "UNVERIFIED ROWS HIDDEN",
        "VERIFIED RECEIPT ",
        "No recommendation is shown without verified evidence.",
        "state.edgeState = String(payload.computationState || 'ready').toLowerCase()",
        "state.edgeState = 'unavailable'",
    ):
        assert marker in source


def test_signal_cards_offer_one_tap_device_private_save_controls():
    source = (ROOT / "static" / "product-hub.js").read_text(encoding="utf-8")

    assert "function persistWatchlist()" in source
    assert "function setPlayerSaved(name, saved)" in source
    assert "data-toggle-player=" in source
    assert "wireSignalActions();" in source
    assert "writeJson(WATCHLIST_KEY, Array.from(state.watchlist))" in source
    assert "renderSavedOpportunityDigest();" in source


def test_saved_player_digest_preserves_phone_touch_contract():
    css = (ROOT / "static" / "product-hub.css").read_text(encoding="utf-8")

    assert ".saved-opportunity-list" in css
    assert ".saved-opportunity.ready" in css
    assert ".saved-opportunity.quiet" in css
    assert ".signal-save" in css
    assert "@media(max-width:480px)" in css
    assert ".watchlist-form button,.signal-save,.saved-opportunity-track,.market-chip" in css
    assert "min-height:44px" in css


def test_phase_470_is_documented_as_active():
    roadmap = (ROOT / "docs" / "MLB_ANALYTICS_HUB_ROADMAP.md").read_text(
        encoding="utf-8"
    )

    assert "Phase 4.81 is the active phase." in roadmap
    assert "### Phase 4.70 — Saved-player verified opportunity digest" in roadmap
