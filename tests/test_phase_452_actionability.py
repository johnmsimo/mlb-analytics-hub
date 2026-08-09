from actionability import (
    ACTIONABILITY_VERSION,
    evaluate_actionability,
    filter_actionable,
)
from intelligence_core import build_recommendations


def base_row(**changes):
    row = {
        "gamePk": 7,
        "gameStatus": "Scheduled",
        "gameAbstractState": "Preview",
        "gameStartIso": "2099-08-06T23:10:00+00:00",
        "player": "Valid Hitter",
        "playerId": 101,
        "playerRole": "batter",
        "playerPosition": "CF",
        "lineupStatus": "confirmed",
        "marketKey": "batter_hits",
        "line": 0.5,
        "recommendedSide": "Over",
        "adjProb": 0.64,
        "canonicalProbability": 0.64,
        "bestAvailablePrice": -110,
        "bestAvailableBook": "Book A",
        "bestOverPrice": -110,
        "bestUnderPrice": -105,
        "oddsUpdatedAt": "2099-08-06T15:58:00+00:00",
        "modelVersion": "hits-xgb-2026.08",
        "matchupSimulationVersion": "4.35",
        "gameSimN": 1500,
        "canonicalPrice": -110,
        "canonicalBook": "Book A",
        "canonicalEdge": 0.08,
        "edge": 0.08,
        "confidenceScore": 80.0,
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
        base_row(
            canonicalProbability=None,
            adjProb=None,
            probability=None,
            winProb=None,
            canonicalPrice=None,
            bestAvailablePrice=None,
            marketPrice=None,
            bestOverPrice=None,
            bestUnderPrice=None,
        ),
        require_market_validation=True,
    )
    projected = evaluate_actionability(
        base_row(
            canonicalPrice=None,
            bestAvailablePrice=None,
            marketPrice=None,
            bestOverPrice=None,
            bestUnderPrice=None,
        ),
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
        base_row(
            canonicalPrice=None,
            bestAvailablePrice=None,
            marketPrice=None,
            bestOverPrice=None,
            bestUnderPrice=None,
        ),
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
        base_row(
            marketGatePromoted=False,
            marketGateStatus="warming_up",
            marketSideGateStatus="warming_up",
            id="priced",
        ),
    ])
    assert len(result["card"]) == 1
    assert result["card"][0]["actionabilityStage"] == "Actionable"
    assert result["actionabilityVersion"] == ACTIONABILITY_VERSION
    assert result["actionabilityAudit"]["actionableCount"] == 1
