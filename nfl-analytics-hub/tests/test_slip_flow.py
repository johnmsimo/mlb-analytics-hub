"""Regression guard: the tracker pick API must accept the FULL row shape the
bet slip posts (app.js confirmSlip spreads a props-board row), and grading
must work from what survives validation. A trimmed whitelist broke this once
— every slip confirm 400'd and picks were lost."""
from __future__ import annotations

# The exact shape props.html builds + confirmSlip adds (source).
SLIP_ROW = {
    "gameId": "401772510", "season": 2025, "week": 1, "gameday": "2025-09-05",
    "player": "CeeDee Lamb", "playerId": "4241389", "team": "DAL",
    "opponent": "PHI", "position": "WR", "marketKey": "rec_yds",
    "marketLabel": "Rec Yards", "line": 89.5, "side": "over", "price": -110,
    "book": "TestBook", "stakeDollars": 50, "stakeUnits": 5,
    "modelProb": 0.55, "impliedProb": 0.524, "fairProb": 0.5, "edge": 0.026,
    "evPct": 0.05, "kellyPct": 0.013, "modelSource": "analytic",
    "source": "bet_slip",
}


def test_slip_shaped_pick_saves_and_grades(client):
    import pytest
    import nfl_data
    try:
        have_stats = any(r["game_id"] == SLIP_ROW["gameId"]
                         for r in nfl_data.get_player_week_stats(2025, max_new_games=0))
    except Exception:
        have_stats = False

    r = client.post("/api/tracker/pick", json=SLIP_ROW)
    assert r.status_code == 200, r.get_json()
    saved = r.get_json()
    assert saved["grade"] == "pending"
    # Fields grading/CLV depend on must survive validation.
    assert saved["season"] == 2025
    assert saved["gameday"] == "2025-09-05"
    assert saved["playerId"] == "4241389"

    if not have_stats:
        client.delete(f"/api/tracker/pick/2025-09-05/{saved['id']}")
        pytest.skip("2025 stat cache not present — save/validation covered above")

    # Grade against the real cached 2025 result (Lamb: 110 rec yds > 89.5).
    r = client.post("/api/tracker/grade")
    assert r.status_code == 200
    day = client.get("/api/tracker/picks?date=2025-09-05").get_json()["2025-09-05"]
    entry = next(e for e in day["entries"] if e["id"] == saved["id"])
    assert entry["grade"] == "win"
    assert entry["actual"] == 110
    assert entry["profitDollars"] == 45.45

    client.delete(f"/api/tracker/pick/2025-09-05/{saved['id']}")


def test_settings_form_shape_accepted(client):
    # tracker.html posts all four fields — max_bet_pct included.
    r = client.post("/api/tracker/settings", json={
        "bankroll": 2000, "kelly_fraction": 0.25,
        "max_bet_pct": 0.05, "unit_pct": 0.01})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["bankroll"] == 2000
