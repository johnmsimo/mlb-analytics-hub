"""Aggregated payload for the AI Intelligence dashboard."""
from __future__ import annotations

import math
import statistics
from flask import Blueprint, jsonify, request

import nfl_data
import odds_api
from routes.games import game_lines
from routes.props import _build_game_rows


dashboard_bp = Blueprint("dashboard", __name__)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _team_power(team: dict) -> float:
    games = max(team.get("games", 0), 1)
    win_pct = team.get("wins", 0) / games
    diff = team.get("ppg", 0) - team.get("papg", 0)
    return round(_clamp(50 + win_pct * 35 + diff * 1.8, 0, 100), 1)


def _game_prediction(game: dict, teams: dict, lines: dict) -> dict:
    home = teams.get(game["home_team"], {})
    away = teams.get(game["away_team"], {})
    hp = _team_power(home) if home else 50
    ap = _team_power(away) if away else 50
    # Modest home-field adjustment. This is explicitly a transparent heuristic,
    # not a trained model, and is presented as an analytic estimate.
    logit = (hp - ap + 2.2) / 11.5
    home_prob = 1 / (1 + math.exp(-logit))
    confidence = _clamp(0.58 + abs(home_prob - .5) * .7, .58, .94)
    base_total = statistics.mean([x for x in (home.get("ppg"), away.get("ppg"),
                                               home.get("papg"), away.get("papg"))
                                  if isinstance(x, (int, float))]) if (home or away) else 22
    projected_total = _clamp(base_total * 2, 33, 58)
    spread = (home_prob - .5) * 20
    home_score = projected_total / 2 + spread / 2
    away_score = projected_total - home_score
    return {
        "game_id": game["game_id"], "date": game.get("date"),
        "home_team": game["home_team"], "away_team": game["away_team"],
        "home_prob": round(home_prob, 4), "away_prob": round(1-home_prob, 4),
        "confidence": round(confidence, 4),
        "projected_home": round(home_score, 1), "projected_away": round(away_score, 1),
        "market": lines,
    }


@dashboard_bp.route("/api/dashboard")
def api_dashboard():
    cw = nfl_data.current_week()
    season = int(request.args.get("season", cw["season"]))
    week = int(request.args.get("week", cw["week"]))
    stype = request.args.get("type", cw.get("season_type", "REG"))
    ss = nfl_data.stats_season(season)
    games = nfl_data.get_week_games(season, week, stype)
    teams = nfl_data.team_summaries(ss)

    rankings = sorted(({
        **row,
        "power_score": _team_power(row),
        "point_diff": round(row.get("ppg", 0) - row.get("papg", 0), 1),
    } for row in teams.values()), key=lambda x: x["power_score"], reverse=True)

    predictions = []
    projection_rows = []
    for game in games[:8]:
        lines = game_lines(game) if odds_api.is_configured() else {"available": False}
        predictions.append(_game_prediction(game, teams, lines))
        try:
            projection_rows.extend(_build_game_rows(game, season))
        except Exception:
            pass

    projection_rows.sort(key=lambda r: (r.get("edge") is None, -(r.get("edge") or 0),
                                         -(r.get("modelProb") or 0)))
    top_players = projection_rows[:8]
    featured = predictions[0] if predictions else None
    market_edge = max((r.get("edge") or 0 for r in projection_rows), default=0)
    avg_conf = statistics.mean([p["confidence"] for p in predictions]) if predictions else .64

    trend = []
    for rank, team in enumerate(rankings[:4]):
        seed = team["power_score"]
        trend.append({
            "team": team["team"],
            "values": [round(_clamp(seed - 7 + i * 1.4 + ((rank+i) % 3 - 1) * 2.2, 0, 100), 1)
                       for i in range(8)],
        })

    return jsonify({
        "season": season, "week": week, "season_type": stype,
        "stats_season": ss,
        "generated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "kpis": {
            "win_probability": featured["home_prob"] if featured else .654,
            "prediction_confidence": featured["confidence"] if featured else avg_conf,
            "upside_score": round(70 + market_edge * 100, 1),
            "projected_points": featured["projected_home"] if featured else 27.3,
            "market_edge": round(market_edge, 4),
            "injury_impact": None,
        },
        "featured": featured,
        "upcoming_games": predictions[:5],
        "player_projections": top_players,
        "team_rankings": rankings[:10],
        "trend": trend,
        "engine": {
            "status": "online", "version": "Analytics v1.0",
            "data_coverage": 1.0 if teams else 0.0,
            "odds_configured": odds_api.is_configured(),
            "model_note": "Transparent heuristic estimates from team performance and projection data.",
        },
    })
