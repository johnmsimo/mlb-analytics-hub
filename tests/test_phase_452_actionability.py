from actionability import (
    ACTIONABILITY_VERSION,
    evaluate_actionability,
    filter_actionable,
)
from intelligence_core import build_recommendations


def base_row(**changes):
    row = {
        "marketKey": "batter_hits",
        "canonicalProbability": 0.64,
        "canonicalPrice": -110,
        "canonicalBook": "Book A",
        "canonicalEdge": 0.08,
        "actionable": True,
        "marketGatePromoted": True,
        "marketGateStatus": "promoted",
        "marketSideGateStatus": "promoted",
        "grade": "pending",
    }
    row.update(changes)
    return row


def test_actionability_contract_has_explicit_fail_closed_stages():
    assert ACTIONABILITY_VERSION == "4.52"
    research = evaluate_actionability(
        base_row(canonicalProbability=None, canonicalPrice=None),
        require_market_validation=True,
    )
    projected = evaluate_actionability(
        base_row(canonicalPrice=None),
        require_market_validation=True,
    )
    priced = evaluate_actionability(
        base_row(marketGatePromoted=False, marketGateStatus="warming_up"),
        require_market_validation=True,
    )
    validated = evaluate_actionability(
        base_row(actionable=False, integrityReasons=["stale odds"]),
        require_market_validation=True,
    )
    actionable = evaluate_actionability(
        base_row(),
        require_market_validation=True,
    )
    graded = evaluate_actionability(
        base_row(grade="win"),
        require_market_validation=True,
    )
    assert research["actionabilityStage"] == "Research"
    assert projected["actionabilityStage"] == "Projected"
    assert priced["actionabilityStage"] == "Priced"
    assert validated["actionabilityStage"] == "Validated"
    assert actionable["actionabilityStage"] == "Actionable"
    assert graded["actionabilityStage"] == "Graded"
    assert actionable["actionable"] is True
    assert all(
        item["actionable"] is False
        for item in (research, projected, priced, validated, graded)
    )


def test_filter_actionable_reports_rows_that_must_not_reach_betting_surfaces():
    result = filter_actionable([
        base_row(),
        base_row(canonicalPrice=None),
        base_row(marketGatePromoted=False, marketGateStatus="warming_up"),
    ])
    assert [row["actionabilityStage"] for row in result["actionable"]] == ["Actionable"]
    assert result["audit"]["actionableCount"] == 1
    assert result["audit"]["stageCounts"]["Projected"] == 1
    assert result["audit"]["stageCounts"]["Priced"] == 1
    assert result["audit"]["rejectedCount"] == 2
    assert result["audit"]["stageCounts"] == {
        "Actionable": 1,
        "Priced": 1,
        "Projected": 1,
    }


def test_recommendation_builder_drops_non_actionable_market_rows():
    result = build_recommendations([
        base_row(),
        base_row(canonicalPrice=None, id="projected"),
    ])
    assert len(result["card"]) == 1
    assert result["card"][0]["actionabilityStage"] == "Actionable"
    assert result["actionabilityVersion"] == ACTIONABILITY_VERSION
    assert result["actionabilityAudit"]["actionableCount"] == 1
