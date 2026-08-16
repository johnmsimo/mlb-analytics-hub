from pathlib import Path

from product_hub import (
    PERSONALIZED_SIGNAL_PROVENANCE_VERSION,
    PRODUCT_HUB_VERSION,
    product_hub_bp,
)


ROOT = Path(__file__).resolve().parents[1]


def test_signal_provenance_contract_is_explanatory_and_fail_closed():
    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(product_hub_bp)
    with app.test_client() as client:
        payload = client.get("/api/product/journey").get_json()

    contract = payload["personalizedSignalProvenance"]
    assert payload["version"] == PRODUCT_HUB_VERSION == "4.64"
    assert contract["version"] == PERSONALIZED_SIGNAL_PROVENANCE_VERSION == "4.76"
    assert contract["sourceContractVersion"] == "4.75"
    assert contract["sourceEndpoint"] == "/api/edges/today"
    assert contract["reasonOrder"] == ["preferred_market", "saved_player"]
    assert contract["requiresActionable"] is True
    assert contract["requiresCanonicalPreferredMarket"] is True
    assert contract["savedPlayerReasonUsesExplicitWatchlist"] is True
    assert contract["provenanceOnly"] is True
    assert contract["eligibilityChanged"] is False
    assert contract["rankingChanged"] is False
    assert contract["learningPerformanceUsed"] is False
    assert contract["recommendation"] is False
    assert contract["serverPersistence"] is False
    assert contract["failClosed"] is True


def test_provenance_requires_actionability_and_explicit_preferred_market():
    source = (ROOT / "static" / "product-hub.js").read_text(encoding="utf-8")
    reasons = source.split("function personalizedSignalReasons(row)", 1)[1].split(
        "function personalizedEdges()", 1
    )[0]

    assert "isActionable(row)" in reasons
    assert "isSupportedMarketKey(marketKey)" in reasons
    assert "state.preferred.has(marketKeyOf(row))" in reasons
    assert "return [];" in reasons
    assert "preferred_market" in reasons


def test_saved_player_reason_uses_only_explicit_watchlist_state():
    source = (ROOT / "static" / "product-hub.js").read_text(encoding="utf-8")
    reasons = source.split("function personalizedSignalReasons(row)", 1)[1].split(
        "function personalizedEdges()", 1
    )[0]

    assert "state.watchlist.has(playerKey(row.player))" in reasons
    assert "reasons.push({ key: 'saved_player', label: 'Saved player' });" in reasons
    assert reasons.index("preferred_market") < reasons.index("saved_player")
    for forbidden in ("tracker", "learning", "performance", "roi", "clv", "winRate"):
        assert forbidden not in reasons


def test_signal_cards_render_machine_readable_and_accessible_reasons():
    html = (ROOT / "product_hub.html").read_text(encoding="utf-8")
    source = (ROOT / "static" / "product-hub.js").read_text(encoding="utf-8")
    css = (ROOT / "static" / "product-hub.css").read_text(encoding="utf-8")

    assert "FEATURE 4.81" in html
    assert "data-personalization-reasons" in source
    assert "data-personalization-reason" in source
    assert "Personalization reasons:" in source
    assert "<strong>Shown because</strong>" in source
    assert ".signal-provenance" in css
    assert '[data-personalization-reason="saved_player"]' in css


def test_signal_rendering_fails_closed_without_provenance_reasons():
    source = (ROOT / "static" / "product-hub.js").read_text(encoding="utf-8")
    signal = source.split("function signalHtml(row)", 1)[1].split(
        "function renderSignals()", 1
    )[0]

    assert "var reasons = personalizedSignalReasons(row);" in signal
    assert "if (!reasons.length) return '';" in signal
    assert "reasonKeys.join(',')" in signal
    assert "reasonLabels.join(', ')" in signal


def test_provenance_does_not_change_existing_personalized_sort_inputs():
    source = (ROOT / "static" / "product-hub.js").read_text(encoding="utf-8")
    personalized = source.split("function personalizedEdges()", 1)[1].split(
        "function signalHtml(row)", 1
    )[0]

    assert "personalizedSignalReasons(row).length > 0" in personalized
    assert "rightSaved - leftSaved" in personalized
    assert "edgeValue(right)" in personalized
    assert "tracker" not in personalized
    assert "learning" not in personalized


def test_phase_476_is_documented_as_active():
    roadmap = (ROOT / "docs" / "MLB_ANALYTICS_HUB_ROADMAP.md").read_text(
        encoding="utf-8"
    )

    assert "Phase 4.81 is the active phase." in roadmap
    assert "### Phase 4.76 — Personalized signal provenance" in roadmap
    assert "Provenance is explanatory only." in roadmap
