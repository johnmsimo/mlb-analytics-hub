import json
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask

import app as mlb_app
from guided_parlays import build_guided_parlay, verify_guided_leg
from product_hub import product_hub_bp
from scripts.production_contract_gate import (
    _validate_guided_parlays,
    _validate_journey,
)


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 20, 18, 0, tzinfo=timezone.utc)


def verified_edge(index=1, *, game_pk=None, shopping_state="ready", quote_age=30):
    candidate_id = f"candidate-{index}"
    fingerprint = f"fingerprint-{index}"
    player = f"Verified Player {index}"
    market = "batter_hits" if index % 2 else "batter_total_bases"
    line = 0.5 if market == "batter_hits" else 1.5
    price = -105 - index
    game_pk = game_pk if game_pk is not None else 5100 + index
    captured_at = "2026-08-20T17:59:30+00:00"
    shopping = {
        "version": "5.9",
        "sourceDecisionVersion": "5.3.0",
        "state": shopping_state,
        "reviewRequired": True,
        "changesRecommendation": False,
        "providerHealth": {
            "provider": "The Odds API",
            "state": "ready" if shopping_state == "ready" else shopping_state,
            "configured": True,
            "capturedAt": captured_at,
            "eventCount": 12,
            "fetchedEventCount": 12,
            "degradedEventCount": 0,
            "message": "Fresh multi-book prices are available.",
        },
        "consensus": {
            "requiredBooks": 2,
            "acceptedBookCount": 2,
            "rejectedQuoteCount": 0,
            "fairProbability": 0.51,
            "spread": 0.02,
            "maximumSpread": 0.08,
        },
        "priceShopping": {
            "bestAvailableBook": f"Book {index}",
            "bestAvailablePrice": price,
            "capturedAt": captured_at,
            "quotes": [
                {
                    "book": f"Book {index}",
                    "source": "the-odds-api",
                    "capturedAt": captured_at,
                    "ageSeconds": quote_age,
                    "line": line,
                    "overPrice": price,
                    "underPrice": -114 + index,
                    "selectedPrice": price,
                    "fairProbability": 0.51,
                },
                {
                    "book": f"Book {index + 10}",
                    "source": "the-odds-api",
                    "capturedAt": captured_at,
                    "ageSeconds": quote_age,
                    "line": line,
                    "overPrice": price - 5,
                    "underPrice": -109 + index,
                    "selectedPrice": price - 5,
                    "fairProbability": 0.50,
                },
            ],
        },
        "decision": {
            "status": "qualified",
            "qualifiedForReview": True,
            "approved": False,
            "reasons": [],
            "modelEdge": 0.10,
            "expectedValue": 0.18,
            "thresholds": {"minimumEdge": 0.025, "minimumExpectedValue": 0.03},
            "checkedAt": NOW.isoformat(),
            "fingerprint": f"decision-{index}",
        },
    }
    return {
        "actionable": True,
        "actionabilityStage": "Actionable",
        "player": player,
        "playerId": 51000 + index,
        "team": f"T{index}",
        "opp": f"O{index}",
        "gamePk": game_pk,
        "marketKey": market,
        "marketLabel": "Hits" if market == "batter_hits" else "Total Bases",
        "line": line,
        "side": "Over",
        "canonicalCandidateId": candidate_id,
        "canonicalFingerprint": fingerprint,
        "canonicalMarketKey": market,
        "canonicalSide": "Over",
        "canonicalProbability": 0.66 - index * 0.01,
        "canonicalPrice": price,
        "canonicalBook": f"Book {index}",
        "canonicalEdge": 0.08 - index * 0.002,
        "evidenceReceipt": {
            "contractVersion": "4.69",
            "candidateId": candidate_id,
            "fingerprint": fingerprint,
            "selection": {"marketKey": market, "side": "Over", "line": line},
            "price": {
                "american": price,
                "book": f"Book {index}",
                "observedAt": captured_at,
                "ageSeconds": 30,
                "maximumAgeSeconds": 900,
                "fresh": True,
            },
        },
        "multiBookShoppingVersion": "5.9",
        "multiBookShopping": shopping,
    }


def guided_payload(parlays):
    return {
        "success": True,
        "version": "5.10",
        "date": "2026-08-20",
        "state": "ready" if parlays else "no_verified_combinations",
        "candidateCount": 2,
        "verifiedCandidateCount": 2,
        "withheldCandidateCount": 0,
        "withheldReasonCounts": {},
        "cached": True,
        "computing": False,
        "message": None,
        "generatedAt": NOW.isoformat(),
        "minimumVerifiedLegs": 2,
        "maximumGuidedLegs": 4,
        "requiresEvidenceReceiptVersion": "4.69",
        "requiresMultiBookShoppingVersion": "5.9",
        "reviewRequired": True,
        "approved": False,
        "readOnly": True,
        "failClosed": True,
        "parlays": parlays,
    }


def test_verified_cross_game_legs_build_review_only_combined_risk():
    result = build_guided_parlay(
        [verified_edge(1), verified_edge(2)],
        name="Two-Leg Foundation",
        risk_tier="conservative",
        generated_at=NOW,
    )

    assert result["version"] == "5.10"
    assert result["state"] == "ready"
    assert result["verifiedLegCount"] == 2
    assert result["correlation"]["state"] == "clear"
    assert result["correlation"]["warnings"][0]["type"] == "independence_assumption"
    assert result["combinedRisk"]["atLeastOneLegMissProbability"] > 0.5
    assert len(result["combinedRisk"]["explanations"]) == 3
    assert result["referencePrice"]["bookOfferVerified"] is False
    assert result["decision"] == {
        "status": "review_required",
        "reviewRequired": True,
        "approved": False,
        "trackable": True,
        "reasons": ["human review is required before tracking"],
    }
    encoded = json.dumps(result).lower()
    assert "bankroll" not in encoded
    assert "stakedollars" not in encoded
    assert "payoutper100" not in encoded


