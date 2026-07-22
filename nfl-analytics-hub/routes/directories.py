"""Directory APIs for games, players, teams, and projection discovery pages."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

import nfl_data
import projections as pj
from routes.dashboard_api import _team_power


directories_bp = Blueprint("directories", __name__)


def _int_arg(name: str, default: int, lo: int = 1, hi: int = 500) -> int:
    try:
        return max(lo, min(hi, int(request.args.get(name, default))))
    except (TypeError, ValueError):
        return default


def _next_game(team: str, season: int) -> dict | None:
    for game in nfl_data.get_schedule(season):
        if not game.get("completed") and team in (game.get("home_team"), game.get("away_team")):
            return game
    return None


@directories_bp.route("/api/players")
def api_players_directory():
    cw = nfl_data.current_week()
    season = int(request.args.get("season", cw["season"]))
    ss = nfl_data.stats_season(season)
    query = (request.args.get("q") or "").strip().lower()
    position = (request.args.get("position") or "ALL").upper()
    team = (request.args.get("team") or "ALL").upper()
    limit = _int_arg("limit", 150, 1, 500)

    idx = nfl_data.player_index(ss)
    logs = nfl_data.player_game_logs(ss)
    rows = []
    for pid, meta in idx.items():
        if query and query not in meta["name"].lower() and query not in meta["team"].lower():
            continue
        if position != "ALL" and meta["position"] != position:
            continue
        if team != "ALL" and meta["team"] != team:
            continue
        history = logs.get(pid, [])
        recent = history[-4:]
        games = max(len(history), 1)
        rows.append({
            "playerId": pid,
            "name": meta["name"],
            "team": meta["team"],
            "position": meta["position"],
            "games": len(history),
            "pass_yds_pg": round(sum(r["passing_yards"] for r in history) / games, 1),
            "rush_yds_pg": round(sum(r["rushing_yards"] for r in history) / games, 1),
            "rec_yds_pg": round(sum(r["receiving_yards"] for r in history) / games, 1),
            "receptions_pg": round(sum(r["receptions"] for r in history) / games, 1),
            "tds": sum(r["passing_tds"] + r["rushing_tds"] + r["receiving_tds"] for r in history),
            "recent_usage": round(sum(r["targets"] + r["carries"] for r in recent) / max(len(recent), 1), 1),
        })
    rows.sort(key=lambda r: (-r["recent_usage"], -r["rec_yds_pg"] - r["rush_yds_pg"], r["name"]))
    teams = sorted({r["team"] for r in rows})
    positions = sorted({r["position"] for r in rows})
    return jsonify({"season": season, "stats_season": ss, "players": rows[:limit],
                    "total": len(rows), "teams": teams, "positions": positions})


@directories_bp.route("/api/teams")
def api_teams_directory():
    cw = nfl_data.current_week()
    season = int(request.args.get("season", cw["season"]))
    ss = nfl_data.stats_season(season)
    summaries = nfl_data.team_summaries(ss)
    dvp = nfl_data.defense_vs_position(ss)
    rows = []
    for team, summary in summaries.items():
        nxt = _next_game(team, season)
        opponent = None
        if nxt:
            opponent = nxt["away_team"] if nxt["home_team"] == team else nxt["home_team"]
        defense = dvp.get(team, {})
        ratios = []
        for pos in ("QB", "RB", "WR", "TE"):
            for key, value in (defense.get(pos) or {}).items():
                if key.endswith("_ratio") and isinstance(value, (int, float)):
                    ratios.append(value)
        defense_index = round(100 / (sum(ratios) / len(ratios)), 1) if ratios else 100.0
        rows.append({
            **summary,
            "power_score": _team_power(summary),
            "point_diff": round(summary.get("ppg", 0) - summary.get("papg", 0), 1),
            "defense_index": defense_index,
            "next_opponent": opponent,
            "next_game_id": nxt.get("game_id") if nxt else None,
            "next_game_date": nxt.get("date") if nxt else None,
        })
    rows.sort(key=lambda r: r["power_score"], reverse=True)
    for i, row in enumerate(rows, 1):
        row["rank"] = i
    return jsonify({"season": season, "stats_season": ss, "teams": rows, "total": len(rows)})


@directories_bp.route("/api/projections")
def api_projections_directory():
    cw = nfl_data.current_week()
    season = int(request.args.get("season", cw["season"]))
    ss = nfl_data.stats_season(season)
    query = (request.args.get("q") or "").strip().lower()
    market_filter = (request.args.get("market") or "ALL").lower()
    position_filter = (request.args.get("position") or "ALL").upper()
    limit = _int_arg("limit", 250, 1, 500)

    idx = nfl_data.player_index(ss)
    logs = nfl_data.player_game_logs(ss)
    dvp = nfl_data.defense_vs_position(ss)
    rows = []
    for pid, meta in idx.items():
        if query and query not in meta["name"].lower() and query not in meta["team"].lower():
            continue
        if position_filter != "ALL" and meta["position"] != position_filter:
            continue
        nxt = _next_game(meta["team"], season)
        opponent = None
        if nxt:
            opponent = nxt["away_team"] if nxt["home_team"] == meta["team"] else nxt["home_team"]
        for market in pj.relevant_markets(meta["position"]):
            if market_filter != "all" and market != market_filter:
                continue
            projection = pj.project_stat(logs.get(pid, []), market, opponent=opponent,
                                         dvp=dvp, position=meta["position"])
            if not projection or projection["mean"] < pj.MIN_MEAN[market]:
                continue
            default_line = 0.5 if market == "anytime_td" else int(projection["mean"]) + 0.5
            probability = pj.prob_over(projection, default_line)
            rows.append({
                "playerId": pid, "player": meta["name"], "team": meta["team"],
                "position": meta["position"], "marketKey": market,
                "marketLabel": pj.MARKET_LABELS[market], "projection": round(projection["mean"], 1),
                "line": default_line, "probOver": round(probability, 4),
                "confidence": projection.get("confidence"), "games": projection.get("games"),
                "opponent": opponent, "gameId": nxt.get("game_id") if nxt else None,
                "opponentFactor": projection.get("opponentFactor", projection.get("opponent_factor")),
            })
    rows.sort(key=lambda r: (-(abs(r["probOver"] - .5)), -r["projection"], r["player"]))
    return jsonify({"season": season, "stats_season": ss, "projections": rows[:limit],
                    "total": len(rows), "markets": [{"key": k, "label": v} for k, v in pj.MARKET_LABELS.items()]})
