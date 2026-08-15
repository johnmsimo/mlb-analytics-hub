from datetime import datetime, timezone
from pathlib import Path

from canonical_consistency import (
    CANONICAL_CONTRACT_VERSION,
    consistency_audit,
    normalize_candidate,
    normalize_payload,
    normalize_rows,
)


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)


def candidate(**changes):
    row = {
        "gamePk": 7,
        "gameStatus": "Scheduled",
        "gameAbstractState": "Preview",
        "gameStartIso": "2099-08-09T23:10:00+00:00",
        "player": "Canonical Hitter",
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
        "bestOverBook": "Book A",
        "bestUnderPrice": -105,
        "bestUnderBook": "Book B",
        "oddsUpdatedAt": NOW.isoformat(),
        "modelVersion": "hits-xgb-2026.08",
        "matchupSimulationVersion": "4.35",
        "gameSimN": 1500,
        "grade": "pending",
    }
    row.update(changes)
    return row


def test_same_candidate_has_one_canonical_snapshot_across_field_aliases():
    first = normalize_candidate(candidate(), surface="props", now=NOW)
    second = normalize_candidate(
        candidate(
            probability=0.62,
            adjProb=None,
            price=-110,
            book="Book A",
        ),
        surface="value_bets",
        now=NOW,
    )

    assert first["canonicalContractVersion"] == CANONICAL_CONTRACT_VERSION
    assert first["canonicalCandidateId"] == second["canonicalCandidateId"]
    assert first["canonicalSnapshot"] == second["canonicalSnapshot"]
    assert first["canonicalProbability"] == 0.62
    assert first["canonicalPrice"] == -110
    assert first["canonicalBook"] == "Book A"


def test_research_rows_are_consistently_non_actionable():
    row = normalize_candidate(
        candidate(
            bestAvailablePrice=None,
            bestAvailableBook=None,
            bestOverPrice=None,
            bestUnderPrice=None,
        ),
        surface="deep_dive",
        now=NOW,
    )

    assert row["canonicalContractVersion"] == CANONICAL_CONTRACT_VERSION
    assert row["actionable"] is False
    assert row["actionabilityStage"] in {"Projected", "Validated"}
    assert row["canonicalPrice"] is None
    assert row["canonicalBook"] is None


def test_normalize_payload_carries_contract_audit_and_dedupes_rows():
    payload = normalize_payload(
        {"props": [candidate(), candidate()]},
        surface="props",
        now=NOW,
    )

    assert payload["canonicalContractVersion"] == CANONICAL_CONTRACT_VERSION
    assert len(payload["props"]) == 1
    assert payload["canonicalCandidateAudit"]["rowCount"] == 1
    assert payload["canonicalCandidateAudit"]["actionableCount"] == 1


def test_consistency_audit_identifies_probability_mismatch():
    audit = consistency_audit({
        "props": [candidate()],
        "value_bets": [candidate(adjProb=0.58)],
    })

    assert audit["consistent"] is False
    assert audit["mismatchCount"] == 1
    assert "canonicalProbability" in audit["mismatches"][0]["fields"]


def test_phase_457_contract_is_installed_and_documented():
    wsgi = (ROOT / "wsgi.py").read_text(encoding="utf-8")
    roadmap = (ROOT / "docs" / "MLB_ANALYTICS_HUB_ROADMAP.md").read_text(
        encoding="utf-8"
    )
    contract = (ROOT / "canonical_consistency.py").read_text(encoding="utf-8")
    assert "install_canonical_consistency_api" in wsgi
    assert "install_canonical_response_hook" in contract
    assert "/api/props/projections" in contract
    assert "/api/cheatsheets/today" in contract
    assert "/api/tracker" in contract
    assert "/api/deepdive" in contract
    assert "/api/gameside" in contract
    assert 'Phase 4.80 is the active phase.' in roadmap
    assert 'CANONICAL_CONTRACT_VERSION = "4.57"' in contract
