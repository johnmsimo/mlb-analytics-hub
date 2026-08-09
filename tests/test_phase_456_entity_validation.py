from datetime import datetime, timezone

from candidate_integrity import evaluate_candidate
from entity_validation import ENTITY_VALIDATION_VERSION, validate_entity_data


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
        "team": "NYY",
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


def test_entity_contract_version_and_valid_rows():
    result = validate_entity_data(candidate(), market_key="batter_hits", role="batter", now=NOW)

    assert result["version"] == ENTITY_VALIDATION_VERSION
    assert result["valid"] is True
    assert result["status"] == "valid"


def test_player_team_identity_mismatch_is_rejected_before_recommendation():
    result = evaluate_candidate(candidate(playerTeam="BOS"), now=NOW)

    assert result["actionable"] is False
    assert "player/team identity mismatch" in result["integrityReasons"]
    assert result["entityValidation"]["status"] == "rejected"


def test_invalid_lineup_and_handedness_are_rejected():
    result = validate_entity_data(
        candidate(lineupStatus="scratched", playerHand="sideways"),
        market_key="batter_hits",
        role="batter",
        now=NOW,
    )

    assert result["valid"] is False
    assert "invalid lineup status" in result["reasons"]
    assert "invalid handedness" in result["reasons"]


def test_impossible_single_game_stats_are_rejected():
    result = validate_entity_data(
        candidate(hits=99, battingAverage=1.4),
        market_key="batter_hits",
        role="batter",
        now=NOW,
    )

    assert result["valid"] is False
    assert "suspicious or impossible stat: hits" in result["reasons"]
    assert "suspicious or impossible stat: battingAverage" in result["reasons"]


def test_stale_probable_pitcher_and_failed_asset_are_rejected():
    result = validate_entity_data(
        candidate(
            playerRole="pitcher",
            probablePitcherStale=True,
            assetRequired=True,
            assetStatus="failed",
        ),
        market_key="pitcher_strikeouts",
        role="pitcher",
        now=NOW,
    )

    assert result["valid"] is False
    assert "probable pitcher is stale" in result["reasons"]
    assert "missing or invalid asset" in result["reasons"]


def test_market_and_line_conflicts_are_rejected():
    result = validate_entity_data(
        candidate(market="pitcher_strikeouts", propLine=1.5),
        market_key="batter_hits",
        role="batter",
        now=NOW,
    )

    assert result["valid"] is False
    assert "inconsistent market names" in result["reasons"]
    assert "inconsistent market lines" in result["reasons"]


def test_valid_optional_evidence_does_not_change_eligibility():
    result = evaluate_candidate(
        candidate(
            playerTeam="NYY",
            playerHand="L",
            hits=2,
            battingAverage=0.300,
            logoUrl="/static/team-logos/nyy.png",
            probablePitcherUpdatedAt="2026-08-06T15:30:00+00:00",
        ),
        now=NOW,
    )

    assert result["actionable"] is True
    assert result["entityValidation"]["valid"] is True
