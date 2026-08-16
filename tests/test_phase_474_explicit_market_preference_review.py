from pathlib import Path

from product_hub import (
    PRODUCT_HUB_VERSION,
    VERIFIED_DECISION_MARKET_PREFERENCE_REVIEW_VERSION,
    product_hub_bp,
)


ROOT = Path(__file__).resolve().parents[1]


def test_preference_review_contract_requires_explicit_device_local_action():
    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(product_hub_bp)
    with app.test_client() as client:
        payload = client.get("/api/product/journey").get_json()

    contract = payload["verifiedDecisionMarketPreferenceReview"]
    assert payload["version"] == PRODUCT_HUB_VERSION == "4.64"
    assert contract["version"] == VERIFIED_DECISION_MARKET_PREFERENCE_REVIEW_VERSION == "4.74"
    assert contract["sourceContractVersion"] == "4.73"
    assert contract["storageKey"] == "mlb_market_preferences"
    assert contract["explicitUserActionRequired"] is True
    assert contract["deviceLocal"] is True
    assert contract["serverPersistence"] is False
    assert contract["automaticPreferenceMutation"] is False
    assert contract["rankingEnabled"] is False
    assert contract["recommendation"] is False
    assert contract["requiresRepresentedCanonicalMarket"] is True
    assert contract["syncsDiscoverPreferences"] is True
    assert contract["failClosed"] is True


def test_market_learning_preferences_are_user_initiated_and_share_one_store():
    source = (ROOT / "static" / "product-hub.js").read_text(encoding="utf-8")

    assert "var MARKET_KEY = 'mlb_market_preferences';" in source
    assert "function wireMarketLearningActions()" in source
    assert "data-market-learning-preference" in source
    assert "setPreferredMarket(key, !state.preferred.has(key), 'market_learning');" in source
    assert "writeJson(MARKET_KEY, Array.from(state.preferred));" in source
    assert "data-market-preference-key" in source
    assert "syncMarketPreferenceControls();" in source


def test_rendering_learning_never_mutates_preferences_automatically():
    source = (ROOT / "static" / "product-hub.js").read_text(encoding="utf-8")
    render = source.split(
        "function renderVerifiedDecisionMarketLearning(learning)", 1
    )[1].split("function wireMarketLearningActions()", 1)[0]

    assert "setPreferredMarket(" not in render
    assert "writeJson(MARKET_KEY" not in render
    assert "performance never changes it automatically." in render
    assert "marketLearning.rankingEnabled === false" in render
    assert "marketLearning.preferenceMutation === false" in render
    assert "marketLearning.recommendation === false" in render


def test_preference_actions_fail_closed_to_represented_canonical_markets():
    source = (ROOT / "static" / "product-hub.js").read_text(encoding="utf-8")

    assert "function isSupportedMarketKey(key)" in source
    assert "if (!isSupportedMarketKey(key)) return false;" in source
    assert "supported.indexOf(key) >= 0 && !seen[key]" in source
    assert "No market conclusion is shown." in source
    assert "preferences remain unchanged." in source


def test_workspace_exposes_accessible_synchronized_review_controls():
    html = (ROOT / "product_hub.html").read_text(encoding="utf-8")
    source = (ROOT / "static" / "product-hub.js").read_text(encoding="utf-8")
    css = (ROOT / "static" / "product-hub.css").read_text(encoding="utf-8")

    assert "FEATURE 4.85" in html
    assert 'id="verifiedDecisionMarketLearning"' in html
    assert 'class="market-learning-preference"' in source
    assert "aria-pressed" in source
    assert "aria-label" in source
    assert "Preferred" in source
    assert "Add preference" in source
    assert ".market-learning-preference" in css
    assert ".market-learning-preference[aria-pressed=\"true\"]" in css
    assert ".market-chip,.market-learning-preference,.panel-head" in css
    assert ".market-learning-row{grid-template-columns:1fr}" in css


def test_phase_474_is_documented_as_active():
    roadmap = (ROOT / "docs" / "MLB_ANALYTICS_HUB_ROADMAP.md").read_text(
        encoding="utf-8"
    )

    assert "Phase 4.85 is the active phase." in roadmap
    assert "### Phase 4.74 — Explicit market preference review" in roadmap
    assert "every preference change requires an explicit user action" in roadmap