def test_degraded_or_stale_multi_book_leg_is_withheld():
    degraded = verified_edge(1, shopping_state="partial")
    stale = verified_edge(2, quote_age=301)

    assert verify_guided_leg(degraded)[0] is None
    assert "multi-book consensus is not ready" in verify_guided_leg(degraded)[1]
    assert verify_guided_leg(stale)[0] is None
    assert "leg contains a stale or malformed quote" in verify_guided_leg(stale)[1]
    result = build_guided_parlay(
        [degraded, stale], name="Withheld", risk_tier="moderate", generated_at=NOW
    )
    assert result["state"] == "unavailable"
    assert result["legs"] == []
    assert result["decision"]["trackable"] is False


def test_unmeasured_same_game_correlation_is_visible_and_not_trackable():
    result = build_guided_parlay(
        [verified_edge(1, game_pk=510), verified_edge(2, game_pk=510)],
        name="Same Game",
        risk_tier="moderate",
        generated_at=NOW,
    )

    assert result["state"] == "review_required"
    assert result["correlation"]["state"] == "unresolved"
    assert result["correlation"]["sameGamePairCount"] == 1
    assert any(
        warning["type"] == "same_game"
        for warning in result["correlation"]["warnings"]
    )
    assert result["decision"]["trackable"] is False


def test_auto_payload_uses_verified_edge_snapshot_and_contract(monkeypatch):
    rows = [verified_edge(i) for i in range(1, 6)]
    monkeypatch.setattr(
        mlb_app,
        "_edge_finder_payload",
        lambda *args, **kwargs: {
            "success": True,
            "computationState": "ready",
            "computing": False,
            "cached": True,
            "edges": rows,
        },
    )

    payload = mlb_app._auto_parlays_payload("2026-08-20")

    assert payload["version"] == "5.10"
    assert payload["state"] == "ready"
    assert payload["verifiedCandidateCount"] == 5
    assert payload["approved"] is False
    assert payload["readOnly"] is True
    assert payload["parlays"]
    _validate_guided_parlays(payload)


def test_product_journey_and_mobile_surface_publish_phase_510_contract():
    app = Flask(__name__)
    app.register_blueprint(product_hub_bp)
    payload = app.test_client().get("/api/product/journey").get_json()
    contract = payload["guidedParlays"]

    assert contract["version"] == "5.10"
    assert contract["requiresEvidenceReceiptVersion"] == "4.69"
    assert contract["requiresMultiBookShoppingVersion"] == "5.9"
    assert contract["unresolvedSameGameCorrelationTrackable"] is False
    assert contract["referencePriceIsBookOffer"] is False
    _validate_journey(payload)

    source = (ROOT / "edge_lab.html").read_text(encoding="utf-8")
    assert "Guided Parlays" in source
    assert "Correlation warning" in source
    assert "At least one leg misses" in source
    assert "Reference odds · not a book offer" in source
    assert "Verified 4.69 + 5.9" in source
    assert "Review & track" in source


def test_app_uses_hot_edge_snapshot_and_revalidates_before_tracker_write():
    source = (ROOT / "app.py").read_text(encoding="utf-8")

    assert "base = _edge_finder_payload(date_str, min_edge=0.025, limit=100)" in source
    assert "build_guided_parlay(" in source
    assert "verify_guided_leg(" in source
    assert "A Phase 5.10 guided-parlay receipt is required." in source
    assert "Current verified recommendation snapshot is unavailable." in source
    assert "denied = _check_admin_auth()" in source


def test_tracker_write_revalidates_fingerprints_and_stores_unapproved_receipt(monkeypatch):
    rows = [verified_edge(1), verified_edge(2)]
    guided = build_guided_parlay(
        rows,
        name="Two-Leg Foundation",
        risk_tier="conservative",
        generated_at=NOW,
    )
    monkeypatch.setattr(mlb_app, "_check_admin_auth", lambda: None)
    monkeypatch.setattr(
        mlb_app,
        "_edge_finder_payload",
        lambda *args, **kwargs: {
            "success": True,
            "computationState": "ready",
            "computing": False,
            "edges": rows,
        },
    )
    committed = {}
    monkeypatch.setattr(mlb_app, "_tracker_store_for_dates", lambda dates: {})
    monkeypatch.setattr(
        mlb_app,
        "_tracker_commit_day",
        lambda date_str, day: committed.update({"date": date_str, "day": day}),
    )
    client = mlb_app.app.test_client()

    changed = json.loads(json.dumps(guided))
    changed["legs"][0]["canonicalFingerprint"] = "expired-fingerprint"
    rejected = client.post(
        "/api/parlay/send-to-tracker",
        json={"date": "2026-08-20", "parlay": changed},
    )
    assert rejected.status_code == 422
    assert "expired, changed, or unverified" in rejected.get_json()["error"]
    assert committed == {}

    accepted = client.post(
        "/api/parlay/send-to-tracker",
        json={"date": "2026-08-20", "parlay": guided},
    )
    assert accepted.status_code == 200
    entry = committed["day"]["entries"][0]
    assert entry["guidedParlayVersion"] == "5.10"
    assert entry["reviewRequired"] is True
    assert entry["approved"] is False
    assert len(entry["selections"]) == 2
