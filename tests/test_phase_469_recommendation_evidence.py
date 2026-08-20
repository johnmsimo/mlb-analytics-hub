import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from canonical_consistency import (
    RECOMMENDATION_EVIDENCE_VERSION,
    normalize_candidate,
)
from multi_book_shopping import build_multi_book_shopping
from scripts.production_contract_gate import (
    ContractError,
    validate_actionable_edges,
)


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 15, 16, 0, tzinfo=timezone.utc)


def actionable_candidate(**changes):
    row = {
        "gamePk": 469,
        "gameStatus": "Scheduled",
        "gameAbstractState": "Preview",
        "gameStartIso": "2099-08-15T23:10:00+00:00",
        "player": "Evidence Hitter",
        "playerId": 46901,
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
        "marketValidationVersion": "4.38",
        "calibrationStatus": "passed",
        "calibrationDriftStatus": "stable",
        "marketGateStatus": "promoted",
        "marketGatePromoted": True,
        "marketSideGateStatus": "promoted",
        "promotionStatus": "promoted",
    }
    row.update(changes)
    return row


def normalized_edge(**changes):
    row = normalize_candidate(
        actionable_candidate(**changes),
        surface="edge_lab",
        now=NOW,
    )
    provider = {
        "provider": "The Odds API",
        "state": "ready",
        "configured": True,
        "capturedAt": NOW.isoformat(),
        "eventCount": 15,
        "fetchedEventCount": 15,
        "degradedEventCount": 0,
        "message": "Fresh multi-book prices are available.",
    }
    quotes = [
        {
            "book": "Book A",
            "source": "the-odds-api",
            "capturedAt": NOW.isoformat(),
            "line": 0.5,
            "overPrice": -110,
            "underPrice": -110,
        },
        {
            "book": "Book B",
            "source": "the-odds-api",
            "capturedAt": NOW.isoformat(),
            "line": 0.5,
            "overPrice": -105,
            "underPrice": -115,
        },
    ]
    row["multiBookShoppingVersion"] = "5.9"
    row["multiBookShopping"] = build_multi_book_shopping(
        row,
        quotes,
        provider_health=provider,
        now=NOW,
    )
    return row


def test_actionable_edge_receipt_is_complete_and_bound_to_fingerprint():
    row = normalized_edge()
    receipt = row["evidenceReceipt"]

    assert row["actionable"] is True
    assert receipt["contractVersion"] == RECOMMENDATION_EVIDENCE_VERSION == "4.69"
    assert receipt["candidateId"] == row["canonicalCandidateId"]
    assert receipt["fingerprint"] == row["canonicalFingerprint"]
    assert receipt["selection"] == {
        "marketKey": "batter_hits",
        "side": "Over",
        "line": 0.5,
    }
    assert receipt["price"]["american"] == -110
    assert receipt["price"]["book"] == "Book A"
    assert receipt["price"]["fresh"] is True
    assert receipt["price"]["maximumAgeSeconds"] == 900
    assert receipt["model"]["probability"] == 0.62
    assert receipt["market"]["edge"] == row["canonicalEdge"]
    assert receipt["validation"]["calibrationStatus"] == "passed"
    assert receipt["validation"]["marketGateStatus"] == "promoted"
    assert receipt["explanation"]


def test_edge_lab_fails_closed_without_fresh_complete_evidence():
    row = normalized_edge(
        oddsUpdatedAt=(NOW - timedelta(seconds=901)).isoformat(),
    )

    assert row["actionable"] is False
    assert row["actionabilityStage"] == "Validated"
    assert row["evidenceReceipt"] is None
    assert "sportsbook price is stale" in row["actionabilityReasons"]
    assert "candidate integrity or downstream evidence gate rejected row" in row[
        "actionabilityReasons"
    ]


def test_live_gate_revalidates_receipt_against_enclosing_edge():
    row = normalized_edge()
    payload = {
        "success": True,
        "count": 1,
        "computing": False,
        "computationState": "ready",
        "multiBookShoppingVersion": "5.9",
        "oddsProviderHealth": row["multiBookShopping"]["providerHealth"],
        "edges": [row],
    }
    validate_actionable_edges(payload)

    mismatched = copy.deepcopy(payload)
    mismatched["edges"][0]["evidenceReceipt"]["fingerprint"] = "tampered"
    with pytest.raises(ContractError, match="fingerprint mismatch"):
        validate_actionable_edges(mismatched)


def test_my_hub_requires_and_explains_evidence_receipts():
    source = (ROOT / "static" / "product-hub.js").read_text(encoding="utf-8")
    css = (ROOT / "static" / "product-hub.css").read_text(encoding="utf-8")
    app_source = (ROOT / "app.py").read_text(encoding="utf-8")

    assert "function hasEvidenceReceipt(row)" in source
    assert "edge != null && edge > 0 && hasEvidenceReceipt(row)" in source
    assert "Why this qualifies:" in source
    assert "RECEIPT 4.69" in source
    assert ".signal-why" in css
    for field in (
        "'oddsUpdatedAt': p.get('oddsUpdatedAt')",
        "'modelVersion': p.get('modelVersion')",
        "'marketGatePromoted': p.get('marketGatePromoted')",
        "'recommendationEvidenceVersion': '4.69'",
    ):
        assert field in app_source
