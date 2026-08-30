from datetime import datetime, timedelta, timezone
from pathlib import Path

import app as mlb_app
from matchup_simulation_intelligence import build_simulation_signal


ROOT = Path(__file__).resolve().parents[1]


def test_live_recommendation_refresh_replaces_only_core_markets():
    current = [{
        "key": "book-a",
        "title": "Book A",
        "markets": [
            {"key": "batter_hits", "outcomes": [{"price": -130}]},
            {"key": "batter_home_runs", "outcomes": [{"price": 400}]},
        ],
    }]
    refreshed = [
        {
            "key": "book-a",
            "title": "Book A",
            "last_update": "2026-08-21T14:20:00Z",
            "markets": [
                {"key": "batter_hits", "outcomes": [{"price": -105}]},
                {"key": "h2h", "outcomes": [{"price": 110}]},
            ],
        },
        {
            "key": "book-b",
            "title": "Book B",
            "markets": [
                {"key": "pitcher_strikeouts", "outcomes": [{"price": 100}]},
            ],
        },
    ]

    merged = mlb_app._merge_recommendation_bookmakers(current, refreshed)
    by_key = {book["key"]: book for book in merged}
    book_a = {market["key"]: market for market in by_key["book-a"]["markets"]}

    assert book_a["batter_hits"]["outcomes"][0]["price"] == -105
    assert book_a["batter_home_runs"]["outcomes"][0]["price"] == 400
    assert book_a["h2h"]["outcomes"][0]["price"] == 110
    assert by_key["book-b"]["markets"][0]["key"] == "pitcher_strikeouts"


def test_provider_health_uses_live_recommendation_clock(monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(mlb_app, "ODDS_API_KEY", "configured")
    values = {
            "date": mlb_app._odds_today_key(),
            "complete": True,
            "running": False,
            "completedAt": (now - timedelta(hours=8)).isoformat(),
            "recommendationCompletedAt": (now - timedelta(seconds=30)).isoformat(),
            "eventsCount": 2,
            "eventsFetched": 2,
            "recommendationEventsFetched": 2,
            "recommendationErrors": [],
    }
    with mlb_app._ODDS_CACHE_LOCK:
        for key, value in values.items():
            monkeypatch.setitem(mlb_app._ODDS_SNAPSHOT_META, key, value)

    assert mlb_app._recommendation_odds_snapshot_fresh(now=now) is True
    assert mlb_app._odds_recommendation_provider_health(now=now)["state"] == "ready"

    expired = now + timedelta(
        seconds=mlb_app._ODDS_RECOMMENDATION_TTL_SEC + 1,
    )
    assert mlb_app._recommendation_odds_snapshot_fresh(now=expired) is False
    assert mlb_app._odds_recommendation_provider_health(now=expired)["state"] == "stale"


def test_projected_lineup_and_quick_prop_analysis_contracts_are_explicit():
    app_source = (ROOT / "app.py").read_text(encoding="utf-8")
    integration = (ROOT / "intelligence_integration.py").read_text(
        encoding="utf-8",
    )
    dashboard = (ROOT / "dashboard.html").read_text(encoding="utf-8")

    assert "active_roster_season_pa" in app_source
    assert "out.sort(key=lambda row" in app_source
    assert "away_lineup_source = 'projected'" in app_source
    assert "row.get('oddsObservedAt')" in app_source
    assert "scan.get('props')" in integration
    assert "'watchlistPicks': watchlist_picks" in integration
    assert "WATCHLIST · ANALYSIS ONLY" in dashboard
    assert "Tracking and parlay actions are disabled" in dashboard


def test_hub_surfaces_simulated_prop_when_only_live_price_evidence_is_missing():
    now = datetime.now(timezone.utc)
    candidate = {
        "gamePk": 7,
        "gameStatus": "Scheduled",
        "gameAbstractState": "Preview",
        "gameStartIso": (now + timedelta(hours=3)).isoformat(),
        "player": "Projection Hitter",
        "playerId": 101,
        "team": "NYY",
        "playerRole": "batter",
        "playerPosition": "CF",
        "lineupStatus": "projected",
        "marketKey": "batter_hits",
        "line": 0.5,
        "recommendedSide": "Over",
        "adjProb": 0.68,
        "bestAvailablePrice": None,
        "bestAvailableBook": None,
        "bestOverPrice": None,
        "bestUnderPrice": None,
        "oddsUpdatedAt": None,
        "modelVersion": "hits-xgb-2026.08",
        "grade": "pending",
        "hubRating": 82,
    }
    candidate.update(build_simulation_signal(
        .68,
        1500,
        mode="linked_test_game_simulation",
        matchup="Projection Hitter versus starter",
    ))

    rows = mlb_app._projection_analysis_edges([candidate])

    assert len(rows) == 1
    assert rows[0]["player"] == "Projection Hitter"
    assert rows[0]["modelProb"] == .68
    assert rows[0]["selectionMode"] == "projection_only"
    assert rows[0]["promotionStatus"] == "projection_only"
    assert rows[0]["actionable"] is False
    assert "two-sided sportsbook quote" in rows[0]["watchlistReason"]


def test_monte_carlo_board_does_not_render_skip_rows(monkeypatch):
    monkeypatch.setattr(
        mlb_app,
        "_props_scan_today_payload",
        lambda _date: {"props": [], "computing": False, "cached": True},
    )
    monkeypatch.setattr(
        mlb_app,
        "_evaluate_promotable_candidates",
        lambda _rows, _date: {
            "eligible": [{
                "player": "Marginal Hitter",
                "marketKey": "batter_hits",
                "line": .5,
                "adjProb": .60,
                "marketFairProbability": .59,
                "canonicalEdge": .01,
                "canonicalPrice": -110,
                "canonicalBook": "Book A",
                "integrityVersion": "4.37",
            }],
            "audit": {"sourceCount": 1, "eligibleCount": 1},
        },
    )

    payload = mlb_app._mc_board_payload("2026-08-21")

    assert payload["topProps"] == []
    assert payload["filteredSkipCount"] == 1


def test_hub_ui_accepts_projection_only_rows_during_refresh():
    source = (ROOT / "static" / "product-hub.js").read_text(
        encoding="utf-8",
    )

    assert "MODEL PROJECTION · VERIFY PRICE" in source
    assert "['research_only', 'projection_only']" in source
    assert "watchlistRows.length ? 'no_bet' : 'computing'" in source
    assert "Top player-prop analysis is ready" in source


def test_game_card_refresh_is_bounded_and_cache_contract_is_versioned():
    source = (ROOT / "intelligence_integration.py").read_text(
        encoding="utf-8",
    )

    assert "_GAME_CARD_JOB_TIMEOUT_SECONDS = 150" in source
    assert "timeout_seconds=_GAME_CARD_JOB_TIMEOUT_SECONDS" in source
    assert "max_attempts=1" in source
    assert "game_card_intelligence_v439" in source
