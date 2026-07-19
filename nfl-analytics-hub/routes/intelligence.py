"""Analytics and rankings APIs for the Option D product sections."""
from __future__ import annotations

from collections import defaultdict
from flask import Blueprint, jsonify, request

import nfl_data
import projections as pj
from routes.dashboard_api import _team_power

intelligence_bp = Blueprint("intelligence", __name__)


def _season() -> tuple[int, int]:
    raw = request.args.get("season")
    if raw is not None:
        season = int(raw)
    else:
        try:
            season = int(nfl_data.current_week()["season"])
        except Exception:
            season = int(nfl_data.default_season())
    try:
        stats = nfl_data.stats_season(season)
    except Exception:
        stats = season - 1
    return season, stats


def _schedule(season: int) -> list[dict]:
    try:
        return nfl_data.get_schedule(season)
    except Exception:
        return []


def _next_opponent(team: str, season: int) -> str | None:
    for game in _schedule(season):
        if not game.get("completed") and team in (game.get("home_team"), game.get("away_team")):
            return game["away_team"] if game["home_team"] == team else game["home_team"]
    return None


@intelligence_bp.route("/api/analytics")
def api_analytics():
    season, ss = _season()
    summaries = nfl_data.team_summaries(ss)
    logs = nfl_data.player_game_logs(ss)
    idx = nfl_data.player_index(ss)
    dvp = nfl_data.defense_vs_position(ss)

    teams = []
    for code, row in summaries.items():
        diff = round(row.get("ppg", 0) - row.get("papg", 0), 1)
        teams.append({
            "team": code,
            "record": row.get("record", "0-0"),
            "games": row.get("games", 0),
            "ppg": row.get("ppg", 0),
            "papg": row.get("papg", 0),
            "point_diff": diff,
            "power_score": _team_power(row),
            "next_opponent": _next_opponent(code, season),
        })
    teams.sort(key=lambda x: x["power_score"], reverse=True)

    market_rows = []
    position_counts = defaultdict(int)
    for pid, meta in idx.items():
        history = logs.get(pid, [])
        if len(history) < 3:
            continue
        position_counts[meta["position"]] += 1
        opponent = _next_opponent(meta["team"], season)
        for market in pj.relevant_markets(meta["position"]):
            proj = pj.project_stat(history, market, opponent=opponent, dvp=dvp,
                                   position=meta["position"])
            if not proj or proj["mean"] < pj.MIN_MEAN[market]:
                continue
            line = 0.5 if market == "anytime_td" else int(proj["mean"]) + 0.5
            prob = pj.prob_over(proj, line)
            market_rows.append({
                "playerId": pid, "player": meta["name"], "team": meta["team"],
                "position": meta["position"], "marketKey": market,
                "marketLabel": pj.MARKET_LABELS[market], "projection": round(proj["mean"], 1),
                "line": line, "probOver": round(prob, 4), "opponent": opponent,
                "signal": round(abs(prob - .5), 4),
            })
    market_rows.sort(key=lambda x: x["signal"], reverse=True)

    total_games = sum(t["games"] for t in teams)
    avg_points = round(sum(t["ppg"] for t in teams) / max(len(teams), 1), 1)
    avg_diff = round(sum(abs(t["point_diff"]) for t in teams) / max(len(teams), 1), 1)
    return jsonify({
        "season": season, "stats_season": ss,
        "kpis": {
            "teams": len(teams), "player_pool": len(idx), "team_games": total_games,
            "avg_points": avg_points, "avg_abs_diff": avg_diff,
            "projection_signals": len(market_rows),
        },
        "team_efficiency": teams,
        "top_signals": market_rows[:20],
        "position_pool": [{"position": k, "players": v} for k, v in sorted(position_counts.items())],
        "methodology": "Descriptive team efficiency plus transparent distribution-based player projections. Signals measure distance from a 50% over probability at a reference line; they are not betting recommendations.",
    })


@intelligence_bp.route("/api/rankings")
def api_rankings():
    season, ss = _season()
    summaries = nfl_data.team_summaries(ss)
    logs = nfl_data.player_game_logs(ss)
    idx = nfl_data.player_index(ss)

    team_rows = sorted(({
        **row,
        "power_score": _team_power(row),
        "point_diff": round(row.get("ppg", 0) - row.get("papg", 0), 1),
    } for row in summaries.values()), key=lambda x: x["power_score"], reverse=True)
    for i, row in enumerate(team_rows, 1):
        row["rank"] = i

    leaders: dict[str, list[dict]] = {"QB": [], "RB": [], "WR": [], "TE": []}
    for pid, meta in idx.items():
        pos = meta.get("position")
        if pos not in leaders:
            continue
        history = logs.get(pid, [])
        if len(history) < 3:
            continue
        n = len(history)
        if pos == "QB":
            primary = sum(r["passing_yards"] for r in history) / n
            secondary = sum(r["passing_tds"] for r in history) / n
            score = primary / 8 + secondary * 8
            metric = "Pass Yds/G"
        elif pos == "RB":
            primary = sum(r["rushing_yards"] + r["receiving_yards"] for r in history) / n
            secondary = sum(r["carries"] + r["targets"] for r in history) / n
            score = primary / 3 + secondary
            metric = "Scrim Yds/G"
        else:
            primary = sum(r["receiving_yards"] for r in history) / n
            secondary = sum(r["targets"] for r in history) / n
            score = primary / 2.5 + secondary * 1.5
            metric = "Rec Yds/G"
        leaders[pos].append({
            "playerId": pid, "player": meta["name"], "team": meta["team"],
            "games": n, "primary": round(primary, 1), "usage": round(secondary, 1),
            "score": round(score, 1), "metric": metric,
        })
    for pos, rows in leaders.items():
        rows.sort(key=lambda x: x["score"], reverse=True)
        leaders[pos] = rows[:12]
        for i, row in enumerate(leaders[pos], 1):
            row["rank"] = i

    return jsonify({"season": season, "stats_season": ss,
                    "teams": team_rows, "leaders": leaders})
