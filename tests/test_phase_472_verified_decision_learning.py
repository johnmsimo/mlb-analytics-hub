from pathlib import Path

from product_hub import (
    PRODUCT_HUB_VERSION,
    VERIFIED_DECISION_LEARNING_VERSION,
    product_hub_bp,
)
from tracker_learning import (
    MINIMUM_GRADED_SAMPLE,
    VERIFIED_DECISION_LEARNING_VERSION as TRACKER_LEARNING_VERSION,
    VERIFIED_DECISION_SOURCE,
    build_verified_decision_learning,
)


ROOT = Path(__file__).resolve().parents[1]


def _row(grade="pending", **updates):
    row = {
        "source": VERIFIED_DECISION_SOURCE,
        "grade": grade,
        "stakeDollars": 10,
        "profitDollars": 0,
        "profitUnits": 0,
        "clvEdge": None,
    }
    row.update(updates)
    return row


def test_learning_contract_is_aggregate_only_and_fail_closed():
    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(product_hub_bp)
    with app.test_client() as client:
        payload = client.get("/api/product/journey").get_json()

    learning = payload["verifiedDecisionLearning"]
    assert payload["version"] == PRODUCT_HUB_VERSION == "4.64"
    assert learning["version"] == VERIFIED_DECISION_LEARNING_VERSION == "4.72"
    assert learning["trackedSource"] == VERIFIED_DECISION_SOURCE
    assert learning["minimumGradedSample"] == MINIMUM_GRADED_SAMPLE == 10
    assert learning["aggregateOnly"] is True
    assert learning["rowsIncluded"] is False
    assert learning["metricsAreDescriptive"] is True
    assert learning["failClosed"] is True


def test_learning_ignores_other_tracker_sources_and_returns_no_rows():
    payload = build_verified_decision_learning([
        _row("win", profitDollars=8, profitUnits=0.8, clvEdge=0.04),
        {"source": "props_board", "grade": "loss", "stakeDollars": 1000},
    ])

    assert payload["version"] == TRACKER_LEARNING_VERSION == "4.72"
    assert payload["source"] == VERIFIED_DECISION_SOURCE
    assert payload["decisionCount"] == 1
    assert payload["gradedCount"] == 1
    assert payload["wins"] == 1
    assert payload["state"] == "learning"
    assert payload["rowsIncluded"] is False
    assert "rows" not in payload
    assert "entries" not in payload


def test_learning_states_progress_without_overclaiming_small_samples():
    empty = build_verified_decision_learning([])
    pending = build_verified_decision_learning([_row()])
    early = build_verified_decision_learning([_row("win")])
    ready = build_verified_decision_learning([
        _row("win") for _ in range(MINIMUM_GRADED_SAMPLE)
    ])

    assert empty["state"] == "no_verified_decisions"
    assert pending["state"] == "awaiting_outcomes"
    assert early["state"] == "learning"
    assert early["sampleReady"] is False
    assert ready["state"] == "sample_ready"
    assert ready["sampleReady"] is True


def test_learning_metrics_handle_pushes_clv_and_zero_risk_explicitly():
    payload = build_verified_decision_learning([
        _row("win", stakeDollars=0, profitDollars=5, profitUnits=0.5, clvEdge=0.03),
        _row("loss", stakeDollars=0, profitDollars=-5, profitUnits=-0.5, clvEdge=-0.01),
        _row("push", stakeDollars=0, clvEdge=None),
        _row("pending"),
    ])

    assert payload["decisionCount"] == 4
    assert payload["pendingCount"] == 1
    assert payload["gradedCount"] == 3
    assert payload["wins"] == 1
    assert payload["losses"] == 1
    assert payload["pushes"] == 1
    assert payload["hitRate"] == 0.5
    assert payload["roi"] is None
    assert payload["averageClv"] == 0.01
    assert payload["beatCloseRate"] == 0.5


def test_performance_endpoint_attaches_aggregate_learning_payload():
    source = (ROOT / "app.py").read_text(encoding="utf-8")

    assert "from tracker_learning import build_verified_decision_learning" in source
    assert "'verifiedDecisionLearning': build_verified_decision_learning(entries)" in source


def test_workspace_renders_explicit_learning_and_unavailable_states():
    html = (ROOT / "product_hub.html").read_text(encoding="utf-8")
    source = (ROOT / "static" / "product-hub.js").read_text(encoding="utf-8")
    css = (ROOT / "static" / "product-hub.css").read_text(encoding="utf-8")

    assert "FEATURE 4.74" in html
    assert 'id="verifiedDecisionLearning"' in html
    assert 'aria-live="polite"' in html
    for marker in (
        "function renderVerifiedDecisionLearning()",
        "no_verified_decisions",
        "awaiting_outcomes",
        "sample_ready",
        "Early metrics are descriptive only.",
        "Source-attributed learning is unavailable; no conclusion is shown.",
        "learning.rowsIncluded === false",
    ):
        assert marker in source
    assert ".decision-learning" in css
    assert ".learning-metrics" in css
    assert ".learning-metrics{grid-template-columns:1fr 1fr}" in css


def test_phase_472_is_documented_as_active():
    roadmap = (ROOT / "docs" / "MLB_ANALYTICS_HUB_ROADMAP.md").read_text(
        encoding="utf-8"
    )

    assert "Phase 4.74 is the active phase." in roadmap
    assert "### Phase 4.72 — Verified decision learning loop" in roadmap
