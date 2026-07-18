"""
Player detail API — the research drill-down behind /player/<pid>.

One payload powers the whole page: identity, full game log, per-market
projections against the player's NEXT opponent (with the book line when the
odds snapshot has one), and the opponent's defense-vs-position context.
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

import nfl_data
import projections as pj

players_bp = Blueprint("players", __name__)


def _next_game(team: str, season: int) -> dict | None:
    for g in nfl_data.get_schedule(season):
        if not g["completed"] and team in (g["home_team"], g["away_team"]):
            return g
    return None


@players_bp.route("/api/player/<pid>")
def api_player(pid):
    cw = nfl_data.current_week()
    season = int(request.args.get("season", cw["season"]))
    ss = nfl_data.stats_season(season)
    logs = nfl_data.player_game_logs(ss)
    idx = nfl_data.player_index(ss)
    meta = idx.get(pid)
    rows = logs.get(pid)
    if not meta or not rows:
        return jsonify({"error": "player not found", "season": ss}), 404

    nxt = _next_game(meta["team"], season)
    opponent = None
    if nxt:
        opponent = nxt["away_team"] if nxt["home_team"] == meta["team"] else nxt["home_team"]
    dvp = nfl_data.defense_vs_position(ss)

    # Book lines for the next game, when the odds snapshot has this player.
    book_lines: dict[str, dict] = {}
    if nxt:
        from routes.props import _build_game_rows
        for r in _build_game_rows(nxt, season):
            if r["playerId"] == pid and not r["noOdds"]:
                book_lines[r["marketKey"]] = {
                    "line": r["line"], "bestOver": r["bestOver"],
                    "bestUnder": r["bestUnder"], "edge": r["edge"],
                    "evPct": r["evPct"], "side": r["side"],
                    "modelProb": r["modelProb"],
                }

    markets = []
    for mk in pj.relevant_markets(meta["position"]):
        proj = pj.project_stat(rows, mk, opponent=opponent, dvp=dvp,
                               position=meta["position"])
        if not proj or proj["mean"] < pj.MIN_MEAN[mk]:
            continue
        col, _ = pj.MARKETS[mk]
        bl = book_lines.get(mk)
        line = bl["line"] if bl else (0.5 if mk == "anytime_td"
                                      else int(proj["mean"]) + 0.5)
        markets.append({
            "marketKey": mk, "label": pj.MARKET_LABELS[mk],
            "statCol": col, "proj": proj, "line": line,
            "probOver": pj.prob_over(proj, line),
            "book": bl, "noOdds": bl is None,
        })

    history = [{
        "week": r["week"], "season_type": r["season_type"],
        "gameday": r["gameday"], "opponent": r["opponent"],
        "home": r["home"], "game_id": r["game_id"],
        **{c: r[c] for c in nfl_data.STAT_COLS},
    } for r in rows]

    return jsonify({
        "player": {"id": pid, "name": meta["name"], "team": meta["team"],
                   "position": meta["position"], "games": meta["games"]},
        "stats_season": ss,
        "next_game": nxt, "opponent": opponent,
        "opponent_dvp": (dvp.get(opponent) or {}).get(
            meta["position"] if meta["position"] in nfl_data.POS_GROUPS else "WR")
            if opponent else None,
        "markets": markets,
        "history": history,
    })
