from flask import Blueprint, jsonify, request
from intelligence_service import live_games, matchup_intelligence, platform_health, team_intelligence

v2_bp = Blueprint("v2", __name__, url_prefix="/api/v2")

def _season():
    try: return int(request.args.get("season", 2025))
    except ValueError: return 2025

@v2_bp.get("/platform/health")
def health(): return jsonify(platform_health())

@v2_bp.get("/teams/<abbr>/intelligence")
def team(abbr):
    data=team_intelligence(abbr,_season())
    return (jsonify(data),200) if data else (jsonify({"error":"team not found"}),404)

@v2_bp.get("/games/<game_id>/intelligence")
def matchup(game_id):
    data=matchup_intelligence(game_id)
    return (jsonify(data),200) if data else (jsonify({"error":"game not found"}),404)

@v2_bp.get("/live")
def live():
    week=request.args.get("week")
    try: week=int(week) if week is not None else None
    except ValueError: return jsonify({"error":"week must be an integer"}),400
    return jsonify({"season":_season(),"week":week,"games":live_games(_season(),week)})
