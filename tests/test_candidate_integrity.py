from datetime import datetime, timezone

from candidate_integrity import (
    INTEGRITY_VERSION,
    evaluate_candidate,
    evaluate_candidates,
    projection_analysis_candidate,
)


NOW = datetime(2026, 8, 6, 16, 0, tzinfo=timezone.utc)


def candidate(**changes):
    row = {
        "gamePk": 7,
        "gameStatus": "Scheduled",
        "gameAbstractState": "Preview",
        "gameStartIso": "2026-08-06T23:10:00+00:00",
        "player": "Valid Hitter",
        "playerId": 101,
        "playerRole": "batter",
        "playerPosition": "CF",
        "lineupStatus": "confirmed",
        "marketKey": "batter_hits",
        "line": 0.5,
        "recommendedSide": "Over",
        "adjProb": 0.62,
        "bestAvailablePrice": -110,
        "bestAvailableBook": "Book A",
        "bestOverPrice": -110,
        "bestUnderPrice": -105,
        "oddsUpdatedAt": "2026-08-06T15:58:00+00:00",
        "modelVersion": "hits-xgb-2026.08",
        "matchupSimulationVersion": "4.35",
        "gameSimN": 1500,
        "grade": "pending",
    }
    row.update(changes)
    return row


def test_valid_candidate_gets_one_stable_actionable_identity():
    result = evaluate_candidate(candidate(), now=NOW)

    assert result["actionable"] is True
    assert result["integrityStatus"] == "eligible"
    assert result["integrityVersion"] == INTEGRITY_VERSION
    assert result["canonicalCandidateId"].startswith("candidate:")
    assert result["marketFairProbability"] == 0.505605
    assert result["canonicalEdge"] == 0.114395
    assert result["edge"] == result["canonicalEdge"]


def test_pitcher_cannot_enter_a_hitter_market():
    result = evaluate_candidate(
        candidate(playerRole="pitcher", playerPosition="SP"), now=NOW
    )

    assert result["actionable"] is False
    assert "player role does not match market" in result["integrityReasons"]
    assert "invalid batter position" in result["integrityReasons"]


def test_unknown_position_cannot_be_promoted_as_a_batter():
    result = evaluate_candidate(candidate(playerPosition="?"), now=NOW)

    assert result["actionable"] is False
    assert "invalid batter position" in result["integrityReasons"]


def test_completed_or_started_games_are_never_actionable():
    completed = evaluate_candidate(
        candidate(gameStatus="Final", gameAbstractState="Final"), now=NOW
    )
    started = evaluate_candidate(
        candidate(gameStartIso="2026-08-06T15:00:00+00:00"), now=NOW
    )

    assert "game is not upcoming" in completed["integrityReasons"]
    assert "game is not upcoming" in started["integrityReasons"]


def test_projected_lineup_is_explicitly_allowed_but_roster_fallback_is_not():
    projected = evaluate_candidate(candidate(lineupStatus="projected"), now=NOW)
    roster = evaluate_candidate(candidate(lineupStatus="roster"), now=NOW)

    assert projected["actionable"] is True
    assert roster["actionable"] is False
    assert "lineup is neither confirmed nor projected" in roster["integrityReasons"]


def test_real_two_sided_fresh_sportsbook_price_is_required():
    missing_book = evaluate_candidate(candidate(bestAvailableBook=None), now=NOW)
    missing_other_side = evaluate_candidate(
        candidate(bestUnderPrice=None), now=NOW
    )
    stale = evaluate_candidate(
        candidate(oddsUpdatedAt="2026-08-06T15:30:00+00:00"), now=NOW
    )

    assert "missing real sportsbook" in missing_book["integrityReasons"]
    assert "missing opposite-side price for de-vigging" in missing_other_side[
        "integrityReasons"
    ]
    assert "sportsbook price is stale" in stale["integrityReasons"]


