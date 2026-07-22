"""
Game routes: weekly slate, single-game detail, lines/totals with de-vig
fair probabilities, best-price EV and line shopping.

Game lines are market-relative (no pretend power-rating model): fair prob =
median de-vig across books, edge/EV = best available price vs that consensus.
"""
from __future__ import annotations

import statistics

from flask import Blueprint, jsonify, request

import nfl_data
import odds_api
import value_engine as ve

games_bp = Blueprint("games", __name__)

EDGE_DISPLAY_CAP = 0.30


def _cap(x: float | None) -> float | None:
    if x is None:
        return None
    return round(max(-EDGE_DISPLAY_CAP, min(EDGE_DISPLAY_CAP, x)), 4)


def _consensus(fairs: list[float]) -> float | None:
    fairs = [f for f in fairs if isinstance(f, (int, float))]
    return round(statistics.median(fairs), 4) if fairs else None


def _two_way_board(rows: list[dict], a_key: str, b_key: str,
                   a_price: str, b_price: str) -> dict:
    """Generic two-way market rollup: per-book devig, consensus fair prob for
    side A, best price each side with EV vs consensus."""
    books, fairs_a = [], []
    for r in rows:
        pa, pb = r.get(a_price), r.get(b_price)
        fair = ve.devig_two_way(pa, pb)
        fa = fair[0] if fair else None
        if fa is not None:
            fairs_a.append(fa)
        books.append({**r, "fair_" + a_key: round(fa, 4) if fa is not None else None})
    cons_a = _consensus(fairs_a)
    out = {"books": books, "consensus_fair": {a_key: cons_a,
                                              b_key: round(1 - cons_a, 4) if cons_a is not None else None}}
    for side, price_key, prob in ((a_key, a_price, cons_a),
                                  (b_key, b_price, 1 - cons_a if cons_a is not None else None)):
        priced = [r for r in rows if isinstance(r.get(price_key), (int, float))]
        if not priced or prob is None:
            out[f"best_{side}"] = None
            continue
        best = max(priced, key=lambda r: ve.american_to_decimal(r[price_key]) or 0)
        ev = ve.expected_value(prob, best[price_key])
        out[f"best_{side}"] = {"book": best["book"], "price": best[price_key],
                               "point": best.get(f"{side}_point", best.get("point")),
                               "ev": round(ev, 4) if ev is not None else None,
                               "edge": _cap(prob - (ve.american_to_implied(best[price_key]) or 0))}
    return out


def _majority_point(rows: list[dict], key: str):
    pts = [r.get(key) for r in rows if isinstance(r.get(key), (int, float))]
    if not pts:
        return None
    return statistics.mode(pts)


def game_lines(game: dict) -> dict:
    """Full line board for one schedule game (empty shells without odds)."""
    ev = odds_api.find_event_for_game(game)
    if not ev:
        return {"available": False}
    mk = odds_api.parse_game_markets(ev)
    out: dict = {"available": True, "odds_event_id": ev.get("id"),
                 "commence_time": ev.get("commence_time")}
    out["h2h"] = _two_way_board(mk["h2h"], "home", "away",
                                "home_price", "away_price")
    sp_point = _majority_point(mk["spreads"], "home_point")
    sp_rows = [r for r in mk["spreads"] if r.get("home_point") == sp_point]
    out["spreads"] = {"point": sp_point,
                      **_two_way_board(sp_rows, "home", "away",
                                       "home_price", "away_price")}
    tot_point = _majority_point(mk["totals"], "point")
    tot_rows = [r for r in mk["totals"] if r.get("point") == tot_point]
    out["totals"] = {"point": tot_point,
                     **_two_way_board(tot_rows, "over", "under",
                                      "over_price", "under_price")}
    return out


@games_bp.route("/api/games/current")
def api_current():
    cw = nfl_data.current_week()
    cw["stats_season"] = nfl_data.stats_season(cw["season"])
    cw["odds_configured"] = odds_api.is_configured()
    return jsonify(cw)


@games_bp.route("/api/games/week")
def api_week():
    cw = nfl_data.current_week()
    season = int(request.args.get("season", cw["season"]))
    week = int(request.args.get("week", cw["week"]))
    stype = request.args.get("type", cw["season_type"] if season == cw["season"] else "REG")
    live = request.args.get("live") == "1"
    games = nfl_data.get_week_games(season, week, stype, live=live)
    teams = nfl_data.team_summaries(nfl_data.stats_season(season))
    out = []
    for g in games:
        row = dict(g)
        row["home_summary"] = teams.get(g["home_team"])
        row["away_summary"] = teams.get(g["away_team"])
        row["lines"] = game_lines(g) if odds_api.is_configured() else {"available": False}
        out.append(row)
    return jsonify({"season": season, "week": week, "season_type": stype,
                    "games": out})


@games_bp.route("/api/game/<game_id>")
def api_game(game_id):
    cw = nfl_data.current_week()
    season = int(request.args.get("season", cw["season"]))
    game = next((g for g in nfl_data.get_schedule(season)
                 if g["game_id"] == game_id), None)
    if not game:
        return jsonify({"error": "game not found", "season": season}), 404
    ss = nfl_data.stats_season(season)
    teams = nfl_data.team_summaries(ss)
    dvp = nfl_data.defense_vs_position(ss)
    return jsonify({
        "game": game,
        "stats_season": ss,
        "home_summary": teams.get(game["home_team"]),
        "away_summary": teams.get(game["away_team"]),
        "home_dvp": dvp.get(game["home_team"]),
        "away_dvp": dvp.get(game["away_team"]),
        "lines": game_lines(game),
    })


@games_bp.route("/api/odds/status")
def api_odds_status():
    return jsonify(odds_api.snapshot_status())
