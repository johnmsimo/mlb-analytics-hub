from pathlib import Path

from product_hub import (
    PRODUCT_HUB_VERSION,
    VERIFIED_DECISION_MARKET_LEARNING_VERSION,
    product_hub_bp,
)
from tracker_learning import (
    MINIMUM_GRADED_SAMPLE,
    SUPPORTED_MARKETS,
    VERIFIED_DECISION_MARKET_LEARNING_VERSION as TRACKER_MARKET_VERSION,
    VERIFIED_DECISION_SOURCE,
    build_verified_decision_learning,
)


ROOT = Path(__file__).resolve().parents[1]


def _row(market_key, grade="pending", **updates):
    row = {
        "source": VERIFIED_DECISION_SOURCE,
        "marketKey": market_key,
        "grade": grade,
        "stakeDollars": 10,
        "profitDollars": 0,
        "profitUnits": 0,
        "clvEdge": None,
    }
    row.update(updates)
    return row


def test_market_learning_contract_disables_ranking_and_preference_mutation():
    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(product_hub_bp)
    with app.test_client() as client:
        payload = client.get("/api/product/journey").get_json()

    contract = payload["verifiedDecisionMarketLearning"]
    assert payload["version"] == PRODUCT_HUB_VERSION == "4.64"
    assert contract["version"] == VERIFIED_DECISION_MARKET_LEARNING_VERSION == "4.73"
    assert contract["parentContractVersion"] == "4.72"
    assert contract["supportedMarkets"] == list(SUPPORTED_MARKETS)
    assert contract["minimumGradedSamplePerMarket"] == MINIMUM_GRADED_SAMPLE
    assert contract["aggregateOnly"] is True
    assert contract["trackerRowsIncluded"] is False
    assert contract["rankingEnabled"] is False
    assert contract["preferenceMutation"] is False
    assert contract["recommendation"] is False
    assert contract["failClosed"] is True


def test_market_learning_is_source_filtered_and_omits_unknown_markets():
    payload = build_verified_decision_learning([
        _row("batter_hits", "win", clvEdge=0.03),
        _row("unknown_market", "win", clvEdge=0.20),
        {
            "source": "props_board",
            "marketKey": "batter_hits",
            "grade": "loss",
            "clvEdge": -0.50,
        },
    ])
    lens = payload["marketLearning"]

    assert lens["version"] == TRACKER_MARKET_VERSION == "4.73"
    assert lens["marketCount"] == 1
    assert [item["marketKey"] for item in lens["markets"]] == ["batter_hits"]
    assert lens["markets"][0]["wins"] == 1
    assert lens["markets"][0]["beatCloseRate"] == 1.0
    assert lens["trackerRowsIncluded"] is False
    assert lens["rankingEnabled"] is False


def test_market_learning_preserves_canonical_order_instead_of_performance_order():
    payload = build_verified_decision_learning([
        _row("pitcher_strikeouts", "win", clvEdge=0.20),
        _row("batter_hits", "loss", clvEdge=-0.10),
        _row("batter_home_runs", "win", clvEdge=0.05),
    ])
    keys = [item["marketKey"] for item in payload["marketLearning"]["markets"]]

    assert keys == [
        "batter_hits",
        "batter_home_runs",
        "pitcher_strikeouts",
    ]


def test_market_samples_use_explicit_outcome_and_sample_states():
    pending = build_verified_decision_learning([
        _row("batter_hits"),
    ])["marketLearning"]
    learning = build_verified_decision_learning([
        _row("batter_hits", "win"),
    ])["marketLearning"]
    ready = build_verified_decision_learning([
        _row("batter_hits", "win")
        for _ in range(MINIMUM_GRADED_SAMPLE)
    ])["marketLearning"]

    assert pending["state"] == "awaiting_outcomes"
    assert pending["markets"][0]["state"] == "awaiting_outcomes"
    assert learning["state"] == "learning"
    assert learning["markets"][0]["sampleReady"] is False
    assert ready["state"] == "sample_ready"
    assert ready["markets"][0]["sampleReady"] is True


def test_market_lens_remains_aggregate_only():
    lens = build_verified_decision_learning([
        _row("batter_hits", "win"),
    ])["marketLearning"]

    assert "player" not in str(lens).lower()
    assert "selection" not in str(lens).lower()
    assert "entries" not in lens
    assert "rows" not in lens
    assert lens["metricsAreDescriptive"] is True
    assert lens["recommendation"] is False


def test_workspace_renders_market_lens_without_ranking():
    html = (ROOT / "product_hub.html").read_text(encoding="utf-8")
    source = (ROOT / "static" / "product-hub.js").read_text(encoding="utf-8")
    css = (ROOT / "static" / "product-hub.css").read_text(encoding="utf-8")

    assert "FEATURE 4.74" in html
    assert 'id="verifiedDecisionMarketLearning"' in html
    assert 'id="marketLearningList"' in html
    for marker in (
        "function renderVerifiedDecisionMarketLearning(learning)",
        "marketLearning.rankingEnabled === false",
        "marketLearning.preferenceMutation === false",
        "marketLearning.recommendation === false",
        "Markets stay in canonical order.",
        "preferences remain unchanged.",
    ):
        assert marker in source
    assert ".market-learning-row" in css
    assert ".market-learning-row{grid-template-columns:1fr}" in css


def test_phase_473_is_documented_as_active():
    roadmap = (ROOT / "docs" / "MLB_ANALYTICS_HUB_ROADMAP.md").read_text(
        encoding="utf-8"
    )

    assert "Phase 4.74 is the active phase." in roadmap
    assert "### Phase 4.73 — Verified decision market learning lens" in roadmap
