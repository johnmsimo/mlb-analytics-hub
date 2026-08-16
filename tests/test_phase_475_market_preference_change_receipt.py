from pathlib import Path

from product_hub import (
    MARKET_PREFERENCE_CHANGE_RECEIPT_VERSION,
    PRODUCT_HUB_VERSION,
    product_hub_bp,
)


ROOT = Path(__file__).resolve().parents[1]


def test_change_receipt_contract_is_explicit_device_local_and_fail_closed():
    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(product_hub_bp)
    with app.test_client() as client:
        payload = client.get("/api/product/journey").get_json()

    contract = payload["marketPreferenceChangeReceipt"]
    assert payload["version"] == PRODUCT_HUB_VERSION == "4.64"
    assert contract["version"] == MARKET_PREFERENCE_CHANGE_RECEIPT_VERSION == "4.75"
    assert contract["sourceContractVersion"] == "4.74"
    assert contract["preferenceStorageKey"] == "mlb_market_preferences"
    assert contract["states"] == ["idle", "applied", "undone", "unavailable"]
    assert contract["receiptPersistence"] == "session_only"
    assert contract["deviceLocal"] is True
    assert contract["serverPersistence"] is False
    assert contract["explicitUserActionRequired"] is True
    assert contract["undoRequiresExplicitAction"] is True
    assert contract["automaticPreferenceMutation"] is False
    assert contract["performanceDriven"] is False
    assert contract["recommendation"] is False
    assert contract["signalImpactSource"] == "current_actionable_edges"
    assert contract["signalImpactRequiresReadyState"] is True
    assert contract["failClosed"] is True


def test_workspace_exposes_accessible_change_receipt_and_explicit_undo():
    html = (ROOT / "product_hub.html").read_text(encoding="utf-8")
    source = (ROOT / "static" / "product-hub.js").read_text(encoding="utf-8")
    css = (ROOT / "static" / "product-hub.css").read_text(encoding="utf-8")

    assert "FEATURE 4.86" in html
    assert 'id="marketPreferenceReceipt"' in html
    assert 'data-receipt-state="idle"' in html
    assert 'aria-live="polite"' in html
    assert 'id="undoMarketPreference"' in html
    assert "function wireMarketPreferenceReceipt()" in source
    assert "undo.addEventListener('click', undoMarketPreferenceChange);" in source
    assert ".market-preference-receipt" in css
    assert ".market-preference-undo" in css
    assert ".alert-actions button,.market-preference-undo{min-height:44px" in css


def test_receipt_captures_exact_previous_value_and_control_source():
    source = (ROOT / "static" / "product-hub.js").read_text(encoding="utf-8")

    assert "previous: state.preferred.has(key)" in source
    assert "source === 'market_learning' ? 'market_learning' : 'preferences'" in source
    assert "setPreferredMarket(item.key, !state.preferred.has(item.key), 'preferences');" in source
    assert "setPreferredMarket(key, !state.preferred.has(key), 'market_learning');" in source
    assert "if (!isSupportedMarketKey(key) || state.preferred.has(key) === preferred) return;" in source


def test_undo_restores_only_the_preceding_device_preference():
    source = (ROOT / "static" / "product-hub.js").read_text(encoding="utf-8")
    undo = source.split("function undoMarketPreferenceChange()", 1)[1].split(
        "function wireMarketPreferenceReceipt()", 1
    )[0]

    assert "receipt.state !== 'applied'" in undo
    assert "var restored = receipt.previous;" in undo
    assert "applyPreferredMarket(receipt.marketKey, restored)" in undo
    assert "state: 'undone'" in undo
    assert "fetch(" not in undo


def test_signal_impact_is_shown_only_for_ready_actionable_edges():
    source = (ROOT / "static" / "product-hub.js").read_text(encoding="utf-8")
    receipt = source.split("function renderMarketPreferenceReceipt()", 1)[1].split(
        "function applyPreferredMarket", 1
    )[0]

    assert "state.edgeState === 'ready'" in receipt
    assert "personalizedEdges().length" in receipt
    assert "Matching signal count is unavailable until current edges are ready." in receipt
    assert "performance" not in receipt.lower()
    assert "roi" not in receipt.lower()
    assert "clv" not in receipt.lower()
    assert "win rate" not in receipt.lower()


def test_unknown_saved_market_keys_are_discarded():
    source = (ROOT / "static" / "product-hub.js").read_text(encoding="utf-8")

    assert ".filter(isSupportedMarketKey)" in source
    assert "if (!isSupportedMarketKey(key)) return false;" in source
    assert "Preference receipt unavailable; no additional change was made." in source


def test_phase_475_is_documented_as_active():
    roadmap = (ROOT / "docs" / "MLB_ANALYTICS_HUB_ROADMAP.md").read_text(
        encoding="utf-8"
    )

    assert "Phase 5.0 is the active phase." in roadmap
    assert "### Phase 4.75 — Market preference change receipt" in roadmap
    assert "announced accessibly" in roadmap
