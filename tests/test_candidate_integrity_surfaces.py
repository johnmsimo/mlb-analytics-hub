from datetime import datetime, timezone
from pathlib import Path

import pytest

import app as mlb_app


@pytest.fixture(autouse=True)
def promoted_hits_market(monkeypatch):
    monkeypatch.setattr(
        mlb_app,
        "_current_market_validation_report",
        lambda *_a, **_k: {
            "version": "4.38",
            "promotedMarkets": ["batter_hits"],
            "marketGates": {
                "batter_hits": {
                    "marketKey": "batter_hits",
                    "status": "promoted",
                    "promoted": True,
                    "reasons": [],
                    "metrics": {},
                },
            },
            "marketSideGates": {
                "batter_hits|over": {
                    "status": "promoted",
                    "promoted": True,
                    "reasons": [],
                    "metrics": {},
                },
            },
        },
    )


def surface_candidate(player="Valid Hitter", **changes):
    row = {
        "gamePk": 7,
        "matchup": "NYY @ BOS",
        "gameStatus": "Scheduled",
        "gameAbstractState": "Preview",
        "gameStartIso": "2099-08-06T23:10:00+00:00",
        "player": player,
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
        "marketPrice": -110,
        "bookmaker": "Book A",
        "oddsUpdatedAt": datetime.now(timezone.utc).isoformat(),
        "modelVersion": "hits-xgb-2026.08",
        "matchupSimulationVersion": "4.35",
        "gameSimN": 1500,
        "sharedSimulationBacked": True,
        "hubRating": 80,
        "evPct": 0.10,
        "grade": "pending",
    }
    row.update(changes)
    return row


def test_edge_and_monte_carlo_surfaces_share_the_same_integrity_gate(monkeypatch):
    valid = surface_candidate()
    completed = surface_candidate(
        player="Completed Hitter",
        playerId=202,
        gameStatus="Final",
        gameAbstractState="Final",
    )
    payload = {
        "success": True,
        "date": "2026-08-06",
        "props": [valid, completed],
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "cached": False,
        "computing": False,
        "cacheAgeSec": 0,
    }
    monkeypatch.setattr(mlb_app, "_props_scan_today_payload", lambda *_a, **_k: payload)

    edges = mlb_app._edge_finder_payload("2026-08-06", min_edge=0.01)
    monte_carlo = mlb_app._mc_board_payload("2026-08-06")

    assert edges["count"] == 1
    assert edges["edges"][0]["player"] == "Valid Hitter"
    assert edges["edges"][0]["actionable"] is True
    assert edges["candidateIntegrityAudit"]["rejectedCount"] == 1
    assert len(monte_carlo["topProps"]) == 1
    assert monte_carlo["topProps"][0]["player"] == "Valid Hitter"
    assert monte_carlo["candidateIntegrityAudit"]["rejectedCount"] == 1


def test_parlays_never_fabricate_model_prices_or_accept_invalid_rows():
    valid = surface_candidate()
    unpriced = surface_candidate(
        player="Unpriced Hitter",
        playerId=303,
        bestAvailablePrice=None,
        bestAvailableBook=None,
        bestOverPrice=None,
        bestUnderPrice=None,
        marketPrice=None,
        bookmaker=None,
    )

    legs = mlb_app._parlay_leg_candidates([valid, unpriced])

    assert len(legs) == 1
    assert legs[0]["player"] == "Valid Hitter"
    assert legs[0]["priceSource"] == "market"
    assert legs[0]["actionable"] is True
    assert legs[0]["canonicalCandidateId"]


def test_manual_parlay_uses_canonical_book_price_and_rejects_research(monkeypatch):
    valid = surface_candidate()
    payload = {"success": True, "props": [valid], "computing": False}
    monkeypatch.setattr(
        mlb_app, "_props_scan_today_payload", lambda *_a, **_k: payload
    )

    with mlb_app.app.test_request_context(
        "/api/parlay/build",
        method="POST",
        json={"selections": [{
            "game_pk": 7,
            "player": "Valid Hitter",
            "market": "batter_hits",
            "line": 0.5,
            "side": "Over",
        }]},
    ):
        response = mlb_app.api_build_parlay()
        result = response.get_json()

    leg = result["parlay"]["selections"][0]
    assert result["success"] is True
    assert leg["american_odds"] == -110
    assert leg["bookmaker"] == "Book A"
    assert leg["canonicalCandidateId"]
    assert result["parlay"]["summary"]["combined_model_probability"] == 0.62
    assert result["parlay"]["summary"]["break_even_prob"] == 0.5238

    with mlb_app.app.test_request_context(
        "/api/parlay/build",
        method="POST",
        json={"selections": [{
            "game_pk": 7,
            "player": "Research Only",
            "market": "batter_hits",
            "line": 0.5,
            "side": "Over",
        }]},
    ):
        response, status = mlb_app.api_build_parlay()

    assert status == 422
    assert response.get_json()["invalidSelections"][0]["player"] == "Research Only"


def test_research_templates_do_not_fabricate_odds_or_offer_parlay_actions():
    root = Path(__file__).resolve().parents[1]
    deep_dive = (root / "deepdive.html").read_text(encoding="utf-8")
    game_side = (root / "gameside_deepdive.html").read_text(encoding="utf-8")
    props = (root / "props.html").read_text(encoding="utf-8")

    combined = deep_dive + game_side + props
    assert "2500 -" not in combined
    assert "DraftKings':pf>50?'FanDuel':'Kalshi" not in combined
    assert "Add To Parlay" not in combined
    assert "Add to Parlay" not in combined
    assert "RESEARCH ONLY" in combined