def test_missing_quote_does_not_invent_a_negative_edge_rejection():
    result = evaluate_candidate(
        candidate(
            bestAvailablePrice=None,
            bestAvailableBook=None,
            bestOverPrice=None,
            bestUnderPrice=None,
            oddsUpdatedAt=None,
        ),
        now=NOW,
    )

    assert "missing sportsbook price" in result["integrityReasons"]
    assert "no positive edge after de-vigging" not in result["integrityReasons"]
    assert result["canonicalEdge"] is None
    assert result["oddsUpdatedAt"] is None


def test_price_only_failure_can_be_sanitized_as_projection_analysis():
    result = projection_analysis_candidate(
        candidate(
            bestAvailablePrice=None,
            bestAvailableBook=None,
            bestOverPrice=None,
            bestUnderPrice=None,
            oddsUpdatedAt=None,
            sharedSimulationBacked=True,
        ),
        now=NOW,
    )

    assert result is not None
    assert result["actionable"] is False
    assert result["selectionMode"] == "projection_only"
    assert result["promotionStatus"] == "projection_only"
    assert set(result["projectionAnalysisReasons"]) == {
        "missing sportsbook price",
        "missing real sportsbook",
        "missing opposite-side price for de-vigging",
        "missing odds freshness timestamp",
    }


def test_projection_analysis_still_rejects_lineup_or_simulation_failures():
    lineup_failure = projection_analysis_candidate(
        candidate(
            lineupStatus="roster",
            bestAvailablePrice=None,
            bestAvailableBook=None,
            bestOverPrice=None,
            bestUnderPrice=None,
            oddsUpdatedAt=None,
            sharedSimulationBacked=True,
        ),
        now=NOW,
    )
    simulation_failure = projection_analysis_candidate(
        candidate(
            bestAvailablePrice=None,
            bestAvailableBook=None,
            bestOverPrice=None,
            bestUnderPrice=None,
            oddsUpdatedAt=None,
            sharedSimulationBacked=False,
        ),
        now=NOW,
    )

    assert lineup_failure is None
    assert simulation_failure is None


def test_projection_labels_and_non_american_prices_are_not_sportsbooks():
    projection = evaluate_candidate(
        candidate(bestAvailableBook="Research"), now=NOW
    )
    invalid_price = evaluate_candidate(
        candidate(bestAvailablePrice=50, bestOverPrice=50), now=NOW
    )

    assert "missing real sportsbook" in projection["integrityReasons"]
    assert "missing sportsbook price" in invalid_price["integrityReasons"]


def test_positive_raw_edge_cannot_override_negative_devigged_edge():
    result = evaluate_candidate(
        candidate(adjProb=0.48, edge=0.20, bestOverPrice=100,
                  bestAvailablePrice=100, bestUnderPrice=-110),
        now=NOW,
    )

    assert result["quotedEdge"] == 0.20
    assert result["canonicalEdge"] < 0
    assert result["actionable"] is False
    assert "no positive edge after de-vigging" in result["integrityReasons"]


def test_shared_simulation_markets_require_version_and_minimum_trials():
    result = evaluate_candidate(
        candidate(matchupSimulationVersion=None, gameSimN=500), now=NOW
    )

    assert "missing simulation version" in result["integrityReasons"]
    assert "insufficient simulation coverage" in result["integrityReasons"]


def test_bulk_evaluation_deduplicates_market_identity_and_reports_reasons():
    duplicate_better_price = candidate(
        id="different-uuid",
        bestAvailablePrice=-105,
        bestOverPrice=-105,
    )
    rejected = candidate(
        player="Bad Position",
        playerId=202,
        playerPosition="SP",
    )
    result = evaluate_candidates(
        [candidate(), duplicate_better_price, rejected], now=NOW
    )

    assert result["audit"]["sourceCount"] == 3
    assert result["audit"]["uniqueCount"] == 2
    assert result["audit"]["duplicateCount"] == 1
    assert result["audit"]["eligibleCount"] == 1
    assert result["eligible"][0]["canonicalPrice"] == -105
    assert result["audit"]["rejectionReasons"]["invalid batter position"] == 1
    assert sum(result["audit"]["primaryRejectionReasons"].values()) == 1
