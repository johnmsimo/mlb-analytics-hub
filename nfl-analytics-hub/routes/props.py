"""
Props routes: per-game prop projections and the weekly edge board.

Every row runs the analytic projection at the book's line (when odds exist)
or a synthetic line at the projected median (model-only, tagged no_odds).
Edge/EV/Kelly all flow through value_engine; displayed edge is capped ±0.30.
Row field names follow the tracker schema so "save pick" is a pass-through.
"""
from __future__ import annotations

import threading
import time

from flask import Blueprint, jsonify, request

import nfl_data
import odds_api
import projections as pj
import value_engine as ve

_norm_name = odds_api.norm_player_name

props_bp = Blueprint("props", __name__)

EDGE_DISPLAY_CAP = 0.30
_RESP_CACHE: dict = {}
_RESP_TTL = 600
_cache_lock = threading.Lock()


def _cache_get(key):
    with _cache_lock:
        hit = _RESP_CACHE.get(key)
        if hit and hit[0] > time.time():
            return hit[1]
    return None


def _cache_set(key, val, ttl=_RESP_TTL):
    with _cache_lock:
        _RESP_CACHE[key] = (time.time() + ttl, val)


def _cap(x):
    if x is None:
        return None
    return round(max(-EDGE_DISPLAY_CAP, min(EDGE_DISPLAY_CAP, x)), 4)


def _best_price(rows: list[dict], side: str):
    priced = [r for r in rows if r["side"] == side and isinstance(r.get("price"), (int, float))]
    if not priced:
        return None
    best = max(priced, key=lambda r: ve.american_to_decimal(r["price"]) or 0)
    return {"book": best["book"], "price": best["price"]}


def _build_game_rows(game: dict, season: int) -> list[dict]:
    """All prop rows for one game: model projections joined to book prices."""
    ss = nfl_data.stats_season(season)
    logs = nfl_data.player_game_logs(ss)
    idx = nfl_data.player_index(ss)
    dvp = nfl_data.defense_vs_position(ss)

    # book prop rows grouped by (normalized player, market, line)
    odds_rows: dict[tuple, list[dict]] = {}
    ev = odds_api.find_event_for_game(game) if odds_api.is_configured() else None
    if ev:
        for r in odds_api.parse_prop_markets(odds_api.get_event_props(ev["id"])):
            mk = pj.ODDS_KEY_TO_MARKET.get(r["base_key"])
            if mk and isinstance(r.get("line"), (int, float)):
                odds_rows.setdefault((_norm_name(r["player"]), mk, r["line"]), []).append(r)

    home, away = game["home_team"], game["away_team"]
    rows: list[dict] = []
    for pid, meta in idx.items():
        team = meta["team"]
        if team not in (home, away) or meta["games"] < 3:
            continue
        pos = meta["position"]
        markets = pj.relevant_markets(pos)
        if not markets:
            continue
        opponent = away if team == home else home
        nkey = _norm_name(meta["name"])
        for mk in markets:
            proj = pj.project_stat(logs[pid], mk, opponent=opponent,
                                   dvp=dvp, position=pos)
            if not proj or proj["mean"] < pj.MIN_MEAN[mk]:
                continue
            booked = [(ln, brs) for (nm, m, ln), brs in odds_rows.items()
                      if nm == nkey and m == mk]
            if booked:
                # use the line offered by the most books
                line, brs = max(booked, key=lambda t: len({b["book"] for b in t[1]}))
                no_odds = False
            elif mk == "anytime_td":
                line, brs, no_odds = 0.5, [], True
            else:
                line, brs, no_odds = int(proj["mean"]) + 0.5, [], True
            p_over = pj.prob_over(proj, line)
            best_over, best_under = _best_price(brs, "over"), _best_price(brs, "under")
            fair = None
            if best_over and best_under:
                fair = ve.fair_prob(best_over["price"], best_under["price"])
            side = "over" if p_over >= 0.5 else "under"
            p_side = p_over if side == "over" else 1 - p_over
            best_side = best_over if side == "over" else best_under
            edge = ev_pct = kelly = implied = None
            if best_side:
                implied = ve.american_to_implied(best_side["price"])
                edge = _cap(p_side - implied)
                e = ve.expected_value(p_side, best_side["price"])
                ev_pct = round(e, 4) if e is not None else None
                kelly = ve.kelly_stake(p_side, best_side["price"])["stake_pct"]
            rows.append({
                "gameId": game["game_id"], "season": season,
                "week": game["week"], "gameday": (game.get("date") or "")[:10],
                "player": meta["name"], "playerId": pid, "team": team,
                "opponent": opponent, "position": pos,
                "marketKey": mk, "marketLabel": pj.MARKET_LABELS[mk],
                "line": line, "modelMean": proj["mean"],
                "seasonMean": proj["season_mean"], "l4Mean": proj["l4_mean"],
                "oppFactor": proj["opp_factor"], "games": proj["n"],
                "probOver": p_over, "side": side, "modelProb": round(p_side, 4),
                "bestOver": best_over, "bestUnder": best_under,
                "fairProb": round(fair, 4) if fair is not None else None,
                "impliedProb": round(implied, 4) if implied is not None else None,
                "edge": edge, "evPct": ev_pct, "kellyPct": kelly,
                "grade": ve.edge_grade(edge),
                "bookCount": len({b["book"] for b in brs}),
                "noOdds": no_odds, "modelSource": "analytic",
            })
    rows.sort(key=lambda r: (r["edge"] is None, -(r["edge"] or 0)))
    return rows


@props_bp.route("/api/props/game/<game_id>")
def api_props_game(game_id):
    cw = nfl_data.current_week()
    season = int(request.args.get("season", cw["season"]))
    key = ("game", game_id, season)
    hit = _cache_get(key)
    if hit and request.args.get("refresh") != "1":
        return jsonify(hit)
    game = next((g for g in nfl_data.get_schedule(season)
                 if g["game_id"] == game_id), None)
    if not game:
        return jsonify({"error": "game not found"}), 404
    out = {"game": game, "stats_season": nfl_data.stats_season(season),
           "rows": _build_game_rows(game, season)}
    _cache_set(key, out)
    return jsonify(out)


@props_bp.route("/api/props/board")
def api_props_board():
    cw = nfl_data.current_week()
    season = int(request.args.get("season", cw["season"]))
    week = int(request.args.get("week", cw["week"]))
    stype = request.args.get("type", cw["season_type"] if season == cw["season"] else "REG")
    key = ("board", season, week, stype)
    hit = _cache_get(key)
    if hit and request.args.get("refresh") != "1":
        return jsonify(hit)
    games = nfl_data.get_week_games(season, week, stype)
    rows: list[dict] = []
    for g in games:
        rows.extend(_build_game_rows(g, season))
    rows.sort(key=lambda r: (r["edge"] is None, -(r["edge"] or 0), -r["probOver"]))
    out = {"season": season, "week": week, "season_type": stype,
           "games": len(games), "rows": rows,
           "odds_configured": odds_api.is_configured()}
    _cache_set(key, out)
    return jsonify(out)


@props_bp.route("/api/edges/week")
def api_edges_week():
    """Quant feed: the board filtered to positive-EV priced rows."""
    min_ev = float(request.args.get("minEv", "0.03"))
    resp = api_props_board()
    data = resp.get_json()
    rows = [r for r in data["rows"]
            if r.get("evPct") is not None and r["evPct"] >= min_ev]
    return jsonify({**data, "rows": rows, "minEv": min_ev})
