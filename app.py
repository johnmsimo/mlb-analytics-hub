import os, threading, traceback, difflib, io, csv as csvmod, json, re, time, uuid, unicodedata, logging, glob as _glob
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s %(message)s')
import requests
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo
ET = ZoneInfo("America/New_York")
from flask import Flask, jsonify, request, Response
from flask_cors import CORS


def _load_local_env_file(env_path):
    """Load simple KEY=VALUE pairs from .env into os.environ if missing."""
    if not os.path.exists(env_path):
        return
    try:
        with open(env_path, 'r', encoding='utf-8') as f:
            for raw in f:
                line = (raw or '').strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, val = line.split('=', 1)
                key = key.strip()
                val = val.strip()
                if not key:
                    continue
                if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                    val = val[1:-1]
                os.environ.setdefault(key, val)
    except Exception as ex:
        logging.warning(f"[env] failed loading {env_path}: {ex}")


_load_local_env_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

# XGBoost prop scorer — loaded once at startup; falls back gracefully if models missing
try:
    from xgb_prop_scorer import xgb_hit_prob, xgb_k_prob, xgb_ready, enrich_batter, enrich_pitcher
    _XGB_AVAILABLE = True
except ImportError:
    _XGB_AVAILABLE = False
    def xgb_hit_prob(*a, **k):   return None
    def xgb_k_prob(*a, **k):     return None
    def xgb_ready(_=None):       return False
    def enrich_batter(d, **k):   return d
    def enrich_pitcher(d, **k):  return d

from mc_upgrades import (
    AntitheticRandom,
    BATX_WEIGHTS_V2,
    LEAGUE_PLATOON_SPLITS,
    MARKET_MODEL_WEIGHTS,
    PLATOON_M,
    build_ump_sim_adjustments,
    build_weather_multipliers,
    devig_power,
    derive_probs_v2,
    get_sim_env_adjustments,
    get_prewarm_status,
    logit_blend_prob,
    pitcher_component_v2,
    platoon_blend_v2,
    prewarm_today_caches,
    relief_fatigue_penalty,
    ttop_woba_penalty,
)

# ── Matchup Pipeline ───────────────────────────────────────────────────────────
try:
    from pipeline_scheduler import start_scheduler, get_matchup_df, get_games_df, get_pipeline_status
    from pipeline_routes import pipeline_bp
    _PIPELINE_AVAILABLE = True
except ImportError:
    _PIPELINE_AVAILABLE = False
    logging.warning("[pipeline] pipeline_scheduler or pipeline_routes not found — skipping.")


from brain_merge_patch import (
    load_brain_overlays,
    fg_batter as _brain_fg_batter,
    fg_pitcher as _brain_fg_pitcher,
    sv_batter as _brain_sv_batter,
    sv_pitcher as _brain_sv_pitcher,
    _brain_bat_overlay,
    _brain_pit_overlay,
    _brain_overlay_lock,
)


from utils import ET, _normalize_date_str, _safe_float, _safe_int, _safe_num
from pitcher_stats import configure_pitcher_stats_context, pitcher_stats_mlb, pitcherstatsmlb
from simulation import *
from odds import *
from tracker import *
from props import *

app = Flask(__name__)
CORS(app)

from nrfi_odds_routes import register_nrfi_odds_routes
register_nrfi_odds_routes(app)
register_simulation_routes(app)
register_odds_routes(app)
register_tracker_routes(app)
register_props_routes(app)

if _PIPELINE_AVAILABLE:
    app.register_blueprint(pipeline_bp)
    logging.info("[pipeline] Blueprint registered at /api/pipeline/*")


# --- MLB API PLAYERS INGEST ROUTE (must be after app = Flask(__name__)) ---
@app.route('/api/brain/fetch-mlb-players', methods=['POST'])
def api_brain_fetch_mlb_players():
    """
    Manually fetch and ingest all MLB API player data for all teams (current season).
    """
    try:
        season = request.get_json(silent=True) or {}
        year = season.get('season') or datetime.now().year
        team_ids = [i for i in range(108, 146)]
        result = _memory_ingest_mlb_api_player_stats(team_ids, season=year)
        return jsonify({'success': True, 'summary': result.get('summary', {}), 'details': result})
    except Exception as ex:
        print(f'[api_brain_fetch_mlb_players] {traceback.format_exc()}')
        return jsonify({'success': False, 'error': str(ex)}), 500

# ══════════════════════════════════════════════════════════════════════════════
# BVP CONTEXT ENDPOINTS — Last 10 Games, Platoon Splits, Arsenal Matchup
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/player/<int:player_id>/recent-games', methods=['GET'])
def api_player_recent_games(player_id):
    """Last 10 games for batter with rolling AVG/OPS/HR stats."""
    try:
        year = datetime.now().year
        url = f"{MLB_API}/people/{player_id}/stats"
        params = {"stats": "gameLog", "group": "hitting", "season": year}
        
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        
        stats = r.json().get("stats", [])
        if not stats:
            return jsonify({"last10": {}, "games": []})
        
        splits = stats[0].get("splits", [])
        recent = splits[-10:] if len(splits) >= 10 else splits
        
        # Aggregate stats
        total_ab = sum(_safe_num(g.get("stat", {}).get("atBats"), 0) for g in recent)
        total_hits = sum(_safe_num(g.get("stat", {}).get("hits"), 0) for g in recent)
        total_hr = sum(_safe_num(g.get("stat", {}).get("homeRuns"), 0) for g in recent)
        total_bb = sum(_safe_num(g.get("stat", {}).get("baseOnBalls"), 0) for g in recent)
        total_hbp = sum(_safe_num(g.get("stat", {}).get("hitByPitch"), 0) for g in recent)
        total_sf = sum(_safe_num(g.get("stat", {}).get("sacFlies"), 0) for g in recent)
        
        avg = round(total_hits / total_ab, 3) if total_ab > 0 else 0.000
        
        obp_num = total_hits + total_bb + total_hbp
        obp_den = total_ab + total_bb + total_hbp + total_sf
        obp = round(obp_num / obp_den, 3) if obp_den > 0 else 0.000
        
        doubles = sum(_safe_num(g.get("stat", {}).get("doubles"), 0) for g in recent)
        triples = sum(_safe_num(g.get("stat", {}).get("triples"), 0) for g in recent)
        total_bases = total_hits + doubles + (2 * triples) + (3 * total_hr)
        slg = round(total_bases / total_ab, 3) if total_ab > 0 else 0.000
        ops = round(obp + slg, 3)
        
        games = []
        for g in recent:
            stat = g.get("stat", {})
            games.append({
                "date": g.get("date", ""),
                "opponent": (g.get("opponent", {}) or {}).get("name", ""),
                "ab": int(stat.get("atBats", 0)),
                "hits": int(stat.get("hits", 0)),
                "hr": int(stat.get("homeRuns", 0)),
                "rbi": int(stat.get("rbi", 0)),
                "bb": int(stat.get("baseOnBalls", 0)),
                "k": int(stat.get("strikeOuts", 0))
            })
        
        return jsonify({
            "last10": {
                "games": len(recent),
                "avg": avg,
                "obp": obp,
                "slg": slg,
                "ops": ops,
                "hr": int(total_hr),
                "hits": int(total_hits),
                "ab": int(total_ab)
            },
            "games": games
        })
        
    except Exception as ex:
        logging.error(f"[api_player_recent_games] {ex}")
        return jsonify({"error": str(ex), "last10": {}, "games": []}), 500


@app.route('/api/player/<int:player_id>/platoon-splits', methods=['GET'])
def api_player_platoon_splits(player_id):
    """Career splits vs LHP and RHP."""
    try:
        year = datetime.now().year
        url = f"{MLB_API}/people/{player_id}/stats"
        params = {"stats": "statSplits", "group": "hitting", "sitCodes": "vl,vr", "season": year}

        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()

        stats = r.json().get("stats", [])
        lhp_data = {}
        rhp_data = {}

        for stat_group in stats:
            for split in stat_group.get("splits", []):
                stat = split.get("stat", {})
                split_code = (split.get("split", {}) or {}).get("code", "")

                data_obj = {
                    "pa": int(_safe_num(stat.get("plateAppearances"), 0)),
                    "avg": round(_safe_num(stat.get("avg"), 0), 3),
                    "obp": round(_safe_num(stat.get("obp"), 0), 3),
                    "slg": round(_safe_num(stat.get("slg"), 0), 3),
                    "ops": round(_safe_num(stat.get("ops"), 0), 3),
                    "hr": int(_safe_num(stat.get("homeRuns"), 0))
                }

                if split_code == "vl":
                    lhp_data = data_obj
                elif split_code == "vr":
                    rhp_data = data_obj
        
        return jsonify({"vsLHP": lhp_data, "vsRHP": rhp_data})
        
    except Exception as ex:
        logging.error(f"[api_player_platoon_splits] {ex}")
        return jsonify({"error": str(ex), "vsLHP": {}, "vsRHP": {}}), 500


@app.route('/api/player/<int:batter_id>/arsenal-matchup/<int:pitcher_id>', methods=['GET'])
def api_player_arsenal_matchup(batter_id, pitcher_id):
    """Batter's performance vs pitchers with similar arsenals."""
    try:
        pitcher_info_url = f"{MLB_API}/people/{pitcher_id}"
        p_resp = requests.get(pitcher_info_url, timeout=8)
        p_resp.raise_for_status()
        people = p_resp.json().get("people", [])
        if not people:
            return jsonify({"error": "Pitcher not found", "similarArsenal": {}}), 404
        
        pitcher_name = people[0].get("fullName", "")
        pitcher_hand = ((people[0].get("pitchHand") or {}).get("code") or "R")
        pitcher_arsenal = sv_pitcher(pitcher_name).get("sv_arsenal_pct", {})
        
        if not pitcher_arsenal:
            return jsonify({
                "pitcherName": pitcher_name,
                "pitcherHand": pitcher_hand,
                "arsenal": {},
                "similarArsenal": {},
                "message": "No arsenal data available"
            })
        
        primary_pitch = max(pitcher_arsenal.items(), key=lambda x: x[1])[0] if pitcher_arsenal else None
        
        with _sv_lock:
            arsenal_pct_cache = dict(_sv_arsenal_pct)
        
        similar_pitchers = []
        for name, arsenal in arsenal_pct_cache.items():
            if not arsenal:
                continue
            other_primary = max(arsenal.items(), key=lambda x: x[1])[0] if arsenal else None
            if other_primary == primary_pitch:
                score = _arsenal_similarity(pitcher_arsenal, arsenal)
                if score >= 0.70 and name.lower() != pitcher_name.lower():
                    similar_pitchers.append({"name": name, "similarity": round(score, 2), "arsenal": arsenal})
        
        similar_pitchers.sort(key=lambda x: x["similarity"], reverse=True)
        similar_pitchers = similar_pitchers[:10]
        
        aggregate_stats = {
            "pitchers": len(similar_pitchers),
            "pa": 0,
            "avg": 0.000,
            "ops": 0.000,
            "hr": 0,
            "message": f"Found {len(similar_pitchers)} pitchers with similar arsenal ({PITCH_LABELS.get(primary_pitch, primary_pitch)}-primary)"
        }
        
        return jsonify({
            "pitcherName": pitcher_name,
            "pitcherHand": pitcher_hand,
            "primaryPitch": PITCH_LABELS.get(primary_pitch, primary_pitch),
            "arsenal": {PITCH_LABELS.get(k, k): v for k, v in pitcher_arsenal.items()},
            "similarPitchers": [{
                "name": p["name"],
                "similarity": p["similarity"],
                "primaryPitch": PITCH_LABELS.get(max(p["arsenal"].items(), key=lambda x: x[1])[0], "")
            } for p in similar_pitchers],
            "similarArsenal": aggregate_stats
        })
        
    except Exception as ex:
        logging.error(f"[api_player_arsenal_matchup] {ex}")
        return jsonify({"error": str(ex), "similarArsenal": {}}), 500


def _arsenal_similarity(arsenal1, arsenal2):
    """Cosine similarity between two pitch arsenals (0.0-1.0)."""
    all_pitches = set(list(arsenal1.keys()) + list(arsenal2.keys()))
    vec1 = [arsenal1.get(p, 0) for p in all_pitches]
    vec2 = [arsenal2.get(p, 0) for p in all_pitches]
    dot_product = sum(a * b for a, b in zip(vec1, vec2))
    mag1 = sum(a * a for a in vec1) ** 0.5
    mag2 = sum(b * b for b in vec2) ** 0.5
    return dot_product / (mag1 * mag2) if mag1 and mag2 else 0.0

# ══════════════════════════════════════════════════════════════════════════════
# PERFORMANCE BADGE SYSTEM — Batter & Pitcher Trends
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/player/<int:player_id>/performance-badges', methods=['GET'])
def api_player_performance_badges(player_id):
    """Returns performance badges for a batter based on recent trends."""
    try:
        year = datetime.now().year
        
        # Get player info
        player_info_url = f"{MLB_API}/people/{player_id}"
        p_resp = requests.get(player_info_url, timeout=8)
        p_resp.raise_for_status()
        people = p_resp.json().get("people", [])
        if not people:
            return jsonify({"badges": [], "metrics": {}}), 404
        
        player_name = people[0].get("fullName", "")
        
        # Get last 10 games
        url = f"{MLB_API}/people/{player_id}/stats"
        params = {"stats": "gameLog", "group": "hitting", "season": year}
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        
        stats = r.json().get("stats", [])
        if not stats:
            return jsonify({"badges": [], "metrics": {}})
        
        splits = stats[0].get("splits", [])
        recent = splits[-10:] if len(splits) >= 10 else splits
        last_5 = splits[-5:] if len(splits) >= 5 else splits
        
        badges = []
        metrics = {}
        
        # Calculate streak metrics
        consecutive_hits = 0
        for game in reversed(recent):
            if int(game.get("stat", {}).get("hits", 0)) >= 1:
                consecutive_hits += 1
            else:
                break
        
        # Last 5 games stats
        l5_hits = sum(int(g.get("stat", {}).get("hits", 0)) for g in last_5)
        l5_ab = sum(int(g.get("stat", {}).get("atBats", 0)) for g in last_5)
        l5_hr = sum(int(g.get("stat", {}).get("homeRuns", 0)) for g in last_5)
        l5_xbh = sum(int(g.get("stat", {}).get("doubles", 0)) + int(g.get("stat", {}).get("triples", 0)) for g in last_5)
        l5_k = sum(int(g.get("stat", {}).get("strikeOuts", 0)) for g in last_5)
        
        l5_avg = round(l5_hits / l5_ab, 3) if l5_ab > 0 else 0.000
        l5_k_rate = round(l5_k / l5_ab, 3) if l5_ab > 0 else 0.000
        
        # Get Savant data for barrel rate
        savant_data = sv_batter(player_name) or {}
        barrel_pct = _safe_num(savant_data.get("svbrlpct"), 0)
        hard_hit_pct = _safe_num(savant_data.get("svhhpct"), 0)
        
        metrics = {
            "consecutiveHitGames": consecutive_hits,
            "last5Avg": l5_avg,
            "last5HR": l5_hr,
            "last5XBH": l5_xbh,
            "last5Krate": l5_k_rate,
            "barrelPct": barrel_pct,
            "hardHitPct": hard_hit_pct
        }
        
        # Badge Logic
        if consecutive_hits >= 5:
            badges.append({"type": "HOT_STREAK", "label": f"{consecutive_hits}G Streak", "color": "green", "props": ["HIT", "TB"]})
        elif consecutive_hits >= 3:
            badges.append({"type": "HEATING_UP", "label": f"{consecutive_hits}G Streak", "color": "yellow", "props": ["HIT"]})
        elif consecutive_hits == 0 and l5_avg < 0.180:
            badges.append({"type": "COLD_STREAK", "label": f".{int(l5_avg*1000)} L5", "color": "red", "props": []})
        
        if l5_hr >= 3 or (l5_hr >= 2 and barrel_pct > 12):
            badges.append({"type": "POWER_SURGE", "label": f"{l5_hr} HR L5", "color": "orange", "props": ["HR", "TB"]})
        
        if l5_k_rate < 0.15 and l5_avg > 0.280:
            badges.append({"type": "CONTACT_MODE", "label": f".{int(l5_avg*1000)}/{int(l5_k_rate*100)}%K", "color": "blue", "props": ["HIT", "RBI"]})
        
        if barrel_pct > 15:
            badges.append({"type": "BARREL_KING", "label": f"{barrel_pct:.1f}% BRL", "color": "purple", "props": ["HR", "TB"]})
        
        return jsonify({"badges": badges, "metrics": metrics})
        
    except Exception as ex:
        logging.error(f"[api_player_performance_badges] {ex}")
        return jsonify({"error": str(ex), "badges": [], "metrics": {}}), 500


@app.route('/api/pitcher/<int:pitcher_id>/performance-badges', methods=['GET'])
def api_pitcher_performance_badges(pitcher_id):
    """Returns performance badges for a pitcher based on recent trends & command."""
    try:
        year = datetime.now().year
        
        # Get pitcher info
        pitcher_info_url = f"{MLB_API}/people/{pitcher_id}"
        p_resp = requests.get(pitcher_info_url, timeout=8)
        p_resp.raise_for_status()
        people = p_resp.json().get("people", [])
        if not people:
            return jsonify({"badges": [], "metrics": {}}), 404
        
        pitcher_name = people[0].get("fullName", "")
        pitcher_hand = ((people[0].get("pitchHand") or {}).get("code") or "R")
        
        # Get last 5 starts
        url = f"{MLB_API}/people/{pitcher_id}/stats"
        params = {"stats": "gameLog", "group": "pitching", "season": year}
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        
        stats = r.json().get("stats", [])
        if not stats:
            return jsonify({"badges": [], "metrics": {}})
        
        splits = stats[0].get("splits", [])
        last_5 = splits[-5:] if len(splits) >= 5 else splits
        
        badges = []
        metrics = {}
        
        # Calculate recent stats
        l5_ip = sum(_safe_num(g.get("stat", {}).get("inningsPitched"), 0) for g in last_5)
        l5_bb = sum(int(g.get("stat", {}).get("baseOnBalls", 0)) for g in last_5)
        l5_k = sum(int(g.get("stat", {}).get("strikeOuts", 0)) for g in last_5)
        l5_hits = sum(int(g.get("stat", {}).get("hits", 0)) for g in last_5)
        l5_er = sum(int(g.get("stat", {}).get("earnedRuns", 0)) for g in last_5)
        
        bb_per_9 = round((l5_bb / l5_ip) * 9, 2) if l5_ip > 0 else 0
        k_per_9 = round((l5_k / l5_ip) * 9, 2) if l5_ip > 0 else 0
        era_l5 = round((l5_er / l5_ip) * 9, 2) if l5_ip > 0 else 0
        
        # Get season stats for platoon splits
        split_params = {"stats": "statSplits", "group": "pitching", "sitCodes": "l,r", "season": year}
        split_r = requests.get(f"{MLB_API}/people/{pitcher_id}/stats", params=split_params, timeout=10)
        split_r.raise_for_status()
        split_stats = split_r.json().get("stats", [])
        
        vs_lhb_ops = 0.000
        vs_rhb_ops = 0.000
        
        for stat_group in split_stats:
            for split in stat_group.get("splits", []):
                stat = split.get("stat", {})
                split_code = (split.get("split", {}) or {}).get("code", "")
                if split_code == "l":
                    vs_lhb_ops = round(_safe_num(stat.get("ops"), 0), 3)
                elif split_code == "r":
                    vs_rhb_ops = round(_safe_num(stat.get("ops"), 0), 3)
        
        metrics = {
            "bb9_l5": bb_per_9,
            "k9_l5": k_per_9,
            "era_l5": era_l5,
            "vsLHB_ops": vs_lhb_ops,
            "vsRHB_ops": vs_rhb_ops,
            "pitcherHand": pitcher_hand
        }
        
        # Badge Logic
        if bb_per_9 > 4.0:
            badges.append({"type": "STRUGGLING_COMMAND", "label": f"{bb_per_9} BB/9", "color": "red", "props": ["FADE_K"]})
        
        if era_l5 > 6.0:
            badges.append({"type": "GETTING_SHELLED", "label": f"{era_l5} ERA L5", "color": "red", "props": ["TARGET_BATS"]})
        
        if k_per_9 > 11.0:
            badges.append({"type": "STRIKEOUT_MODE", "label": f"{k_per_9} K/9", "color": "green", "props": ["K_PROP"]})
        
        # Platoon vulnerability
        if pitcher_hand == "R" and vs_lhb_ops > 0.850:
            badges.append({"type": "VULNERABLE_LHB", "label": f"LHB .{int(vs_lhb_ops*1000)}", "color": "orange", "props": ["TARGET_LHB"]})
        elif pitcher_hand == "L" and vs_rhb_ops > 0.850:
            badges.append({"type": "VULNERABLE_RHB", "label": f"RHB .{int(vs_rhb_ops*1000)}", "color": "orange", "props": ["TARGET_RHB"]})
        
        return jsonify({"badges": badges, "metrics": metrics})
        
    except Exception as ex:
        logging.error(f"[api_pitcher_performance_badges] {ex}")
        return jsonify({"error": str(ex), "badges": [], "metrics": {}}), 500

# ── Global error handler for uncaught exceptions ──
@app.errorhandler(Exception)
def handle_exception(e):
    """Return JSON for uncaught exceptions and log the error."""
    logging.error("[Flask] Unhandled exception", exc_info=e)
    resp = {
        "success": False,
        "error": str(e),
        "type": type(e).__name__,
    }
    return jsonify(resp), 500

_HERE = os.path.dirname(os.path.abspath(__file__))

# ── Admin token auth ─────────────────────────────────────────────────────────
# Set ADMIN_TOKEN env var to enable write-route protection.
# When set, POST/PATCH/DELETE requests to admin/tracker routes require:
#   Authorization: Bearer <token>   OR   X-Admin-Token: <token>
_ADMIN_TOKEN = os.getenv('ADMIN_TOKEN', '').strip()

def _check_admin_auth():
    """Return None if auth passes (or is disabled), else a 401 Response."""
    if not _ADMIN_TOKEN:
        return None
    auth_header = request.headers.get('Authorization', '')
    token_header = request.headers.get('X-Admin-Token', '')
    bearer = auth_header.removeprefix('Bearer ').strip() if auth_header.startswith('Bearer ') else ''
    provided = bearer or token_header.strip()
    if provided == _ADMIN_TOKEN:
        return None
    return jsonify({'success': False, 'error': 'Unauthorized'}), 401

def _read_html_or_fallback(filename):
    path = os.path.join(_HERE, filename)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        return f"<h1>{filename} missing from project root</h1>"
    except Exception as ex:
        return f"<h1>Failed to load {filename}: {ex}</h1>"


DASHBOARD_HTML = _read_html_or_fallback('dashboard.html')
DEEP_DIVE_HTML = _read_html_or_fallback('deepdive.html')
PROPS_HTML = _read_html_or_fallback('props.html')
CHEATSHEET_HTML = _read_html_or_fallback('cheatsheet.html')
TRACKER_HTML = _read_html_or_fallback('tracker.html')
CONSISTENCY_HTML = _read_html_or_fallback('consistency.html')
# Pitcher Analysis page - linked from dashboard header as /pitcher-deep-dive.
PITCHER_DEEP_DIVE_HTML = _read_html_or_fallback('pitcher_deepdive.html')
GAMESIDE_DEEPDIVE_HTML = _read_html_or_fallback('gameside_deepdive.html')
BREAKOUT_DETECTOR_HTML = _read_html_or_fallback('breakout_detector.html')
HR_ANALYTICS_HTML = _read_html_or_fallback('hr_analytics.html')
BVP_HTML = _read_html_or_fallback('bvp.html')
VALUE_BETS_HTML = _read_html_or_fallback('value_bets.html')
NRFI_HTML = _read_html_or_fallback('nrfi.html')
TOOLS_HTML = _read_html_or_fallback('tools.html')
DATA_DIR = os.path.join(_HERE, 'data')
os.makedirs(DATA_DIR, exist_ok=True)
BRAIN_DATA_DIR = os.path.join(DATA_DIR, 'brain_uploads')
os.makedirs(BRAIN_DATA_DIR, exist_ok=True)
BRAIN_UPLOAD_STATE_STORE = os.path.join(DATA_DIR, 'brain_upload_state.json')
TRACKER_STORE = os.path.join(DATA_DIR, 'daily_tracker.json')
ADJUST_STORE = os.path.join(DATA_DIR, 'model_adjustments.json')
CAL_HISTORY_STORE = os.path.join(DATA_DIR, 'calibration_history.json')
VALUE_HISTORY_STORE = os.path.join(DATA_DIR, 'value_history.json')
MLB_MEMORY_STORE = os.path.join(DATA_DIR, 'mlb_memory_store.json')
ADMIN_SETTINGS_STORE = os.path.join(DATA_DIR, 'admin_settings.json')
SETTINGS_STORE = os.path.join(DATA_DIR, 'app_settings.json')
MODEL_DAILY_SUMMARY_STORE = os.path.join(DATA_DIR, 'model_daily_summary.json')
_MLB_MEMORY_KEEP_SNAPSHOTS = max(6, int(float(os.getenv('MLB_MEMORY_KEEP_SNAPSHOTS', '30') or 30)))
_MLB_MEMORY_MAX_BYTES = max(500_000, int(float(os.getenv('MLB_MEMORY_MAX_BYTES', '12000000') or 12000000)))
_PROPS_SCAN_CACHE = {}
_CONSISTENCY_CACHE = {}
_props_scan_cache_lock = threading.Lock()
_props_scan_refreshing = False
_consistency_cache_lock = threading.Lock()
_consistency_refreshing = False
_daily_summary_push_lock = threading.Lock()
_daily_summary_push_jobs = {}
_brain_upload_lock = threading.Lock()
_mlb_memory_lock = threading.Lock()
_mlb_memory_collecting = False
_mlb_memory_last_collect = None
_mlb_memory_last_error = None
_PROPS_SCAN_TTL = 20 * 60
_CONSISTENCY_TTL = 20 * 60
_weather_cache_lock = threading.Lock()
_weather_cache = {}
_WEATHER_TTL = 20 * 60
_WEATHER_FAIL_TTL = 30
# Seconds to wait for FG / Savant caches on each request during cold starts.
_CACHE_WAIT_TIMEOUT_SEC = 5
_active_roster_cache_lock = threading.Lock()
_active_roster_cache = {}
_ACTIVE_ROSTER_TTL = 30 * 60


def _load_json(path, default):
    try:
        if not os.path.exists(path):
            return default
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path, payload):
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=2)
        return True
    except Exception:
        return False


def _get_active_roster(team_id, ttl_sec=_ACTIVE_ROSTER_TTL):
    """Fetch and cache active roster entries for a team to avoid repeated API calls."""
    if not team_id:
        return []
    key = str(team_id)
    now = time.time()
    with _active_roster_cache_lock:
        cached = _active_roster_cache.get(key)
        if cached and (now - float(cached.get('ts') or 0.0)) < float(ttl_sec):
            return list(cached.get('roster') or [])
    try:
        rr = requests.get(f"{MLB_API}/teams/{team_id}/roster?rosterType=active", timeout=8)
        rr.raise_for_status()
        roster = list((rr.json() or {}).get('roster') or [])
    except Exception:
        roster = []
    # Only cache successful (non-empty) responses — an empty result from a
    # transient API failure would otherwise poison the cache for 30 minutes.
    if roster:
        with _active_roster_cache_lock:
            _active_roster_cache[key] = {'ts': now, 'roster': roster}
    return list(roster)


def _brain_upload_state_default():
    return {
        "files": {},
        "updatedAt": None,
        "lastIngestedAt": None,
    }


def _get_brain_upload_state():
    payload = _load_json(BRAIN_UPLOAD_STATE_STORE, _brain_upload_state_default())
    if not isinstance(payload, dict):
        payload = _brain_upload_state_default()
    files = payload.get('files')
    if not isinstance(files, dict):
        files = {}
    payload['files'] = files
    return payload


def _save_brain_upload_state(payload):
    base = _brain_upload_state_default()
    if isinstance(payload, dict):
        base.update(payload)
    base['updatedAt'] = datetime.now(timezone.utc).isoformat()
    _save_json(BRAIN_UPLOAD_STATE_STORE, base)
    return base


def _safe_brain_upload_name(filename):
    safe_name = "".join(c for c in (filename or '') if c.isalnum() or c in ('._-'))
    return safe_name or f"upload_{uuid4().hex[:8]}.dat"


def _unique_brain_upload_name(filename):
    safe_name = _safe_brain_upload_name(filename)
    stem, ext = os.path.splitext(safe_name)
    candidate = safe_name
    idx = 1
    while os.path.exists(os.path.join(BRAIN_DATA_DIR, candidate)):
        candidate = f"{stem}_{idx}{ext}"
        idx += 1
    return candidate


def _brain_upload_detect_record_count(payload):
    if isinstance(payload, list):
        return len(payload), payload[:3]
    if isinstance(payload, dict):
        for key in ('rows', 'data', 'items', 'records', 'players', 'games'):
            value = payload.get(key)
            if isinstance(value, list):
                return len(value), value[:3]
        return len(payload.keys()), [dict(list(payload.items())[:5])]
    return 1, [payload]


def _brain_upload_parse_file(file_path, category, existing_entry=None):
    ext = os.path.splitext(file_path)[1].lower()
    stat = os.stat(file_path)
    base = {
        'category': category or 'other',
        'sizeBytes': stat.st_size,
        'sizeKB': round(stat.st_size / 1024, 2),
        'modifiedAt': datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        'parsedAt': datetime.now(timezone.utc).isoformat(),
        'ingestState': 'ingested',
        'error': None,
        'recordCount': 0,
        'fieldCount': 0,
        'fields': [],
        'sample': [],
    }
    try:
        if ext == '.csv':
            with open(file_path, 'r', encoding='utf-8-sig', newline='') as f:
                reader = csvmod.reader(f)
                rows = list(reader)
            if rows:
                header = rows[0]
                data_rows = rows[1:] if header else rows
                base['fieldCount'] = len(header)
                base['fields'] = header[:12]
                base['recordCount'] = len(data_rows)
                base['sample'] = [row[:12] for row in data_rows[:3]]
            else:
                base['ingestState'] = 'empty'
        elif ext == '.json':
            with open(file_path, 'r', encoding='utf-8-sig') as f:
                payload = json.load(f)
            count, sample = _brain_upload_detect_record_count(payload)
            base['recordCount'] = count
            if isinstance(payload, dict):
                base['fields'] = list(payload.keys())[:12]
                base['fieldCount'] = len(payload.keys())
            elif sample and isinstance(sample[0], dict):
                base['fields'] = list(sample[0].keys())[:12]
                base['fieldCount'] = len(sample[0].keys())
            base['sample'] = sample
        else:
            with open(file_path, 'r', encoding='utf-8-sig', errors='replace') as f:
                lines = [line.strip() for line in f.readlines()]
            non_empty = [line for line in lines if line]
            base['recordCount'] = len(non_empty)
            base['fieldCount'] = 1
            base['fields'] = ['text']
            base['sample'] = non_empty[:3]
            if not non_empty:
                base['ingestState'] = 'empty'
    except Exception as ex:
        base['ingestState'] = 'error'
        base['error'] = str(ex)
    if existing_entry:
        base['uploadedAt'] = existing_entry.get('uploadedAt')
    return base


def _process_uploaded_brain_files(force=False):
    with _brain_upload_lock:
        state = _get_brain_upload_state()
        files_state = state.get('files', {})
        existing_names = set(files_state.keys())
        disk_names = set()
        for fname in sorted(os.listdir(BRAIN_DATA_DIR)) if os.path.exists(BRAIN_DATA_DIR) else []:
            fpath = os.path.join(BRAIN_DATA_DIR, fname)
            if not os.path.isfile(fpath):
                continue
            disk_names.add(fname)
            entry = files_state.get(fname) or {}
            if not entry:
                files_state[fname] = {
                    'filename': fname,
                    'category': 'other',
                    'uploadedAt': datetime.fromtimestamp(os.path.getmtime(fpath), tz=timezone.utc).isoformat(),
                    'ingestState': 'staged',
                }
                entry = files_state[fname]
            modified_at = datetime.fromtimestamp(os.path.getmtime(fpath), tz=timezone.utc).isoformat()
            needs_parse = force or entry.get('parsedAt') is None or entry.get('modifiedAt') != modified_at or entry.get('ingestState') in ('staged', 'error')
            entry['filename'] = fname
            entry['modifiedAt'] = modified_at
            entry['sizeBytes'] = os.path.getsize(fpath)
            entry['sizeKB'] = round(entry['sizeBytes'] / 1024, 2)
            if needs_parse:
                parsed = _brain_upload_parse_file(fpath, entry.get('category'), entry)
                entry.update(parsed)
            files_state[fname] = entry
        for stale_name in sorted(existing_names - disk_names):
            files_state.pop(stale_name, None)
        state['files'] = files_state
        state['lastIngestedAt'] = datetime.now(timezone.utc).isoformat()
        _save_brain_upload_state(state)
        return _brain_uploaded_files_summary(state=state)


def _brain_uploaded_files_summary(state=None):
    if state is None:
        state = _get_brain_upload_state()
    files_state = state.get('files', {})
    files = []
    total_size_bytes = 0
    total_records = 0
    ingested_files = 0
    staged_files = 0
    error_files = 0
    empty_files = 0
    latest_modified_at = None
    latest_filename = ''
    for fname in sorted(files_state.keys()):
        entry = dict(files_state.get(fname) or {})
        fpath = os.path.join(BRAIN_DATA_DIR, fname)
        if not os.path.isfile(fpath):
            continue
        size_bytes = int(entry.get('sizeBytes') or os.path.getsize(fpath) or 0)
        total_size_bytes += size_bytes
        total_records += int(entry.get('recordCount') or 0)
        ingest_state = entry.get('ingestState') or 'staged'
        if ingest_state == 'ingested':
            ingested_files += 1
        elif ingest_state == 'error':
            error_files += 1
        elif ingest_state == 'empty':
            empty_files += 1
        else:
            staged_files += 1
        modified_at = entry.get('modifiedAt') or datetime.fromtimestamp(os.path.getmtime(fpath), tz=timezone.utc).isoformat()
        if latest_modified_at is None or modified_at > latest_modified_at:
            latest_modified_at = modified_at
            latest_filename = fname
        ext = os.path.splitext(fname)[1].lower()
        files.append({
            'filename': fname,
            'sizeBytes': size_bytes,
            'sizeKB': round(size_bytes / 1024, 2),
            'type': 'JSON' if ext == '.json' else 'CSV' if ext == '.csv' else 'TXT' if ext == '.txt' else 'File',
            'category': entry.get('category') or 'other',
            'uploadedAt': entry.get('uploadedAt'),
            'modifiedAt': modified_at,
            'parsedAt': entry.get('parsedAt'),
            'ingestState': ingest_state,
            'recordCount': int(entry.get('recordCount') or 0),
            'fieldCount': int(entry.get('fieldCount') or 0),
            'fields': entry.get('fields') or [],
            'error': entry.get('error'),
        })
    status_parts = []
    if ingested_files:
        status_parts.append(f"{ingested_files} ingested")
    if staged_files:
        status_parts.append(f"{staged_files} staged")
    if empty_files:
        status_parts.append(f"{empty_files} empty")
    if error_files:
        status_parts.append(f"{error_files} error")
    ingestion_state = 'empty'
    if error_files:
        ingestion_state = 'partial_error' if ingested_files else 'error'
    elif staged_files:
        ingestion_state = 'staged' if not ingested_files else 'partial'
    elif ingested_files or empty_files:
        ingestion_state = 'ingested'
    message = 'No uploaded files.'
    if files:
        if staged_files:
            message = 'Uploaded files are staged. Run INGEST NOW to parse them into brain memory.'
        elif error_files:
            message = 'Some uploaded files failed to ingest. Check the file list for details.'
        else:
            message = 'Uploaded files have been parsed into manual brain memory for this session.'
    return {
        'count': len(files),
        'totalSizeBytes': total_size_bytes,
        'totalSizeKB': round(total_size_bytes / 1024, 2),
        'totalRecords': total_records,
        'ingestedFiles': ingested_files,
        'stagedFiles': staged_files,
        'errorFiles': error_files,
        'emptyFiles': empty_files,
        'latestFilename': latest_filename,
        'latestModifiedAt': latest_modified_at,
        'files': files,
        'ingestionState': ingestion_state,
        'lastIngestedAt': state.get('lastIngestedAt'),
        'message': message,
        'statusLabel': ', '.join(status_parts) if status_parts else '0 files',
    }


def _admin_settings_default():
    return {
        "orgName": "MLB Analytics Hub",
        "adminName": "",
        "contactEmail": "",
        "timezone": "America/New_York",
        "notes": "",
        "operationalMode": "normal",
        "updatedAt": None,
    }


def _get_admin_settings():
    payload = _load_json(ADMIN_SETTINGS_STORE, _admin_settings_default())
    if not isinstance(payload, dict):
        payload = _admin_settings_default()
    base = _admin_settings_default()
    base.update({k: v for k, v in payload.items() if k in base})
    return base


def _save_admin_settings(payload):
    current = _get_admin_settings()
    for k in ("orgName", "adminName", "contactEmail", "timezone", "notes", "operationalMode"):
        if k in payload:
            current[k] = payload.get(k)
    current["updatedAt"] = datetime.now(timezone.utc).isoformat()
    _save_json(ADMIN_SETTINGS_STORE, current)
    return current


def _app_settings_default():
    return {
        "intelligence": {
            "enableAI": True,
            "claudeModel": "claude-sonnet-4-20250514",
            "insightStyle": "detailed",
            "parkFactorWeight": 0.30,
            "weatherWeight": 0.20,
            "opponentWeight": 0.25,
            "homeDepthWeight": 0.10,
            "awayDepthWeight": 0.15,
        },
        "dataCollection": {
            "enableFanGraphs": True,
            "enableSavant": True,
            "enableOdds": True,
            "enableWeather": True,
            "fgRefreshHours": 6,
            "savantRefreshHours": 4,
            "oddsRefreshMinutes": 30,
            "weatherRefreshMinutes": 60,
        },
        "apiConfig": {
            "oddsEventsTTLSec": int(os.getenv('ODDS_EVENTS_TTL_SEC', '21600')),
            "oddsGameTTLSec": int(os.getenv('ODDS_GAME_TTL_SEC', '86400')),
            "oddsNRFITTLSec": int(os.getenv('ODDS_NRFI_TTL_SEC', '300')),
            "memoryKeepSnapshots": _MLB_MEMORY_KEEP_SNAPSHOTS,
            "memoryMaxBytesMB": _MLB_MEMORY_MAX_BYTES // (1024 * 1024),
            "weatherCacheTTLMin": 20,
            "propScanCacheTTLMin": 20,
            "consistencyCacheTTLMin": 20,
        },
        "performance": {
            "backgroundWorkerIntervalMin": 180,
            "maxConcurrentRequests": 10,
            "apiTimeoutSec": 30,
            "enableDiagnostics": False,
            "logLevel": "INFO",
        },
        "updatedAt": None,
    }


def _get_app_settings():
    payload = _load_json(SETTINGS_STORE, _app_settings_default())
    if not isinstance(payload, dict):
        payload = _app_settings_default()
    base = _app_settings_default()
    base.update({k: v for k, v in payload.items() if k in base})
    for section in ['intelligence', 'dataCollection', 'apiConfig', 'performance']:
        if section in base and section in payload:
            base[section].update({k: v for k, v in payload[section].items() if k in base[section]})
    return base


def _save_app_settings(payload):
    if not isinstance(payload, dict):
        return _get_app_settings()
    current = _get_app_settings()
    current['updatedAt'] = datetime.now(timezone.utc).isoformat()
    for section in ['intelligence', 'dataCollection', 'apiConfig', 'performance']:
        if section in payload and isinstance(payload[section], dict):
            for key, val in payload[section].items():
                if key in current.get(section, {}):
                    current[section][key] = val
    _save_json(SETTINGS_STORE, current)
    return current


def _mlb_memory_store_default():
    return {
        "latest": None,
        "snapshots": [],
        "updatedAt": None,
    }


def _mlb_memory_store_payload():
    payload = _load_json(MLB_MEMORY_STORE, _mlb_memory_store_default())
    if not isinstance(payload, dict):
        return _mlb_memory_store_default()
    payload.setdefault("latest", None)
    payload.setdefault("snapshots", [])
    payload.setdefault("updatedAt", None)
    if not isinstance(payload.get("snapshots"), list):
        payload["snapshots"] = []
    return payload


def _append_mlb_memory_snapshot(snapshot, keep=30):
    payload = _mlb_memory_store_payload()
    snapshots = payload.get("snapshots") or []
    snapshots.append(snapshot)
    keep = max(6, int(keep or _MLB_MEMORY_KEEP_SNAPSHOTS))
    if len(snapshots) > keep:
        snapshots = snapshots[-keep:]

    # Keep newest snapshots rich, compact older ones to preserve long-term history.
    compacted = []
    for idx, snap in enumerate(snapshots):
        is_recent = idx >= max(0, len(snapshots) - 3)
        compacted.append(_compact_mlb_memory_snapshot(snap, keep_detail=is_recent))
    snapshots = compacted

    # Prune oldest snapshots until file size target is respected.
    while snapshots:
        test_payload = {
            "latest": snapshots[-1],
            "snapshots": snapshots,
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        }
        est_size = len(json.dumps(test_payload, ensure_ascii=False))
        if est_size <= _MLB_MEMORY_MAX_BYTES or len(snapshots) <= 6:
            break
        snapshots = snapshots[1:]

    payload["snapshots"] = snapshots
    payload["latest"] = snapshots[-1] if snapshots else _compact_mlb_memory_snapshot(snapshot, keep_detail=True)
    payload["updatedAt"] = datetime.now(timezone.utc).isoformat()
    _save_json(MLB_MEMORY_STORE, payload)
    return payload


def _compact_mlb_memory_snapshot(snapshot, keep_detail=False):
    if not isinstance(snapshot, dict):
        return {}
    out = dict(snapshot)
    out["compact"] = not bool(keep_detail)

    games = dict(out.get("games") or {})
    boxscores = list(games.get("boxscores") or [])
    if not keep_detail and len(boxscores) > 6:
        games["boxscoresSample"] = boxscores[:6]
        games["boxscoreCount"] = len(boxscores)
        games.pop("boxscores", None)
    out["games"] = games

    players = dict(out.get("players") or {})
    featured = list(players.get("featured") or [])
    if not keep_detail and len(featured) > 40:
        players["featuredSample"] = featured[:40]
        players["featuredCount"] = len(featured)
        players.pop("featured", None)
    out["players"] = players

    schedule = list(out.get("schedule") or [])
    if not keep_detail and len(schedule) > 3:
        out["schedule"] = schedule[:3]
        out["scheduleTruncated"] = True

    return out

MLB_API   = "https://statsapi.mlb.com/api/v1"
WX_API    = "https://api.open-meteo.com/v1/forecast"

# MLB stadium coordinates keyed by MLB venue ID (from /api/v1/venues)
STADIUM_COORDS = {
    1:    (33.80019044, -117.8823996),  # Angel Stadium, Anaheim
    2:    (39.283787,   -76.621689),    # Oriole Park at Camden Yards, Baltimore
    3:    (42.346456,   -71.097441),    # Fenway Park, Boston
    4:    (41.83,       -87.634167),    # Rate Field (Guaranteed Rate), Chicago
    5:    (41.495861,   -81.685255),    # Progressive Field, Cleveland
    7:    (39.051567,   -94.480483),    # Kauffman Stadium, Kansas City
    12:   (27.767778,   -82.6525),      # Tropicana Field, St. Petersburg (dome)
    14:   (43.64155,    -79.38915),     # Rogers Centre, Toronto (dome)
    15:   (33.445302,   -112.066687),   # Chase Field, Phoenix (retractable)
    17:   (41.948171,   -87.655503),    # Wrigley Field, Chicago
    19:   (39.756042,   -104.994136),   # Coors Field, Denver
    22:   (34.07368,    -118.24053),    # Dodger Stadium, Los Angeles
    31:   (40.446904,   -80.005753),    # PNC Park, Pittsburgh
    32:   (43.02838,    -87.97099),     # American Family Field, Milwaukee (retractable)
    680:  (47.591333,   -122.33251),    # T-Mobile Park, Seattle (retractable)
    2392: (29.756967,   -95.355509),    # Daikin Park (Minute Maid), Houston (retractable)
    2394: (42.3391151,  -83.048695),    # Comerica Park, Detroit
    2395: (37.778383,   -122.389448),   # Oracle Park, San Francisco
    2529: (38.57994,    -121.51246),    # Sutter Health Park, Sacramento
    2602: (39.097389,   -84.506611),    # Great American Ball Park, Cincinnati
    2680: (32.707861,   -117.157278),   # Petco Park, San Diego
    2681: (39.90539086, -75.16716957),  # Citizens Bank Park, Philadelphia
    2889: (38.62256667, -90.19286667),  # Busch Stadium, St. Louis
    3289: (40.75753012, -73.84559155),  # Citi Field, New York (Mets)
    3309: (38.872861,   -77.007501),    # Nationals Park, Washington DC
    3312: (44.981829,   -93.277891),    # Target Field, Minneapolis
    3313: (40.82919482, -73.9264977),   # Yankee Stadium, New York
    4169: (25.77796236, -80.21951795),  # loanDepot park, Miami (retractable)
    4705: (33.890672,   -84.467641),    # Truist Park, Atlanta
    5325: (32.747299,   -97.081818),    # Globe Life Field, Arlington TX (retractable)
    4321: (32.747299,   -97.081818),    # Globe Life Field alt ID
}

# Domed / retractable-roof stadiums (weather is always INDOOR/controlled)
# ── Team Defensive Metrics (UZR) ────────────────────────────────────────────
# UZR (Ultimate Zone Rating) per team from FanGraphs team defense.
# Positive = above-average defense; Negative = below-average.
# Updated at startup via _load_fg_data(); default 0.0 = league average.
# Source: FanGraphs /leaders.aspx?pos=all&stats=fld&lg=all&qual=0&type=1
_TEAM_UZR: dict[int, float] = {}  # team_id → UZR (populated at startup)
_LEAGUE_UZR_AVG = 0.0

def _team_uzr_hit_mult(fielding_team_id: int) -> float:
    """
    Convert team UZR to a hit-suppression multiplier applied to p_hit in sim.
    Research basis: +10 UZR ≈ −0.010 BABIP (Lichtman 2010, FanGraphs team def).
    Effect clamped to ±6% to avoid overweighting in small samples.
    """
    uzr = _TEAM_UZR.get(fielding_team_id, _LEAGUE_UZR_AVG)
    babip_delta = (uzr - _LEAGUE_UZR_AVG) / 10.0 * 0.010
    return min(1.06, max(0.94, 1.0 - babip_delta * 3.0))

DOME_VENUES = {
    12,    # Tropicana Field (fixed dome)
    14,    # Rogers Centre (retractable)
    15,    # Chase Field (retractable)
    32,    # American Family Field (retractable)
    680,   # T-Mobile Park (retractable)
    2392,  # Daikin Park / Minute Maid (retractable)
    4169,  # loanDepot park (retractable)
    5325,  # Globe Life Field (retractable)
    4321,  # Globe Life Field alt
}

LOGO_BASE = "https://www.mlbstatic.com/team-logos/{team_id}.svg"
PARK_FACTORS = {
    133:1.08,144:0.92,110:0.97,111:1.04,112:0.97,137:0.95,109:1.06,
    145:1.03,116:1.00,158:0.97,142:1.00,147:0.97,143:1.03,140:1.05,
    146:0.95,121:0.97,136:0.93,138:1.02,141:0.98,139:0.99,108:0.96,
    117:0.97,135:0.98,120:0.98,134:0.97,119:0.95,118:1.02,114:1.01,
    113:0.94,115:1.00
}

# ── HR-specific Park Factors (index 100 = neutral, higher = more HRs) ────────
# Approximate 3-year rolling HR park index from Statcast data.
# Camden Yards (110) reflects the 2025 wall restoration; override if Savant
# rolling window hasn't fully refreshed yet.
HR_PARK_FACTORS = {
    109: 114, 144: 94,  110: 108, 111: 101, 112: 109, 137: 92,
    115: 112, 116: 100, 117: 103, 118: 108, 119: 104, 108: 95,
    146: 95,  158: 99,  142: 99,  121: 100, 147: 118, 133: 107,
    143: 111, 134: 95,  136: 95,  138: 98,  139: 96,  140: 112,
    141: 102, 120: 99,  135: 97,  145: 95,  113: 100, 114: 98,
}

_fg_lock = threading.Lock()
_fg_bat = {}
_fg_pit = {}
_fg_loaded = False
_fg_load_date = None
_fg_loading = False


def _load_fg_data():
    global _fg_loaded, _fg_load_date
    logging.info("[FG] _load_fg_data: starting MLB-API-derived load…")
    _load_fg_data_from_mlb_api()
    with _fg_lock:
        has_data = bool(_fg_pit) or bool(_fg_bat)
        _fg_loaded = has_data
        _fg_load_date = datetime.now().date() if has_data else None
    logging.info(f"[FG] _load_fg_data done: loaded={_fg_loaded} pit={len(_fg_pit)} bat={len(_fg_bat)}")




def _normalize_date_str(value, fallback=None):
    """Return a YYYY-MM-DD string from loose date/datetime inputs."""
    fb = (fallback or datetime.now(ET).strftime("%Y-%m-%d")).strip()
    raw = str(value or "").strip()
    if not raw:
        return fb

    token = raw.split("T", 1)[0].strip().replace("/", "-")
    try:
        return datetime.strptime(token, "%Y-%m-%d").strftime("%Y-%m-%d")
    except Exception:
        return fb


def _season_candidates(depth=4):
    """Return candidate MLB seasons from current year backwards."""
    y = datetime.now().year
    out = []
    for i in range(max(1, int(depth)) + 1):
        out.append(max(2000, y - i))
    # Preserve order while removing duplicates.
    return list(dict.fromkeys(out))


def _load_fg_data_from_mlb_api():
    """Derive FG-compatible batting/pitching caches from the MLB Stats API
    *bulk* stats endpoint (`/stats?playerPool=all`).

    Using the bulk endpoint instead of 900+ per-player calls:
      * one API call for all pitchers, one for all hitters
      * ~2 seconds instead of 90 seconds
      * zero silent drops from rate limiting / timeouts
      * no `list index out of range` on empty splits

    Populates the same keys as pybaseball, so fg_pitcher()/fg_batter() lookups
    work without any downstream changes.
    """
    global _fg_bat, _fg_pit
    year = datetime.now().year
    logging.info("[FG] Starting MLB-API-derived load…")

    # Fetch every active player's batSide so the matchup projection can use
    # handedness.  One small call for the whole league.
    bat_side_by_name = {}
    try:
        r = requests.get(
            f"{MLB_API}/sports/1/players?season={year}&activeStatus=Y",
            timeout=20,
        )
        r.raise_for_status()
        for p in (r.json().get("people", []) or []):
            name = (p.get("fullName") or "").strip()
            if name:
                bat_side_by_name[name.lower()] = ((p.get("batSide") or {}).get("code") or "R")
        logging.info(f"[FG] {len(bat_side_by_name)} active players found")
    except Exception as ex:
        logging.warning(f"[FG-derived] player list failed: {ex}")

    def _fetch_bulk_splits(group_name):
        """Fetch MLB bulk stat splits with fallbacks for endpoint quirks."""
        seasons = _season_candidates(depth=4)
        variants = [
            {"gameType": "R", "sportId": 1},
            {"sportId": 1},
            {"gameType": "R"},
            {},
        ]
        for season in seasons:
            for variant in variants:
                params = {
                    "stats": "season",
                    "group": group_name,
                    "season": season,
                    "playerPool": "all",
                    "limit": 5000,
                    "min": 1,
                }
                params.update(variant)
                try:
                    r = requests.get(f"{MLB_API}/stats", params=params, timeout=45)
                    r.raise_for_status()
                    stats_groups = r.json().get("stats", []) or []
                    splits = stats_groups[0].get("splits", []) if stats_groups else []
                    if splits:
                        logging.info(f"[FG-derived] {group_name} splits={len(splits)} season={season}")
                        return splits, season
                    logging.info(f"[FG-derived] {group_name} empty season={season} — trying next variant")
                except requests.exceptions.Timeout:
                    logging.warning(f"[FG-derived] {group_name} TIMEOUT season={season} — trying next")
                except Exception as ex:
                    logging.warning(f"[FG-derived] {group_name} fetch failed season={season}: {ex}")
        return [], None

    # ── Pitchers (one bulk call) ──────────────────────────────────────────
    pit_out = {}
    splits, pit_season = _fetch_bulk_splits("pitching")
    if pit_season and pit_season != year:
        logging.info(f"[FG-derived] Using fallback pitching season {pit_season}")

    for row in splits:
        try:
            person = row.get("player") or {}
            pid    = person.get("id")
            name   = (person.get("fullName") or "").strip()
            if not name: continue
            s = row.get("stat") or {}
            if not s: continue

            era  = _safe_num(s.get("era"), 4.50)
            whip = _safe_num(s.get("whip"), 1.30)
            ip   = _safe_num(s.get("inningsPitched"), 0)
            k9   = _safe_num(s.get("strikeoutsPer9Inn"), 8.5)
            bb9  = _safe_num(s.get("walksPer9Inn"), 3.2)
            hr9  = _safe_num(s.get("homeRunsPer9"), 1.1)
            so   = _safe_num(s.get("strikeOuts"), 0)
            bb   = _safe_num(s.get("baseOnBalls"), 0)
            hr   = _safe_num(s.get("homeRuns"), 0)
            hits = _safe_num(s.get("hits"), 0)
            hbp  = _safe_num(s.get("hitByPitch"), 0)
            bf   = _safe_num(s.get("battersFaced"), 0)
            ab   = _safe_num(s.get("atBats"), 0)
            ao   = _safe_num(s.get("airOuts"), 0)
            go   = _safe_num(s.get("groundOuts"), 0)
            runs = _safe_num(s.get("runs"), 0)

            fip = (round((13*hr + 3*(bb + hbp) - 2*so) / ip + 3.10, 2)
                   if ip > 0 else round(era, 2))
            league_hrfb = 0.108
            fly_balls   = ao
            exp_hr = fly_balls * league_hrfb if fly_balls > 0 else hr
            xfip = (round((13*exp_hr + 3*(bb + hbp) - 2*so) / ip + 3.10, 2)
                    if ip > 0 else fip)

            pa_est  = bf if bf > 0 else max(ip * 4.3, 1)
            kpct_v  = round(so / pa_est, 3) if pa_est > 0 else 0.0
            bbpct_v = round(bb / pa_est, 3) if pa_est > 0 else 0.0

            bip_den = ab - so - hr
            babip_v = round(max(0.0, (hits - hr)) / bip_den, 3) if bip_den > 0 else 0.290

            lob_num = hits + bb + hbp - runs
            lob_den = hits + bb + hbp - 1.4*hr
            lob_v   = round(max(0.0, min(1.0, lob_num / lob_den)), 3) if lob_den > 0 else 0.72

            war_v = round(max(0.0, (4.00 - fip) * (ip / 9.0) * 0.11), 1)

            pit_out[name.lower()] = {
                "fg_era":   round(era, 2),
                "fg_fip":   fip,
                "fg_xfip":  xfip,
                "fg_whip":  round(whip, 2),
                "fg_k9":    round(k9, 2),
                "fg_bb9":   round(bb9, 2),
                "fg_hr9":   round(hr9, 2),
                "fg_kpct":  kpct_v,
                "fg_bbpct": bbpct_v,
                "fg_babip": babip_v,
                "fg_lob":   lob_v,
                "fg_war":   war_v,
                "fg_ip":    round(ip, 1),
                "fg_g":     int(_safe_num(s.get("gamesPlayed"),  0)),
                "fg_gs":    int(_safe_num(s.get("gamesStarted"), 0)),
                "fg_w":     int(_safe_num(s.get("wins"),   0)),
                "fg_l":     int(_safe_num(s.get("losses"), 0)),
                "fg_gb_pct": round(go / (go + ao), 3) if (go + ao) > 0 else 0.45,
            }
        except Exception as ex:
            logging.warning(f"[FG-pit-derived] pid={person.get('id') if 'person' in dir() else '?'}: {ex}")

    # ── Batters (one bulk call) ───────────────────────────────────────────
    bat_out = {}
    splits, bat_season = _fetch_bulk_splits("hitting")
    if bat_season and bat_season != year:
        logging.info(f"[FG-derived] Using fallback hitting season {bat_season}")

    for row in splits:
        try:
            person = row.get("player") or {}
            pid    = person.get("id")
            name   = (person.get("fullName") or "").strip()
            if not name: continue
            s = row.get("stat") or {}
            if not s: continue

            avg  = _safe_num(s.get("avg"), 0.0)
            obp  = _safe_num(s.get("obp"), 0.0)
            slg  = _safe_num(s.get("slg"), 0.0)
            ops  = _safe_num(s.get("ops"), round(obp + slg, 3))
            pa   = int(_safe_num(s.get("plateAppearances"), 0))
            ab   = _safe_num(s.get("atBats"), 0)
            so   = _safe_num(s.get("strikeOuts"), 0)
            bb   = _safe_num(s.get("baseOnBalls"), 0)
            hbp  = _safe_num(s.get("hitByPitch"), 0)
            hits = _safe_num(s.get("hits"), 0)
            hr   = _safe_num(s.get("homeRuns"), 0)
            dbl  = _safe_num(s.get("doubles"), 0)
            tpl  = _safe_num(s.get("triples"), 0)
            sf   = _safe_num(s.get("sacFlies"), 0)
            singles = max(0.0, hits - dbl - tpl - hr)

            woba_num = 0.696*bb + 0.726*hbp + 0.888*singles + 1.258*dbl + 1.599*tpl + 2.054*hr
            woba_den = ab + bb + sf + hbp
            woba = round(woba_num / woba_den, 3) if woba_den > 0 else 0.320
            wrc  = round(((woba - 0.320) / 1.15 + 0.325) / 0.325 * 100) if woba > 0 else 100
            bip_den = ab - so - hr + sf
            babip_v = round(max(0.0, (hits - hr)) / bip_den, 3) if bip_den > 0 else 0.295

            bat_out[name.lower()] = {
                "fg_avg":   round(avg, 3),
                "fg_obp":   round(obp, 3),
                "fg_slg":   round(slg, 3),
                "fg_ops":   round(ops, 3),
                "fg_woba":  woba,
                "fg_wrc":   wrc,
                "fg_pa":    pa,
                "fg_r":     int(_safe_num(s.get("runs"), 0)),
                "fg_hr":    int(hr),
                "fg_rbi":   int(_safe_num(s.get("rbi"), 0)),
                "fg_sb":    int(_safe_num(s.get("stolenBases"), 0)),
                "fg_war":   round(max(0.0, (woba - 0.320) * (pa / 600.0) * 6.0), 1) if pa > 0 else 0.0,
                "fg_babip": babip_v,
                "fg_bbpct": round(bb / pa, 3) if pa > 0 else 0.0,
                "fg_kpct":  round(so / pa, 3) if pa > 0 else 0.0,
                "fg_iso":   round(max(0.0, slg - avg), 3) if avg > 0 else 0.0,
                "fg_bats":  bat_side_by_name.get(name.lower(), "R"),
            }
        except Exception as ex:
            logging.warning(f"[FG-bat-derived] pid={person.get('id') if 'person' in dir() else '?'}: {ex}")

    with _fg_lock:
        _fg_pit = pit_out
        _fg_bat = bat_out
    logging.info(f"[FG] Done — {len(pit_out)} pitchers / {len(bat_out)} batters")
    logging.info(f"[FG] Cache ready — {len(pit_out)} P / {len(bat_out)} B")

def _maybe_refresh_fg():
    global _fg_loading
    with _fg_lock:
        loaded = _fg_loaded; date = _fg_load_date
        already_loading = _fg_loading
    if already_loading:
        return
    if not loaded or date != datetime.now().date():
        with _fg_lock: _fg_loading = True
        def _runner():
            global _fg_loading
            try:
                _load_fg_data()
            except Exception as _ex:
                logging.error(f"[FG] background load error: {_ex}")
            finally:
                with _fg_lock:
                    _fg_loading = False  # ALWAYS reset, even on crash
        threading.Thread(target=_runner, daemon=True).start()


def _wait_for_fg_data(timeout_sec=30):
    """Wait for FG data to be loaded or trigger a load if not started.

    Timeout raised to 30 s to survive Render cold-starts.
    Condition relaxed: passes if pitchers OR batters are loaded.

    Args:
        timeout_sec: Maximum time to wait in seconds

    Returns:
        True if data is loaded, False if timeout/error occurred
    """
    _maybe_refresh_fg()  # Trigger load if not already loading

    start = time.time()
    while time.time() - start < timeout_sec:
        should_trigger = False
        with _fg_lock:
            if _fg_loaded and (len(_fg_pit) > 0 or len(_fg_bat) > 0):
                return True
            should_trigger = not _fg_loading
        if should_trigger:
            # Call outside the lock to avoid re-entrant lock deadlock.
            _maybe_refresh_fg()
        time.sleep(0.2)

    return False


def _wait_for_savant_data(timeout_sec=30):
    """Wait for Savant data to be loaded or trigger a load if not started.

    Timeout raised to 30 s to survive Render cold-starts.
    Condition relaxed: passes if pitcherXStats OR batterXStats loaded.
    Arsenal failing alone no longer blocks every API call.

    Args:
        timeout_sec: Maximum time to wait in seconds

    Returns:
        True if data is loaded, False if timeout/error occurred
    """
    _maybe_refresh_savant()  # Trigger load if not already loading

    start = time.time()
    while time.time() - start < timeout_sec:
        should_trigger = False
        with _sv_lock:
            if _sv_loaded and (len(_sv_pit_xstats) > 0 or len(_sv_bat_xstats) > 0):
                return True
            should_trigger = not _sv_loading
        if should_trigger:
            # Call outside the lock to avoid re-entrant lock deadlock.
            _maybe_refresh_savant()
        time.sleep(0.1)

    return False


def _fuzzy_lookup(name, cache):
    if not name or not cache: return {}
    k = name.strip().lower()
    if k in cache: return cache[k]
    m = difflib.get_close_matches(k, cache.keys(), n=1, cutoff=0.78)
    return cache[m[0]] if m else {}

def fg_batter(name):
    """FanGraphs batter stats with Brain overlay fallback.
    Live FG data wins any key conflict; brain fills gaps for missing/thin players.
    """
    with _fg_lock:
        c = dict(_fg_bat)
    live = _fuzzy_lookup(name, c)
    with _brain_overlay_lock:
        brain = _brain_fg_batter.__wrapped_brain_only__(name) if hasattr(_brain_fg_batter, '__wrapped_brain_only__') else {}
    try:
        from brain_merge_patch import _brain_fuzzy, _brain_bat_overlay as _bbo
        with _brain_overlay_lock:
            brain = _brain_fuzzy(name, _bbo)
    except Exception:
        brain = {}
    if not live and not brain:
        return {}
    if not live:
        return brain
    if brain:
        merged = dict(brain)
        merged.update(live)
        return merged
    return live

def fg_pitcher(name):
    """FanGraphs pitcher stats with Brain overlay fallback.
    Live FG data wins any key conflict; brain fills gaps for missing/thin players.
    """
    with _fg_lock:
        c = dict(_fg_pit)
    live = _fuzzy_lookup(name, c)
    try:
        from brain_merge_patch import _brain_fuzzy, _brain_pit_overlay as _bpo
        with _brain_overlay_lock:
            brain = _brain_fuzzy(name, _bpo)
    except Exception:
        brain = {}
    if not live and not brain:
        return {}
    if not live:
        return brain
    if brain:
        merged = dict(brain)
        merged.update(live)
        return merged
    return live

# ── Baseball Savant Cache ─────────────────────────────────────────────────────
_sv_lock         = threading.Lock()
_sv_pit_xstats   = {}
_sv_bat_xstats   = {}
_sv_bat_statcast = {}
_sv_arsenal_pct       = {}
_sv_arsenal_velo      = {}
_sv_pit_arsenal_stats = {}  # {player_id_str: {pitch_name: {usage,ba,slg,woba,whiff_pct,hh_pct,k_pct,run_val}}}
_sv_bat_arsenal_stats = {}  # {player_id_str: {pitch_name: {slg,woba,whiff_pct,hh_pct}}}
_sv_loaded    = False
_sv_load_date = None
_sv_loading   = False

# Local pitcher arsenal cache loaded from data/ CSVs (populated once on first pitcher lookup)
# These are declared at module level so _pitcher_model does not need globals() tricks.
_local_arsenal_cache: object = None   # None → not yet loaded; tuple(dict, dict) → loaded
_local_arsenal_lock  = threading.Lock()

# Per-player direct Savant lookup cache {(player_id, year): (date, dict)}
_sv_player_batter_cache: dict = {}
_sv_player_batter_lock  = threading.Lock()

# ── Injury Cache (MLB transactions) ─────────────────────────────────────────
_injury_lock = threading.Lock()
_injury_cache = {}
_injury_last_refresh = None
_injury_loading = False
_injury_worker_started = False

# ── NRFI Cache (same-day, lineup-sensitive) ─────────────────────────────────
_nrfi_cache = {}

# ── Per-game correlation cache (same-day, lineup-sensitive) ────────────────
_correlation_cache = {}

# ── Spray Chart Cache (batted ball data) ────────────────────────────────────
_spray_cache = {}  # keyed by player_id, stores {'date': YYYY-MM-DD, 'data': [...]} 

# ── Strike Zone Chart Cache (zone metrics) ──────────────────────────────────
_zonechart_cache = {}  # keyed by player_id, stores 9-zone metrics
_zonechart_lock = threading.Lock()
_zonechart_prefetching = set()

PITCH_ORDER  = ["ff","si","fc","st","sl","cu","ch","fs","kn","sv"]
PITCH_LABELS = {
    "ff":"4-Seam","si":"Sinker","fc":"Cutter","st":"Sweeper",
    "sl":"Slider","cu":"Curveball","ch":"Changeup",
    "fs":"Splitter","kn":"Knuckleball","sv":"Slurve",
}

def _sv_key(raw):
    if "," in raw:
        last, first = raw.split(",", 1)
        return (first.strip() + " " + last.strip()).lower()
    return raw.strip().lower()

def _sv_f(val):
    try: return round(float(val), 2)
    except Exception: return "N/A"

def _fetch_sv_csv(url):
    """Fetch a Baseball Savant CSV with realistic browser headers and retry logic.

    Uses full Chrome User-Agent + Referer to avoid bot-detection 429s.
    Retries once after a 3-second back-off on any transient error.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://baseballsavant.mlb.com",
    }
    last_ex = None
    for attempt in range(2):
        try:
            r = requests.get(url, headers=headers, timeout=25)
            r.raise_for_status()
            text = r.text.lstrip("\ufeff")
            return list(csvmod.DictReader(io.StringIO(text)))
        except Exception as ex:
            last_ex = ex
            if attempt == 0:
                time.sleep(3)  # brief back-off before retry
    raise last_ex


def _fetch_sv_csv_by_season(url_template, seasons):
    """Fetch Savant CSV using first season that returns non-empty rows."""
    for y in seasons:
        url = url_template.format(year=y)
        endpoint = url.split('?')[0].split('/')[-1]
        try:
            rows = _fetch_sv_csv(url)
            if rows:
                logging.info(f"[Savant] {endpoint} rows={len(rows)} season={y}")
                return rows, y
            logging.info(f"[Savant] {endpoint} empty season={y} — trying next season")
        except requests.exceptions.Timeout:
            logging.warning(f"[Savant] {endpoint} TIMEOUT season={y} — trying next")
        except Exception as ex:
            logging.warning(f"[Savant] {endpoint} fetch failed season={y}: {ex}")
    return [], None

def _load_savant_data():
    global _sv_pit_xstats, _sv_bat_xstats, _sv_bat_statcast
    global _sv_arsenal_pct, _sv_arsenal_velo, _sv_loaded, _sv_load_date
    y = datetime.now().year
    seasons = _season_candidates(depth=4)
    BASE = "https://baseballsavant.mlb.com"

    # 1. Pitcher xERA
    try:
        rows, sy = _fetch_sv_csv_by_season(
            f"{BASE}/leaderboard/expected_statistics?type=pitcher&year={{year}}&position=&team=&min=1&csv=true",
            seasons,
        )
        if sy:
            y = sy
        d = {}
        for row in rows:
            raw = row.get("last_name, first_name","").strip()
            if not raw: continue
            d[_sv_key(raw)] = {
                "sv_xera":    _sv_f(row.get("xera")),
                "sv_era_p":   _sv_f(row.get("era")),
                "sv_xwoba_p": _sv_f(row.get("est_woba")),
                "sv_k_pct":   _sv_f(row.get("k_percent")),
                "sv_bb_pct":  _sv_f(row.get("bb_percent")),
                "sv_whiff":   _sv_f(row.get("whiff_percent")),
                "sv_pid":     row.get("player_id",""),
            }
        with _sv_lock: _sv_pit_xstats = d
        logging.info(f"[Savant] Pitcher xStats: {len(d)}")
    except Exception as ex:
        logging.warning(f"[Savant] Pitcher xStats failed: {ex}")

    # 2. Batter xBA/xSLG/xwOBA
    try:
        rows, _ = _fetch_sv_csv_by_season(
            f"{BASE}/leaderboard/expected_statistics?type=batter&year={{year}}&position=&team=&min=1&csv=true",
            [y] + [s for s in seasons if s != y],
        )
        d = {}
        for row in rows:
            raw = row.get("last_name, first_name","").strip()
            if not raw: continue
            d[_sv_key(raw)] = {
                "sv_xba":   _sv_f(row.get("est_ba")),
                "sv_xslg":  _sv_f(row.get("est_slg")),
                "sv_xwoba": _sv_f(row.get("est_woba")),
                "sv_k_pct": _sv_f(row.get("k_percent")),
                "sv_bb_pct":_sv_f(row.get("bb_percent")),
                "sv_pid":   row.get("player_id",""),
            }
        with _sv_lock: _sv_bat_xstats = d
        logging.info(f"[Savant] Batter xStats: {len(d)}")
    except Exception as ex:
        logging.warning(f"[Savant] Batter xStats failed: {ex}")

    # 3. Statcast batter EV / HH% / Barrel%
    try:
        rows, _ = _fetch_sv_csv_by_season(
            f"{BASE}/leaderboard/statcast?type=batter&year={{year}}&position=&team=&min=1&csv=true",
            [y] + [s for s in seasons if s != y],
        )
        d = {}
        for row in rows:
            raw = row.get("last_name, first_name","").strip()
            if not raw: continue
            d[_sv_key(raw)] = {
                "sv_ev":     _sv_f(row.get("avg_hit_speed")),
                "sv_hh_pct": _sv_f(row.get("ev95percent")),
                "sv_brl_pct":_sv_f(row.get("brl_percent")),
                "pull_pct_air": _sv_f(row.get("pull_percent")),
                "sv_brl_pa": _sv_f(row.get("brl_pa")),
                "sv_la":     _sv_f(row.get("avg_hit_angle")),
                "sv_ss_pct": _sv_f(row.get("anglesweetspotpercent")),
                "sv_max_ev": _sv_f(row.get("max_hit_speed")),
            }
        with _sv_lock: _sv_bat_statcast = d
        logging.info(f"[Savant] Batter Statcast: {len(d)}")
    except Exception as ex:
        logging.warning(f"[Savant] Batter Statcast failed: {ex}")

    # 4. Pitch arsenal % usage
    try:
        rows, _ = _fetch_sv_csv_by_season(
            f"{BASE}/leaderboard/pitch-arsenals?year={{year}}&min=1&type=n_&hand=&csv=true",
            [y] + [s for s in seasons if s != y],
        )
        d = {}
        for row in rows:
            raw = row.get("last_name, first_name","").strip()
            if not raw: continue
            pitches = {}
            for pt in PITCH_ORDER:
                v = row.get("n_" + pt,"").strip()
                if v:
                    try: pitches[pt] = round(float(v), 1)
                    except Exception: pass
            if pitches: d[_sv_key(raw)] = pitches
        with _sv_lock: _sv_arsenal_pct = d
        logging.info(f"[Savant] Arsenal %: {len(d)}")
    except Exception as ex:
        logging.warning(f"[Savant] Arsenal % failed: {ex}")

    # 5. Pitch arsenal velocities
    try:
        rows, _ = _fetch_sv_csv_by_season(
            f"{BASE}/leaderboard/pitch-arsenals?year={{year}}&min=1&type=avg_speed&hand=&csv=true",
            [y] + [s for s in seasons if s != y],
        )
        d = {}
        for row in rows:
            raw = row.get("last_name, first_name","").strip()
            if not raw: continue
            velos = {}
            for pt in PITCH_ORDER:
                v = row.get(pt + "_avg_speed","").strip()
                if v:
                    try: velos[pt] = round(float(v), 1)
                    except Exception: pass
            if velos: d[_sv_key(raw)] = velos
        with _sv_lock: _sv_arsenal_velo = d
        logging.info(f"[Savant] Arsenal velo: {len(d)}")
    except Exception as ex:
        logging.warning(f"[Savant] Arsenal velo failed: {ex}")

    # 6. Pitcher pitch-arsenal outcome stats (BA/SLG/wOBA/whiff%/HH% per pitch type)
    try:
        rows, _ = _fetch_sv_csv_by_season(
            f"{BASE}/leaderboard/pitch-arsenal-stats?type=pitcher&pitchType=&year={{year}}&team=&min=25&csv=true",
            [y] + [s for s in seasons if s != y],
        )
        d = {}
        for row in rows:
            pid_str = (row.get("player_id") or "").strip()
            ptype   = (row.get("pitch_name") or "").strip()
            if not pid_str or not ptype:
                continue
            if pid_str not in d:
                d[pid_str] = {}
            try:
                usage = float(row.get("pitch_usage") or 0)
            except Exception:
                usage = 0.0
            d[pid_str][ptype] = {
                "usage":     usage,
                "ba":        _sv_f(row.get("ba")),
                "slg":       _sv_f(row.get("slg")),
                "woba":      _sv_f(row.get("woba")),
                "whiff_pct": _sv_f(row.get("whiff_percent")),
                "hh_pct":    _sv_f(row.get("hard_hit_percent")),
                "k_pct":     _sv_f(row.get("k_percent")),
                "run_val":   _sv_f(row.get("run_value_per_100")),
            }
        with _sv_lock: _sv_pit_arsenal_stats = d
        logging.info(f"[Savant] Pitcher arsenal stats: {len(d)} pitchers")
    except Exception as ex:
        logging.warning(f"[Savant] Pitcher arsenal stats failed: {ex}")

    # 7. Batter pitch-arsenal outcome stats (SLG/wOBA/whiff%/HH% per pitch type faced)
    try:
        rows, _ = _fetch_sv_csv_by_season(
            f"{BASE}/leaderboard/pitch-arsenal-stats?type=batter&pitchType=&year={{year}}&team=&min=25&csv=true",
            [y] + [s for s in seasons if s != y],
        )
        d = {}
        for row in rows:
            pid_str = (row.get("player_id") or "").strip()
            ptype   = (row.get("pitch_name") or "").strip()
            if not pid_str or not ptype:
                continue
            if pid_str not in d:
                d[pid_str] = {}
            d[pid_str][ptype] = {
                "slg":       _sv_f(row.get("slg")),
                "woba":      _sv_f(row.get("woba")),
                "whiff_pct": _sv_f(row.get("whiff_percent")),
                "hh_pct":    _sv_f(row.get("hard_hit_percent")),
            }
        with _sv_lock: _sv_bat_arsenal_stats = d
        logging.info(f"[Savant] Batter arsenal stats: {len(d)} batters")
    except Exception as ex:
        logging.warning(f"[Savant] Batter arsenal stats failed: {ex}")

    with _sv_lock:
        has_data = bool(_sv_pit_xstats) or bool(_sv_bat_xstats) or bool(_sv_bat_statcast) or bool(_sv_arsenal_pct) or bool(_sv_arsenal_velo)
        _sv_loaded    = has_data
        _sv_load_date = datetime.now().date() if has_data else None
    logging.info(f"[Savant] All caches ready: pitxstats={len(_sv_pit_xstats)} batxstats={len(_sv_bat_xstats)} statcast={len(_sv_bat_statcast)} arsenal={len(_sv_arsenal_pct)} velo={len(_sv_arsenal_velo)} pit_arsenal={len(_sv_pit_arsenal_stats)} bat_arsenal={len(_sv_bat_arsenal_stats)} loaded={_sv_loaded}")

def _maybe_refresh_savant():
    global _sv_loading
    with _sv_lock:
        loaded = _sv_loaded; date = _sv_load_date
        already_loading = _sv_loading
    if already_loading:
        return
    if not loaded or date != datetime.now().date():
        with _sv_lock: _sv_loading = True
        def _runner():
            global _sv_loading
            try:
                _load_savant_data()
            except Exception as _ex:
                print(f"[Savant] background load error: {_ex}")
            finally:
                with _sv_lock:
                    _sv_loading = False  # ALWAYS reset, even on crash
        threading.Thread(target=_runner, daemon=True).start()

def sv_pitcher(name):
    """Savant pitcher stats with Brain overlay. Brain fills missing sv keys only."""
    with _sv_lock:
        xs = dict(_sv_pit_xstats)
        ap = dict(_sv_arsenal_pct)
        av = dict(_sv_arsenal_velo)
    lx = _fuzzy_lookup(name, xs)
    r = dict(lx) if lx else {}
    lap = _fuzzy_lookup(name, ap)
    lav = _fuzzy_lookup(name, av)
    r["sv_arsenal_pct"] = lap if lap else {}
    r["sv_arsenal_velo"] = lav if lav else {}
    try:
        from brain_merge_patch import _brain_fuzzy, _brain_pit_overlay as _bpo
        with _brain_overlay_lock:
            brain = _brain_fuzzy(name, _bpo)
        for k, v in brain.items():
            if k not in r or r[k] in (None, "", "NA", "N/A"):
                r[k] = v
    except Exception:
        pass
    return r

def sv_batter(name):
    """Savant batter stats with Brain overlay. Brain fills missing sv keys only."""
    with _sv_lock:
        xs = dict(_sv_bat_xstats)
        sc = dict(_sv_bat_statcast)
    lx = _fuzzy_lookup(name, xs)
    ls = _fuzzy_lookup(name, sc)
    r = dict(lx) if lx else {}
    if ls:
        r.update(ls)
    try:
        from brain_merge_patch import _brain_fuzzy, _brain_bat_overlay as _bbo
        with _brain_overlay_lock:
            brain = _brain_fuzzy(name, _bbo)
        for k, v in brain.items():
            if k not in r or r[k] in (None, "", "NA", "N/A"):
                r[k] = v
    except Exception:
        pass
    return r


def _fetch_savant_player_batter(player_id, year):
    """Direct per-player Savant CSV fetch by MLB player_id (same ID Savant uses).
    Falls back to empty dict — never raises. Results cached daily."""
    today = datetime.now().date()
    key = (int(player_id), int(year))
    with _sv_player_batter_lock:
        cached = _sv_player_batter_cache.get(key)
        if cached and cached[0] == today:
            return cached[1]

    BASE = "https://baseballsavant.mlb.com"
    result = {}
    try:
        rows = _fetch_sv_csv(
            f"{BASE}/leaderboard/expected_statistics?type=batter&year={year}"
            f"&position=&team=&min=1&player_id={player_id}&csv=true"
        )
        for row in rows:
            result.update({
                "sv_xba":   _sv_f(row.get("est_ba")),
                "sv_xslg":  _sv_f(row.get("est_slg")),
                "sv_xwoba": _sv_f(row.get("est_woba")),
                "sv_k_pct": _sv_f(row.get("k_percent")),
                "sv_bb_pct":_sv_f(row.get("bb_percent")),
            })
    except Exception:
        pass
    try:
        rows = _fetch_sv_csv(
            f"{BASE}/leaderboard/statcast?type=batter&year={year}"
            f"&position=&team=&min=1&player_id={player_id}&csv=true"
        )
        for row in rows:
            result.update({
                "sv_ev":      _sv_f(row.get("avg_hit_speed")),
                "sv_hh_pct":  _sv_f(row.get("ev95percent")),
                "sv_brl_pct": _sv_f(row.get("brl_percent")),
            })
    except Exception:
        pass

    with _sv_player_batter_lock:
        _sv_player_batter_cache[key] = (today, result)
    return result


def _pct_rank(values, value):
    """Return percentile rank (0-100) of value against a numeric list."""
    try:
        v = float(value)
    except Exception:
        return None
    nums = []
    for x in values or []:
        try:
            nums.append(float(x))
        except Exception:
            continue
    if not nums:
        return None
    below_or_equal = sum(1 for n in nums if n <= v)
    return max(0, min(100, int(round((below_or_equal / len(nums)) * 100))))


def _injury_status_from_text(txt):
    s = (txt or "").lower()
    if "60-day" in s and "injured list" in s:
        return "IL_60"
    if ("10-day" in s or "15-day" in s or "7-day" in s) and "injured list" in s:
        return "IL_10"
    if "day-to-day" in s:
        return "DTD"
    if "game-time" in s or "game time" in s or "questionable" in s:
        return "GTD"
    return None


def _fetch_injury_status(force=False):
    """Refresh injury cache from MLB transactions endpoint."""
    global _injury_last_refresh, _injury_loading
    now = datetime.now(ET)

    with _injury_lock:
        if _injury_loading:
            return
        if not force and _injury_last_refresh and (now - _injury_last_refresh).total_seconds() < 3600:
            return
        _injury_loading = True

    try:
        date_str = now.strftime("%Y-%m-%d")
        team_abbr_by_id = {}
        try:
            for g in fetch_schedule(date_str) or []:
                away = (((g.get("teams") or {}).get("away") or {}).get("team") or {})
                home = (((g.get("teams") or {}).get("home") or {}).get("team") or {})
                if away.get("id"):
                    team_abbr_by_id[away.get("id")] = away.get("abbreviation", "?")
                if home.get("id"):
                    team_abbr_by_id[home.get("id")] = home.get("abbreviation", "?")
        except Exception:
            team_abbr_by_id = {}

        r = requests.get(
            f"{MLB_API}/transactions",
            params={"sportId": 1, "startDate": date_str, "endDate": date_str},
            timeout=12,
        )
        r.raise_for_status()
        txs = r.json().get("transactions", []) or []

        fresh = {}
        for tx in txs:
            person = tx.get("person") or tx.get("player") or {}
            pid = person.get("id")
            if not pid:
                continue
            name = person.get("fullName") or tx.get("playerName") or "Unknown"
            to_team = tx.get("toTeam") or tx.get("team") or {}
            team_id = to_team.get("id")
            if not team_id:
                team_id = (tx.get("fromTeam") or {}).get("id")
            desc = tx.get("description") or tx.get("note") or tx.get("typeDesc") or ""
            type_desc = tx.get("typeDesc") or ""
            type_code = tx.get("typeCode") or ""
            combined = " | ".join([str(type_desc), str(type_code), str(desc)])
            status = _injury_status_from_text(combined)
            if not status:
                continue

            fresh[int(pid)] = {
                "playerId": int(pid),
                "name": name,
                "teamId": team_id,
                "team": team_abbr_by_id.get(team_id, "?"),
                "status": status,
                "type": type_desc or type_code or status,
                "date": tx.get("date") or date_str,
                "description": desc or type_desc or status,
                "updatedAt": now.isoformat(),
            }

        with _injury_lock:
            _injury_cache.clear()
            _injury_cache.update(fresh)
            _injury_last_refresh = now
    except Exception as ex:
        print(f"[_fetch_injury_status] {ex}")
    finally:
        with _injury_lock:
            _injury_loading = False


def _start_injury_worker():
    """Start background injury refresh worker once per process."""
    global _injury_worker_started
    with _injury_lock:
        if _injury_worker_started:
            return
        _injury_worker_started = True

    def _runner():
        while True:
            try:
                _fetch_injury_status(force=True)
            except Exception as ex:
                print(f"[_injury_worker] {ex}")
            time.sleep(3600)

    threading.Thread(target=_runner, daemon=True).start()


def _get_player_injury(player_id):
    try:
        pid = int(player_id)
    except Exception:
        return None
    with _injury_lock:
        return dict(_injury_cache.get(pid)) if pid in _injury_cache else None


def _top_team_injury(team_id):
    """Return one key injury alert row for team (IL/GTD/DTD)."""
    if not team_id:
        return None
    sev = {"IL_60": 4, "IL_10": 3, "GTD": 2, "DTD": 1}
    with _injury_lock:
        rows = [v for v in _injury_cache.values() if v.get("teamId") == team_id]
    if not rows:
        return None
    rows.sort(key=lambda x: sev.get(x.get("status"), 0), reverse=True)
    return rows[0]


# ── MLB API Helpers ───────────────────────────────────────────────────────────
def fetch_schedule(date_str):
    url = (f"{MLB_API}/schedule?sportId=1&date={date_str}"
        "&hydrate=team,probablePitcher,lineups,linescore,venue(location),weather")
    r = requests.get(url, timeout=10); r.raise_for_status()
    dates = r.json().get("dates", [])
    return dates[0].get("games", []) if dates else []


def fetch_schedule_game(game_pk):
    url = (f"{MLB_API}/schedule?sportId=1&gamePk={game_pk}"
        "&hydrate=team,probablePitcher,lineups,linescore,venue(location),weather")
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    dates = r.json().get("dates", [])
    if not dates:
        return None
    games = dates[0].get("games", [])
    return games[0] if games else None


def _collect_mlb_endpoint(url, params=None, timeout=15, default=None):
    default = {} if default is None else default
    try:
        r = requests.get(url, params=params or {}, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.Timeout:
        print(f"[MLB API TIMEOUT] {url}")
        return default
    except requests.exceptions.HTTPError as e:
        print(f"[MLB API HTTP {e.response.status_code}] {url}")
        return default
    except Exception as e:
        print(f"[MLB API ERROR] {url} → {e}")
        return default


def _memory_collect_schedule_window(date_str, days_back=2, max_games_per_day=30):
    try:
        start_dt = datetime.strptime(date_str, "%Y-%m-%d").date()
    except Exception:
        start_dt = datetime.now(ET).date()
    days = []
    game_pks = []
    for d in range(max(0, int(days_back)) + 1):
        ds = (start_dt - timedelta(days=d)).isoformat()
        games = fetch_schedule(ds)
        if max_games_per_day:
            games = games[:max(1, int(max_games_per_day))]
        parsed = [g for g in (parse_game(x) for x in games) if g]
        days.append({
            "date": ds,
            "gameCount": len(parsed),
            "games": parsed,
        })
        game_pks.extend([g.get("gamePk") for g in parsed if g.get("gamePk")])
    return days, game_pks


def _memory_collect_boxscores(game_pks, max_games=20):
    picked = [int(gpk) for gpk in (game_pks or []) if gpk][:max(1, int(max_games))]
    boxscores = []
    player_ids = set()
    for gpk in picked:
        payload = _collect_mlb_endpoint(f"{MLB_API}/game/{gpk}/boxscore", timeout=12, default={})
        teams = payload.get("teams") or {}
        away = teams.get("away") or {}
        home = teams.get("home") or {}
        away_batters = get_batters_from_boxscore(away, "away")
        home_batters = get_batters_from_boxscore(home, "home")
        for row in (away_batters + home_batters):
            pid = row.get("id")
            if pid:
                try:
                    player_ids.add(int(pid))
                except Exception:
                    pass
        for side in (away, home):
            team_pitchers = side.get("pitchers") or []
            for pid in team_pitchers[:6]:
                try:
                    player_ids.add(int(pid))
                except Exception:
                    pass
        boxscores.append({
            "gamePk": gpk,
            "awayTeam": ((away.get("team") or {}).get("abbreviation") or "AWAY"),
            "homeTeam": ((home.get("team") or {}).get("abbreviation") or "HOME"),
            "awayLineup": away_batters,
            "homeLineup": home_batters,
            "awayPitchers": list((away.get("pitchers") or []))[:10],
            "homePitchers": list((home.get("pitchers") or []))[:10],
        })
    return boxscores, sorted(player_ids)


def _memory_collect_player_cards(player_ids, max_players=160):
    out = []
    for pid in (player_ids or [])[:max(1, int(max_players))]:
        payload = _collect_mlb_endpoint(
            f"{MLB_API}/people/{pid}",
            params={
                "hydrate": f"stats(group=[hitting,pitching],type=season,season={datetime.now().year}),currentTeam",
            },
            timeout=10,
            default={},
        )
        people = payload.get("people") or []
        if not people:
            continue
        p = people[0]
        team = p.get("currentTeam") or {}
        stats_payload = {}
        for row in (p.get("stats") or []):
            grp = ((row.get("group") or {}).get("displayName") or "").lower()
            splits = row.get("splits") or []
            if not splits:
                continue
            stats_payload[grp or "unknown"] = splits[0].get("stat") or {}
        out.append({
            "id": pid,
            "name": p.get("fullName") or "Unknown",
            "teamId": team.get("id"),
            "team": team.get("abbreviation") or "?",
            "position": ((p.get("primaryPosition") or {}).get("abbreviation") or "?"),
            "bats": ((p.get("batSide") or {}).get("code") or "?"),
            "throws": ((p.get("pitchHand") or {}).get("code") or "?"),
            "stats": stats_payload,
            "injury": _get_player_injury(pid),
        })
    return out


def _memory_mode_for_now(mode):
    m = (mode or 'auto').strip().lower()
    if m in ('light', 'deep'):
        return m
    hr = datetime.now(ET).hour
    return 'deep' if 2 <= hr < 6 else 'light'


def _memory_mode_defaults(mode):
    resolved = _memory_mode_for_now(mode)
    if resolved == 'deep':
        return {
            'mode': 'deep',
            'days_back': 7,
            'max_games_per_day': 30,
            'include_boxscores': True,
            'max_players': 360,
            'include_team_rosters': True,
            'transactions_days': 7,
        }
    return {
        'mode': 'light',
        'days_back': 2,
        'max_games_per_day': 30,
        'include_boxscores': True,
        'max_players': 180,
        'include_team_rosters': False,
        'transactions_days': 2,
    }


def _memory_collect_team_stats(team_ids):
    rows = []
    yr = datetime.now().year
    for tid in (team_ids or []):
        hit = _collect_mlb_endpoint(
            f"{MLB_API}/teams/{tid}/stats",
            params={"stats": "season", "group": "hitting", "season": yr},
            timeout=10,
            default={},
        )
        pit = _collect_mlb_endpoint(
            f"{MLB_API}/teams/{tid}/stats",
            params={"stats": "season", "group": "pitching", "season": yr},
            timeout=10,
            default={},
        )
        rows.append({
            "teamId": tid,
            "hitting": (((hit.get('stats') or [{}])[0].get('splits') or [{}])[0].get('stat') or {}),
            "pitching": (((pit.get('stats') or [{}])[0].get('splits') or [{}])[0].get('stat') or {}),
        })
    return rows


def _memory_collect_team_rosters(team_ids, max_players_per_team=45):
    out = []
    for tid in (team_ids or []):
        payload = _collect_mlb_endpoint(
            f"{MLB_API}/teams/{tid}/roster",
            params={"rosterType": "active"},
            timeout=10,
            default={},
        )
        rows = []
        for p in (payload.get('roster') or [])[:max(10, int(max_players_per_team))]:
            person = p.get('person') or {}
            rows.append({
                "id": person.get('id'),
                "name": person.get('fullName'),
                "pos": ((p.get('position') or {}).get('abbreviation') or '?'),
                "status": ((p.get('status') or {}).get('description') or ''),
            })
        out.append({"teamId": tid, "players": rows})
    return out


def _memory_collect_transactions(date_str, days_back=2, max_rows=500):
    try:
        end_dt = datetime.strptime(date_str, '%Y-%m-%d').date()
    except Exception:
        end_dt = datetime.now(ET).date()
    start_dt = end_dt - timedelta(days=max(0, int(days_back)))
    payload = _collect_mlb_endpoint(
        f"{MLB_API}/transactions",
        params={
            "sportId": 1,
            "startDate": start_dt.isoformat(),
            "endDate": end_dt.isoformat(),
        },
        timeout=12,
        default={},
    )
    txs = list(payload.get('transactions') or [])[:max(50, int(max_rows))]
    return [{
        "id": t.get('id'),
        "date": t.get('date'),
        "type": t.get('typeDesc') or t.get('typeCode'),
        "player": ((t.get('person') or t.get('player') or {}).get('fullName') or t.get('playerName')),
        "teamId": ((t.get('team') or t.get('toTeam') or {}).get('id') or (t.get('fromTeam') or {}).get('id')),
        "description": t.get('description') or t.get('note') or '',
    } for t in txs]

def _memory_ingest_statscast_data(date_str=None, max_records=5000):
    """
    Comprehensively ingest Statscast/Savant data: pitch-level, xStats, batted ball data.
    Collects from loaded caches and returns structured summary + sample data.
    """
    try:
        if not date_str:
            date_str = datetime.now(ET).strftime("%Y-%m-%d")
        out = {"date": date_str, "sources": [], "summary": {}, "sampleData": {}}
        
        with _sv_lock:
            if _sv_loaded:
                out["summary"]["pitcherXStats"] = len(_sv_pit_xstats)
                out["summary"]["batterXStats"] = len(_sv_bat_xstats)
                out["summary"]["batterStatcast"] = len(_sv_bat_statcast)
                out["summary"]["pitchArsenal"] = len(_sv_arsenal_pct)
                out["sources"].append("Savant xStats")
                out["sources"].append("Savant Statcast")
                out["sources"].append("Savant Pitch Arsenal")
                
                if _sv_bat_xstats:
                    sample_batter = list(_sv_bat_xstats.items())[:5]
                    out["sampleData"]["batter_xstats"] = [{"name": name, "stats": stats} for name, stats in sample_batter]
                if _sv_pit_xstats:
                    sample_pitcher = list(_sv_pit_xstats.items())[:5]
                    out["sampleData"]["pitcher_xstats"] = [{"name": name, "stats": stats} for name, stats in sample_pitcher]
                if _sv_arsenal_pct:
                    sample_arsenal = list(_sv_arsenal_pct.items())[:3]
                    out["sampleData"]["pitch_arsenal"] = [{"name": name, "arsenal": arsenal} for name, arsenal in sample_arsenal]
        
        return out
    except Exception as e:
        print(f"[_memory_ingest_statscast_data] Error: {e}")
        return {"date": date_str, "sources": [], "summary": {}, "error": str(e)}


def _memory_ingest_fangraphs_data(max_records=5000):
    """
    Comprehensively ingest FanGraphs data: batter and pitcher stat records.
    Returns structured summary + sample stat records.
    """
    try:
        out = {"sources": [], "summary": {}, "sampleData": {}}
        
        with _fg_lock:
            if _fg_loaded:
                batter_count = len(_fg_bat)
                pitcher_count = len(_fg_pit)
                out["summary"]["batters"] = batter_count
                out["summary"]["pitchers"] = pitcher_count
                out["sources"].append("FanGraphs (via MLB API)")
                
                if _fg_bat:
                    sample_batters = list(_fg_bat.items())[:10]
                    out["sampleData"]["batters"] = [{"name": name, "stats": stats} for name, stats in sample_batters]
                if _fg_pit:
                    sample_pitchers = list(_fg_pit.items())[:10]
                    out["sampleData"]["pitchers"] = [{"name": name, "stats": stats} for name, stats in sample_pitchers]
        
        return out
    except Exception as e:
        print(f"[_memory_ingest_fangraphs_data] Error: {e}")
        return {"sources": [], "summary": {}, "error": str(e)}


def _memory_ingest_mlb_api_player_stats(team_ids, max_players=200, season=None):
    """
    Comprehensively ingest MLB API player stats: hitting, pitching, fielding stats per team.
    Returns structured player performance data organized by position/role.
    """
    try:
        if not season:
            season = datetime.now().year
        
        out = {"season": season, "teams": {}, "summary": {}}
        total_players = 0
        
        for tid in team_ids[:30]:
            try:
                payload = _collect_mlb_endpoint(
                    f"{MLB_API}/teams/{tid}?hydrate=roster",
                    timeout=12,
                    default={}
                )
                # MLB API now returns 'teams' array
                teams = payload.get("teams") or []
                if not teams:
                    print(f"[_memory_ingest_mlb_api_player_stats] Team {tid} missing in API response.")
                    continue
                team_info = teams[0]
                roster_obj = team_info.get("roster") or {}
                roster = roster_obj.get("roster", []) if isinstance(roster_obj, dict) else []

                players_by_type = {"batters": [], "pitchers": [], "other": []}

                for player_entry in roster[:max_players]:
                    person = player_entry.get("person") or {}
                    position = player_entry.get("position") or {}
                    player_id = person.get("id")
                    if not player_id:
                        continue
                    player_info = {
                        "id": player_id,
                        "name": person.get("fullName"),
                        "position": position.get("abbreviation") or position.get("name"),
                        "jersey": player_entry.get("jerseyNumber"),
                        "status": (player_entry.get("status") or {}).get("description"),
                    }
                    pos_code = position.get("code") or ""
                    if pos_code == "P":
                        players_by_type["pitchers"].append(player_info)
                    elif pos_code in ("C", "1B", "2B", "3B", "SS", "LF", "CF", "RF", "DH"):
                        players_by_type["batters"].append(player_info)
                    else:
                        players_by_type["other"].append(player_info)
                    total_players += 1

                out["teams"][str(tid)] = {
                    "teamName": team_info.get("name"),
                    "players": players_by_type,
                    "rosterSize": len(roster),
                }
            except Exception as e:
                print(f"[_memory_ingest_mlb_api_player_stats] Team {tid} error: {e}")
        
        out["summary"]["totalTeams"] = len(out["teams"])
        out["summary"]["totalPlayers"] = total_players
        out["sources"] = ["MLB API Team Rosters"]
        
        return out
    except Exception as e:
        print(f"[_memory_ingest_mlb_api_player_stats] Error: {e}")
        return {"season": season, "teams": {}, "summary": {}, "error": str(e)}


def _memory_collect_comprehensive_data(date_str=None, team_ids=None, mode='light'):
    """
    Orchestrates comprehensive data ingestion from all sources:
    - Statscast/Savant (pitch-level, xStats, batted balls)
    - FanGraphs (batter/pitcher stats)
    - MLB API (player stats, advanced metrics)
    """
    try:
        if not date_str:
            date_str = datetime.now(ET).strftime("%Y-%m-%d")
        if not team_ids:
            team_ids = []
        
        # Ingest from each source
        statscast_data = _memory_ingest_statscast_data(date_str)
        fangraphs_data = _memory_ingest_fangraphs_data()
        mlb_api_data = _memory_ingest_mlb_api_player_stats(team_ids)
        manual_uploads_data = _process_uploaded_brain_files(force=(mode == 'manual'))
        manual_upload_records = int(manual_uploads_data.get('totalRecords') or 0)
        # Reload brain overlays so new uploads are immediately searchable
        try:
            load_brain_overlays(force=True)
        except Exception as _be:
            print(f"[ingest] brain overlay reload failed: {_be}")
        
        # Compile comprehensive summary
        comprehensive = {
            "date": date_str,
            "mode": mode,
            "collectedAt": datetime.now(timezone.utc).isoformat(),
            "sources": {
                "statscast": statscast_data,
                "fangraphs": fangraphs_data,
                "mlbApi": mlb_api_data,
                "manualUploads": manual_uploads_data,
            },
            "totalDataPoints": (
                sum(statscast_data.get("summary", {}).values()) +
                sum(fangraphs_data.get("summary", {}).values()) +
                mlb_api_data.get("summary", {}).get("totalPlayers", 0) +
                manual_upload_records
            ),
            "manualUploadRecords": manual_upload_records,
        }
        
        return comprehensive
    except Exception as e:
        print(f"[_memory_collect_comprehensive_data] Error: {e}")
        return {"error": str(e)}



def _collect_mlb_memory_snapshot(date_str=None, days_back=2, max_games_per_day=30, include_boxscores=True, max_players=160, mode='light', include_team_rosters=False, transactions_days=2):
    _maybe_refresh_fg()
    _maybe_refresh_savant()
    _fetch_injury_status(force=False)

    today_et = (date_str or datetime.now(ET).strftime("%Y-%m-%d")).strip()
    created_at = datetime.now(timezone.utc).isoformat()

    teams_payload = _collect_mlb_endpoint(
        f"{MLB_API}/teams",
        params={"sportId": 1, "activeStatus": "Y"},
        timeout=12,
        default={},
    )
    teams = [
        {
            "id": t.get("id"),
            "name": t.get("name"),
            "abbr": t.get("abbreviation"),
            "venueId": ((t.get("venue") or {}).get("id")),
            "division": ((t.get("division") or {}).get("name")),
            "league": ((t.get("league") or {}).get("name")),
        }
        for t in (teams_payload.get("teams") or [])
        if t.get("id")
    ]
    team_ids = [t.get("id") for t in teams if t.get("id")]

    standings_payload = _collect_mlb_endpoint(
        f"{MLB_API}/standings",
        params={"sportId": 1, "standingsType": "regularSeason"},
        timeout=12,
        default={},
    )

    leaders_payload = _collect_mlb_endpoint(
        f"{MLB_API}/stats/leaders",
        params={
            "leaderCategories": "homeRuns,runsBattedIn,battingAverage,onBasePlusSlugging,earnedRunAverage,strikeouts,whip",
            "sportId": 1,
            "season": datetime.now().year,
            "limit": 15,
        },
        timeout=12,
        default={},
    )

    schedule_days, game_pks = _memory_collect_schedule_window(
        today_et,
        days_back=days_back,
        max_games_per_day=max_games_per_day,
    )

    boxscores = []
    box_player_ids = []
    if include_boxscores:
        boxscores, box_player_ids = _memory_collect_boxscores(game_pks, max_games=20)

    probable_pitcher_ids = []
    raw_today = fetch_schedule(today_et)
    for g in (raw_today or []):
        away_pp = (((g.get("teams") or {}).get("away") or {}).get("probablePitcher") or {}).get("id")
        home_pp = (((g.get("teams") or {}).get("home") or {}).get("probablePitcher") or {}).get("id")
        if away_pp:
            probable_pitcher_ids.append(away_pp)
        if home_pp:
            probable_pitcher_ids.append(home_pp)

    featured_player_ids = sorted({int(x) for x in (box_player_ids + probable_pitcher_ids) if x})
    player_cards = _memory_collect_player_cards(featured_player_ids, max_players=max_players)

    team_stats = _memory_collect_team_stats(team_ids)
    team_rosters = _memory_collect_team_rosters(team_ids) if include_team_rosters else []
    transactions = _memory_collect_transactions(today_et, days_back=transactions_days)

    comprehensive_data = _memory_collect_comprehensive_data(today_et, team_ids, mode)
    team_ids = [t.get("id") for t in teams if t.get("id")]
    team_ids = [t.get("id") for t in teams if t.get("id")]

    with _fg_lock:
        fg_summary = {
            "loaded": _fg_loaded,
            "loadDate": str(_fg_load_date) if _fg_load_date else None,
            "batters": len(_fg_bat),
            "pitchers": len(_fg_pit),
        }
    with _sv_lock:
        sv_summary = {
            "loaded": _sv_loaded,
            "loadDate": str(_sv_load_date) if _sv_load_date else None,
            "pitcherXStats": len(_sv_pit_xstats),
            "batterXStats": len(_sv_bat_xstats),
            "batterStatcast": len(_sv_bat_statcast),
            "pitchArsenal": len(_sv_arsenal_pct),
        }
    with _injury_lock:
        injury_summary = {
            "updatedAt": _injury_last_refresh.isoformat() if _injury_last_refresh else None,
            "count": len(_injury_cache),
            "rows": list(_injury_cache.values())[:120],
        }

    snapshot = {
        "createdAt": created_at,
        "targetDateET": today_et,
        "windowDays": int(max(0, days_back)) + 1,
        "meta": {
            "mode": mode,
            "teamCount": len(teams),
            "scheduleDays": len(schedule_days),
            "gameCount": sum(int(d.get("gameCount") or 0) for d in schedule_days),
            "boxscoreCount": len(boxscores),
            "featuredPlayers": len(player_cards),
            "transactions": len(transactions),
        },
        "league": {
            "teams": teams,
            "standings": standings_payload.get("records") or [],
            "leaders": leaders_payload.get("leagueLeaders") or [],
            "teamStats": team_stats,
            "transactions": transactions,
        },
        "schedule": schedule_days,
        "games": {
            "boxscores": boxscores,
        },
        "players": {
            "featured": player_cards,
            "teamRosters": team_rosters,
        },
        "caches": {
            "fangraphs": fg_summary,
            "savant": sv_summary,
            "injuries": injury_summary,
            "odds": _odds_cache_status_payload(),
            "comprehensive": comprehensive_data,
        },
    }
    return snapshot


# UTC offset for each MLB venue — used to find correct local hour for Open-Meteo index
# ET=-5, CT=-6, MT=-7, PT=-8
VENUE_UTC_OFFSET = {
    1:    -8,  # Angel Stadium         (LAA - PT)
    22:   -8,  # Dodger Stadium        (LAD - PT)
    2395: -8,  # Oracle Park           (SF  - PT)
    2680: -8,  # Petco Park            (SD  - PT)
    680:  -8,  # T-Mobile Park         (SEA - PT)
    2529: -8,  # Sutter Health Park    (OAK/SAC - PT)
    15:   -7,  # Chase Field           (ARI - MT)
    19:   -7,  # Coors Field           (COL - MT)
    4:    -6,  # Guaranteed Rate Field (CWS - CT)
    17:   -6,  # Wrigley Field         (CHC - CT)
    7:    -6,  # Kauffman Stadium      (KC  - CT)
    3312: -6,  # Target Field          (MIN - CT)
    32:   -6,  # American Family Field (MIL - CT)
    2392: -6,  # Daikin Park           (HOU - CT)
    5325: -6,  # Globe Life Field      (TEX - CT)
    4321: -6,  # Globe Life Field alt  (TEX - CT)
    # All other venues default to ET (-5) — BOS, NYY, NYM, PHI, BAL, WSH, ATL, MIA, CLE, PIT, CIN, STL, DET, TOR
}







def _deg_to_compass(deg):
    if deg is None:
        return ""
    dirs = ["N","NNE","NE","ENE","E","ESE","SE","SSE",
            "S","SSW","SW","WSW","W","WNW","NW","NNW"]
    return dirs[int((float(deg) + 11.25) / 22.5) % 16]

def get_weather(lat, lon, game_hour=13, venue_id=None):
    # Dome/retractable roof: return indoor conditions immediately
    if venue_id and venue_id in DOME_VENUES:
        return {"temp":"DOME","rain_chance":0,"wind_speed":0,"condition":"Dome","dome":True}
    # Last-resort: fill coords from hardcoded stadium map
    if (lat is None or lon is None) and venue_id and venue_id in STADIUM_COORDS:
        lat, lon = STADIUM_COORDS[venue_id]
    if lat is None or lon is None:
        return {"temp":"N/A","rain_chance":"N/A","wind_speed":"N/A","condition":"N/A"}

    cache_key = (int(venue_id or 0), round(float(lat), 3), round(float(lon), 3), int(game_hour))
    now = time.time()
    with _weather_cache_lock:
        cached = _weather_cache.get(cache_key)
    if cached and (now - float(cached.get("ts") or 0)) < float(cached.get("ttl") or _WEATHER_TTL):
        return dict(cached.get("payload") or {})

    try:
        r = requests.get(WX_API, params={
            "latitude":lat,"longitude":lon,
            "hourly":"temperature_2m,precipitation_probability,windspeed_10m,winddirection_10m,weathercode",
            "temperature_unit":"fahrenheit","windspeed_unit":"mph",
            "forecast_days":2,"timezone":"America/New_York"
        }, timeout=8)
        r.raise_for_status()
        h = r.json().get("hourly",{})
        idx = max(0, min(len(h.get("temperature_2m",[])) - 1, int(game_hour)))
        wcode_map = {0:"Clear",1:"Mainly Clear",2:"Partly Cloudy",3:"Overcast",
                     45:"Foggy",48:"Foggy",51:"Drizzle",53:"Drizzle",55:"Drizzle",
                     61:"Rain",63:"Rain",65:"Heavy Rain",71:"Snow",73:"Snow",75:"Snow",
                     80:"Showers",81:"Showers",82:"Heavy Showers",
                     95:"Thunderstorm",96:"Thunderstorm",99:"Thunderstorm"}
        temps   = h.get("temperature_2m",[70]*48)
        precip  = h.get("precipitation_probability",[0]*48)
        wind    = h.get("windspeed_10m",[0]*48)
        wdir    = h.get("winddirection_10m",[0]*48)
        wcodes  = h.get("weathercode",[0]*48)
        wcode   = wcodes[idx] if idx < len(wcodes) else 0
        wdeg    = wdir[idx]  if idx < len(wdir)   else 0
        compass = _deg_to_compass(wdeg)
        wind_speed = round(wind[idx]) if idx < len(wind) else "N/A"
        wind_str = f"{wind_speed} mph {compass}".strip()
        payload = {
            "temp":       round(temps[idx]) if idx < len(temps) else "N/A",
            "rain_chance":precip[idx] if idx < len(precip) else "N/A",
            "wind_speed": wind_speed,
            "wind_dir":   compass,
            "wind":       wind_str,
            "condition":  wcode_map.get(wcode, "Clear"),
        }
        with _weather_cache_lock:
            _weather_cache[cache_key] = {"ts": now, "ttl": _WEATHER_TTL, "payload": payload}
        return payload
    except Exception as ex:
        print(f"[get_weather] lat={lat} lon={lon} hour={game_hour} venue={venue_id} err={ex}")
        payload = {"temp":"N/A","rain_chance":"N/A","wind_speed":"N/A","wind_dir":"","wind":"N/A","condition":"N/A"}
        with _weather_cache_lock:
            # Don't overwrite a valid entry that a concurrent thread may have written.
            existing = _weather_cache.get(cache_key)
            if not existing or existing.get("payload", {}).get("temp") in (None, "N/A"):
                _weather_cache[cache_key] = {"ts": now, "ttl": _WEATHER_FAIL_TTL, "payload": payload}
        return payload


def get_batters_from_boxscore(team_data, side):
    out = []
    batters = team_data.get("batters", [])
    players = team_data.get("players", {})
    for pid in batters:
        key = f"ID{pid}"
        p = players.get(key, {})
        name = p.get("person", {}).get("fullName", "")
        pos = p.get("position", {}).get("abbreviation", "?")
        s = p.get("stats", {}).get("batting", {})
        ss = p.get("seasonStats", {}).get("batting", {})
        slot_raw = p.get("battingOrder", 0)
        slot = 0
        if isinstance(slot_raw, int):
            slot = slot_raw if 1 <= slot_raw <= 9 else 0
        elif isinstance(slot_raw, str):
            if slot_raw and slot_raw[0].isdigit():
                slot = int(slot_raw[0])
        fgb = fg_batter(name)
        svb = sv_batter(name)
        bio = _bio_cache.get(pid, {})
        spl = _hit_split_cache.get(pid, {})
        out.append({
            "slot": slot, "id": pid, "name": name, "pos": pos,
            "lineup_status": "confirmed",
            "bats": bio.get("bats", "S"),
            "avg": ss.get("avg", fgb.get("fg_avg", ".---")),
            "obp": ss.get("obp", fgb.get("fg_obp", ".---")),
            "slg": ss.get("slg", fgb.get("fg_slg", ".---")),
            "ops": ss.get("ops", fgb.get("fg_ops", ".---")),
            "ab": s.get("atBats", 0), "hits": s.get("hits", 0),
            "hr": s.get("homeRuns", 0), "rbi": s.get("rbi", 0),
            "vs_l_avg": round(spl.get('vl', {}).get('avg', 0), 3) if spl.get('vl') else "N/A",
            "vs_r_avg": round(spl.get('vr', {}).get('avg', 0), 3) if spl.get('vr') else "N/A",
            "vs_l_ops": round(spl.get('vl', {}).get('ops', 0), 3) if spl.get('vl') else "N/A",
            "vs_r_ops": round(spl.get('vr', {}).get('ops', 0), 3) if spl.get('vr') else "N/A",
            "fg_pa": fgb.get("fg_pa", "N/A"), "fg_r": fgb.get("fg_r", "N/A"),
            "fg_sb": fgb.get("fg_sb", "N/A"), "fg_woba": fgb.get("fg_woba", "N/A"),
            "fg_wrc": fgb.get("fg_wrc", "N/A"), "fg_war": fgb.get("fg_war", "N/A"),
            "sv_xba": svb.get("sv_xba", "N/A"), "sv_xslg": svb.get("sv_xslg", "N/A"),
            "sv_xwoba": svb.get("sv_xwoba", "N/A"), "sv_ev": svb.get("sv_ev", "N/A"),
            "sv_hh_pct": svb.get("sv_hh_pct", "N/A"), "sv_brl_pct": svb.get("sv_brl_pct", "N/A"),
            "pull_pct_air": svb.get("pull_pct_air", "N/A"),
            "sv_la": svb.get("sv_la", "N/A"),
        })
    return out

def parse_game(g, prefer_live_weather=True):
    try:
        pk   = g.get("gamePk")
        stat = g.get("status",{}).get("detailedState","Scheduled")
        st_l = str(stat or "").lower()
        is_live = "progress" in st_l or "manager challenge" in st_l or "review" in st_l
        is_final = any(token in st_l for token in ("final", "game over", "completed early"))
        is_postponed = "postponed" in st_l
        is_cancelled = "cancelled" in st_l or "canceled" in st_l
        is_suspended = "suspended" in st_l
        if is_postponed:
            st = "Postponed"
        elif is_cancelled:
            st = "Cancelled"
        elif is_suspended:
            st = "Suspended"
        else:
            st = "Live" if is_live else ("Final" if is_final else "Scheduled")
        away = g.get("teams",{}).get("away",{})
        home = g.get("teams",{}).get("home",{})
        at   = away.get("team",{}); ht = home.get("team",{})
        aid  = at.get("id"); hid = ht.get("id")
        ap   = away.get("probablePitcher",{}); hp = home.get("probablePitcher",{})
        ven  = g.get("venue",{})
        venue_id = ven.get("id")
        vloc = ven.get("location",{}) or {}
        coords = vloc.get("defaultCoordinates",{}) or {}
        lat  = coords.get("latitude")
        lon  = coords.get("longitude")
        try:
            dt_utc_wx       = datetime.fromisoformat(g.get("gameDate","").replace("Z","+00:00"))
            from datetime import timedelta
            utc_offset      = VENUE_UTC_OFFSET.get(venue_id, -5)
            game_hour_local = (dt_utc_wx + timedelta(hours=utc_offset)).hour
        except Exception:
            game_hour_local = 13
        raw_weather = g.get("weather", {}) or {}
        if not prefer_live_weather:
            # Bulk dashboard load: use MLB schedule weather if present; otherwise skip
            # the Open-Meteo call entirely to avoid parallel cache pollution.
            if raw_weather:
                wx = {
                    "temp": raw_weather.get("temp", "N/A"),
                    "condition": raw_weather.get("condition", "N/A"),
                    "wind": raw_weather.get("wind", "N/A"),
                    "wind_speed": raw_weather.get("wind", "N/A"),
                    "wind_dir": "",
                    "rain_chance": raw_weather.get("precipitationChance", "N/A"),
                }
            else:
                wx = {"temp": "N/A", "condition": "N/A", "wind": "N/A",
                      "wind_speed": "N/A", "wind_dir": "", "rain_chance": "N/A"}
        else:
            wx = get_weather(lat, lon, game_hour_local, venue_id=venue_id)
            # Fallback: use MLB schedule weather when Open-Meteo fails or returns unavailable data.
            if (wx.get("temp") in (None, "N/A") or wx.get("condition") in (None, "", "N/A")) and raw_weather:
                print(f"[weather_fallback] using MLB weather for gamePk={pk} venue={venue_id}")
                wx = {
                    "temp": raw_weather.get("temp", "N/A"),
                    "condition": raw_weather.get("condition", "N/A"),
                    "wind": raw_weather.get("wind", "N/A"),
                    "wind_speed": raw_weather.get("wind", "N/A"),
                    "wind_dir": "",
                    "rain_chance": raw_weather.get("precipitationChance", "N/A"),
                }
        gt   = g.get("gameDate","")
        try:
            dt_utc = datetime.fromisoformat(gt.replace("Z","+00:00"))
            dt_et  = dt_utc.astimezone(ET)
            gt_fmt = dt_et.strftime("%-I:%M %p ET")
        except Exception: gt_fmt = "TBD"
        pf   = PARK_FACTORS.get(hid, 1.0)
        series_game  = int(g.get("seriesGameNumber") or 1)
        series_total = int(g.get("gamesInSeries")    or 3)
        double_header = str(g.get("doubleHeader") or "N").upper()
        game_number   = int(g.get("gameNumber") or 1)
        is_double_header = double_header == "Y"
        ap_n = ap.get("fullName","TBD"); hp_n = hp.get("fullName","TBD")
        fgap = fg_pitcher(ap_n); fghp = fg_pitcher(hp_n)
        era_a = float(fgap.get("fg_era") or 4.50); era_h = float(fghp.get("fg_era") or 4.50)
        edge = round(abs(era_a - era_h) * 2 + (pf - 1.0) * 10, 1)
        # Series context modifier: opener likely has ace, finale has back-end arms
        if series_game == 1:
            edge = round(edge + 0.2, 1)
        elif series_total > 1 and series_game == series_total:
            edge = round(max(0.0, edge - 0.3), 1)
        bar  = min(100, int(edge * 9))
        wc   = (wx.get("condition","") or "").lower()
        wi   = "🌧" if "rain" in wc else ("⛅" if "cloud" in wc else "☀")

        injury_alert = None
        try:
            away_inj = _top_team_injury(aid)
            home_inj = _top_team_injury(hid)
            cand = [x for x in (away_inj, home_inj) if x]
            if cand:
                rank = {"IL_60": 4, "IL_10": 3, "GTD": 2, "DTD": 1}
                cand.sort(key=lambda x: rank.get(x.get("status"), 0), reverse=True)
                c = cand[0]
                detail = c.get("description") or c.get("status")
                injury_alert = f"{c.get('name')} — {c.get('status').replace('_', '-')} ({detail})"
        except Exception:
            injury_alert = None

        # Team records
        away_rec = away.get("leagueRecord", {})
        home_rec = home.get("leagueRecord", {})
        away_w = away_rec.get("wins", ""); away_l = away_rec.get("losses", "")
        home_w = home_rec.get("wins", ""); home_l = home_rec.get("losses", "")
        away_record = f"{away_w}-{away_l}" if (away_w != "" and away_l != "") else ""
        home_record = f"{home_w}-{home_l}" if (home_w != "" and home_l != "") else ""
        # Final scores
        away_score = away.get("score")
        home_score = home.get("score")
        return {
            "gamePk": pk, "status": st,
            "isPostponed": is_postponed,
            "isCancelled": is_cancelled,
            "isSuspended": is_suspended,
            "isDoubleHeader": is_double_header,
            "gameNumber": game_number,
            "awayAbbr": at.get("abbreviation","?"), "awayName": at.get("name",""),
            "homeAbbr": ht.get("abbreviation","?"), "homeName": ht.get("name",""),
            "awayLogo": LOGO_BASE.format(team_id=aid) if aid else "",
            "homeLogo": LOGO_BASE.format(team_id=hid) if hid else "",
            "awayPitcher": ap_n, "homePitcher": hp_n,
            "awayRecord": away_record, "homeRecord": home_record,
            "awayScore": away_score, "homeScore": home_score,
            "venue": ven.get("name",""), "gameTime": gt_fmt,
            "parkFactor": pf, "edge": edge, "barPct": bar,
            "seriesGame": series_game, "seriesTotal": series_total,
            "temp": wx.get("temp","N/A"), "wind": wx.get("wind", f"{wx.get('wind_speed','?')} mph {wx.get('wind_dir','')}").strip(),
            "condition": wx.get("condition",""), "rainChance": wx.get("rain_chance","N/A"),
            "weatherIcon": wi,
            "injuryAlert": injury_alert,
        }
    except Exception as ex:
        print("[parse_game]", ex); return None

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def dashboard():
    return DASHBOARD_HTML

@app.route("/deep-dive/<int:game_pk>")
def deep_dive(game_pk):
    # Read fresh on every request so a stale startup-time cache never shows
    # the "missing from project root" fallback after a file restore.
    html = _read_html_or_fallback('deepdive.html')
    return Response(html, mimetype='text/html', headers={
        'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0',
        'Pragma': 'no-cache',
    })

@app.route('/props')
def props_page():
    html = _read_html_or_fallback('props.html')
    return Response(
        html,
        mimetype='text/html',
        headers={
            'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0',
            'Pragma': 'no-cache',
            'Expires': '0',
        },
    )

@app.route('/cheatsheets')
def cheatsheets_page():
    return CHEATSHEET_HTML

@app.route('/tracker')
def tracker_page():
    html = _read_html_or_fallback('tracker.html')
    return Response(
        html,
        mimetype='text/html',
        headers={
            'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0',
            'Pragma': 'no-cache',
            'Expires': '0',
        },
    )

@app.route('/consistency')
def consistency_page():
    return CONSISTENCY_HTML

@app.route('/batter-vs-pitcher')
def bvp_page():
    return BVP_HTML

@app.route('/value-bets')
def value_bets_page():
    return VALUE_BETS_HTML

@app.route('/nrfi')
def nrfi_page():
    return NRFI_HTML

@app.route('/tools')
def tools_page():
    return TOOLS_HTML

@app.route('/pitcher-deep-dive')
@app.route('/pitcher-deep-dive/<int:pitcher_id>')
def pitcher_deep_dive_page(pitcher_id=None):
    """Pitcher Analysis page (linked from dashboard header)."""
    return PITCHER_DEEP_DIVE_HTML

@app.route("/api/status")
def api_status():
    t0 = time.time()
    with _fg_lock:
        fgl, fgd, fgb, fgp = _fg_loaded, _fg_load_date, len(_fg_bat), len(_fg_pit)
    with _sv_lock:
        svl, svd = _sv_loaded, _sv_load_date
        svpi, svbi, svsc, svar = len(_sv_pit_xstats), len(_sv_bat_xstats), len(_sv_bat_statcast), len(_sv_arsenal_pct)
    resp = jsonify({
        "fangraphs": {"loaded":fgl,"date":str(fgd),"batters":fgb,"pitchers":fgp},
        "savant":    {"loaded":svl,"date":str(svd),"pit_xstats":svpi,"bat_xstats":svbi,"statcast":svsc,"arsenals":svar},
        "mlbMemory": _mlb_memory_status_payload(),
    })
    logging.info(f"[API] /api/status took {time.time() - t0:.3f}s")
    return resp

@app.route('/health')
def health_check():
    t0 = time.time()
    with _fg_lock:
        fg_ready = _fg_loaded
    with _sv_lock:
        sv_ready = _sv_loaded
    resp = {
        'status': 'ok',
        'fg_loaded': fg_ready,
        'sv_loaded': sv_ready,
    }
    logging.info(f"[API] /health took {time.time()-t0:.3f}s fg={fg_ready} sv={sv_ready}")
    return resp, 200


@app.route('/api/mc-upgrades/status')
def api_mc_upgrades_status():
    t0 = time.time()
    resp = jsonify({
        "success": True,
        "prewarm": get_prewarm_status(),
        "batx_weights": BATX_WEIGHTS_V2,
        "market_model_weights": MARKET_MODEL_WEIGHTS,
        "platoon_m": PLATOON_M,
        "league_platoon_splits": LEAGUE_PLATOON_SPLITS,
    })
    logging.info(f"[API] /api/mc-upgrades/status took {time.time() - t0:.3f}s")
    return resp


@app.route('/api/tracker/blend-debug')
def api_tracker_blend_debug():
    """QA endpoint: returns per-row rawProb, rawMultProb, marketImplied, adjProb, edge for a game."""
    t0 = time.time()
    game_pk = request.args.get('gamePk') or request.args.get('game_pk')
    if not game_pk:
        logging.info(f"[API] /api/tracker/blend-debug took {time.time() - t0:.3f}s (missing gamePk)")
        return jsonify({"error": "gamePk required"}), 400
    try:
        game_pk = int(game_pk)
    except Exception:
        logging.info(f"[API] /api/tracker/blend-debug took {time.time() - t0:.3f}s (bad gamePk)")
        return jsonify({"error": "gamePk must be an integer"}), 400
    adjustments = _get_adjustments()
    _maybe_refresh_fg()
    game_obj = fetch_schedule_game(game_pk)
    if not game_obj:
        logging.info(f"[API] /api/tracker/blend-debug took {time.time() - t0:.3f}s (game not found)")
        return jsonify({"error": "game not found"}), 404
    game_date = ((game_obj.get('gameDate') or '').split('T')[0] or
                 datetime.now(ET).strftime('%Y-%m-%d'))
    rows = _build_tracker_rows_for_game(game_pk, game_date, adjustments=adjustments, _sched=[game_obj], include_odds=True)
    debug_rows = []
    for r in rows:
        market_implied = r.get('marketImplied')
        under_implied = _american_to_implied(r.get('bestUnderPrice'))
        fair_over = None
        fair_under = None
        if market_implied and under_implied:
            fair_over, fair_under = devig_power(market_implied, under_implied)
        debug_rows.append({
            "player": r.get('player'),
            "team": r.get('team'),
            "marketKey": r.get('marketKey'),
            "line": r.get('line'),
            "rawProb": r.get('rawProb'),
            "rawMultProb": r.get('rawMultProb'),
            "marketImplied": market_implied,
            "underImplied": under_implied,
            "fairOver": fair_over,
            "fairUnder": fair_under,
            "adjProb": r.get('adjProb'),
            "edge": r.get('edge'),
            "hubRating": r.get('hubRating'),
            "evPct": r.get('evPct'),
            "blended": r.get('marketImplied') is not None and r.get('marketImplied', 0) > 0,
        })
    blended_count = sum(1 for r in debug_rows if r['blended'])
    return jsonify({
        "gamePk": game_pk,
        "gameDate": game_date,
        "totalRows": len(debug_rows),
        "blendedRows": blended_count,
        "multiplicativeOnlyRows": len(debug_rows) - blended_count,
        "rows": debug_rows,
    })


def _mlb_memory_status_payload():
    payload = _mlb_memory_store_payload()
    latest = payload.get("latest") if isinstance(payload.get("latest"), dict) else None
    latest_meta = (latest or {}).get("meta") or {}
    file_exists = os.path.exists(MLB_MEMORY_STORE)
    with _mlb_memory_lock:
        collecting = bool(_mlb_memory_collecting)
        last_collect = _mlb_memory_last_collect
        last_error = _mlb_memory_last_error
    return {
        "collecting": collecting,
        "lastCollectAt": last_collect,
        "lastError": last_error,
        "nextMode": _memory_mode_for_now('auto'),
        "snapshotCount": len(payload.get("snapshots") or []),
        "latest": {
            "createdAt": (latest or {}).get("createdAt"),
            "targetDateET": (latest or {}).get("targetDateET"),
            "teamCount": latest_meta.get("teamCount"),
            "gameCount": latest_meta.get("gameCount"),
            "featuredPlayers": latest_meta.get("featuredPlayers"),
            "mode": latest_meta.get("mode"),
        },
        "file": {
            "exists": file_exists,
            "path": MLB_MEMORY_STORE,
            "sizeBytes": os.path.getsize(MLB_MEMORY_STORE) if file_exists else 0,
            "modifiedAt": datetime.fromtimestamp(os.path.getmtime(MLB_MEMORY_STORE), tz=timezone.utc).isoformat() if file_exists else None,
        },
        "updatedAt": payload.get("updatedAt"),
    }


def _run_mlb_memory_collect(date_str=None, days_back=2, max_games_per_day=30, include_boxscores=True, max_players=160, mode=None):
    global _mlb_memory_collecting, _mlb_memory_last_collect, _mlb_memory_last_error
    with _mlb_memory_lock:
        if _mlb_memory_collecting:
            return None, "Collect already in progress"
        _mlb_memory_collecting = True
        _mlb_memory_last_error = None
    try:
        defaults = _memory_mode_defaults(mode)
        eff_mode = defaults.get('mode')
        snapshot = _collect_mlb_memory_snapshot(
            date_str=date_str,
            days_back=int(defaults.get('days_back', days_back) if mode else days_back),
            max_games_per_day=int(defaults.get('max_games_per_day', max_games_per_day) if mode else max_games_per_day),
            include_boxscores=bool(defaults.get('include_boxscores', include_boxscores) if mode else include_boxscores),
            max_players=int(defaults.get('max_players', max_players) if mode else max_players),
            mode=eff_mode if mode else 'manual',
            include_team_rosters=bool(defaults.get('include_team_rosters', False) if mode else False),
            transactions_days=int(defaults.get('transactions_days', 2) if mode else 2),
        )
        _append_mlb_memory_snapshot(snapshot, keep=_MLB_MEMORY_KEEP_SNAPSHOTS)
        _mlb_memory_last_collect = datetime.now(timezone.utc).isoformat()
        return snapshot, None
    except Exception as ex:
        _mlb_memory_last_error = str(ex)
        return None, str(ex)
    finally:
        with _mlb_memory_lock:
            _mlb_memory_collecting = False


_mlb_memory_worker_started = False


def _start_mlb_memory_worker(interval_sec=3 * 60 * 60):
    global _mlb_memory_worker_started
    with _mlb_memory_lock:
        if _mlb_memory_worker_started:
            return
        _mlb_memory_worker_started = True

    def _runner():
        while True:
            try:
                _run_mlb_memory_collect(
                    date_str=datetime.now(ET).strftime('%Y-%m-%d'),
                    mode='auto',
                )
            except Exception as ex:
                print(f'[mlb_memory_worker] {ex}')
            time.sleep(max(1800, int(interval_sec)))

    threading.Thread(target=_runner, daemon=True).start()


@app.route('/api/memory/status')
def api_memory_status():
    try:
        return jsonify({"success": True, "status": _mlb_memory_status_payload()})
    except Exception as ex:
        print(f'[api_memory_status] {traceback.format_exc()}')
        return jsonify({"success": False, "error": str(ex)}), 500


@app.route('/api/admin/settings', methods=['GET', 'POST'])
def api_admin_settings():
    if request.method == 'POST':
        denied = _check_admin_auth()
        if denied:
            return denied
    try:
        if request.method == 'GET':
            return jsonify({'success': True, 'settings': _get_admin_settings()})
        payload = request.get_json(silent=True) or {}
        saved = _save_admin_settings(payload)
        return jsonify({'success': True, 'settings': saved})
    except Exception as ex:
        print(f'[api_admin_settings] {traceback.format_exc()}')
        return jsonify({'success': False, 'error': str(ex)}), 500


@app.route('/api/app-settings', methods=['GET', 'POST'])
def api_app_settings():
    if request.method == 'POST':
        denied = _check_admin_auth()
        if denied:
            return denied
    try:
        if request.method == 'GET':
            return jsonify({'success': True, 'settings': _get_app_settings()})
        payload = request.get_json(silent=True) or {}
        saved = _save_app_settings(payload)
        return jsonify({'success': True, 'settings': saved})
    except Exception as ex:
        print(f'[api_app_settings] {traceback.format_exc()}')
        return jsonify({'success': False, 'error': str(ex)}), 500


@app.route('/api/brain-data/upload', methods=['POST'])
def api_brain_data_upload():
    try:
        files_uploaded = []
        file_type = request.form.get('type', 'other')
        uploaded_files = request.files.getlist('files')
        if not uploaded_files:
            return jsonify({'success': False, 'error': 'No files provided'}), 400
        for f in uploaded_files:
            if f and f.filename:
                try:
                    filename = f.filename
                    safe_name = _unique_brain_upload_name(filename)
                    file_path = os.path.join(BRAIN_DATA_DIR, safe_name)
                    f.save(file_path)
                    files_uploaded.append(safe_name)
                    print(f"[api_brain_data_upload] Uploaded {safe_name} ({file_type})")
                except Exception as ex:
                    print(f"[api_brain_data_upload] File save failed for {f.filename}: {ex}")
        if not files_uploaded:
            return jsonify({'success': False, 'error': 'No files successfully uploaded'}), 400
        with _brain_upload_lock:
            state = _get_brain_upload_state()
            files_state = state.get('files', {})
            uploaded_at = datetime.now(timezone.utc).isoformat()
            for safe_name in files_uploaded:
                fpath = os.path.join(BRAIN_DATA_DIR, safe_name)
                stat = os.stat(fpath)
                files_state[safe_name] = {
                    'filename': safe_name,
                    'category': file_type or 'other',
                    'uploadedAt': uploaded_at,
                    'modifiedAt': datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                    'sizeBytes': stat.st_size,
                    'sizeKB': round(stat.st_size / 1024, 2),
                    'ingestState': 'staged',
                    'parsedAt': None,
                    'recordCount': 0,
                    'fieldCount': 0,
                    'fields': [],
                    'error': None,
                }
            state['files'] = files_state
            _save_brain_upload_state(state)
        upload_summary = _brain_uploaded_files_summary()
        return jsonify({
            'success': True,
            'uploadedCount': len(files_uploaded),
            'files': files_uploaded,
            'uploadSummary': upload_summary,
        })
    except Exception as ex:
        print(f'[api_brain_data_upload] {traceback.format_exc()}')
        return jsonify({'success': False, 'error': str(ex)}), 500


@app.route('/api/brain-data/list', methods=['GET'])
def api_brain_data_list():
    try:
        upload_summary = _brain_uploaded_files_summary()
        return jsonify({'success': True, 'files': upload_summary.get('files', []), 'uploadSummary': upload_summary})
    except Exception as ex:
        print(f'[api_brain_data_list] {traceback.format_exc()}')
        return jsonify({'success': False, 'error': str(ex)}), 500


@app.route('/api/brain-data/delete', methods=['POST'])
def api_brain_data_delete():
    try:
        payload = request.get_json(silent=True) or {}
        filename = payload.get('filename', '').strip()
        if not filename:
            return jsonify({'success': False, 'error': 'No filename provided'}), 400
        safe_name = "".join(c for c in filename if c.isalnum() or c in ('._-'))
        if safe_name != filename:
            return jsonify({'success': False, 'error': 'Invalid filename'}), 400
        fpath = os.path.join(BRAIN_DATA_DIR, safe_name)
        if not os.path.exists(fpath):
            return jsonify({'success': False, 'error': 'File not found'}), 404
        os.remove(fpath)
        with _brain_upload_lock:
            state = _get_brain_upload_state()
            files_state = state.get('files', {})
            files_state.pop(safe_name, None)
            state['files'] = files_state
            _save_brain_upload_state(state)
        print(f"[api_brain_data_delete] Deleted {safe_name}")
        return jsonify({'success': True, 'message': 'File deleted'})
    except Exception as ex:
        print(f'[api_brain_data_delete] {traceback.format_exc()}')
        return jsonify({'success': False, 'error': str(ex)}), 500


@app.route('/api/brain/ingest-status', methods=['GET'])
def api_brain_ingest_status():
    """
    Returns comprehensive ingestion status: what data sources are loaded and how much data.
    Shows Statscast, FanGraphs, and MLB API availability in the brain.
    """
    try:
        today_et = datetime.now(ET).strftime("%Y-%m-%d")

        statscast = _memory_ingest_statscast_data(today_et)
        fangraphs = _memory_ingest_fangraphs_data()
        mlb_api = _memory_ingest_mlb_api_player_stats([i for i in range(108, 146)])

        total_statscast = sum(statscast.get('summary', {}).values())
        total_fangraphs = sum(fangraphs.get('summary', {}).values())
        total_players = mlb_api.get('summary', {}).get('totalPlayers', 0)
        upload_summary = _brain_uploaded_files_summary()
        manual_records = int(upload_summary.get('totalRecords') or 0)
        total_points = int(total_statscast) + int(total_fangraphs) + int(total_players) + manual_records

        return jsonify({
            'success': True,
            'brainStatus': {
                'lastUpdated': datetime.now(timezone.utc).isoformat(),
                'dataIngestedAt': upload_summary.get('lastIngestedAt') or datetime.now(timezone.utc).isoformat(),
            },
            'ingestion': {
                'statscast': statscast,
                'fangraphs': fangraphs,
                'mlbApi': mlb_api,
                'manualUploads': upload_summary,
            },
            'summary': {
                'totalStatscastRecords': total_statscast,
                'totalFanGraphsRecords': total_fangraphs,
                'totalPlayerRecords': total_players,
                'totalManualUploadRecords': manual_records,
                'totalDataPoints': total_points,
                'totalLiveDataPoints': int(total_statscast) + int(total_fangraphs) + int(total_players),
                'manualUploadFiles': upload_summary.get('count', 0),
                'manualUploadSizeKB': upload_summary.get('totalSizeKB', 0),
                'manualUploadState': upload_summary.get('ingestionState', 'staged_only'),
                'dataSourcesActive': len([s for s in statscast.get('sources', []) if s] + [f for f in fangraphs.get('sources', []) if f] + [m for m in mlb_api.get('sources', []) if m]),
            }
        })
    except Exception as ex:
        print(f'[api_brain_ingest_status] {traceback.format_exc()}')
        return jsonify({'success': False, 'error': str(ex)}), 500


@app.route('/api/brain/ingest-trigger', methods=['POST'])
def api_brain_ingest_trigger():
    """
    Manually trigger comprehensive data ingestion from all sources.
    Refresh Statscast, FanGraphs, and MLB API caches.
    """
    try:
        force_refresh = request.get_json(silent=True) or {}
        force = force_refresh.get('force', False)

        _maybe_refresh_fg()
        _maybe_refresh_savant()
        _fetch_injury_status(force=force)

        today_et = datetime.now(ET).strftime("%Y-%m-%d")
        comprehensive = _memory_collect_comprehensive_data(today_et, team_ids=[i for i in range(108, 146)], mode='manual')
        manual_uploads = comprehensive.get('sources', {}).get('manualUploads', {})

        return jsonify({
            'success': True,
            'message': 'Ingestion triggered and completed',
            'data': comprehensive,
            'manualUploads': manual_uploads,
        })
    except Exception as ex:
        print(f'[api_brain_ingest_trigger] {traceback.format_exc()}')
        return jsonify({'success': False, 'error': str(ex)}), 500


@app.route('/api/memory/latest')
def api_memory_latest():
    try:
        payload = _mlb_memory_store_payload()
        latest = payload.get("latest") if isinstance(payload.get("latest"), dict) else None
        if not latest:
            return jsonify({"success": False, "error": "No MLB memory snapshot collected yet"}), 404
        summary_only = str(request.args.get('summaryOnly') or '').strip().lower() in ('1', 'true', 'yes')
        if summary_only:
            return jsonify({
                "success": True,
                "snapshot": {
                    "createdAt": latest.get("createdAt"),
                    "targetDateET": latest.get("targetDateET"),
                    "meta": latest.get("meta") or {},
                },
            })
        return jsonify({"success": True, "snapshot": latest})
    except Exception as ex:
        print(f'[api_memory_latest] {traceback.format_exc()}')
        return jsonify({"success": False, "error": str(ex)}), 500


@app.route('/api/memory/collect', methods=['POST'])
def api_memory_collect():
    payload = request.get_json(silent=True) or {}
    try:
        date_str = (payload.get('date') or request.args.get('date') or datetime.now(ET).strftime('%Y-%m-%d')).strip()
        days_back = int(payload.get('daysBack', request.args.get('daysBack', 2)) or 2)
        max_games_per_day = int(payload.get('maxGamesPerDay', request.args.get('maxGamesPerDay', 30)) or 30)
        include_boxscores = str(payload.get('includeBoxscores', request.args.get('includeBoxscores', '1'))).strip().lower() in ('1', 'true', 'yes')
        max_players = int(payload.get('maxPlayers', request.args.get('maxPlayers', 160)) or 160)
        mode = str(payload.get('mode', request.args.get('mode', 'manual'))).strip().lower()
        if mode not in ('manual', 'light', 'deep', 'auto'):
            mode = 'manual'

        snapshot, err = _run_mlb_memory_collect(
            date_str=date_str,
            days_back=max(0, min(days_back, 14)),
            max_games_per_day=max(1, min(max_games_per_day, 30)),
            include_boxscores=include_boxscores,
            max_players=max(20, min(max_players, 500)),
            mode=None if mode == 'manual' else mode,
        )
        if err:
            code = 409 if "in progress" in err.lower() else 500
            return jsonify({"success": False, "error": err}), code

        return jsonify({
            "success": True,
            "snapshotMeta": snapshot.get("meta") or {},
            "createdAt": snapshot.get("createdAt"),
            "targetDateET": snapshot.get("targetDateET"),
            "mode": mode,
            "status": _mlb_memory_status_payload(),
        })
    except Exception as ex:
        print(f'[api_memory_collect] {traceback.format_exc()}')
        return jsonify({"success": False, "error": str(ex)}), 500


@app.route('/api/memory/collect/deep', methods=['POST'])
def api_memory_collect_deep():
    try:
        payload = request.get_json(silent=True) or {}
        date_str = (payload.get('date') or request.args.get('date') or datetime.now(ET).strftime('%Y-%m-%d')).strip()
        snapshot, err = _run_mlb_memory_collect(date_str=date_str, mode='deep')
        if err:
            code = 409 if "in progress" in err.lower() else 500
            return jsonify({"success": False, "error": err}), code
        return jsonify({
            "success": True,
            "createdAt": snapshot.get("createdAt"),
            "targetDateET": snapshot.get("targetDateET"),
            "snapshotMeta": snapshot.get("meta") or {},
            "status": _mlb_memory_status_payload(),
        })
    except Exception as ex:
        print(f'[api_memory_collect_deep] {traceback.format_exc()}')
        return jsonify({"success": False, "error": str(ex)}), 500

@app.route("/api/games/today")
def api_games_today():
    t0 = time.perf_counter()
    # Keep dashboard responsive during cold starts; refresh in background.
    _maybe_refresh_fg()
    _maybe_refresh_savant()
    _fetch_injury_status(force=False)
    try:
        date_str = _normalize_date_str(request.args.get('date'))
        raw   = fetch_schedule(date_str)
        workers = min(12, max(1, len(raw)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            games = [g for g in ex.map(lambda x: parse_game(x, prefer_live_weather=False), raw) if g]
        resp = jsonify({"success":True,"games":games,"count":len(games)})
        logging.info(f"[API] /api/games/today took {time.perf_counter() - t0:.3f}s")
        return resp
    except Exception as ex:
        logging.error(f"[api_games_today] {traceback.format_exc()}")
        return jsonify({"success":False,"error":str(ex),"games":[]}), 500


@app.route("/api/game-summary/<int:game_pk>")
def api_game_summary(game_pk):
    t0 = time.perf_counter()
    try:
        gdata = fetch_schedule_game(game_pk)
        if not gdata:
            logging.info(f"[API] /api/game-summary/{{game_pk}} took {time.perf_counter() - t0:.3f}s (not found)")
            return jsonify({"success": False, "error": "Game not found"}), 404
        parsed = parse_game(gdata)
        if not parsed:
            logging.info(f"[API] /api/game-summary/{{game_pk}} took {time.perf_counter() - t0:.3f}s (parse fail)")
            return jsonify({"success": False, "error": "Unable to parse game"}), 500
        resp = jsonify({"success": True, "game": parsed})
        logging.info(f"[API] /api/game-summary/{{game_pk}} took {time.perf_counter() - t0:.3f}s")
        return resp
    except Exception as ex:
        logging.error(f"[api_game_summary] {traceback.format_exc()}")
        return jsonify({"success": False, "error": str(ex)}), 500


def _as_json_payload(resp):
    """Normalize Flask view return values (Response or (Response, status)) to a dict payload."""
    try:
        if isinstance(resp, tuple):
            resp = resp[0]
        if hasattr(resp, 'get_json'):
            return resp.get_json(silent=True) or {}
    except Exception:
        pass
    return {}




def _build_model_actual_compare_payload(game_pk, sims=1200, include_sim=True):
    gdata = fetch_schedule_game(game_pk)
    if not gdata:
        return {"success": False, "error": "Game not found"}

    parsed = parse_game(gdata) or {}
    game_status = str(parsed.get('status') or gdata.get('status', {}).get('detailedState') or '')
    is_final = game_status in ('Final', 'Game Over', 'Completed Early')
    away_abbr = parsed.get('awayAbbr') or ((gdata.get('teams', {}).get('away', {}).get('team', {}) or {}).get('abbreviation', 'AWAY'))
    home_abbr = parsed.get('homeAbbr') or ((gdata.get('teams', {}).get('home', {}).get('team', {}) or {}).get('abbreviation', 'HOME'))

    proj = _as_json_payload(api_game_projection(game_pk))
    f5 = _as_json_payload(api_f5_model(game_pk))
    sim = _as_json_payload(api_simulate(game_pk)) if include_sim else {}

    away_actual = int(parsed.get('awayScore') or 0)
    home_actual = int(parsed.get('homeScore') or 0)
    actual_total = away_actual + home_actual

    model_total = _safe_num(proj.get('total'))
    total_error = abs(model_total - actual_total) if is_final else None

    model_winner = proj.get('favorite') or 'EVEN'
    actual_winner = 'PUSH' if away_actual == home_actual else (away_abbr if away_actual > home_actual else home_abbr)
    winner_hit = 'NO EDGE' if model_winner == 'EVEN' else ('CORRECT' if model_winner == actual_winner else 'MISSED')

    sim_team = (sim.get('team') or {}) if isinstance(sim, dict) else {}
    if not sim_team:
        # Lightweight fallback when skipping expensive simulation calls.
        sim_team = {
            'home_win_pct': 0.5,
            'away_win_pct': 0.5,
            'mean_total': model_total,
        }
    sim_home_win = _safe_num(sim_team.get('home_win_pct'))
    sim_away_win = _safe_num(sim_team.get('away_win_pct'))
    sim_mean_total = _safe_num(sim_team.get('mean_total'))

    top_sgp = (sim.get('top_sgp_combos') or [])[:3] if include_sim else []
    top_parlay = top_sgp[0] if top_sgp else None
    parlay_rec = {
        'label': 'No strong correlation parlay found',
        'combinedProb': None,
        'combinedEVPct': None,
        'legs': [],
    }
    if top_parlay:
        parlay_rec = {
            'label': 'Top Monte Carlo SGP pair',
            'combinedProb': top_parlay.get('combined_prob'),
            'combinedEVPct': top_parlay.get('combined_ev_pct'),
            'legs': top_parlay.get('legs') or [],
        }

    moneyline_lean = proj.get('favorite') or 'EVEN'  # FIX: initialize — prevents UnboundLocalError on NEUTRAL/UNDER paths
    total_signal = 'NEUTRAL'
    if model_total >= 9.5 and sim_mean_total >= 9.0:
        total_signal = 'OVER LEAN'
        moneyline_lean = home_abbr if sim_home_win >= sim_away_win else away_abbr
        if proj.get('favorite') in (home_abbr, away_abbr):
            moneyline_lean = proj.get('favorite')

    elif model_total <= 7.6 and sim_mean_total <= 8.0:
        total_signal = 'UNDER LEAN'

    adjustment_reco = {
        'severity': 'low',
        'recommendation': 'Hold model weights',
        'reason': 'Projection and simulation are aligned',
    }
    if is_final and total_error is not None:
        if total_error >= 3.0:
            adjustment_reco = {
                'severity': 'high',
                'recommendation': 'Reduce total confidence and lower high-volatility market multipliers by 0.02',
                'reason': f'Large total miss ({total_error:.1f} runs)',
            }
        elif total_error >= 1.8:
            adjustment_reco = {
                'severity': 'medium',
                'recommendation': 'Slightly de-risk totals and game-side exposure',
                'reason': f'Moderate total miss ({total_error:.1f} runs)',
            }

    return {
        'success': True,
        'gamePk': game_pk,
        'status': game_status,
        'isFinal': is_final,
        'teams': {'away': away_abbr, 'home': home_abbr},
        'projection': {
            'awayRuns': proj.get('awayRuns'),
            'homeRuns': proj.get('homeRuns'),
            'total': proj.get('total'),
            'favorite': proj.get('favorite'),
            'runEnv': proj.get('runEnv'),
            'storylines': proj.get('matchup_insights') or [],
        },
        'actual': {
            'awayRuns': away_actual,
            'homeRuns': home_actual,
            'total': actual_total,
            'winner': actual_winner,
        },
        'comparison': {
            'winnerCall': winner_hit,
            'modelWinner': model_winner,
            'actualWinner': actual_winner,
            'totalError': round(total_error, 2) if total_error is not None else None,
            'projectionBias': round((model_total - actual_total), 2) if is_final else None,
        },
        'f5Model': {
            'awayF5': f5.get('awayF5'),
            'homeF5': f5.get('homeF5'),
            'totalF5': f5.get('totalF5'),
            'signal': f5.get('signal'),
            'favorite': f5.get('f5Favorite'),
        },
        'monteCarlo': {
            'sims': (sim.get('meta') or {}).get('sims') if include_sim else None,
            'meanTotal': sim_team.get('mean_total'),
            'homeWinPct': sim_team.get('home_win_pct'),
            'awayWinPct': sim_team.get('away_win_pct'),
            'topCorrelatedCombos': top_sgp,
        },
        'recommendations': {
            'parlay': parlay_rec,
            'totalSignal': total_signal,
            'moneylineLean': moneyline_lean,
            'adjustment': adjustment_reco,
        },
        'snapshotAt': datetime.now(timezone.utc).isoformat(),
    }


def _build_model_daily_summary_payload(date_str=None, sims=500, include_sim=True, max_games=20):
    date_str = date_str or datetime.now(ET).strftime('%Y-%m-%d')
    schedule = fetch_schedule(date_str) or []
    games = []
    for g in schedule[:max(1, int(max_games or 20))]:
        gpk = g.get('gamePk')
        if not gpk:
            continue
        try:
            games.append(_build_model_actual_compare_payload(int(gpk), sims=sims, include_sim=include_sim))
        except Exception as ex:
            games.append({'success': False, 'gamePk': gpk, 'error': str(ex)})

    ok_games = [g for g in games if g.get('success')]
    finals = [g for g in ok_games if g.get('isFinal')]
    final_count = len(finals)
    winner_correct = sum(1 for g in finals if (g.get('comparison') or {}).get('winnerCall') == 'CORRECT')
    winner_accuracy = round(winner_correct / max(1, final_count), 4) if final_count else None
    total_errors = [float((g.get('comparison') or {}).get('totalError')) for g in finals if (g.get('comparison') or {}).get('totalError') is not None]
    avg_total_error = round(sum(total_errors) / len(total_errors), 3) if total_errors else None

    avg_f5_total = round(sum(_safe_num((g.get('f5Model') or {}).get('totalF5')) for g in ok_games) / max(1, len(ok_games)), 3) if ok_games else 0
    avg_sim_total = round(sum(_safe_num((g.get('monteCarlo') or {}).get('meanTotal')) for g in ok_games) / max(1, len(ok_games)), 3) if ok_games else 0

    top_parlays = []
    for g in ok_games:
        p = ((g.get('recommendations') or {}).get('parlay') or {})
        if p.get('combinedProb') is not None:
            top_parlays.append({
                'gamePk': g.get('gamePk'),
                'teams': g.get('teams'),
                'combinedProb': p.get('combinedProb'),
                'combinedEVPct': p.get('combinedEVPct'),
                'legs': p.get('legs') or [],
            })
    top_parlays.sort(key=lambda x: float(x.get('combinedProb') or 0), reverse=True)
    top_parlays = top_parlays[:8]

    current_adj = _get_adjustments()
    calibration_markets = _market_calibration(_collect_window_entries(date_str, 14), current_adj)
    calibration_actions = [m for m in calibration_markets if m.get('action') != 'hold'][:8]

    model_adjustment_recommendations = []
    if avg_total_error is not None and avg_total_error >= 2.0:
        model_adjustment_recommendations.append({
            'type': 'totals_calibration',
            'severity': 'high' if avg_total_error >= 2.8 else 'medium',
            'recommendation': 'Tighten total projections and reduce aggregate totals exposure',
            'metric': {'avgTotalError': avg_total_error},
        })
    if winner_accuracy is not None and winner_accuracy < 0.5:
        model_adjustment_recommendations.append({
            'type': 'winner_calibration',
            'severity': 'medium',
            'recommendation': 'Lower moneyline confidence and increase threshold for game-side recommendations',
            'metric': {'winnerAccuracy': winner_accuracy},
        })
    if not model_adjustment_recommendations:
        model_adjustment_recommendations.append({
            'type': 'stability',
            'severity': 'low',
            'recommendation': 'No urgent model shifts; keep current multipliers and monitor',
            'metric': {'winnerAccuracy': winner_accuracy, 'avgTotalError': avg_total_error},
        })

    final_summary = {
        'headline': f"Slate summary for {date_str}: {len(ok_games)} games processed",
        'gamesProcessed': len(ok_games),
        'finalGames': final_count,
        'winnerAccuracy': winner_accuracy,
        'avgTotalError': avg_total_error,
        'avgF5Total': avg_f5_total,
        'avgMonteCarloTotal': avg_sim_total,
        'topParlays': top_parlays,
        'modelAdjustmentRecommendations': model_adjustment_recommendations,
        'trackerCalibrationRecommendations': calibration_actions,
    }

    return {
        'success': True,
        'date': date_str,
        'generatedAt': datetime.now(timezone.utc).isoformat(),
        'includeSimulation': bool(include_sim),
        'maxGamesProcessed': max(1, int(max_games or 20)),
        'games': ok_games,
        'summary': final_summary,
    }


def _set_daily_summary_push_job(date_str, state, message='', error=''):
    with _daily_summary_push_lock:
        prev = _daily_summary_push_jobs.get(date_str) or {}
        payload = {
            'date': date_str,
            'state': state,
            'message': message or prev.get('message', ''),
            'error': error or '',
            'startedAt': prev.get('startedAt') or datetime.now(timezone.utc).isoformat(),
            'updatedAt': datetime.now(timezone.utc).isoformat(),
        }
        if state in ('completed', 'failed'):
            payload['completedAt'] = datetime.now(timezone.utc).isoformat()
        _daily_summary_push_jobs[date_str] = payload
        return payload


def _run_daily_summary_push_job(date_str, sims, include_sim, max_games):
    _set_daily_summary_push_job(date_str, 'running', 'Building final daily summary...')
    try:
        summary_payload = _build_model_daily_summary_payload(
            date_str=date_str,
            sims=sims,
            include_sim=include_sim,
            max_games=max_games,
        )
        admin = _get_admin_settings()
        summary_payload['pushedAt'] = datetime.now(timezone.utc).isoformat()
        summary_payload['pushedBy'] = admin.get('adminName') or 'system'
        summary_payload['pushedOrg'] = admin.get('orgName') or 'MLB Analytics Hub'

        store = _load_json(MODEL_DAILY_SUMMARY_STORE, {})
        store[date_str] = summary_payload
        _save_json(MODEL_DAILY_SUMMARY_STORE, store)

        _append_calibration_history('daily_summary_push', _get_adjustments(), {
            'date': date_str,
            'note': 'Pushed model adjustment recommendations into final daily summary',
            'applied': summary_payload.get('summary', {}).get('modelAdjustmentRecommendations', []),
        })
        _set_daily_summary_push_job(
            date_str,
            'completed',
            f"Push complete: {(summary_payload.get('summary') or {}).get('gamesProcessed', 0)} games processed.",
        )
    except Exception as ex:
        print('[daily_summary_push_job]', traceback.format_exc())
        _set_daily_summary_push_job(date_str, 'failed', 'Push failed.', str(ex))


@app.route('/api/model-actual/compare/<int:game_pk>')
def api_model_actual_compare(game_pk):
    try:
        sims = int(request.args.get('sims', 1200) or 1200)
        sims = max(500, min(2500, sims))
        payload = _build_model_actual_compare_payload(game_pk, sims=sims)
        code = 200 if payload.get('success') else 404
        return jsonify(payload), code
    except Exception as ex:
        print('[api_model_actual_compare]', traceback.format_exc())
        return jsonify({'success': False, 'error': str(ex)}), 500


@app.route('/api/model-actual/daily-summary')
def api_model_actual_daily_summary():
    try:
        date_str = request.args.get('date') or datetime.now(ET).strftime('%Y-%m-%d')
        sims = int(request.args.get('sims', 500) or 500)
        sims = max(300, min(1000, sims))
        include_sim = str(request.args.get('includeSim', '0')).strip().lower() in ('1', 'true', 'yes')
        max_games = int(request.args.get('maxGames', 20) or 20)
        max_games = max(5, min(20, max_games))
        return jsonify(_build_model_daily_summary_payload(date_str=date_str, sims=sims, include_sim=include_sim, max_games=max_games))
    except Exception as ex:
        print('[api_model_actual_daily_summary]', traceback.format_exc())
        return jsonify({'success': False, 'error': str(ex)}), 500


@app.route('/api/model-actual/daily-summary/push', methods=['POST'])
def api_model_actual_daily_summary_push():
    try:
        payload = request.get_json(silent=True) or {}
        date_str = payload.get('date') or datetime.now(ET).strftime('%Y-%m-%d')
        sims = int(payload.get('sims', 800) or 800)
        sims = max(300, min(1500, sims))
        include_sim = str(payload.get('includeSim', '1')).strip().lower() in ('1', 'true', 'yes')
        max_games = int(payload.get('maxGames', 20) or 20)
        max_games = max(5, min(20, max_games))

        with _daily_summary_push_lock:
            running = (_daily_summary_push_jobs.get(date_str) or {}).get('state') == 'running'
            running_status = _daily_summary_push_jobs.get(date_str)
        if running:
            return jsonify({'success': True, 'queued': True, 'alreadyRunning': True, 'date': date_str, 'status': running_status})

        _set_daily_summary_push_job(date_str, 'queued', 'Push queued...')
        threading.Thread(
            target=_run_daily_summary_push_job,
            args=(date_str, sims, include_sim, max_games),
            daemon=True,
        ).start()
        return jsonify({
            'success': True,
            'queued': True,
            'date': date_str,
            'status': _daily_summary_push_jobs.get(date_str),
            'config': {'sims': sims, 'includeSim': include_sim, 'maxGames': max_games},
        })
    except Exception as ex:
        print('[api_model_actual_daily_summary_push]', traceback.format_exc())
        return jsonify({'success': False, 'error': str(ex)}), 500


@app.route('/api/model-actual/daily-summary/push-status')
def api_model_actual_daily_summary_push_status():
    try:
        date_str = request.args.get('date') or datetime.now(ET).strftime('%Y-%m-%d')
        with _daily_summary_push_lock:
            status = _daily_summary_push_jobs.get(date_str)
        if not status:
            return jsonify({'success': True, 'date': date_str, 'state': 'idle', 'status': None})
        return jsonify({'success': True, 'date': date_str, 'state': status.get('state') or 'idle', 'status': status})
    except Exception as ex:
        print('[api_model_actual_daily_summary_push_status]', traceback.format_exc())
        return jsonify({'success': False, 'error': str(ex)}), 500


@app.route('/api/model-actual/daily-summary/stored')
def api_model_actual_daily_summary_stored():
    try:
        date_str = request.args.get('date') or datetime.now(ET).strftime('%Y-%m-%d')
        store = _load_json(MODEL_DAILY_SUMMARY_STORE, {})
        payload = store.get(date_str)
        if not isinstance(payload, dict):
            return jsonify({'success': False, 'error': 'No stored daily summary for date', 'date': date_str}), 404
        return jsonify({'success': True, 'date': date_str, 'payload': payload})
    except Exception as ex:
        print('[api_model_actual_daily_summary_stored]', traceback.format_exc())
        return jsonify({'success': False, 'error': str(ex)}), 500

@app.route("/api/game/<int:game_pk>")
def api_game_detail(game_pk):
    t0 = time.perf_counter()
    # Wait up to 5 s for each cache so FG/Savant columns appear even on cold starts.
    _wait_for_fg_data(timeout_sec=_CACHE_WAIT_TIMEOUT_SEC)
    _wait_for_savant_data(timeout_sec=_CACHE_WAIT_TIMEOUT_SEC)
    try:
        # Use _props_fetch_game as the single source of truth: it handles boxscore,
        # schedule lineups, and roster fallback in one call.
        gdata, away_bats, home_bats, away_t, home_t, _ = _props_fetch_game(game_pk)
        if not gdata:
            return jsonify({"success": False, "error": "Game not found", "awayBatters": [], "homeBatters": []}), 404
        _gstatus = str((gdata.get("status") or {}).get("detailedState") or "").lower()
        _is_inactive = any(tok in _gstatus for tok in ("postponed", "cancelled", "canceled", "suspended"))
        return jsonify({
            "success": True,
            "awayBatters": away_bats,
            "homeBatters": home_bats,
            "isPostponed": "postponed" in _gstatus,
            "isCancelled": any(tok in _gstatus for tok in ("cancelled", "canceled")),
            "isSuspended": "suspended" in _gstatus,
            "isInactive": _is_inactive,
            "gameStatus": _gstatus,
        })
    except Exception as ex:
        print("[api_game_detail]", traceback.format_exc())
        return jsonify({"success":False,"error":str(ex),"awayBatters":[],"homeBatters":[]}), 500
    finally:
        ms = int((time.perf_counter() - t0) * 1000)
        if ms >= 1000:
            print(f"[perf] /api/game/{game_pk} {ms}ms")

@app.route("/api/game/livedata/<int:game_pk>")
def api_game_livedata(game_pk):
    try:
        r = requests.get(
            "https://statsapi.mlb.com/api/v1.1/game/{}/feed/live".format(game_pk),
            timeout=10
        )
        r.raise_for_status()
        data = r.json()
        ls    = data.get("liveData", {}).get("linescore", {})
        box   = data.get("liveData", {}).get("boxscore",  {})
        gdata = data.get("gameData", {})
        sd    = gdata.get("status", {}).get("detailedState", "")

        inum  = ls.get("currentInning", 0)
        ihalf = ls.get("inningHalf", "Top")
        if any(x in sd for x in ["Middle", "Mid ", "Between"]):
            ilbl = "MID {}".format(inum)
        elif ihalf == "Bottom":
            ilbl = "BOT {}".format(inum)
        else:
            ilbl = "TOP {}".format(inum)

        ht = ls.get("teams", {}).get("home", {})
        at = ls.get("teams", {}).get("away", {})
        offense = ls.get("offense", {})
        bases = {
            "first":  bool(offense.get("first")),
            "second": bool(offense.get("second")),
            "third":  bool(offense.get("third")),
        }
        bid   = (offense.get("batter")  or {}).get("id")
        bname = (offense.get("batter")  or {}).get("fullName", "")
        od    = (offense.get("onDeck")  or {}).get("fullName", "")
        ih    = (offense.get("inHole")  or {}).get("fullName", "")
        pid2  = (ls.get("defense", {}).get("pitcher") or {}).get("id")
        pname = (ls.get("defense", {}).get("pitcher") or {}).get("fullName", "")

        pip = "x2014"; per = "x2014"; bab = 0; bah = 0; baops = "x2014"
        for side in ("home", "away"):
            pl = box.get("teams", {}).get(side, {}).get("players", {})
            if pid2:
                ps = pl.get("ID{}".format(pid2), {})
                st = ps.get("stats", {}).get("pitching", {})
                if st:
                    pip = st.get("inningsPitched", pip)
                    per = st.get("earnedRuns", per)
                    bab = st.get("basesOnBalls", bab)
                    bah = st.get("hits", bah)
                    baops = st.get("hits", baops)
        return jsonify({
            "success": True,
            "inningLabel": ilbl,
            "outs": ls.get("outs", 0),
            "balls": ls.get("balls", 0),
            "strikes": ls.get("strikes", 0),
            "awayRuns": at.get("runs", 0),
            "homeRuns": ht.get("runs", 0),
            "awayHits": at.get("hits", 0),
            "homeHits": ht.get("hits", 0),
            "awayErrors": at.get("errors", 0),
            "homeErrors": ht.get("errors", 0),
            "bases": bases,
            "pitcher": {"name": pname, "ip": pip, "er": per},
            "batter": {"name": bname, "ab": (box.get("teams", {}).get("home", {}).get("players", {}).get("ID{}".format(bid), {}) or {}).get("stats", {}).get("batting", {}).get("atBats", "—"), "h": (box.get("teams", {}).get("home", {}).get("players", {}).get("ID{}".format(bid), {}) or {}).get("stats", {}).get("batting", {}).get("hits", "—"), "ops": "—"},
            "dueUp": [od, ih],
        })
    except Exception as ex:
        print("[api_game_livedata]", traceback.format_exc())
        return jsonify({"success": False, "error": str(ex)})

@app.route("/api/pitchers/<int:game_pk>")
def api_pitchers(game_pk):
    t0 = time.perf_counter()
    # Keep endpoint responsive during cold starts; refresh in background.
    _maybe_refresh_fg()
    _maybe_refresh_savant()
    try:
        g = fetch_schedule_game(game_pk)
        if g:
            ap = g.get("teams",{}).get("away",{}).get("probablePitcher",{})
            hp = g.get("teams",{}).get("home",{}).get("probablePitcher",{})
            # Caller may supply names from the games list to prevent mismatch
            # when the schedule API returns different probable pitchers between calls.
            an = request.args.get("away_name") or ap.get("fullName","TBD")
            hn = request.args.get("home_name") or hp.get("fullName","TBD")
            # Merge MLB API + FanGraphs + Savant for each pitcher
            def build_pitcher_stats(name, pid):
                mlb = pitcher_stats_mlb(pid) if pid else {}
                fg  = fg_pitcher(name)
                sv  = sv_pitcher(name)
                s   = dict(mlb)
                s.update(fg)
                for k,v in sv.items():
                    if k not in ("sv_arsenal_pct","sv_arsenal_velo"):
                        s[k] = v
                s["sv_arsenal_pct"]  = sv.get("sv_arsenal_pct",{})
                s["sv_arsenal_velo"] = sv.get("sv_arsenal_velo",{})
                with _sv_lock:
                    s["sv_pit_arsenal_stats"] = dict(_sv_pit_arsenal_stats.get(str(pid), {})) if pid else {}
                return s

            # Warm zone-chart cache in background so modal opens quickly.
            _trigger_zonechart_prefetch_async(ap.get("id"))
            _trigger_zonechart_prefetch_async(hp.get("id"))

            return jsonify({
                "success": True,
                "awayPitcher": {
                    "id": ap.get("id"),
                    "name": an,
                    "stats": build_pitcher_stats(an, ap.get("id")),
                    "vulnerability": _pitcher_prop_vulnerability(ap.get("id"), game_pk),
                },
                "homePitcher": {
                    "id": hp.get("id"),
                    "name": hn,
                    "stats": build_pitcher_stats(hn, hp.get("id")),
                    "vulnerability": _pitcher_prop_vulnerability(hp.get("id"), game_pk),
                },
            })
        return jsonify({"success":False,"error":"Game not found","awayPitcher":{},"homePitcher":{}})
    except Exception as ex:
        print("[api_pitchers]", traceback.format_exc())
        return jsonify({"success":False,"error":str(ex),"awayPitcher":{},"homePitcher":{}}), 500
    finally:
        ms = int((time.perf_counter() - t0) * 1000)
        if ms >= 1000:
            print(f"[perf] /api/pitchers/{game_pk} {ms}ms")


def _zonechart_cache_get(player_id, today):
    with _zonechart_lock:
        entry = _zonechart_cache.get(player_id)
    if entry is None:
        return None
    if isinstance(entry, dict) and 'data' in entry:
        if entry.get('date') == today:
            return entry.get('data')
        return None
    # Back-compat for older in-memory cache shape (plain list)
    return entry


def _zonechart_cache_set(player_id, today, zone_data):
    with _zonechart_lock:
        _zonechart_cache[player_id] = {"date": today, "data": zone_data}


def _compute_zonechart_data(player_id, force_refresh=False):
    today = datetime.now().strftime("%Y-%m-%d")
    if not force_refresh:
        cached = _zonechart_cache_get(player_id, today)
        if cached is not None:
            return cached

    pr = requests.get(f"{MLB_API}/people/{player_id}", timeout=10)
    pr.raise_for_status()
    people = pr.json().get("people", [])
    if not people:
        raise ValueError("Player not found")

    is_pitcher = (people[0].get("primaryPosition", {}).get("abbreviation", "") or "?") in ("P", "SP", "RP", "CP")

    import pybaseball as pb
    year = datetime.now().year
    start_dt = f"{year}-03-01"
    end_dt = today

    zone_data = [None] * 9
    if is_pitcher:
        sc = pb.statcast_pitcher(start_dt=start_dt, end_dt=end_dt, player_id=int(player_id))
        if sc is None or sc.empty:
            _zonechart_cache_set(player_id, today, zone_data)
            return zone_data
        for zone_id in range(9):
            zone_data[zone_id] = _compute_zone_metrics_pitcher(sc, zone_id, is_pitcher=True)
    else:
        sc = pb.statcast_batter(start_dt=start_dt, end_dt=end_dt, player_id=int(player_id))
        if sc is None or sc.empty:
            _zonechart_cache_set(player_id, today, zone_data)
            return zone_data
        for zone_id in range(9):
            zone_data[zone_id] = _compute_zone_metrics_pitcher(sc, zone_id, is_pitcher=False)

    _zonechart_cache_set(player_id, today, zone_data)
    return zone_data


def _trigger_zonechart_prefetch_async(player_id):
    if not player_id:
        return
    try:
        pid = int(player_id)
    except Exception:
        return

    today = datetime.now().strftime("%Y-%m-%d")
    if _zonechart_cache_get(pid, today) is not None:
        return

    with _zonechart_lock:
        if pid in _zonechart_prefetching:
            return
        _zonechart_prefetching.add(pid)

    def _runner():
        try:
            _compute_zonechart_data(pid)
        except Exception:
            print(f"[zonechart_prefetch] {pid}: {traceback.format_exc()}")
        finally:
            with _zonechart_lock:
                _zonechart_prefetching.discard(pid)

    threading.Thread(target=_runner, daemon=True).start()


# ── Pitcher vs. Opposing Lineup — "Top Damage Threats" ───────────────────────
def _damage_score(batter_stats, pitcher_stats):
    """Compute a 0-100 score representing this batter's projected damage
    against this pitcher.  Higher = more dangerous.

    Inputs are the enriched dicts returned by get_batters_from_boxscore()
    (merged with fg_batter/sv_batter) and build_pitcher_stats().
    """
    def _f(v, d=0.0):
        try:
            f = float(v)
            return f if f == f else d   # nan check
        except Exception:
            return d

    # Batter quality
    xwoba = _f(batter_stats.get("sv_xwoba") or batter_stats.get("fg_woba"), 0.320)
    iso   = _f(batter_stats.get("fg_iso"), 0.150)
    brl_raw = _f(batter_stats.get("sv_brl_pct"), 6.0)
    brl     = brl_raw / 100.0 if brl_raw > 1 else brl_raw
    ev    = _f(batter_stats.get("sv_ev"), 88.5)
    wrc   = _f(batter_stats.get("fg_wrc"), 100)
    pa    = _f(batter_stats.get("fg_pa"), 0)

    # Confidence: don't let a 5-PA April sample dominate.  Shrink towards league avg.
    conf = min(1.0, pa / 80.0) if pa > 0 else 0.5

    # Pitcher weakness
    fip    = _f(pitcher_stats.get("fg_fip"),  4.20)
    xera   = _f(pitcher_stats.get("sv_xera"), _f(pitcher_stats.get("fg_era"), 4.20))
    kpct_p = _f(pitcher_stats.get("fg_kpct"), 0.22)
    bbpct  = _f(pitcher_stats.get("fg_bbpct"), 0.08)
    hr9    = _f(pitcher_stats.get("fg_hr9"),  1.10)

    score = 0.0
    score += (xwoba - 0.310) * 120 * conf
    score += (iso   - 0.150) *  80 * conf
    score += (brl   - 0.065) * 100 * conf
    score += (ev    - 88.5)  * 1.2 * conf
    score += (wrc   - 100)   * 0.18 * conf
    score += (fip   - 4.10)  * 3.5
    score += (xera  - 4.10)  * 2.5
    score += (hr9   - 1.10)  * 4.0
    score += (bbpct - 0.08)  *  80
    score -= (kpct_p - 0.22) *  80
    return round(max(0.0, min(100.0, 50.0 + score)), 1)


def _project_batter_vs_pitcher(batter_stats, pitcher_stats):
    """Return per-game probabilities (1+ hit, 2+ TB, HR, 1+ RBI) + projected
    totals.  Calibrated so elite hitter vs weak SP ≈ 80% hit, 15-25% HR;
    below-average batter ≈ 50% hit, 3-6% HR.  These are per-game odds,
    not per-PA."""
    def _f(v, d=0.0):
        try: return float(v)
        except Exception: return d

    xba   = _f(batter_stats.get("sv_xba")   or batter_stats.get("fg_avg"),  0.250)
    xwoba = _f(batter_stats.get("sv_xwoba") or batter_stats.get("fg_woba"), 0.320)
    iso   = _f(batter_stats.get("fg_iso"),  0.140)
    brl_r = _f(batter_stats.get("sv_brl_pct"), 6.0)
    brl   = brl_r / 100.0 if brl_r > 1 else brl_r
    pa_s  = _f(batter_stats.get("fg_pa"), 0)

    # Shrink tiny samples towards league averages.
    conf = min(1.0, pa_s / 80.0) if pa_s > 0 else 0.4
    xba   = xba   * conf + 0.250 * (1 - conf)
    xwoba = xwoba * conf + 0.320 * (1 - conf)
    iso   = iso   * conf + 0.150 * (1 - conf)

    fip   = _f(pitcher_stats.get("fg_fip"),  4.20)
    hr9   = _f(pitcher_stats.get("fg_hr9"),  1.10)
    kpct  = _f(pitcher_stats.get("fg_kpct"), 0.22)

    pitcher_adj = (4.20 / max(fip, 2.50)) ** 0.5   # dampened, keeps in ~0.85–1.20
    k_adj       = 0.22 / max(kpct, 0.12)      # low-K pitcher → >1

    pa = 4.2
    # Per-PA probabilities — tuned so game-level stays realistic:
    # elite hitter vs weak SP: ~80% hit, 60% TB, 12% HR, 60% RBI
    # avg hitter vs avg SP:    ~50% hit, 35% TB,  4% HR, 35% RBI
    # weak hitter vs elite SP: ~30% hit, 15% TB, 1% HR,  15% RBI
    p_hit = max(0.04, min(0.38, xba * pitcher_adj * 0.90))
    # For TB, use per-PA probability of an extra-base hit, roughly slg-xba.
    slg_proxy = xba + iso
    p_tb_pa   = (slg_proxy - xba * 0.50) * pitcher_adj   # EBH probability
    p_tb      = max(0.06, min(0.30, p_tb_pa))
    # HR rate: typical range 1-4% per PA for big bats.
    hr_pa = (iso * 0.06 + brl * 0.28) * (hr9 / 1.10 if hr9 > 0 else 1.0) * pitcher_adj
    p_hr  = max(0.005, min(0.055, hr_pa))
    p_rbi = max(0.06, min(0.28, xwoba * 0.75 * pitcher_adj))

    def _game_prob(pp):
        # 1 - (1 - p)^PA
        return round(1 - (1 - pp) ** pa, 3)

    hit_prob = _game_prob(p_hit)
    _xgb_hit = None
    if xgb_ready('hits'):
        _xgb_hit = xgb_hit_prob(batter_stats, pitcher_stats)
        if _xgb_hit is not None:
            hit_prob = round(_clamp(0.40 * hit_prob + 0.60 * _xgb_hit, 0.03, 0.97), 3)

    return {
        "hitProb":  hit_prob,
        "tbProb":   _game_prob(p_tb),
        "hrProb":   _game_prob(p_hr),
        "rbiProb":  _game_prob(p_rbi),
        "projHits": round(p_hit * pa, 2),
        "projTB":   round(p_tb  * pa, 2),
        "projHR":   round(p_hr  * pa, 2),
        "projRBI":  round(p_rbi * pa, 2),
        "xgbHitProb": round(_xgb_hit, 4) if _xgb_hit is not None else None,
    }


def _pitcher_prop_vulnerability_from_inputs(p_stats, opposing_lineup, park_factor=1.0):
    """Build Sprint 3.2 pitcher prop vulnerability profile from pitcher+opponent context."""
    def _f(v, d=0.0):
        try:
            f = float(v)
            return f if f == f else d
        except Exception:
            return d

    bats = []
    for b in opposing_lineup or []:
        pos = (b.get("pos") or "").upper()
        if pos in ("P", "SP", "RP", "CP"):
            continue
        bats.append(b)

    park = _f(park_factor, 1.0)
    park = max(0.90, min(1.20, park))

    avg_values = []
    hh_values = []
    for b in bats:
        avg_values.append(_f(b.get("fg_avg") or b.get("avg"), 0.245))
        hh_values.append(_f(b.get("sv_hh_pct"), 37.0))

    opp_avg = sum(avg_values) / len(avg_values) if avg_values else 0.245
    opp_hh = sum(hh_values) / len(hh_values) if hh_values else 37.0

    whip = _f(p_stats.get("fg_whip") or p_stats.get("whip"), 1.25)
    era = _f(p_stats.get("fg_era") or p_stats.get("era"), 4.20)
    hr9 = _f(p_stats.get("fg_hr9") or p_stats.get("hr9"), 1.10)
    kpct = _f(p_stats.get("fg_kpct") or p_stats.get("sv_k_pct"), 0.22)
    if kpct > 1:
        kpct = kpct / 100.0
    xwoba_allowed = _f(p_stats.get("sv_xwoba_p"), 0.315)

    avg_contact_adj = 1.0 + ((whip - 1.25) * 0.22) + ((era - 4.20) * 0.06)
    est_avg_allowed = max(0.180, min(0.340, opp_avg * avg_contact_adj))
    hits_v = est_avg_allowed * park

    xwoba_hh_proxy = 35.0 + max(-4.0, min(7.0, (xwoba_allowed - 0.315) * 120.0))
    hh_allowed = max(28.0, min(49.0, (opp_hh * 0.5) + (xwoba_hh_proxy * 0.5)))

    hits_exploitable = hits_v >= 0.275
    hr_exploitable = hr9 > 1.10
    tb_exploitable = hh_allowed > 40.0
    k_fade_hits = kpct > 0.28

    return {
        "hits_vulnerability": {
            "value": round(hits_v, 3),
            "threshold": 0.275,
            "signal": "EXPLOITABLE" if hits_exploitable else "NEUTRAL",
            "badge": "HITS ↑ EXPLOITABLE" if hits_exploitable else "HITS ↑ NEUTRAL",
            "exploitable": hits_exploitable,
            "context": "opp_avg * park_factor",
        },
        "hr_vulnerability": {
            "value": round(hr9, 2),
            "threshold": 1.10,
            "signal": "EXPLOITABLE" if hr_exploitable else "NEUTRAL",
            "badge": "HR ↑ EXPLOITABLE" if hr_exploitable else "HR ↑ NEUTRAL",
            "exploitable": hr_exploitable,
            "context": "hr_allowed_per_9",
        },
        "k_vulnerability": {
            "value": round(kpct, 3),
            "threshold": 0.28,
            "signal": "FADE BATTER HITS" if k_fade_hits else "HITS FRIENDLY",
            "badge": "K ↓ FADE HITS" if k_fade_hits else "K ↓ HITS OK",
            "exploitable": not k_fade_hits,
            "context": "pitcher_k_rate",
        },
        "tb_vulnerability": {
            "value": round(hh_allowed, 1),
            "threshold": 40.0,
            "signal": "EXPLOITABLE TB UPSIDE" if tb_exploitable else "NEUTRAL",
            "badge": "TB ↑ UP" if tb_exploitable else "TB ↑ NEUTRAL",
            "exploitable": tb_exploitable,
            "context": "hard_hit_pct_allowed_proxy",
        },
    }


def _pitcher_prop_vulnerability(pitcher_id, game_pk):
    """Compute Sprint 3.2 prop vulnerability profile for a starter in a specific game."""
    if not pitcher_id or not game_pk:
        return {}

    try:
        game = fetch_schedule_game(game_pk)
        if not game:
            return {}

        away_team = game.get("teams", {}).get("away", {})
        home_team = game.get("teams", {}).get("home", {})
        ap = away_team.get("probablePitcher", {}) or {}
        hp = home_team.get("probablePitcher", {}) or {}

        is_away_pitcher = ap.get("id") == pitcher_id
        chosen_pitcher = ap if is_away_pitcher else hp
        opp_team = home_team if is_away_pitcher else away_team

        p_name = chosen_pitcher.get("fullName", "")
        p_stats = {}
        if pitcher_id:
            p_stats = dict(pitcher_stats_mlb(pitcher_id) or {})
        p_stats.update(fg_pitcher(p_name) or {})
        p_sv = sv_pitcher(p_name) or {}
        for k, v in p_sv.items():
            if k not in ("sv_arsenal_pct", "sv_arsenal_velo"):
                p_stats[k] = v

        try:
            box = requests.get(f"{MLB_API}/game/{game_pk}/boxscore", timeout=8).json().get("teams", {})
            away_lineup = get_batters_from_boxscore(box.get("away", {}), "away")
            home_lineup = get_batters_from_boxscore(box.get("home", {}), "home")
        except Exception:
            away_lineup, home_lineup = [], []

        def _roster_fallback(team_id):
            try:
                rr = requests.get(f"{MLB_API}/teams/{team_id}/roster?rosterType=active", timeout=6)
                roster = rr.json().get("roster", []) if rr.ok else []
                out = []
                for r in roster:
                    pos = (r.get("position") or {}).get("abbreviation", "?")
                    if pos == "P":
                        continue
                    person = r.get("person", {}) or {}
                    name = person.get("fullName", "")
                    if not name:
                        continue
                    fgb = fg_batter(name) or {}
                    svb = sv_batter(name) or {}
                    out.append({
                        "id": person.get("id"),
                        "name": name,
                        "pos": pos,
                        "slot": 0,
                        **fgb,
                        **svb,
                    })
                return out[:20]
            except Exception:
                return []

        if not away_lineup:
            away_lineup = _roster_fallback(away_team.get("team", {}).get("id"))
        if not home_lineup:
            home_lineup = _roster_fallback(home_team.get("team", {}).get("id"))

        opposing_lineup = home_lineup if is_away_pitcher else away_lineup
        park_factor = game.get("parkFactor") or game.get("park_factor") or 1.0
        out = _pitcher_prop_vulnerability_from_inputs(p_stats, opposing_lineup, park_factor)
        out["opponent"] = opp_team.get("team", {}).get("abbreviation", "")
        out["park_factor"] = park_factor
        return out
    except Exception:
        return {}


@app.route("/api/pitcher-matchup/<int:game_pk>")
def api_pitcher_matchup(game_pk):
    """For a given game, return each probable starter PLUS the top batters
    from the opposing lineup ranked by projected damage.  Enables the
    'Top Damage Threats' panel in the Pitcher Analysis page."""
    _maybe_refresh_fg()
    _maybe_refresh_savant()
    try:
        game = fetch_schedule_game(game_pk)
        if not game:
            return jsonify({"success": False, "error": "Game not found"}), 404

        away_team = game.get("teams", {}).get("away", {})
        home_team = game.get("teams", {}).get("home", {})
        ap = away_team.get("probablePitcher", {}) or {}
        hp = home_team.get("probablePitcher", {}) or {}
        an = ap.get("fullName", "TBD"); hn = hp.get("fullName", "TBD")

        # Get lineups from boxscore.
        try:
            box = requests.get(f"{MLB_API}/game/{game_pk}/boxscore", timeout=8).json().get("teams", {})
            away_lineup = get_batters_from_boxscore(box.get("away", {}), "away")
            home_lineup = get_batters_from_boxscore(box.get("home", {}), "home")
        except Exception:
            away_lineup, home_lineup = [], []

        # If lineup is empty (pre-game, not yet posted), fall back to the
        # team's active roster so the user still sees something.
        def _roster_fallback(team_id):
            try:
                rr = requests.get(f"{MLB_API}/teams/{team_id}/roster?rosterType=active", timeout=6)
                roster = rr.json().get("roster", []) if rr.ok else []
                out = []
                for r in roster:
                    pos = (r.get("position") or {}).get("abbreviation", "?")
                    if pos == "P": continue
                    person = r.get("person", {}) or {}
                    name = person.get("fullName", "")
                    if not name: continue
                    fgb = fg_batter(name) or {}
                    svb = sv_batter(name) or {}
                    out.append({
                        "id":   person.get("id"),
                        "name": name,
                        "pos":  pos,
                        "slot": 0,
                        **fgb, **svb,
                    })
                return out[:20]
            except Exception:
                return []

        if not away_lineup:
            away_lineup = _roster_fallback(away_team.get("team", {}).get("id"))
        if not home_lineup:
            home_lineup = _roster_fallback(home_team.get("team", {}).get("id"))

        def _build_side(pitcher_name, pitcher_id, opposing_lineup, opp_team):
            p_stats = {}
            if pitcher_id:
                p_stats = dict(pitcher_stats_mlb(pitcher_id))
            p_stats.update(fg_pitcher(pitcher_name) or {})
            sv = sv_pitcher(pitcher_name) or {}
            for k, v in sv.items():
                if k not in ("sv_arsenal_pct", "sv_arsenal_velo"):
                    p_stats[k] = v

            threats = []
            seen = set()
            for b in opposing_lineup:
                if not b.get("name") or b["name"] in seen: continue
                # Pitchers sometimes show up in boxscore "batters" arrays —
                # they are never a damage threat, skip them.
                if (b.get("pos") or "").upper() in ("P", "SP", "RP", "CP"):
                    continue
                seen.add(b["name"])
                # Merge any cache values not already present from the boxscore extractor.
                fgb = fg_batter(b["name"]) or {}
                svb = sv_batter(b["name"]) or {}
                merged = {**fgb, **svb, **b}
                proj = _project_batter_vs_pitcher(merged, p_stats)
                score = _damage_score(merged, p_stats)
                threats.append({
                    "id":       merged.get("id"),
                    "name":     merged.get("name"),
                    "pos":      merged.get("pos", "?"),
                    "slot":     merged.get("slot", 0),
                    "bats":     merged.get("fg_bats") or "R",
                    "image":    TEAM_HEADSHOT_BASE.format(player_id=merged["id"]) if merged.get("id") else "",
                    "avg":      merged.get("fg_avg") or merged.get("avg") or "—",
                    "obp":      merged.get("fg_obp") or merged.get("obp") or "—",
                    "slg":      merged.get("fg_slg") or merged.get("slg") or "—",
                    "ops":      merged.get("fg_ops") or merged.get("ops") or "—",
                    "hr":       merged.get("fg_hr"),
                    "rbi":      merged.get("fg_rbi"),
                    "wrc":      merged.get("fg_wrc"),
                    "iso":      merged.get("fg_iso"),
                    "woba":     merged.get("fg_woba"),
                    "xwoba":    merged.get("sv_xwoba"),
                    "xba":      merged.get("sv_xba"),
                    "xslg":     merged.get("sv_xslg"),
                    "ev":       merged.get("sv_ev"),
                    "hh_pct":   merged.get("sv_hh_pct"),
                    "brl_pct":  merged.get("sv_brl_pct"),
                    "score":    score,
                    **proj,
                })
            threats.sort(key=lambda x: x["score"], reverse=True)
            vulnerability = _pitcher_prop_vulnerability_from_inputs(
                p_stats,
                opposing_lineup,
                game.get("parkFactor") or game.get("park_factor") or 1.0,
            )

            return {
                "pitcher": {
                    "id":    pitcher_id,
                    "name":  pitcher_name,
                    "stats": p_stats,
                },
                "opponent":   opp_team,
                "threats":    threats[:9],     # top-9 most dangerous bats
                "lineupSize": len(opposing_lineup),
                "vulnerability": vulnerability,
                "propVulnerability": vulnerability,
            }

        away_pack = _build_side(an, ap.get("id"), home_lineup, home_team.get("team", {}).get("abbreviation", ""))
        home_pack = _build_side(hn, hp.get("id"), away_lineup, away_team.get("team", {}).get("abbreviation", ""))

        return jsonify({
            "success":    True,
            "gamePk":     game_pk,
            "awayTeam":   away_team.get("team", {}).get("abbreviation", ""),
            "homeTeam":   home_team.get("team", {}).get("abbreviation", ""),
            "awaySide":   away_pack,   # away pitcher vs home lineup
            "homeSide":   home_pack,   # home pitcher vs away lineup
        })
    except Exception as ex:
        print("[api_pitcher_matchup]", traceback.format_exc())
        return jsonify({"success": False, "error": str(ex)}), 500


@app.errorhandler(404)
def e404(e): return jsonify({"error":"Not found"}), 404
@app.errorhandler(500)
def e500(e): return jsonify({"error":str(e)}), 500


def _extract_wind_mph(wind_value):
    """Best-effort wind speed parse from strings like '14 mph NW'."""
    try:
        if isinstance(wind_value, (int, float)):
            return float(wind_value)
        m = re.search(r"(-?\d+(?:\.\d+)?)", str(wind_value or ""))
        return float(m.group(1)) if m else None
    except Exception:
        return None


def _fallback_matchup_insights(gdata, away_abbr, home_abbr, total_runs, run_env, favorite,
                               away_pitcher_name, home_pitcher_name,
                               away_pitcher_era, home_pitcher_era,
                               weather_payload):
    """Deterministic storyline fallback when Claude is unavailable."""
    notes = []

    sg = gdata.get("seriesGame")
    st = gdata.get("seriesTotal")
    if sg and st and st > 1:
        if sg == 1:
            notes.append(f"Series opener (Game 1 of {st}) often brings fresh high-leverage arms in late innings.")
        elif sg == st:
            notes.append(f"Series finale (Game {sg} of {st}) can increase lineup variance as teams manage bullpen usage.")
        else:
            notes.append(f"Mid-series spot (Game {sg} of {st}) favors stable baseline assumptions over one-off volatility.")

    if run_env == "HIGH":
        notes.append(f"Model run environment is HIGH with projected total {total_runs} — offense-friendly game script.")
    elif run_env == "LOW":
        notes.append(f"Model run environment is LOW with projected total {total_runs} — pitcher-friendly lean.")

    wind_txt = weather_payload.get("wind") if isinstance(weather_payload, dict) else "N/A"
    wind_mph = _extract_wind_mph(wind_txt)
    if wind_mph is not None and wind_mph >= 12:
        notes.append(f"Wind is notable ({wind_txt}); ball-flight variance is elevated for extra-base outcomes.")

    if away_pitcher_name != "TBD" and away_pitcher_era <= 3.3:
        notes.append(f"{away_pitcher_name} brings strong run prevention indicators (ERA {away_pitcher_era:.2f}) vs {home_abbr} lineup.")
    if home_pitcher_name != "TBD" and home_pitcher_era <= 3.3:
        notes.append(f"{home_pitcher_name} brings strong run prevention indicators (ERA {home_pitcher_era:.2f}) vs {away_abbr} lineup.")

    if favorite and favorite != "EVEN":
        notes.append(f"Model favorite is {favorite}; stack direction is stronger when that edge aligns with confirmed lineup quality.")

    if len(notes) < 3:
        notes.append("Focus on lineup confirmation near lock; late scratches can materially shift prop baselines.")
    if len(notes) < 3:
        notes.append("Target props where market line lags model mean by at least a half-step for cleaner edge capture.")

    return notes[:5]


def _claude_matchup_insights(context_payload):
    """Generate 3-5 plain-language matchup bullets via Claude."""
    try:
        import anthropic
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            return None, "ANTHROPIC_API_KEY not configured"

        client = anthropic.Anthropic(api_key=api_key)
        prompt = (
            "Generate 3 to 5 concise MLB matchup storyline bullets for today's game. "
            "Use only the provided context. Prioritize actionable prop-betting context. "
            "Output JSON only with key matchup_insights as an array of strings.\n\n"
            f"Context JSON:\n{json.dumps(context_payload, ensure_ascii=True)}"
        )

        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            temperature=0.4,
            system=(
                "You are an expert MLB betting analyst. "
                "Respond with raw JSON only, no markdown, no backticks."
            ),
            messages=[{"role": "user", "content": prompt}],
        )

        txt = (response.content[0].text or "").strip()
        clean = txt.lstrip("```json").lstrip("```").rstrip("```").strip()
        j0 = clean.find("{")
        j1 = clean.rfind("}") + 1
        if j0 < 0 or j1 <= j0:
            return None, "Unable to parse Claude storyline JSON"
        payload = json.loads(clean[j0:j1])
        items = payload.get("matchup_insights") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return None, "Claude response missing matchup_insights[]"
        out = [str(x).strip() for x in items if str(x).strip()]
        if not out:
            return None, "Claude storyline list empty"
        return out[:5], None
    except Exception as ex:
        return None, str(ex)


# ── Phase 3 Routes ────────────────────────────────────────────────────────────
@app.route("/api/game-projection/<int:game_pk>")
def api_game_projection(game_pk):
    _maybe_refresh_fg()
    _maybe_refresh_savant()
    try:
        gdata = fetch_schedule_game(game_pk)
        if not gdata:
            return jsonify({"success": False, "error": "Game not found"})
        away_t = gdata.get("teams",{}).get("away",{})
        home_t = gdata.get("teams",{}).get("home",{})
        ap = away_t.get("probablePitcher",{}); hp = home_t.get("probablePitcher",{})
        ap_n = ap.get("fullName","TBD"); hp_n = hp.get("fullName","TBD")
        hid = home_t.get("team",{}).get("id")
        pf = PARK_FACTORS.get(hid, 1.0)
        ap_mlb = pitcher_stats_mlb(ap.get("id")) if ap.get("id") else {}
        hp_mlb = pitcher_stats_mlb(hp.get("id")) if hp.get("id") else {}
        ap_fg = fg_pitcher(ap_n); hp_fg = fg_pitcher(hp_n)
        ap_sv = sv_pitcher(ap_n); hp_sv = sv_pitcher(hp_n)
        def best_era(sv, fg, mlb):
            # FIX: Require min 15 IP before trusting xERA (avoids April small-sample noise)
            xera = sv.get("sv_xera")
            ip = fg.get("fg_ip", 0) or 0
            if xera and float(ip or 0) >= 15:
                try:
                    f = float(xera)
                    if 0 < f < 12: return f
                except Exception: pass
            for v in [fg.get("fg_era"), mlb.get("era")]:
                try:
                    f = float(v)
                    if 0 < f < 12: return f
                except Exception: pass
            return 4.50
        def best_fip(fg, fallback):
            try:
                f = float(fg.get("fg_fip",0))
                if 0 < f < 12: return f
            except Exception: pass
            return fallback
        # home pitcher faces away lineup, away pitcher faces home lineup
        away_pit_era = best_era(hp_sv, hp_fg, hp_mlb)
        home_pit_era = best_era(ap_sv, ap_fg, ap_mlb)
        away_pit_fip = best_fip(hp_fg, away_pit_era)
        home_pit_fip = best_fip(ap_fg, home_pit_era)
        # Try to get lineup xwOBA
        try:
            r = requests.get(f"{MLB_API}/game/{game_pk}/boxscore", timeout=10)
            r.raise_for_status()
            box = r.json().get("teams",{})
            away_bats = get_batters_from_boxscore(box.get("away",{}), "away")
            home_bats = get_batters_from_boxscore(box.get("home",{}), "home")
        except Exception as ex:
            print(f'[api_game_projection] boxscore fetch failed for {game_pk}: {ex}')
            away_bats = []; home_bats = []
        def lineup_xwoba(bats):
            vals = []
            for b in bats:
                for k in ["sv_xwoba","fg_woba"]:
                    try:
                        f = float(b.get(k,0))
                        if 0.1 < f < 0.6: vals.append(f); break
                    except Exception: pass
                else:
                    vals.append(0.320)
            return round(sum(vals)/len(vals), 3) if vals else 0.320
        away_xwoba = lineup_xwoba(away_bats)
        home_xwoba = lineup_xwoba(home_bats)
        # Blended ERA: 60% xERA/ERA + 40% FIP
        away_blend = 0.6*away_pit_era + 0.4*away_pit_fip
        home_blend = 0.6*home_pit_era + 0.4*home_pit_fip
        # Base runs model (empirical: 4.5 R/G MLB avg)
        away_runs = 4.50 * (away_blend/4.50) * (away_xwoba/0.320) * pf  # FIX: corrected ERA direction (lower ERA = fewer runs allowed)
        home_runs = 4.50 * (home_blend/4.50) * (home_xwoba/0.320) * pf  # FIX: corrected ERA direction
        # Weather adjustment
        ven = gdata.get("venue", {})
        venue_id_wx = ven.get("id")
        vloc = ven.get("location", {}) or {}
        coords = vloc.get("defaultCoordinates", {}) or {}
        lat = coords.get("latitude")
        lon = coords.get("longitude")
        try:
            dt_utc_wx = datetime.fromisoformat(gdata.get("gameDate","").replace("Z","+00:00"))
            proj_hour = dt_utc_wx.astimezone(ET).hour
        except Exception:
            proj_hour = 13
        wx = get_weather(lat, lon, proj_hour, venue_id=venue_id_wx)
        # Fallback: use MLB schedule weather for projections when Open-Meteo is unavailable.
        if (wx.get("temp") in (None, "N/A") or wx.get("condition") in (None, "", "N/A")):
            raw_weather = gdata.get("weather", {}) or {}
            if raw_weather:
                print(f"[weather_fallback] using MLB weather for projection gamePk={game_pk} venue={venue_id_wx}")
                wx = {
                    "temp": raw_weather.get("temp", "N/A"),
                    "condition": raw_weather.get("condition", "N/A"),
                    "wind": raw_weather.get("wind", "N/A"),
                    "wind_speed": raw_weather.get("wind", "N/A"),
                    "wind_dir": "",
                    "rain_chance": raw_weather.get("precipitationChance", raw_weather.get("precipChance", "N/A")),
                }
        wx_adj = 0.0
        if not wx.get("dome"):
            try:
                t = float(wx.get("temp","70"))
                if t > 82: wx_adj = 0.20
                elif t > 76: wx_adj = 0.10
                elif t < 48: wx_adj = -0.20
                elif t < 56: wx_adj = -0.10
            except Exception: pass
        away_runs = round(away_runs + wx_adj, 1)
        home_runs = round(home_runs + wx_adj, 1)
        total = round(away_runs + home_runs, 1)
        run_env = "HIGH" if total > 9.5 else ("LOW" if total < 7.5 else "NEUTRAL")
        at_abbr = away_t.get("team",{}).get("abbreviation","AWAY")
        ht_abbr = home_t.get("team",{}).get("abbreviation","HOME")
        diff = abs(home_runs - away_runs)
        if diff > 0.7:
            fav = ht_abbr if home_runs > away_runs else at_abbr
        else:
            fav = "EVEN"

        # Build lightweight BvP context from top lineup spots when available.
        bvp_highlights = []
        try:
            def _bvp_lineup_highlights(lineup, opp_pitcher_id, opp_pitcher_name):
                rows = []
                if not opp_pitcher_id:
                    return rows
                for b in (lineup or [])[:6]:
                    bid = b.get("id")
                    if not bid:
                        continue
                    bp = _fetch_bvp(bid, opp_pitcher_id)
                    if not bp or not bp.get("success"):
                        continue
                    pa = int(bp.get("pa", 0) or 0)
                    avg = bp.get("avg")
                    hr = int(bp.get("hr", 0) or 0)
                    if pa < 6 or avg is None:
                        continue
                    rows.append(
                        f"{b.get('name','Hitter')} vs {opp_pitcher_name}: {pa} PA, AVG {avg:.3f}, HR {hr}"
                    )
                    if len(rows) >= 2:
                        break
                return rows

            bvp_highlights.extend(_bvp_lineup_highlights(away_bats, hp.get("id"), hp_n))
            bvp_highlights.extend(_bvp_lineup_highlights(home_bats, ap.get("id"), ap_n))
        except Exception:
            bvp_highlights = []

        story_context = {
            "game_pk": game_pk,
            "away_team": at_abbr,
            "home_team": ht_abbr,
            "favorite": fav,
            "projected_total_runs": total,
            "run_environment": run_env,
            "weather": {
                "temp": wx.get("temp", "N/A"),
                "condition": wx.get("condition", "N/A"),
                "wind": wx.get("wind", "N/A"),
                "wind_dir": wx.get("wind_dir", ""),
            },
            "series": {
                "game": gdata.get("seriesGame"),
                "total": gdata.get("seriesTotal"),
            },
            "umpire_tendency": "Unknown",
            "pitcher_form": {
                "away_pitcher": {
                    "name": ap_n,
                    "era": round(home_pit_era, 2),
                    "fip": round(home_pit_fip, 2),
                },
                "home_pitcher": {
                    "name": hp_n,
                    "era": round(away_pit_era, 2),
                    "fip": round(away_pit_fip, 2),
                },
            },
            "bvp_highlights": bvp_highlights,
            "lineup_xwoba": {
                "away": away_xwoba,
                "home": home_xwoba,
            },
        }

        matchup_insights, storyline_source = _claude_matchup_insights(story_context)
        if not matchup_insights:
            matchup_insights = _fallback_matchup_insights(
                gdata,
                at_abbr,
                ht_abbr,
                total,
                run_env,
                fav,
                ap_n,
                hp_n,
                home_pit_era,
                away_pit_era,
                wx,
            )
            storyline_source = f"fallback: {storyline_source or 'claude unavailable'}"

        return jsonify({
            "success": True,
            "awayAbbr": at_abbr, "homeAbbr": ht_abbr,
            "awayRuns": away_runs, "homeRuns": home_runs,
            "total": total, "runEnv": run_env, "favorite": fav,
            "awayXwoba": away_xwoba, "homeXwoba": home_xwoba,
            "awayPitcherEra": round(away_pit_era,2),
            "homePitcherEra": round(home_pit_era,2),
            "awayPitcherFip": round(away_pit_fip,2),
            "homePitcherFip": round(home_pit_fip,2),
            "parkFactor": pf, "wxAdj": wx_adj,
            "matchup_insights": matchup_insights[:5],
            "storylineSource": storyline_source,
        })
    except Exception as ex:
        print("[api_game_projection]", traceback.format_exc())
        return jsonify({"success":False,"error":str(ex)}), 500


def _compute_form(logs, is_pitcher):
    """Compute rolling form windows from game log entries (newest-last order)."""
    def _hitter_window(games):
        if not games:
            return None
        ab = h = hr = bb = k = tb = 0
        pa = 0
        for g in games:
            ab += g.get("ab", 0)
            h  += g.get("h", 0)
            hr += g.get("hr", 0)
            bb += g.get("bb", 0)
            k  += g.get("k", 0)
            tb += g.get("tb", 0)
            pa += g.get("ab", 0) + g.get("bb", 0)
        avg  = round(h / ab, 3) if ab else None
        obp  = round((h + bb) / pa, 3) if pa else None
        slg  = round(tb / ab, 3) if ab else None
        ops  = round((obp or 0) + (slg or 0), 3) if (obp and slg) else None
        iso  = round((slg or 0) - (avg or 0), 3) if (slg and avg) else None
        kpct = round(k / pa, 4) if pa else None
        return {
            "games": len(games), "ab": ab, "hr": hr,
            "avg":  f".{int(round((avg or 0)*1000)):03d}" if avg is not None else "---",
            "obp":  f".{int(round((obp or 0)*1000)):03d}" if obp is not None else "---",
            "slg":  f".{int(round((slg or 0)*1000)):03d}" if slg is not None else "---",
            "ops":  f".{int(round((ops or 0)*1000)):03d}" if ops is not None else "---",
            "iso":  f".{int(round((iso or 0)*1000)):03d}" if iso is not None else "---",
            "kpct": round(kpct, 4) if kpct is not None else None,
        }

    def _pitcher_window(games):
        if not games:
            return None
        ip_total = 0.0; er = 0; pk = 0; bb = 0; qs = 0
        for g in games:
            ip_val = float(g.get("ip", 0) or 0)
            ip_total += ip_val
            er  += g.get("er", 0)
            pk  += g.get("k", 0)
            bb  += g.get("bb", 0)
            if ip_val >= 6 and g.get("er", 0) <= 3:
                qs += 1
        era  = round(er / ip_total * 9, 2) if ip_total else None
        k9   = round(pk / ip_total * 9, 2) if ip_total else None
        bb9  = round(bb / ip_total * 9, 2) if ip_total else None
        whip = None  # hits not stored in current game log schema
        return {
            "games": len(games), "ip": round(ip_total, 1), "qs": qs,
            "era":  era, "whip": whip, "k9": k9, "bb9": bb9,
        }

    recent = list(reversed(logs))  # newest first
    if not is_pitcher:
        l7  = _hitter_window(recent[:7])
        l14 = _hitter_window(recent[:14])
        l30 = _hitter_window(recent[:30])
        # Flag based on L7 OPS / AVG
        flag = "neutral"; flag_note = ""; trend = "stable"
        if l7:
            avg7 = float(l7["avg"].replace(".", "0.")) if l7["avg"] != "---" else None
            if avg7 is not None:
                if avg7 >= 0.350:   flag = "hot";     flag_note = f"Batting {l7['avg']} over last 7 games"
                elif avg7 >= 0.280: flag = "warm";    flag_note = f"Solid {l7['avg']} over last 7 games"
                elif avg7 < 0.150:  flag = "cold";    flag_note = f"Struggling at {l7['avg']} over last 7 games"
                elif avg7 < 0.210:  flag = "chilly";  flag_note = f"Below average {l7['avg']} over last 7 games"
            if l14:
                avg14 = float(l14["avg"].replace(".", "0.")) if l14["avg"] != "---" else None
                if avg7 is not None and avg14 is not None:
                    if avg7 > avg14 + 0.040:   trend = "improving"
                    elif avg7 < avg14 - 0.040: trend = "declining"
        return {"kind": "hitter", "flag": flag, "flag_note": flag_note, "trend": trend,
                "l7": l7, "l14": l14, "l30": l30}
    else:
        l3  = _pitcher_window(recent[:3])
        l5  = _pitcher_window(recent[:5])
        l10 = _pitcher_window(recent[:10])
        flag = "neutral"; flag_note = ""; trend = "stable"
        if l3 and l3["era"] is not None:
            era3 = l3["era"]
            if era3 < 2.0:   flag = "dealing";     flag_note = f"{era3:.2f} ERA over last 3 starts"
            elif era3 < 3.5: flag = "hot";         flag_note = f"{era3:.2f} ERA over last 3 starts"
            elif era3 < 4.5: flag = "neutral";     flag_note = f"{era3:.2f} ERA over last 3 starts"
            elif era3 < 6.0: flag = "chilly";      flag_note = f"{era3:.2f} ERA over last 3 starts"
            else:            flag = "struggling";  flag_note = f"{era3:.2f} ERA over last 3 starts"
            if l5 and l5["era"] is not None:
                if era3 < l5["era"] - 0.75:    trend = "improving"
                elif era3 > l5["era"] + 0.75:  trend = "declining"
        return {"kind": "pitcher", "flag": flag, "flag_note": flag_note, "trend": trend,
                "l3": l3, "l5": l5, "l10": l10}


def _build_ai_lines(name, is_pitcher, season, fg, sv, logs):
    """Generate 3-5 plain-text AI scouting sentences from cached + live stats."""
    lines = []
    try:
        first = name.split()[0] if name else "Player"
        if not is_pitcher:
            avg  = _safe_f(fg.get("fg_avg")     or season.get("avg"),  0.0)
            woba = _safe_f(fg.get("fg_woba"),  0.0)
            wrc  = int(_safe_f(fg.get("fg_wrc"), 0))
            xba  = _safe_f(sv.get("sv_xba"),   0.0)
            ev   = _safe_f(sv.get("sv_ev"),    0.0)
            brl  = _safe_f(sv.get("sv_brl_pct"), 0.0)
            hh   = _safe_f(sv.get("sv_hh_pct"),  0.0)
            r_ab = sum(g.get("ab", 0) for g in logs[-7:])
            r_h  = sum(g.get("h",  0) for g in logs[-7:])
            r_avg = round(r_h / r_ab, 3) if r_ab > 0 else None
            if avg > 0:
                tier = "elite" if avg >= 0.310 else "above-avg" if avg >= 0.280 else "below-avg"
                lines.append(
                    f"Batting {avg:.3f} this season ({tier}). wOBA {woba:.3f} ranks in the "
                    f"{'top third' if woba >= 0.340 else 'middle third' if woba >= 0.310 else 'bottom third'} of the league."
                )
            if wrc > 0:
                desc = "elite (well above avg)" if wrc >= 130 else "above avg" if wrc >= 110 else "roughly avg" if wrc >= 95 else "below avg"
                lines.append(f"wRC+ of {wrc} — {first} is {desc} in total offensive production (100 = league avg).")
            if ev > 0:
                quality = "Elite contact — target power props." if ev >= 91 and brl >= 8 else "Solid contact." if ev >= 88 else "Soft contact profile — fade power markets."
                lines.append(f"Avg exit velocity {ev} mph · Barrel% {brl:.1f} · Hard Hit% {hh:.1f}. {quality}")
            if xba > 0 and avg > 0:
                diff = round(xba - avg, 3)
                if abs(diff) >= 0.015:
                    direction = "unlucky — regression upward likely" if diff > 0 else "running hot — may regress"
                    lines.append(f"xBA ({xba:.3f}) vs AVG ({avg:.3f}), gap of {diff:+.3f} — {first} appears {direction}.")
            if r_avg is not None and r_ab >= 10:
                note = "Hot streak — elevated prop target." if r_avg >= 0.300 else "Cold stretch — consider fading hit props." if r_avg < 0.160 else "Normal recent form."
                lines.append(f"Last 7 games: {r_h}-for-{r_ab} ({r_avg:.3f}). {note}")
        else:
            era  = _safe_f(fg.get("fg_era")  or season.get("era"),  0.0)
            fip  = _safe_f(fg.get("fg_fip"),  0.0)
            kpct = _safe_f(fg.get("fg_kpct"), 0.0)
            xera = _safe_f(sv.get("sv_xera"), 0.0)
            if era > 0:
                fip_note = f"FIP {fip:.2f} signals sustainable success." if fip <= era else f"FIP {fip:.2f} suggests ERA may climb."
                lines.append(f"ERA of {era:.2f} this season. {fip_note}")
            if xera > 0:
                if era < xera - 0.30:
                    lines.append(f"xERA {xera:.2f} — outperforming expected metrics, regression risk.")
                elif era > xera + 0.30:
                    lines.append(f"xERA {xera:.2f} — underperforming expected metrics, positive regression candidate.")
                else:
                    lines.append(f"xERA {xera:.2f} aligns closely with ERA — sustainable performance.")
            if kpct > 0:
                k_desc = "elite strikeout arm — target K prop overs" if kpct >= 0.27 else "above-avg swing-and-miss" if kpct >= 0.22 else "below-avg K rate — fade K overs"
                lines.append(f"K% of {kpct*100:.1f}% — {k_desc}.")
            r_starts = [g for g in logs[-5:] if float(g.get("ip", 0) or 0) >= 3.0]
            if r_starts:
                r_k = sum(g.get("k", 0) for g in r_starts)
                lines.append(f"Last {len(r_starts)} starts: {r_k} total Ks ({round(r_k / len(r_starts), 1)} K/start avg).")
    except Exception:
        pass
    if not lines:
        lines.append(f"Insufficient data for a full AI report on {name} this early in the season.")
    return lines


@app.route("/api/player/<int:player_id>")
def api_player_profile(player_id):
    """Full player profile: identity + season stats + FG/Savant cache + game log + platoon + AI."""
    _maybe_refresh_fg()
    _maybe_refresh_savant()
    include_form = request.args.get("includeForm") == "1"
    try:
        year = datetime.now().year

        # 1. Player identity + season stats
        pr = requests.get(
            f"{MLB_API}/people/{player_id}",
            params={"hydrate": f"stats(group=[hitting,pitching],type=season,season={year}),currentTeam"},
            timeout=10
        )
        pr.raise_for_status()
        people = pr.json().get("people", [])
        if not people:
            return jsonify({"success": False, "error": "Player not found"}), 404
        p = people[0]

        name       = p.get("fullName", "Unknown")
        pos_code   = p.get("primaryPosition", {}).get("abbreviation", "?")
        team_obj   = (p.get("currentTeam") or {})
        team_abbr  = team_obj.get("abbreviation", "?")
        team_id    = team_obj.get("id")
        throws     = (p.get("pitchHand")   or {}).get("code", "?")
        bats_side  = (p.get("batSide")     or {}).get("code", "?")
        is_pitcher = pos_code in ("P", "SP", "RP", "CP")

        season = {}
        for sg in p.get("stats", []):
            grp    = (sg.get("group") or {}).get("displayName", "")
            splits = sg.get("splits", [])
            if splits:
                s = splits[0].get("stat", {})
                if grp == "hitting" and not is_pitcher:
                    season = {
                        "avg": s.get("avg"), "obp": s.get("obp"),
                        "slg": s.get("slg"), "ops": s.get("ops"),
                        "homeRuns": s.get("homeRuns", 0), "rbi": s.get("rbi", 0),
                        "runs": s.get("runs", 0), "stolenBases": s.get("stolenBases", 0),
                    }
                elif grp == "pitching" and is_pitcher:
                    season = {
                        "era": s.get("era"), "whip": s.get("whip"),
                        "inningsPitched": s.get("inningsPitched"),
                        "wins": s.get("wins", 0), "losses": s.get("losses", 0),
                    }

        # 2. Cached FG + Savant — zero extra HTTP calls
        fgr = (fg_pitcher(name) if is_pitcher else fg_batter(name)) or {}
        svr = (sv_pitcher(name) if is_pitcher else sv_batter(name)) or {}
        # Keep original keys (e.g. fg_avg, sv_arsenal_pct) so JS can access them directly
        fg_out = dict(fgr)
        sv_out = dict(svr)

        # 3. Game log — last 10 games
        group     = "pitching" if is_pitcher else "hitting"
        game_logs = []
        try:
            lr = requests.get(
                f"{MLB_API}/people/{player_id}/stats",
                params={"stats": "gameLog", "group": group, "season": year},
                timeout=8
            )
            if lr.ok:
                for sp in (lr.json().get("stats", [{}])[0].get("splits", []))[-30 if include_form else -10:]:
                    s   = sp.get("stat", {})
                    opp = (sp.get("opponent") or {}).get("abbreviation", "?")
                    dt  = sp.get("date", "")[:10]
                    if not is_pitcher:
                        game_logs.append({
                            "date": dt, "opp": opp,
                            "ab":  s.get("atBats", 0),     "h":   s.get("hits", 0),
                            "hr":  s.get("homeRuns", 0),   "rbi": s.get("rbi", 0),
                            "k":   s.get("strikeOuts", 0), "bb":  s.get("baseOnBalls", 0),
                            "tb":  s.get("totalBases", 0), "avg": s.get("avg", "---"),
                        })
                    else:
                        game_logs.append({
                            "date": dt, "opp": opp,
                            "ip":  s.get("inningsPitched", "0"), "h":  s.get("hits", 0),
                            "er":  s.get("earnedRuns", 0),
                            "k":   s.get("strikeOuts", 0),       "bb": s.get("baseOnBalls", 0),
                            "era": s.get("era", "---"),
                        })
        except Exception:
            pass

        # 4. Platoon splits vs LHP / RHP
        platoon = {}
        try:
            plr = requests.get(
                f"{MLB_API}/people/{player_id}/stats",
                params={"stats": "statSplits", "group": group, "season": year, "sitCodes": "vl,vr"},
                timeout=8
            )
            if plr.ok:
                for sp in plr.json().get("stats", [{}])[0].get("splits", []):
                    code = (sp.get("split") or {}).get("code", "")
                    s    = sp.get("stat", {})
                    if code in ("vl", "vr"):
                        platoon[code] = {
                            "avg":  s.get("avg",  "---"), "obp": s.get("obp", "---"),
                            "slg":  s.get("slg",  "---"), "ops": s.get("ops", "---"),
                            "pa":   s.get("plateAppearances", 0),
                            "hr":   s.get("homeRuns", 0),
                            "woba": s.get("woba", "---"),
                        }
        except Exception:
            pass

        # 5. AI Scout lines
        ai_lines = _build_ai_lines(name, is_pitcher, season, fgr, svr, game_logs)

        # 6. Form — use the richer _fetch_rolling_form (cached, wOBA-based) when requested
        form = _fetch_rolling_form(player_id, is_pitcher) if include_form else None

        opp_pitcher = None
        try:
            today_raw = fetch_schedule(datetime.now(ET).strftime("%Y-%m-%d"))
            for g in today_raw or []:
                teams = g.get("teams", {})
                away = teams.get("away", {})
                home = teams.get("home", {})
                away_id = (away.get("team") or {}).get("id")
                home_id = (home.get("team") or {}).get("id")
                if team_id not in (away_id, home_id):
                    continue
                if team_id == away_id:
                    opp = home
                else:
                    opp = away
                opp_team = opp.get("team", {})
                opp_pp = opp.get("probablePitcher") or {}
                opp_pitcher = {
                    "id": opp_pp.get("id"),
                    "name": opp_pp.get("fullName", "TBD"),
                    "team": opp_team.get("abbreviation", "?"),
                    "gamePk": g.get("gamePk"),
                }
                break
        except Exception:
            opp_pitcher = None

        return jsonify({
            "success":   True,
            "id":        player_id,
            "name":      name,
            "pos":       pos_code,
            "team":      team_abbr,
            "isPitcher": is_pitcher,
            "throws":    throws,
            "bats":      bats_side,
            "season":    season,
            "fg":        fg_out,
            "sv":        sv_out,
            "gameLogs":  game_logs,
            "platoon":   platoon,
            "aiLines":   ai_lines,
            "form":      form,
            "oppPitcher": opp_pitcher,
        })
        
    except Exception as ex:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(ex)}), 500


@app.route("/api/bvp/<int:batter_id>/<int:pitcher_id>")
def api_bvp(batter_id, pitcher_id):
    """Batter-vs-Pitcher grade and sample detail for props surfaces."""
    result = _fetch_bvp(batter_id, pitcher_id)
    if not result or not result.get("success"):
        return jsonify({
            "success": False,
            "grade": "D",
            "ops": None,
            "avg": None,
            "hr": 0,
            "pa": 0,
            "sample_note": "No BvP data available",
        })
    return jsonify({
        "success": True,
        "grade": result.get("grade", "D"),
        "grade_basis": result.get("grade_basis", "h2h"),
        "pitcher_hand": result.get("pitcher_hand"),
        "platoon_ops": result.get("platoon_ops"),
        "ops": result.get("ops"),
        "avg": result.get("avg"),
        "hr": result.get("hr", 0),
        "pa": result.get("pa", 0),
        "ops_ratio": result.get("ops_ratio"),
        "sample_note": result.get("sample_note") or result.get("note") or "",
        "tooltip": result.get("tooltip") or "",
    })


@app.route("/api/bvp/<int:batter_id>/<int:pitcher_id>/projection")
def api_bvp_projection(batter_id, pitcher_id):
    """
    Full BATX matchup projection + xStats for the BvP detail panel.
    Returns projected game probs (hit1/tb2/hr/rbi1), expected values,
    Statcast quality metrics (xBA, xSLG, xwOBA, EV, barrel%, HH%),
    and pitcher resistance profile.
    Query params: game_pk (int) — enriches park factor + dome flag.
                  slot (int, 1-9) — batter's lineup slot, defaults to 5.
    """
    game_pk     = request.args.get("game_pk",  type=int)
    lineup_slot = request.args.get("slot",     type=int, default=5)

    try:
        _wait_for_fg_data(timeout_sec=3)
        _wait_for_savant_data(timeout_sec=3)

        # ── 1. Resolve player names + handedness ────────────────────────────
        batter_name  = pitcher_name = ""
        bats_code    = "S"
        pitcher_hand = "R"

        with ThreadPoolExecutor(max_workers=2) as ex:
            b_fut = ex.submit(requests.get, f"{MLB_API}/people/{batter_id}",  timeout=6)
            p_fut = ex.submit(requests.get, f"{MLB_API}/people/{pitcher_id}", timeout=6)
            br = b_fut.result(timeout=8)
            pr = p_fut.result(timeout=8)

        if br.ok:
            bp = (br.json().get("people") or [{}])[0]
            batter_name = bp.get("fullName", "")
            bats_code   = (bp.get("batSide")  or {}).get("code", "S") or "S"
        if pr.ok:
            pp = (pr.json().get("people") or [{}])[0]
            pitcher_name = pp.get("fullName", "")
            pitcher_hand = (pp.get("pitchHand") or {}).get("code", "R") or "R"

        # ── 2. Load FG + Savant stats ────────────────────────────────────────
        fg_bat = fg_batter(batter_name)  or {}
        sv_bat = sv_batter(batter_name)  or {}
        fg_pit = fg_pitcher(pitcher_name) or {}
        sv_pit = sv_pitcher(pitcher_name) or {}

        # Tier-2 fallback: direct per-player Savant CSV when bulk cache misses
        _sv_key_fields = ("sv_xba", "sv_ev", "sv_brl_pct")
        if not any(_safe_f(sv_bat.get(k), None) for k in _sv_key_fields):
            _direct = _fetch_savant_player_batter(batter_id, datetime.now().year)
            if _direct:
                sv_bat = dict(sv_bat)
                sv_bat.update({k: v for k, v in _direct.items() if v not in (None, "N/A")})

        # ── 3. Build batter object with platoon splits ───────────────────────
        splits = hitter_split_profile(batter_id)
        vl = splits.get("vl", {})
        vr = splits.get("vr", {})

        batter_obj = {
            "name":     batter_name,
            "bats":     bats_code,
            "slot":     lineup_slot,
            "avg":      _safe_f(fg_bat.get("fg_avg"),  0.245),
            "obp":      _safe_f(fg_bat.get("fg_obp"),  0.315),
            "slg":      _safe_f(fg_bat.get("fg_slg"),  0.390),
            "ops":      _safe_f(fg_bat.get("fg_ops"),  0.705),
            "vs_l_avg": vl.get("avg", 0), "vs_l_ops": vl.get("ops", 0),
            "vs_l_obp": vl.get("obp", 0), "vs_l_slg": vl.get("slg", 0),
            "vs_l_pa":  vl.get("pa",  0),
            "vs_r_avg": vr.get("avg", 0), "vs_r_ops": vr.get("ops", 0),
            "vs_r_obp": vr.get("obp", 0), "vs_r_slg": vr.get("slg", 0),
            "vs_r_pa":  vr.get("pa",  0),
        }

        # ── 4. Game context: park factor + dome ──────────────────────────────
        park_factor = 1.0
        weather     = {}
        if game_pk:
            try:
                sr = requests.get(
                    f"{MLB_API}/schedule",
                    params={"gamePk": game_pk, "hydrate": "team,venue", "sportId": 1},
                    timeout=6,
                )
                if sr.ok:
                    dates = sr.json().get("dates", [])
                    if dates and dates[0].get("games"):
                        gm      = dates[0]["games"][0]
                        home_id = (((gm.get("teams") or {}).get("home") or {}).get("team") or {}).get("id")
                        if home_id:
                            park_factor = PARK_FACTORS.get(home_id, 1.0)
                        venue_id = (gm.get("venue") or {}).get("id")
                        if venue_id and venue_id in DOME_VENUES:
                            weather = {"dome": True, "temp": 72, "wind_speed": 0}
            except Exception:
                pass

        # ── 5. BvP component (already cached) ───────────────────────────────
        bvp_data = _fetch_bvp(batter_id, pitcher_id)

        # ── 5b. Rolling form for batter (daily-cached) ──────────────────────
        batter_form = _fetch_rolling_form(batter_id, False)

        # ── 6. BATX projection (most complete model) ─────────────────────────
        batx = _project_batter_batx(
            batter_obj, pitcher_name, fg_pit, sv_pit,
            park_factor, weather, pitcher_hand,
            opp_pitcher_id=pitcher_id, bvp=bvp_data, form=batter_form,
        )

        # ── 7. Calibrated game probs via _project_batter_vs_pitcher ─────────
        bstats = {
            "sv_xba":    _safe_f(sv_bat.get("sv_xba"),     0.250),
            "sv_xwoba":  _safe_f(sv_bat.get("sv_xwoba"),   0.310),
            "fg_iso":    _safe_f(fg_bat.get("fg_iso"),     0.145),
            "sv_brl_pct":_safe_f(sv_bat.get("sv_brl_pct"), 6.0),
            "fg_pa":     _safe_f(fg_bat.get("fg_pa"),      200),
            "fg_avg":    _safe_f(fg_bat.get("fg_avg"),     0.245),
            "fg_woba":   _safe_f(fg_bat.get("fg_woba"),    0.310),
        }
        pstats = {
            "fg_fip":  _safe_f(fg_pit.get("fg_xfip") or fg_pit.get("fg_era"), 4.20),
            "fg_hr9":  _safe_f(fg_pit.get("fg_hr9"),  1.10),
            "fg_kpct": _safe_f(fg_pit.get("fg_kpct"), 0.22),
        }
        gp = _project_batter_vs_pitcher(bstats, pstats)

        # BATX expected → Poisson probs; blend with calibrated probs (60/40)
        hits_m = float(batx.get("hits", 0) or 0)
        tb_m   = float(batx.get("tb",   0) or 0)
        hr_m   = float(batx.get("hr",   0) or 0)
        rbi_m  = float(batx.get("rbi",  0) or 0)
        r_m    = float(batx.get("r",    0) or 0)

        def _blend_prob(cal, batx_p):
            return round(max(0.01, min(0.99, 0.60 * cal + 0.40 * batx_p)), 3)

        probs = {
            "hit1": _blend_prob(gp["hitProb"], _poisson_over_prob(hits_m, 0.5)),
            "tb2":  _blend_prob(gp["tbProb"],  _poisson_over_prob(tb_m,   1.5)),
            "hr":   _blend_prob(gp["hrProb"],  _poisson_over_prob(hr_m,   0.5)),
            "rbi1": _blend_prob(gp["rbiProb"], _poisson_over_prob(rbi_m,  0.5)),
            "r1":   round(_poisson_over_prob(r_m, 0.5), 3),
            "xgbHitProb": gp.get("xgbHitProb"),
        }

        # ── 8. xStats + Statcast quality metrics ─────────────────────────────
        sv_xba   = _safe_f(sv_bat.get("sv_xba"),    None)
        sv_xslg  = _safe_f(sv_bat.get("sv_xslg"),   None)
        sv_xwoba = _safe_f(sv_bat.get("sv_xwoba"),  None)
        sv_ev    = _safe_f(sv_bat.get("sv_ev"),      None)
        sv_brl   = _safe_f(sv_bat.get("sv_brl_pct"),None)
        sv_hh    = _safe_f(sv_bat.get("sv_hh_pct"), None)

        # Percentile ranks across all Savant batters
        with _sv_lock:
            _sc = dict(_sv_bat_statcast)
            _xs = dict(_sv_bat_xstats)

        def _pct(cache, field, val):
            if val is None:
                return None
            vals = [_safe_f(v.get(field), None) for v in cache.values()]
            vals = [x for x in vals if x is not None and x > 0]
            return _pct_rank(vals, val)

        # FanGraphs tier-3 fallback (always available from MLB API bulk cache)
        fg_woba  = _safe_f(fg_bat.get("fg_woba"),  None)
        fg_iso   = _safe_f(fg_bat.get("fg_iso"),   None)
        fg_kpct  = _safe_f(fg_bat.get("fg_kpct"),  None)
        fg_bbpct = _safe_f(fg_bat.get("fg_bbpct"), None)
        fg_ops   = _safe_f(fg_bat.get("fg_ops"),   None)

        _has_savant = sv_ev is not None or sv_xba is not None
        batter_quality = {
            # Savant metrics (may be None if unavailable even after direct fetch)
            "xba":      round(sv_xba,  3) if sv_xba  is not None else None,
            "xslg":     round(sv_xslg, 3) if sv_xslg is not None else None,
            "xwoba":    round(sv_xwoba,3) if sv_xwoba is not None else None,
            "ev":       round(sv_ev,   1) if sv_ev    is not None else None,
            "brl_pct":  round(sv_brl,  1) if sv_brl   is not None else None,
            "hh_pct":   round(sv_hh,   1) if sv_hh    is not None else None,
            "ev_pct":       _pct(_sc, "sv_ev",      sv_ev),
            "brl_pct_rank": _pct(_sc, "sv_brl_pct", sv_brl),
            "hh_pct_rank":  _pct(_sc, "sv_hh_pct",  sv_hh),
            "xba_pct":      _pct(_xs, "sv_xba",     sv_xba),
            "xwoba_pct":    _pct(_xs, "sv_xwoba",   sv_xwoba),
            # FanGraphs metrics (always present as fallback)
            "fg_woba":  round(fg_woba,  3) if fg_woba  is not None else None,
            "fg_iso":   round(fg_iso,   3) if fg_iso   is not None else None,
            "fg_kpct":  round(fg_kpct,  3) if fg_kpct  is not None else None,
            "fg_bbpct": round(fg_bbpct, 3) if fg_bbpct is not None else None,
            "fg_ops":   round(fg_ops,   3) if fg_ops   is not None else None,
            "source":   "savant" if _has_savant else "fg",
        }

        pitcher_profile = {
            "xera":      _safe_f(sv_pit.get("sv_xera"),    None),
            "xwoba_against": _safe_f(sv_pit.get("sv_xwoba_p"), None),
            "whiff_pct": _safe_f(sv_pit.get("sv_whiff"),   None),
            "k_pct":     _safe_f(sv_pit.get("sv_k_pct"),   None),
            "bb_pct":    _safe_f(sv_pit.get("sv_bb_pct"),  None),
            "era":       _safe_f(fg_pit.get("fg_era"),      None),
            "k9":        _safe_f(fg_pit.get("fg_k9"),       None),
        }

        return jsonify({
            "success":      True,
            "batterName":   batter_name,
            "pitcherName":  pitcher_name,
            "pitcherHand":  pitcher_hand,
            "parkFactor":   park_factor,
            "projectedProbs": probs,
            "expectedValues": {
                "hits": round(hits_m, 3),
                "tb":   round(tb_m,   3),
                "hr":   round(hr_m,   4),
                "rbi":  round(rbi_m,  3),
                "r":    round(r_m,    3),
            },
            "matchupContext": {
                "platoonNote": batx.get("platoon_note", ""),
                "splitOps":    round(float(batx.get("split_ops") or 0), 3),
                "splitAvg":    round(float(batx.get("split_avg") or 0), 3),
                "expPA":       batx.get("expected_pa", 4.0),
                "bvpGrade":    (bvp_data or {}).get("grade", "?"),
                "bvpPA":       (bvp_data or {}).get("pa", 0),
            },
            "batterQuality":  batter_quality,
            "pitcherProfile": pitcher_profile,
            "adjustments":    batx.get("adjustments", {}),
        })

    except Exception as ex:
        print(f"[api_bvp_projection] {traceback.format_exc()}")
        # Partial fallback: return FG-based quality metrics so the card never shows "Stats loading…"
        try:
            _fb = fg_batter(batter_name) if batter_name else {}
            _fallback_quality = {
                "fg_woba":  round(_safe_f(_fb.get("fg_woba"), None), 3) if _safe_f(_fb.get("fg_woba"), None) is not None else None,
                "fg_iso":   round(_safe_f(_fb.get("fg_iso"),  None), 3) if _safe_f(_fb.get("fg_iso"),  None) is not None else None,
                "fg_kpct":  round(_safe_f(_fb.get("fg_kpct"), None), 3) if _safe_f(_fb.get("fg_kpct"), None) is not None else None,
                "fg_bbpct": round(_safe_f(_fb.get("fg_bbpct"),None), 3) if _safe_f(_fb.get("fg_bbpct"),None) is not None else None,
                "fg_ops":   round(_safe_f(_fb.get("fg_ops"),  None), 3) if _safe_f(_fb.get("fg_ops"),  None) is not None else None,
                "source": "fg",
            }
            return jsonify({"success": False, "error": str(ex), "batterQuality": _fallback_quality})
        except Exception:
            return jsonify({"success": False, "error": str(ex)}), 500


@app.route("/api/bvp/<int:batter_id>/<int:pitcher_id>/arsenal")
def api_bvp_arsenal(batter_id, pitcher_id):
    """Pitch arsenal breakdown for batter vs pitcher matchup."""
    try:
        _maybe_refresh_fg()
        _maybe_refresh_savant()

        # Resolve pitcher name + hand
        pitcher_name = ""
        pitcher_hand = "R"
        try:
            rp = requests.get(f"{MLB_API}/people/{pitcher_id}", timeout=8)
            if rp.ok:
                ppl = rp.json().get("people") or [{}]
                pitcher_name = (ppl[0].get("fullName") or "").strip()
                pitcher_hand = ((ppl[0].get("pitchHand") or {}).get("code") or "R").upper()
        except Exception:
            pass

        pit_pid_str = str(pitcher_id)
        bat_pid_str = str(batter_id)

        # Pitch-mix matchup score + per-pitch table
        mix_score, table_rows = _compute_pitch_mix_score(pit_pid_str, bat_pid_str)

        # Pitcher's raw arsenal pct + velo from Savant
        svp = sv_pitcher(pitcher_name) if pitcher_name else {}
        raw_pct  = (svp.get("sv_arsenal_pct")  or {}) if isinstance(svp, dict) else {}
        raw_velo = (svp.get("sv_arsenal_velo") or {}) if isinstance(svp, dict) else {}

        # Build enriched pitch list (merge table_rows with pct/velo)
        pitches = []
        for row in table_rows:
            code = (row.get("pitch") or "").lower()
            label = PITCH_LABELS.get(code, code.upper())
            pct  = raw_pct.get(code)
            velo = raw_velo.get(code)
            # _sv_arsenal_pct already stores % values (e.g. 35.0 = 35%); fall back to pitch_usage from table_rows
            usage_pct = round(float(pct), 1) if pct is not None else row.get("usage")
            pitches.append({
                "code":       code,
                "label":      label,
                "usage":      usage_pct,
                "velo":       round(float(velo), 1) if velo is not None else None,
                "pit_ba":     row.get("pit_ba"),
                "pit_slg":    row.get("pit_slg"),
                "pit_woba":   row.get("pit_woba"),
                "pit_whiff":  row.get("pit_whiff"),
                "bat_slg":    row.get("bat_slg"),
                "bat_woba":   row.get("bat_woba"),
                "bat_whiff":  row.get("bat_whiff"),
                "bat_hh":     row.get("bat_hh"),
                "ratio":      row.get("ratio"),
            })

        # If no pit_arsenal data, fall back to pct-only list (no batter breakdown)
        if not pitches and raw_pct:
            for code, pct_val in sorted(raw_pct.items(), key=lambda kv: kv[1], reverse=True):
                label = PITCH_LABELS.get(code, code.upper())
                velo  = raw_velo.get(code)
                pitches.append({
                    "code":    code,
                    "label":   label,
                    "usage":   round(float(pct_val), 1) if pct_val is not None else None,
                    "velo":    round(float(velo), 1) if velo is not None else None,
                })

        primary_pitch = pitches[0]["label"] if pitches else None

        return jsonify({
            "success":      True,
            "pitcherName":  pitcher_name,
            "pitcherHand":  pitcher_hand,
            "mixScore":     mix_score,
            "primaryPitch": primary_pitch,
            "pitches":      pitches,
        })

    except Exception as ex:
        import traceback as _tb
        print(f"[api_bvp_arsenal] {_tb.format_exc()}")
        return jsonify({"success": False, "error": str(ex)}), 500


@app.route("/api/player/<int:player_id>/bvp/<int:pitcher_id>")
def api_player_bvp_games(player_id, pitcher_id):
    """Opponent-specific game logs: summary + latest matchup games."""
    try:
        bvp = _fetch_bvp(player_id, pitcher_id)
        if not bvp or not bvp.get("success"):
            return jsonify({"success": False, "error": "No BvP data available"}), 404

        year = datetime.now().year
        games = []
        score_cache = {}

        def _game_info(game_pk, batter_team_id=None):
            """Return (score_txt, win_loss, opp_pitcher_name, opp_team_abbr) from boxscore."""
            if not game_pk:
                return "—", "", "—", ""
            if game_pk in score_cache:
                return score_cache[game_pk]
            score_txt = "—"
            win_loss = ""
            opp_pitcher = "—"
            opp_team = ""
            try:
                br = requests.get(f"{MLB_API}/game/{game_pk}/boxscore", timeout=8)
                if br.ok:
                    bj = br.json()
                    t = bj.get("teams", {})
                    away = t.get("away", {})
                    home = t.get("home", {})
                    away_runs = int((((away.get("teamStats") or {}).get("batting") or {}).get("runs") or 0))
                    home_runs = int((((home.get("teamStats") or {}).get("batting") or {}).get("runs") or 0))
                    away_id = ((away.get("team") or {}).get("id"))
                    home_id = ((home.get("team") or {}).get("id"))
                    # Determine which side the batter is on to set score perspective + opp side
                    if batter_team_id and batter_team_id == away_id:
                        score_txt = f"{away_runs}-{home_runs}"
                        win_loss = "W" if away_runs > home_runs else ("L" if away_runs < home_runs else "T")
                        opp_side = home
                    elif batter_team_id and batter_team_id == home_id:
                        score_txt = f"{home_runs}-{away_runs}"
                        win_loss = "W" if home_runs > away_runs else ("L" if home_runs < away_runs else "T")
                        opp_side = away
                    else:
                        score_txt = f"{away_runs}-{home_runs}"
                        opp_side = home
                    # Extract opposing team abbreviation
                    opp_team = ((opp_side.get("team") or {}).get("abbreviation") or
                                (opp_side.get("team") or {}).get("teamName") or "")
                    # Extract opposing starting pitcher (first in pitchers list)
                    opp_pitchers = opp_side.get("pitchers", [])
                    opp_players = opp_side.get("players", {})
                    if opp_pitchers:
                        starter_key = f"ID{opp_pitchers[0]}"
                        starter = opp_players.get(starter_key, {})
                        opp_pitcher = ((starter.get("person") or {}).get("fullName") or
                                       (starter.get("person") or {}).get("lastName") or "—")
            except Exception:
                pass
            score_cache[game_pk] = (score_txt, win_loss, opp_pitcher, opp_team)
            return score_cache[game_pk]

        # Resolve today's pitcher name for summary line
        pitcher_name = "Pitcher"
        try:
            pr = requests.get(f"{MLB_API}/people/{pitcher_id}", timeout=6)
            if pr.ok:
                pitcher_name = ((pr.json().get("people") or [{}])[0].get("fullName") or "Pitcher")
        except Exception:
            pass

        # Fetch recent game log (no opposingPlayerId filter — that param unreliable)
        # and extract the actual pitcher faced from each game's boxscore.
        try:
            gr = requests.get(
                f"{MLB_API}/people/{player_id}/stats",
                params={"stats": "gameLog", "group": "hitting", "season": year, "sportId": 1},
                timeout=10,
            )
            if gr.ok:
                all_splits = (gr.json().get("stats") or [{}])[0].get("splits", [])
                for sp in reversed(all_splits):
                    if len(games) >= 5:
                        break
                    st = sp.get("stat", {})
                    gm = sp.get("game") or {}
                    game_pk = gm.get("gamePk")
                    team_id = ((sp.get("team") or {}).get("id"))
                    score_txt, win_loss, opp_pitcher, opp_team = _game_info(game_pk, batter_team_id=team_id)
                    games.append({
                        "date": (sp.get("date") or "")[:10],
                        "score": score_txt,
                        "pitcher": opp_pitcher,
                        "team": opp_team,
                        "ab": int(st.get("atBats", 0) or 0),
                        "hits": int(st.get("hits", 0) or 0),
                        "hr": int(st.get("homeRuns", 0) or 0),
                        "rbi": int(st.get("rbi", 0) or 0),
                        "k": int(st.get("strikeOuts", 0) or 0),
                        "bb": int(st.get("baseOnBalls", 0) or 0),
                        "result": win_loss,
                    })
        except Exception:
            pass

        # Ensure latest first.
        games = sorted(games, key=lambda x: x.get("date", ""), reverse=True)[:5]

        ab = bvp.get("ab", 0) or 0
        avg = bvp.get("avg")
        hr = bvp.get("hr", 0) or 0
        ops = bvp.get("ops")
        summary_line = f"vs. {pitcher_name}: {ab} AB | {avg if avg is not None else '---'} AVG | {hr} HR | {ops if ops is not None else '---'} OPS"

        return jsonify({
            "success": True,
            "playerId": player_id,
            "pitcherId": pitcher_id,
            "pitcherName": pitcher_name,
            "grade": bvp.get("grade", "D"),
            "ops": bvp.get("ops"),
            "avg": bvp.get("avg"),
            "hr": bvp.get("hr", 0),
            "pa": bvp.get("pa", 0),
            "sample_note": bvp.get("sample_note") or bvp.get("note") or "",
            "summary": summary_line,
            "games": games,
        })
    except Exception as ex:
        print(f"[api_player_bvp_games] {traceback.format_exc()}")
        return jsonify({"success": False, "error": str(ex)}), 500


@app.route("/api/player/form/<int:player_id>")
def api_player_form(player_id):
    """
    Rolling form for a single player — daily cached.
    Query params:
      is_pitcher=1  (default 0)
    Returns same shape as the 'form' key in /api/player/<id>?includeForm=1.
    """
    is_pitcher = request.args.get("is_pitcher") == "1"
    result = _fetch_rolling_form(player_id, is_pitcher)
    if result is None:
        return jsonify({"success": False, "error": "Form data unavailable"}), 404
    return jsonify({"success": True, **result})



@app.route("/api/player-splits/<int:player_id>/<string:group>")
def api_player_splits(player_id, group):
    try:
        year = datetime.now().year
        # Platoon splits (vl = vs lefty, vr = vs righty)
        pr = requests.get(
            f"{MLB_API}/people/{player_id}/stats",
            params={"stats":"statSplits","group":group,"season":year,"sitCodes":"vl,vr"},
            timeout=8
        )
        pr.raise_for_status()
        platoon = {}
        for sp in pr.json().get("stats",[{}])[0].get("splits",[]):
            code = sp.get("split",{}).get("code","")
            s = sp.get("stat",{})
            if code in ("vl","vr"):
                platoon[code] = {
                    "avg":  s.get("avg","---"), "obp": s.get("obp","---"),
                    "slg":  s.get("slg","---"), "ops": s.get("ops","---"),
                    "pa":   s.get("plateAppearances",0),
                    "hr":   s.get("homeRuns",0),
                    "woba": s.get("woba","---"),
                }
        # Game log (recent form)
        lr = requests.get(
            f"{MLB_API}/people/{player_id}/stats",
            params={"stats":"gameLog","group":group,"season":year},
            timeout=8
        )
        lr.raise_for_status()
        all_games = lr.json().get("stats",[{}])[0].get("splits",[])
        last7 = all_games[-7:] if len(all_games) >= 7 else all_games
        recent = []
        if group == "hitting":
            for sp in last7:
                s = sp.get("stat",{})
                recent.append({
                    "date": sp.get("date",""), "opp": sp.get("opponent",{}).get("abbreviation",""),
                    "ab": s.get("atBats",0), "h": s.get("hits",0),
                    "hr": s.get("homeRuns",0), "rbi": s.get("rbi",0),
                    "k":  s.get("strikeOuts",0), "bb": s.get("baseOnBalls",0),
                    "avg": s.get("avg","---"),
                })
            l7_ab  = sum(g["ab"] for g in recent)
            l7_h   = sum(g["h"]  for g in recent)
            l7_hr  = sum(g["hr"] for g in recent)
            l7_rbi = sum(g["rbi"] for g in recent)
            l7_k   = sum(g["k"]  for g in recent)
            l7_avg = round(l7_h/l7_ab,3) if l7_ab > 0 else 0
            is_hot  = l7_avg >= 0.310 or l7_hr >= 2
            is_cold = l7_avg < 0.150 and l7_ab >= 12
            return jsonify({
                "success":True, "group":"hitting",
                "platoon": platoon, "recentGames": recent,
                "l7_avg": l7_avg, "l7_hr": l7_hr, "l7_rbi": l7_rbi, "l7_k": l7_k,
                "isHot": is_hot, "isCold": is_cold,
            })
        else:
            for sp in last7:
                s = sp.get("stat",{})
                recent.append({
                    "date": sp.get("date",""), "opp": sp.get("opponent",{}).get("abbreviation",""),
                    "ip": s.get("inningsPitched","0"), "er": s.get("earnedRuns",0),
                    "k": s.get("strikeOuts",0), "bb": s.get("baseOnBalls",0),
                    "h": s.get("hits",0),
                })
            l7_er = sum(g["er"] for g in recent)
            l7_k  = sum(g["k"]  for g in recent)
            is_hot  = l7_er <= 4 and len(recent) >= 2
            is_cold = l7_er >= 9 and len(recent) >= 2
            return jsonify({
                "success":True, "group":"pitching",
                "platoon": platoon, "recentGames": recent,
                "l7_er": l7_er, "l7_k": l7_k,
                "isHot": is_hot, "isCold": is_cold,
            })
    except Exception as ex:
        print("[api_player_splits]", traceback.format_exc())
        return jsonify({"success":False,"error":str(ex),"platoon":{},"recentGames":[]}), 500


@app.route("/api/player/<int:player_id>/spray")
def api_player_spray(player_id):
    """Spray chart: current season batted ball positions for a batter."""
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        refresh = str(request.args.get('refresh') or '').strip().lower() in ('1', 'true', 'yes')

        # Check cache first (daily TTL)
        cached = _spray_cache.get(player_id)
        if (not refresh) and cached and cached.get("date") == today:
            return jsonify({"success": True, "data": cached.get("data", [])})

        # Fetch player identity and type
        pr = requests.get(f"{MLB_API}/people/{player_id}", timeout=10)
        pr.raise_for_status()
        people = pr.json().get("people", [])
        if not people:
            return jsonify({"success": False, "error": "Player not found"}), 404

        is_pitcher = (people[0].get("primaryPosition", {}).get("abbreviation", "") or "?") in ("P", "SP", "RP", "CP")
        if is_pitcher:
            _spray_cache[player_id] = {"date": today, "data": []}
            return jsonify({"success": True, "data": []})
        
        # Fetch spray chart data from pybaseball
        import pybaseball as pb
        start_dt = f"{datetime.now().year}-03-01"
        end_dt = today
        sc = pb.statcast_batter(start_dt=start_dt, end_dt=end_dt, player_id=int(player_id))
        
        if sc is None or sc.empty:
            _spray_cache[player_id] = {"date": today, "data": []}
            return jsonify({"success": True, "data": []})

        def _to_float(v):
            try:
                if v is None:
                    return None
                if pd.isna(v):
                    return None
                value = float(v)
                if pd.isna(value):
                    return None
                return value
            except Exception:
                return None

        def _to_text(v):
            if v is None:
                return ""
            try:
                if pd.isna(v):
                    return ""
            except Exception:
                pass
            return str(v).strip()

        # Extract batted ball data with key fields
        spray_data = []
        for _, row in sc.iterrows():
            # Skip if no hit coordinates
            hc_x = _to_float(row.get("hc_x"))
            hc_y = _to_float(row.get("hc_y"))
            if hc_x is None or hc_y is None:
                continue

            events = _to_text(row.get("events")).lower()
            result = _to_text(row.get("events")) or _to_text(row.get("des"))
            if events in ("single", "double", "triple"):
                outcome_cat = 'hit'
            elif events == "home_run":
                outcome_cat = 'hr'
            else:
                outcome_cat = 'out'

            ev = _to_float(row.get("launch_speed"))
            la = _to_float(row.get("launch_angle"))
            hit_distance = _to_float(row.get("hit_distance_sc"))

            spray_data.append({
                'hc_x': round(hc_x, 2),
                'hc_y': round(hc_y, 2),
                'events': events or None,
                'hit_distance': hit_distance,
                'launch_angle': la,
                'exit_velocity': ev,
                'pitch_type': row.get('pitch_type', 'UNK') or 'UNK',
                'result': result,
                'outcome': outcome_cat,
                'date': str(row.get('game_date', '')),
                'home_team': row.get('home_team', ''),
                'away_team': row.get('away_team', ''),
                'is_home_game': row.get('inning_topbot') == 'Bot',
                'pitcher_hand': row.get('p_throws', '?') or '?',
            })

        # Cache the result
        _spray_cache[player_id] = {"date": today, "data": spray_data}
        return jsonify({"success": True, "data": spray_data})

    except Exception as ex:
        print(f"[api_player_spray] {player_id}: {traceback.format_exc()}")
        return jsonify({"success": False, "error": str(ex), "data": []}), 500


@app.route("/api/player/<int:player_id>/zonechart")
def api_player_zonechart(player_id):
    """Strike zone chart: 3×3 zone metrics for pitcher or batter."""
    try:
        refresh = str(request.args.get('refresh') or '').strip().lower() in ('1', 'true', 'yes')
        zone_data = _compute_zonechart_data(player_id, force_refresh=refresh)
        return jsonify({"success": True, "data": zone_data})
    
    except ValueError as ex:
        return jsonify({"success": False, "error": str(ex), "data": [None]*9}), 404
    except Exception as ex:
        print(f"[api_player_zonechart] {player_id}: {traceback.format_exc()}")
        return jsonify({"success": False, "error": str(ex), "data": [None]*9}), 500


def _compute_zone_metrics_pitcher(sc, zone_id, is_pitcher=True):
    """Compute metrics for a single zone from statcast data."""
    if sc is None or sc.empty:
        return {
            "zone_id": zone_id,
            "pitch_pct": 0,
            "swing_rate": 0,
            "whiff_rate": 0,
            "ba": 0,
            "slg": 0,
        }
    
    # Map zone_id to plate coordinates (MLB standard: 17" wide × 30" tall strike zone)
    # Zones: top(z > .76), middle(.41 < z < .76), bottom(z < .41)
    # Left(x < -8.5), center(-8.5 < x < 8.5), right(x > 8.5)
    zone_bounds = {
        0: {"x": (-17, -8.5), "z": (0.76, 3.5)},    # Top-left
        1: {"x": (-8.5, 8.5), "z": (0.76, 3.5)},    # Top-center
        2: {"x": (8.5, 17), "z": (0.76, 3.5)},      # Top-right
        3: {"x": (-17, -8.5), "z": (0.41, 0.76)},   # Mid-left
        4: {"x": (-8.5, 8.5), "z": (0.41, 0.76)},   # Heart
        5: {"x": (8.5, 17), "z": (0.41, 0.76)},     # Mid-right
        6: {"x": (-17, -8.5), "z": (-0.5, 0.41)},   # Bot-left
        7: {"x": (-8.5, 8.5), "z": (-0.5, 0.41)},   # Bot-center
        8: {"x": (8.5, 17), "z": (-0.5, 0.41)},     # Bot-right
    }
    
    bounds = zone_bounds.get(zone_id, {})
    x_range = bounds.get("x", (-17, 17))
    z_range = bounds.get("z", (-0.5, 3.5))
    
    # Filter pitches in this zone
    zone_pitches = sc[
        (sc['plate_x'] >= x_range[0]) & (sc['plate_x'] <= x_range[1]) &
        (sc['plate_z'] >= z_range[0]) & (sc['plate_z'] <= z_range[1])
    ]
    
    if len(zone_pitches) == 0:
        return {
            "zone_id": zone_id,
            "pitch_pct": 0,
            "swing_rate": 0,
            "whiff_rate": 0,
            "ba": 0,
            "slg": 0,
        }
    
    total_pitches = len(sc)
    zone_pitch_count = len(zone_pitches)
    pitch_pct = round(zone_pitch_count / total_pitches * 100, 1) if total_pitches > 0 else 0
    
    # Swing rate: % of pitches with a swing
    swings = len(zone_pitches[zone_pitches['description'].str.contains('swinging', case=False, na=False)])
    swing_rate = round(swings / zone_pitch_count * 100, 1) if zone_pitch_count > 0 else 0
    
    # Whiff rate: % of swings that missed
    whiffs = len(zone_pitches[zone_pitches['description'].str.contains('swinging strike', case=False, na=False)])
    whiff_rate = round(whiffs / swings * 100, 1) if swings > 0 else 0
    
    # BA & SLG: only for batted balls
    events = zone_pitches['events'].fillna('') if 'events' in zone_pitches else pd.Series(dtype=object)
    hit_results = ['single', 'double', 'triple', 'home_run']
    normalized_events = events.astype(str).str.strip().str.lower()
    hits = len(zone_pitches[normalized_events.isin(hit_results)])
    home_runs = len(zone_pitches[normalized_events == 'home_run'])
    doubles = len(zone_pitches[normalized_events == 'double'])
    triples = len(zone_pitches[normalized_events == 'triple'])
    singles = hits - home_runs - doubles - triples
    
    batted_balls = len(zone_pitches[zone_pitches['type'] == 'X'])
    ba = round(hits / batted_balls, 3) if batted_balls > 0 else 0
    total_bases = singles + (doubles * 2) + (triples * 3) + (home_runs * 4)
    slg = round(total_bases / batted_balls, 3) if batted_balls > 0 else 0
    
    return {
        "zone_id": zone_id,
        "pitch_pct": pitch_pct,
        "swing_rate": swing_rate,
        "whiff_rate": whiff_rate,
        "ba": ba,
        "slg": slg,
    }


# ── Phase 6 Monte Carlo Simulation ────────────────────────────────────────────
import random, statistics, math

BULLPEN_BASE = {"era":4.05, "whip":1.28, "k9":8.6, "bb9":3.2, "hr9":1.10}
_bio_cache = {}
_hit_split_cache = {}
_team_pitch_cache = {}

# ── Rolling Form Cache (daily per player) ─────────────────────────────────────
_form_cache: dict = {}          # {player_id: (date, form_dict)}
_form_lock  = threading.Lock()

# ── BvP (Batter-vs-Pitcher) Cache ────────────────────────────────────────────
_bvp_cache: dict = {}           # {(batter_id, pitcher_id): (date, bvp_dict)}
_bvp_lock  = threading.Lock()

# ── Pitch-Type Advantage Cache (daily) ───────────────────────────────────────
_pitch_adv_cache: dict = {}            # {(batter_id, pitcher_id): (date, adv_dict)}
_batter_pitch_perf_cache: dict = {}    # {batter_id: (date, perf_dict)}
_pitch_adv_lock = threading.Lock()
_PITCH_ADV_USE_PYBASEBALL = os.getenv("PITCH_ADV_USE_PYBASEBALL", "0") == "1"

# League averages used for Bayesian shrinkage
_LEAGUE_WOBA  = 0.320
_LEAGUE_BABIP = 0.295
_LEAGUE_KPCT  = 0.225
_LEAGUE_BBPCT = 0.082
# wOBA weights (2024 calibrated)
_WOBA_WEIGHTS = {"bb": 0.696, "hbp": 0.726, "single": 0.888,
                 "double": 1.258, "triple": 1.599, "hr": 2.054}
# Shrinkage priors (PA equivalent)
_SHRINK_PA_FORM = 100   # form windows: shrink hard early-season samples
_SHRINK_PA_BVP  = 200   # BvP: even harder, tiny samples


def _woba_from_counts(bb, hbp, singles, doubles, triples, hr, pa):
    """Compute wOBA from raw counting stats."""
    if pa <= 0:
        return None
    w = _WOBA_WEIGHTS
    num = w["bb"]*bb + w["hbp"]*hbp + w["single"]*singles + w["double"]*doubles + w["triple"]*triples + w["hr"]*hr
    return round(num / pa, 3)


def _shrink(observed, n_obs, league_mean, prior_n):
    """Bayesian shrink: blend observed rate with league mean weighted by sample size."""
    if n_obs <= 0:
        return league_mean
    return round((observed * n_obs + league_mean * prior_n) / (n_obs + prior_n), 4)


def parse_ip(ipval):
    """Parse '6.1' style IP where .1 = 1/3 inning, .2 = 2/3 inning."""
    try:
        f = float(ipval or 0)
        whole = int(f)
        outs = round((f - whole) * 10)  # .1→1 out, .2→2 outs
        return whole + outs / 3.0
    except Exception:
        return 0.0


def _fetch_game_log_raw(player_id, group, n=30):
    """Fetch the last *n* game log entries from MLB Stats API. Returns list of stat dicts."""
    year = datetime.now().year
    try:
        r = requests.get(
            f"{MLB_API}/people/{player_id}/stats",
            params={"stats": "gameLog", "group": group, "season": year},
            timeout=10,
        )
        if not r.ok:
            return []
        splits = (r.json().get("stats") or [{}])[0].get("splits", [])
        return splits[-n:]
    except Exception:
        return []


def _fetch_rolling_form(player_id, is_pitcher):
    """
    Compute L7/L14/L30 rolling form for a player. Daily-cached per player_id.
    For hitters: returns wOBA (shrunk), xwOBA (from Savant cache), K%, BB%,
                 AVG, OBP, SLG, ISO, flag, trend.
    For pitchers: same as existing _compute_form, harmonised.
    """
    today = datetime.now().date()
    with _form_lock:
        cached = _form_cache.get(player_id)
        if cached and cached[0] == today:
            return cached[1]

    try:
        if not is_pitcher:
            splits = _fetch_game_log_raw(player_id, "hitting", 30)
            # Build raw per-game rows newest-first
            rows = []
            for sp in reversed(splits):
                s = sp.get("stat", {})
                ab  = int(s.get("atBats", 0) or 0)
                h   = int(s.get("hits", 0) or 0)
                bb  = int(s.get("baseOnBalls", 0) or 0)
                hbp = int(s.get("hitByPitch", 0) or 0)
                hr  = int(s.get("homeRuns", 0) or 0)
                dbl = int(s.get("doubles", 0) or 0)
                tpl = int(s.get("triples", 0) or 0)
                tb  = int(s.get("totalBases", 0) or 0)
                sf  = int(s.get("sacFlies", 0) or 0)
                so  = int(s.get("strikeOuts", 0) or 0)
                singles = max(0, h - dbl - tpl - hr)
                pa  = ab + bb + hbp + sf
                rows.append({"ab": ab, "h": h, "bb": bb, "hbp": hbp, "hr": hr,
                              "dbl": dbl, "tpl": tpl, "tb": tb, "sf": sf,
                              "so": so, "singles": singles, "pa": pa})

            # Pull Savant xwOBA for season-level anchor
            player_name = ""
            try:
                pr = requests.get(f"{MLB_API}/people/{player_id}", timeout=6)
                player_name = (pr.json().get("people") or [{}])[0].get("fullName", "")
            except Exception:
                pass
            sv = sv_batter(player_name) if player_name else {}
            sv_xwoba = _num(sv.get("sv_xwoba"), 0)

            def _window(n):
                g = rows[:n]
                if not g:
                    return None
                ab  = sum(x["ab"]  for x in g)
                h   = sum(x["h"]   for x in g)
                bb  = sum(x["bb"]  for x in g)
                hbp = sum(x["hbp"] for x in g)
                hr  = sum(x["hr"]  for x in g)
                dbl = sum(x["dbl"] for x in g)
                tpl = sum(x["tpl"] for x in g)
                tb  = sum(x["tb"]  for x in g)
                sf  = sum(x["sf"]  for x in g)
                so  = sum(x["so"]  for x in g)
                singles = sum(x["singles"] for x in g)
                pa  = sum(x["pa"]  for x in g)
                avg  = round(h / ab, 3) if ab else None
                obp  = round((h + bb + hbp) / pa, 3) if pa else None
                slg  = round(tb / ab, 3) if ab else None
                ops  = round((obp or 0) + (slg or 0), 3) if (obp and slg) else None
                iso  = round(max(0, (slg or 0) - (avg or 0)), 3) if slg and avg else None
                raw_woba = _woba_from_counts(bb, hbp, singles, dbl, tpl, hr, pa)
                # Shrink wOBA towards league mean
                shrunk_woba = _shrink(raw_woba or _LEAGUE_WOBA, pa,
                                      _LEAGUE_WOBA, _SHRINK_PA_FORM)
                kpct  = round(so / pa, 4) if pa else None
                bbpct = round(bb / pa, 4) if pa else None
                # xwOBA: blend seasonal Savant number (best proxy we have per-game)
                xwoba = sv_xwoba if sv_xwoba else shrunk_woba

                def fmt3(v):
                    if v is None: return "---"
                    s = int(round(v * 1000))
                    return f".{s:03d}"

                return {
                    "games": len(g), "pa": pa, "ab": ab, "hr": hr,
                    "avg":   fmt3(avg),
                    "obp":   fmt3(obp),
                    "slg":   fmt3(slg),
                    "ops":   fmt3(ops),
                    "iso":   fmt3(iso),
                    "woba":  fmt3(shrunk_woba),
                    "xwoba": fmt3(xwoba),
                    "kpct":  round(kpct, 4) if kpct is not None else None,
                    "bbpct": round(bbpct, 4) if bbpct is not None else None,
                    "raw_woba": round(raw_woba, 3) if raw_woba else None,
                }

            l7  = _window(7)
            l14 = _window(14)
            l30 = _window(30)

            # Flag / trend from L7 wOBA (more stable than AVG)
            flag = "neutral"; flag_note = ""; trend = "stable"
            if l7 and l7["raw_woba"] is not None:
                w7 = l7["raw_woba"]
                if l7["pa"] >= 20:
                    if w7 >= 0.380:   flag = "hot";     flag_note = f"Elite wOBA {l7['woba']} over last 7 games"
                    elif w7 >= 0.340: flag = "warm";    flag_note = f"Solid wOBA {l7['woba']} over last 7 games"
                    elif w7 < 0.270:  flag = "cold";    flag_note = f"Struggling — wOBA {l7['woba']} over last 7 games"
                    elif w7 < 0.310:  flag = "chilly";  flag_note = f"Below avg wOBA {l7['woba']} over last 7 games"
                if l14 and l14["raw_woba"] is not None and l14["pa"] >= 30:
                    w14 = l14["raw_woba"]
                    if w7 > w14 + 0.035:   trend = "improving"
                    elif w7 < w14 - 0.035: trend = "declining"

            result = {"kind": "hitter", "flag": flag, "flag_note": flag_note, "trend": trend,
                      "l7": l7, "l14": l14, "l30": l30}
        else:
            # Pitcher: same logic as before but harmonised
            splits = _fetch_game_log_raw(player_id, "pitching", 10)
            rows = []
            for sp in reversed(splits):
                s = sp.get("stat", {})
                rows.append({
                    "ip":  _parse_ip(s.get("inningsPitched", 0)),
                    "er":  int(s.get("earnedRuns", 0) or 0),
                    "k":   int(s.get("strikeOuts", 0) or 0),
                    "bb":  int(s.get("baseOnBalls", 0) or 0),
                    "h":   int(s.get("hits", 0) or 0),
                    "hr":  int(s.get("homeRuns", 0) or 0),
                    "bf":  int(s.get("battersFaced", 0) or 0),
                })

            def _pit_window(n):
                g = rows[:n]
                if not g: return None
                ip = sum(x["ip"] for x in g)
                er = sum(x["er"] for x in g)
                k  = sum(x["k"]  for x in g)
                bb = sum(x["bb"] for x in g)
                h  = sum(x["h"]  for x in g)
                hr = sum(x["hr"] for x in g)
                bf = sum(x["bf"] for x in g)
                qs = sum(1 for x in g if x["ip"] >= 6 and x["er"] <= 3)
                era  = round(er  / ip * 9, 2) if ip else None
                k9   = round(k   / ip * 9, 2) if ip else None
                bb9  = round(bb  / ip * 9, 2) if ip else None
                whip = round((h + bb) / ip, 3) if ip else None
                kpct  = round(k  / bf, 4) if bf else None
                bbpct = round(bb / bf, 4) if bf else None
                return {
                    "games": len(g), "ip": round(ip, 1), "qs": qs,
                    "era": era, "k9": k9, "bb9": bb9, "whip": whip,
                    "kpct": kpct, "bbpct": bbpct,
                }

            l3  = _pit_window(3)
            l5  = _pit_window(5)
            l10 = _pit_window(10)
            flag = "neutral"; flag_note = ""; trend = "stable"
            if l3 and l3["era"] is not None:
                era3 = l3["era"]
                if era3 < 2.0:   flag = "dealing";    flag_note = f"{era3:.2f} ERA over last 3 starts"
                elif era3 < 3.5: flag = "hot";        flag_note = f"{era3:.2f} ERA over last 3 starts"
                elif era3 < 4.5: flag = "neutral";    flag_note = f"{era3:.2f} ERA over last 3 starts"
                elif era3 < 6.0: flag = "chilly";     flag_note = f"{era3:.2f} ERA over last 3 starts"
                else:            flag = "struggling"; flag_note = f"{era3:.2f} ERA over last 3 starts"
                if l5 and l5["era"] is not None:
                    if era3 < l5["era"] - 0.75:   trend = "improving"
                    elif era3 > l5["era"] + 0.75: trend = "declining"
            result = {"kind": "pitcher", "flag": flag, "flag_note": flag_note, "trend": trend,
                      "l3": l3, "l5": l5, "l10": l10}

    except Exception as ex:
        print(f"[form] player {player_id}:", ex)
        result = None

    with _form_lock:
        _form_cache[player_id] = (today, result)
    return result


def _fetch_bvp(batter_id, pitcher_id):
    """
    Fetch career batter-vs-pitcher splits from the MLB Stats API.
    Returns Bayesian-shrunk rates + raw counts so the caller can decide
    how much weight to apply.
    """
    today = datetime.now().date()
    key = (batter_id, pitcher_id)
    with _bvp_lock:
        cached = _bvp_cache.get(key)
        if cached and cached[0] == today:
            return cached[1]

    result = {"success": False, "pa": 0, "note": "No H2H data"}
    try:
        r = requests.get(
            f"{MLB_API}/people/{batter_id}/stats",
            params={
                "stats": "vsPlayer",
                "group": "hitting",
                "opposingPlayerId": pitcher_id,
                "sportId": 1,
            },
            timeout=10,
        )
        r.raise_for_status()
        splits = (r.json().get("stats") or [{}])[0].get("splits", [])
        if not splits:
            # No career H2H — grade based on batter's platoon split vs pitcher handedness
            platoon_grade = "C"
            platoon_ops = None
            pitcher_hand = "R"
            grade_basis = "platoon"
            try:
                pr = requests.get(f"{MLB_API}/people/{pitcher_id}", timeout=6)
                if pr.ok:
                    ppeople = pr.json().get("people", [{}])
                    pitcher_hand = ((ppeople[0].get("pitchHand") or {}).get("code") or "R")
            except Exception:
                pass
            try:
                year_now = datetime.now().year
                sit_code = "l" if pitcher_hand == "L" else "r"
                sr = requests.get(
                    f"{MLB_API}/people/{batter_id}/stats",
                    params={"stats": "statSplits", "group": "hitting",
                            "sitCodes": sit_code, "season": year_now, "sportId": 1},
                    timeout=8,
                )
                if sr.ok:
                    for sg in sr.json().get("stats", []):
                        for sp in sg.get("splits", []):
                            if (sp.get("split") or {}).get("code") == sit_code:
                                raw = sp.get("stat", {})
                                platoon_ops = _safe_f(raw.get("ops"), None)
                                break
                        if platoon_ops is not None:
                            break
                if platoon_ops is not None:
                    if platoon_ops >= 0.950:
                        platoon_grade = "A+"
                    elif platoon_ops >= 0.850:
                        platoon_grade = "A"
                    elif platoon_ops >= 0.750:
                        platoon_grade = "B"
                    elif platoon_ops >= 0.650:
                        platoon_grade = "C"
                    else:
                        platoon_grade = "D"
            except Exception:
                pass
            result = {
                "success": True,
                "pa": 0,
                "ab": 0,
                "h": 0,
                "hr": 0,
                "avg": None,
                "ops": None,
                "season_ops": None,
                "ops_ratio": None,
                "grade": platoon_grade,
                "grade_basis": grade_basis,
                "pitcher_hand": pitcher_hand,
                "platoon_ops": platoon_ops,
                "sample_note": f"No career H2H — grade reflects vs {'LHP' if pitcher_hand == 'L' else 'RHP'} splits",
                "note": "No career H2H on record",
                "tooltip": f"No career BvP sample | vs {'LHP' if pitcher_hand == 'L' else 'RHP'} OPS: {platoon_ops:.3f}" if platoon_ops else "No career BvP sample",
                "shrunk": {},
            }
        else:
            s   = splits[0].get("stat", {})
            ab  = int(s.get("atBats", 0) or 0)
            h   = int(s.get("hits", 0) or 0)
            bb  = int(s.get("baseOnBalls", 0) or 0)
            hbp = int(s.get("hitByPitch", 0) or 0)
            hr  = int(s.get("homeRuns", 0) or 0)
            dbl = int(s.get("doubles", 0) or 0)
            tpl = int(s.get("triples", 0) or 0)
            so  = int(s.get("strikeOuts", 0) or 0)
            sf  = int(s.get("sacFlies", 0) or 0)
            tb  = int(s.get("totalBases", 0) or 0)
            singles = max(0, h - dbl - tpl - hr)
            pa  = ab + bb + hbp + sf

            raw_avg  = round(h / ab, 3) if ab else 0.0
            raw_obp  = round((h + bb + hbp) / pa, 3) if pa else 0.0
            raw_slg  = round(tb / ab, 3) if ab else 0.0
            raw_ops  = round(raw_obp + raw_slg, 3) if pa and ab else None
            raw_woba = _woba_from_counts(bb, hbp, singles, dbl, tpl, hr, pa) or _LEAGUE_WOBA
            raw_kpct = round(so / pa, 4) if pa else _LEAGUE_KPCT
            raw_bbpct= round(bb / pa, 4) if pa else _LEAGUE_BBPCT

            season_ops = None
            try:
                year = datetime.now().year
                rs = requests.get(
                    f"{MLB_API}/people/{batter_id}/stats",
                    params={"stats": "season", "group": "hitting", "season": year, "sportId": 1},
                    timeout=8,
                )
                if rs.ok:
                    sp = (rs.json().get("stats") or [{}])[0].get("splits", [])
                    if sp:
                        season_ops = _safe_f((sp[0].get("stat") or {}).get("ops"), None)
            except Exception:
                season_ops = None

            ops_ratio = None
            if raw_ops is not None and season_ops is not None and season_ops > 0:
                ops_ratio = round(raw_ops / season_ops, 3)

            # Bayesian shrinkage — with < 5 PA the estimate is nearly pure league avg
            shrunk_woba  = _shrink(raw_woba,  pa, _LEAGUE_WOBA,  _SHRINK_PA_BVP)
            shrunk_avg   = _shrink(raw_avg,   ab, 0.250,         _SHRINK_PA_BVP)
            shrunk_kpct  = _shrink(raw_kpct,  pa, _LEAGUE_KPCT,  _SHRINK_PA_BVP)
            shrunk_bbpct = _shrink(raw_bbpct, pa, _LEAGUE_BBPCT, _SHRINK_PA_BVP)

            # Reliability score 0-1 (saturates at 50 PA)
            reliability = round(min(1.0, pa / 50.0), 3)

            # Edge signal: how much does Bayesian wOBA deviate from league?
            woba_edge = round(shrunk_woba - _LEAGUE_WOBA, 4)
            if pa == 0:
                note = "No career H2H; league average assumed."
            elif pa < 10:
                note = f"Only {pa} PA — insufficient sample."
            elif pa < 20:
                note = f"{pa} PA career H2H — moderate sample."
            else:
                note = f"{pa} PA career H2H — strong sample."

            temp = {
                "success": True,
                "pa": pa,
                "ab": ab,
                "h": h,
                "hr": hr,
                "bb": bb,
                "so": so,
                "avg": raw_avg,
                "ops": raw_ops,
                "season_ops": season_ops,
                "ops_ratio": ops_ratio,
                "sample_note": note,
                "raw": {
                    "avg": raw_avg,
                    "obp": raw_obp,
                    "slg": raw_slg,
                    "ops": raw_ops,
                    "woba": raw_woba,
                    "kpct": raw_kpct,
                    "bbpct": raw_bbpct,
                },
                "shrunk": {
                    "woba": shrunk_woba,
                    "avg": shrunk_avg,
                    "kpct": shrunk_kpct,
                    "bbpct": shrunk_bbpct,
                },
                "reliability": reliability,
                "woba_edge": woba_edge,
                "note": note,
            }
            grade = _compute_bvp_grade(temp)
            temp["grade"] = grade
            avg_txt = f"{raw_avg:.3f}" if raw_avg else ".000"
            ops_txt = f"{raw_ops:.3f}" if raw_ops is not None else "N/A"
            temp["tooltip"] = f"{h}-for-{ab} ({avg_txt}) vs this pitcher | {hr} HR | {ops_txt} OPS"

            result = temp
    except Exception as ex:
        print(f"[bvp] {batter_id} vs {pitcher_id}:", ex)
        result = {"success": False, "error": str(ex)}

    with _bvp_lock:
        _bvp_cache[key] = (today, result)
    return result


def _pitch_avg_from_events(events):
    """Compute batting AVG from statcast event strings."""
    if not events:
        return None
    ab_excluded = {
        "walk", "intent_walk", "hit_by_pitch", "sac_bunt", "sac_fly",
        "catcher_interf", "sac_fly_double_play", "sac_bunt_double_play",
    }
    hit_events = {"single", "double", "triple", "home_run"}
    ab = 0
    hits = 0
    for ev in events:
        if not ev:
            continue
        e = str(ev).strip().lower()
        if not e or e in ab_excluded:
            continue
        ab += 1
        if e in hit_events:
            hits += 1
    if ab <= 0:
        return None
    return round(hits / ab, 3)


def _batter_pitch_profile(batter_id):
    """Build per-batter AVG by pitch type from current-season Statcast."""
    if not batter_id:
        return None
    today = datetime.now().date()
    with _pitch_adv_lock:
        cached = _batter_pitch_perf_cache.get(batter_id)
        if cached and cached[0] == today:
            return cached[1]

    profile = None

    # Stability guard: pybaseball Statcast pulls are expensive and can timeout
    # request handlers under load. Enable only when explicitly requested.
    if not _PITCH_ADV_USE_PYBASEBALL:
        with _pitch_adv_lock:
            _batter_pitch_perf_cache[batter_id] = (today, None)
        return None

    try:
        import pybaseball as pb
        start_dt = f"{datetime.now().year}-03-01"
        end_dt = datetime.now().strftime("%Y-%m-%d")
        df = pb.statcast_batter(start_dt=start_dt, end_dt=end_dt, player_id=int(batter_id))
        if df is not None and len(df) > 0 and "events" in df.columns:
            events_all = [x for x in df["events"].tolist() if x is not None]
            overall_avg = _pitch_avg_from_events(events_all)
            by_pitch = {}
            if "pitch_type" in df.columns:
                for pt in sorted(set([str(x).upper() for x in df["pitch_type"].tolist() if x])):
                    sub = df[df["pitch_type"].astype(str).str.upper() == pt]
                    if len(sub) <= 0:
                        continue
                    p_avg = _pitch_avg_from_events([x for x in sub["events"].tolist() if x is not None])
                    if p_avg is not None:
                        by_pitch[pt] = p_avg
            profile = {
                "overall_avg": overall_avg,
                "by_pitch": by_pitch,
                "sample": int(len(df)),
            }
    except Exception as ex:
        print(f"[pitch_adv:batter_profile] {batter_id}: {ex}")
        profile = None

    with _pitch_adv_lock:
        _batter_pitch_perf_cache[batter_id] = (today, profile)
    return profile


def _pitch_type_advantage(batter_id, pitcher_id, batter_name='', pitcher_name=''):
    """Classify batter performance vs pitcher's primary pitch type."""
    if not batter_id or not pitcher_id:
        return {"status": "neutral", "note": "Neutral matchup"}

    today = datetime.now().date()
    key = (int(batter_id), int(pitcher_id))
    with _pitch_adv_lock:
        cached = _pitch_adv_cache.get(key)
        if cached and cached[0] == today:
            return cached[1]

    out = {"status": "neutral", "note": "Neutral matchup"}
    try:
        p_name = (pitcher_name or '').strip()
        if not p_name:
            try:
                rp = requests.get(f"{MLB_API}/people/{pitcher_id}", timeout=8)
                if rp.ok:
                    p_name = ((rp.json().get("people") or [{}])[0].get("fullName") or "").strip()
            except Exception:
                p_name = ''

        svp = sv_pitcher(p_name) if p_name else {}
        arsenal = (svp.get("sv_arsenal_pct") or {}) if isinstance(svp, dict) else {}
        if not arsenal:
            out = {"status": "neutral", "note": "Neutral matchup"}
        else:
            primary_pitch, usage = max(arsenal.items(), key=lambda kv: _safe_f(kv[1], 0.0))
            pt_code = str(primary_pitch or '').upper()
            pt_label = PITCH_LABELS.get(str(primary_pitch or '').lower(), pt_code or 'pitch')

            prof = _batter_pitch_profile(int(batter_id))
            overall_avg = _safe_f((prof or {}).get("overall_avg"), None)
            pitch_avg = _safe_f(((prof or {}).get("by_pitch") or {}).get(pt_code), None)

            # Fast fallback when pitch-type Statcast isn't available:
            # proxy by handedness split vs pitcher's throwing hand.
            proxy_note = None
            if pitch_avg is None:
                phand = (player_profile(int(pitcher_id)).get("throws") or "R").upper()
                splits = hitter_split_profile(int(batter_id)) or {}
                skey = "vl" if phand == "L" else "vr"
                split_avg = _safe_f((splits.get(skey) or {}).get("avg"), None)
                if overall_avg is None:
                    fgb = fg_batter(batter_name or "") if batter_name else {}
                    overall_avg = _safe_f(fgb.get("fg_avg"), None)
                if split_avg is not None:
                    pitch_avg = split_avg
                    proxy_note = "(proxy: handedness split)"

            if pitch_avg is None:
                status = "neutral"
                note = "Neutral matchup"
            else:
                crush = (pitch_avg >= 0.280) or (overall_avg is not None and pitch_avg >= overall_avg + 0.030)
                struggle = (pitch_avg <= 0.200) or (overall_avg is not None and pitch_avg <= overall_avg - 0.030)
                if crush:
                    status = "favorable"
                    note = f"Crushes {pt_label.lower()}s ({pitch_avg:.3f}) — pitcher throws {_safe_f(usage, 0):.0f}% {pt_label.lower()}s"
                elif struggle:
                    status = "unfavorable"
                    note = f"Struggles vs {pt_label.lower()}s ({pitch_avg:.3f}) — pitcher throws {_safe_f(usage, 0):.0f}% {pt_label.lower()}s"
                else:
                    status = "neutral"
                    note = "Neutral matchup"
                if proxy_note and note != "Neutral matchup":
                    note = f"{note} {proxy_note}"

            out = {
                "status": status,
                "note": note,
                "primary_pitch": pt_code,
                "primary_pitch_label": pt_label,
                "usage_pct": round(_safe_f(usage, 0.0), 1),
                "pitch_avg": pitch_avg,
                "overall_avg": overall_avg,
            }
    except Exception as ex:
        print(f"[pitch_adv] {batter_id} vs {pitcher_id}: {ex}")
        out = {"status": "neutral", "note": "Neutral matchup"}

    with _pitch_adv_lock:
        _pitch_adv_cache[key] = (today, out)
    return out



def _num(v, d=0.0):
    try:
        if v in (None, "", "N/A", "---", ".---", "-.--"):
            raise ValueError('empty numeric')
        f = float(v)
        if not math.isfinite(f):
            raise ValueError('non-finite numeric')
        return f
    except Exception:
        try:
            if d is None:
                return None
            fd = float(d)
            if not math.isfinite(fd):
                return 0.0
            return fd
        except Exception:
            return 0.0


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _pct(values, q):
    if not values:
        return 0
    arr = sorted(values)
    if len(arr) == 1:
        return arr[0]
    pos = (len(arr) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return arr[lo]
    w = pos - lo
    return arr[lo] * (1 - w) + arr[hi] * w


def player_profile(player_id):
    if not player_id:
        return {'bats': 'S', 'throws': 'R'}
    if player_id in _bio_cache:
        return _bio_cache[player_id]
    try:
        r = requests.get(f"{MLB_API}/people/{player_id}", timeout=8)
        r.raise_for_status()
        p = (r.json().get('people') or [{}])[0]
        out = {
            'bats': p.get('batSide', {}).get('code', 'S') or 'S',
            'throws': p.get('pitchHand', {}).get('code', 'R') or 'R',
        }
    except Exception:
        out = {'bats': 'S', 'throws': 'R'}
    _bio_cache[player_id] = out
    return out


def hitter_split_profile(player_id):
    if not player_id:
        return {}
    if player_id in _hit_split_cache:
        return _hit_split_cache[player_id]
    year = datetime.now().year
    out = {}
    try:
        r = requests.get(
            f"{MLB_API}/people/{player_id}/stats",
            params={"stats": "statSplits", "group": "hitting", "season": year, "sitCodes": "vl,vr"},
            timeout=8,
        )
        r.raise_for_status()
        splits = (r.json().get('stats') or [{}])[0].get('splits', [])
        for sp in splits:
            code = sp.get('split', {}).get('code', '')
            if code not in ('vl', 'vr'):
                continue
            s = sp.get('stat', {})
            out[code] = {
                'avg': _num(s.get('avg'), 0.0),
                'ops': _num(s.get('ops'), 0.0),
                'obp': _num(s.get('obp'), 0.0),
                'slg': _num(s.get('slg'), 0.0),
                'pa': int(s.get('plateAppearances', 0) or 0),
            }
    except Exception:
        out = {}
    _hit_split_cache[player_id] = out
    return out


def team_pitching_context(team_id):
    if not team_id:
        return {}
    key = (team_id, datetime.now().date().isoformat())
    if key in _team_pitch_cache:
        return _team_pitch_cache[key]
    year = datetime.now().year
    try:
        r = requests.get(
            f"{MLB_API}/teams/{team_id}/stats",
            params={"stats": "season", "group": "pitching", "season": year},
            timeout=8,
        )
        r.raise_for_status()
        s = (r.json().get('stats') or [{}])[0].get('splits', [{}])[0].get('stat', {})
        out = {
            'era': _clamp(_num(s.get('era'), 4.10), 2.8, 6.2),
            'whip': _clamp(_num(s.get('whip'), 1.28), 1.05, 1.60),
            'k9': _clamp(_num(s.get('strikeoutsPer9Inn'), 8.6), 6.5, 11.5),
            'bb9': _clamp(_num(s.get('walksPer9Inn'), 3.2), 2.0, 4.8),
            'hr9': _clamp(_num(s.get('homeRunsPer9'), 1.10), 0.7, 1.7),
        }
    except Exception:
        out = dict(BULLPEN_BASE)
    _team_pitch_cache[key] = out
    return out


def _rank_staff_badge(composite_rank):
    r = int(composite_rank or 0)
    if r and r <= 5:
        return {'rank': r, 'label': 'Top 5 Staff', 'tone': 'top5', 'icon': '🔴'}
    if r and r >= 21:
        return {'rank': r, 'label': 'Bottom 10 Staff', 'tone': 'bottom10', 'icon': '🟢'}
    return {'rank': r or None, 'label': 'Mid-Pack', 'tone': 'mid', 'icon': '⚪'}


_team_pitch_rank_cache = {'data': None, 'byId': {}, 'ts': 0.0}
_team_pitch_rank_lock = threading.Lock()
_TEAM_PITCH_RANK_TTL = 30 * 60


def _get_team_pitching_rankings(force=False):
    now = time.time()
    with _team_pitch_rank_lock:
        cached = _team_pitch_rank_cache.get('data')
        cached_by_id = _team_pitch_rank_cache.get('byId') or {}
        ts = float(_team_pitch_rank_cache.get('ts') or 0)
    if (not force) and cached and (now - ts) < _TEAM_PITCH_RANK_TTL:
        return cached, cached_by_id

    teams_resp = requests.get(f"{MLB_API}/teams?sportId=1&activeStatus=Y", timeout=12)
    teams_resp.raise_for_status()
    teams_raw = [t for t in (teams_resp.json().get('teams') or []) if (t.get('sport') or {}).get('id') == 1]

    rows = []
    for t in teams_raw:
        tid = t.get('id')
        if not tid:
            continue
        ctx = team_pitching_context(tid)
        rows.append({
            'teamId': tid,
            'abbr': t.get('abbreviation', '?'),
            'name': t.get('name', ''),
            'era': round(float(ctx.get('era') or 0), 2),
            'whip': round(float(ctx.get('whip') or 0), 3),
            'k9': round(float(ctx.get('k9') or 0), 2),
            'hr9': round(float(ctx.get('hr9') or 0), 2),
        })

    if not rows:
        return [], {}

    era_rank = {r['teamId']: i + 1 for i, r in enumerate(sorted(rows, key=lambda x: x.get('era', 99)))}
    whip_rank = {r['teamId']: i + 1 for i, r in enumerate(sorted(rows, key=lambda x: x.get('whip', 99)))}
    k9_rank = {r['teamId']: i + 1 for i, r in enumerate(sorted(rows, key=lambda x: x.get('k9', -1), reverse=True))}
    hr9_rank = {r['teamId']: i + 1 for i, r in enumerate(sorted(rows, key=lambda x: x.get('hr9', 99)))}

    for r in rows:
        tid = r['teamId']
        r['era_rank'] = era_rank.get(tid)
        r['whip_rank'] = whip_rank.get(tid)
        r['k9_rank'] = k9_rank.get(tid)
        r['hr9_rank'] = hr9_rank.get(tid)
        composite = (r['era_rank'] + r['whip_rank'] + r['k9_rank'] + r['hr9_rank']) / 4.0
        r['composite_score'] = round(composite, 2)

    rows.sort(key=lambda x: (x.get('composite_score', 99), x.get('era_rank', 99), x.get('abbr', '')))
    for i, r in enumerate(rows, start=1):
        r['composite_rank'] = i

    by_id = {r['teamId']: r for r in rows}
    with _team_pitch_rank_lock:
        _team_pitch_rank_cache['data'] = rows
        _team_pitch_rank_cache['byId'] = by_id
        _team_pitch_rank_cache['ts'] = now
    return rows, by_id




def _pitcher_model(name, pid=None, team_id=None):
    global _local_arsenal_cache
    mlb = pitcher_stats_mlb(pid) if pid else {}

    def _load_local_pitcher_arsenal():
        """Load and cache pitch arsenal stats from local data/ CSVs.

        Only processes FanGraphs pitching CSV files that contain real pitch-type
        usage columns (e.g. SL%, CH%, CB%).  Steamer projection files lack these
        columns and are intentionally skipped to avoid wasted iteration.
        """
        # Pitch-type abbreviation columns used by FanGraphs (2-3 letter codes + %)
        PITCH_TYPE_COLS = {
            'FA%','SI%','FC%','SL%','CU%','CH%','KC%','KN%','EP%','SC%','FO%',
            'PO%','XX%','CT%','CB%','SF%',
        }
        arsenal = {}
        velo = {}
        # Deduplicate paths — fg*_pit*.csv and fg_pitching_*.csv can overlap
        seen: set = set()
        paths = (
            _glob.glob(os.path.join("data", "fg*_pit*.csv"))
            + _glob.glob(os.path.join("data", "fg_pitching_*.csv"))
        )
        for path in paths:
            abs_path = os.path.abspath(path)
            if abs_path in seen:
                continue
            seen.add(abs_path)
            try:
                df = pd.read_csv(path)
                # Skip files that lack real pitch-type arsenal columns
                has_arsenal = any(c in PITCH_TYPE_COLS or c.endswith("_Velo") for c in df.columns)
                if not has_arsenal:
                    continue
                for _, row in df.iterrows():
                    pname = str(row.get("Name") or row.get("PlayerName") or row.get("player_name") or "").strip()
                    if not pname:
                        continue
                    key = _sv_key(pname)
                    pitch_pct = {
                        k.replace("%", "").lower(): float(row[k])
                        for k in row.keys()
                        if k in PITCH_TYPE_COLS and pd.notnull(row[k])
                    }
                    pitch_velo = {
                        k.replace("_Velo", "").lower(): float(row[k])
                        for k in row.keys()
                        if k.endswith("_Velo") and pd.notnull(row[k])
                    }
                    if pitch_pct:
                        arsenal[key] = pitch_pct
                    if pitch_velo:
                        velo[key] = pitch_velo
            except Exception as ex:
                logging.warning(f"[LocalArsenal] Failed to load {path}: {ex}")
        return arsenal, velo

    # Grab a snapshot of all sv caches under _sv_lock (fast — dicts are already built).
    # The local arsenal load is done OUTSIDE _sv_lock to avoid holding the global
    # stats lock for the 2+ seconds it takes to iterate the FanGraphs CSV files.
    with _sv_lock:
        xs = dict(_sv_pit_xstats)
        ap = dict(_sv_arsenal_pct)
        av = dict(_sv_arsenal_velo)
        lx = _fuzzy_lookup(name, xs)
        r = dict(lx) if lx else {}
        lap = _fuzzy_lookup(name, ap)
        lav = _fuzzy_lookup(name, av)
        r["sv_arsenal_pct"] = lap if lap else {}
        r["sv_arsenal_velo"] = lav if lav else {}

    # Supplement missing arsenal data from local FanGraphs CSVs — done outside
    # _sv_lock so that other stat-lookup threads are not blocked during CSV parsing.
    if not r["sv_arsenal_pct"] or not r["sv_arsenal_velo"]:
        with _local_arsenal_lock:
            if _local_arsenal_cache is None:
                _local_arsenal_cache = _load_local_pitcher_arsenal()
            local_pct, local_velo = _local_arsenal_cache
        key = _sv_key(name)
        if not r["sv_arsenal_pct"] and key in local_pct:
            r["sv_arsenal_pct"] = local_pct[key]
        if not r["sv_arsenal_velo"] and key in local_velo:
            r["sv_arsenal_velo"] = local_velo[key]

    try:
        from brain_merge_patch import _brain_fuzzy, _brain_pit_overlay as _bpo
        with _brain_overlay_lock:
            brain = _brain_fuzzy(name, _bpo)
        for k, v in brain.items():
            if k not in r or r[k] in (None, "", "NA", "N/A"):
                r[k] = v
    except Exception:
        pass
    # Always ensure 'name' is set; merge MLB API stats as fallbacks for missing fields.
    # Normalize numeric pitch-stat keys to float so callers (e.g. _starter_outs_target,
    # _tier_blend) can do arithmetic without TypeError on "N/A" strings from the MLB API.
    r['name'] = name
    _PITCH_FLOAT_KEYS = {'era', 'whip', 'k9', 'bb9', 'hr9'}
    for k, v in mlb.items():
        if k not in r or r[k] in (None, "", "NA", "N/A"):
            r[k] = _num(v, BULLPEN_BASE.get(k, 0.0)) if k in _PITCH_FLOAT_KEYS else v
    r.setdefault('pitchHand', 'R')
    return r
def _tier_blend(tm, starter, w_tm, w_base, w_sp, mods):
    out = {}
    for k in ('era', 'whip', 'k9', 'bb9', 'hr9'):
        out[k] = _clamp(
            w_tm * _num(tm.get(k), BULLPEN_BASE[k]) +
            w_base * BULLPEN_BASE[k] +
            w_sp * _num(starter.get(k), BULLPEN_BASE[k]) +
            mods.get(k, 0),
            0.55 if k == 'hr9' else 0.0,
            1.9 if k == 'hr9' else 20.0
        )
    return out



def _bullpen_tiers(starter, team_id=None):
    tm = team_pitching_context(team_id)
    closer = _tier_blend(tm, starter, 0.65, 0.20, 0.15, {'era': -0.28, 'whip': -0.06, 'k9': 0.80, 'bb9': -0.15, 'hr9': -0.08})
    setup = _tier_blend(tm, starter, 0.62, 0.23, 0.15, {'era': -0.12, 'whip': -0.03, 'k9': 0.35, 'bb9': -0.05, 'hr9': -0.03})
    middle = _tier_blend(tm, starter, 0.58, 0.25, 0.17, {'era': 0.18, 'whip': 0.04, 'k9': -0.25, 'bb9': 0.10, 'hr9': 0.08})
    for name, model in [('Closer', closer), ('Setup', setup), ('Middle', middle)]:
        model['name'] = name
        model['pitchHand'] = starter.get('pitchHand', 'R')
    return {'closer': closer, 'setup': setup, 'middle': middle}
# Alias for legacy code
_parse_ip = parse_ip


def _starter_outs_target(starter, rng):
    mean = 16.5 + (4.1 - _num(starter.get('era'), 4.1)) * 1.2 + (_num(starter.get('k9'), 8.2) - 8.2) * 0.20 - max(0, _num(starter.get('whip'), 1.25) - 1.25) * 2.8
    mean = _clamp(mean, 12.0, 21.0)
    return int(_clamp(round(rng.gauss(mean, 2.4)), 9, 24))


def _platoon_adjustments(b, pitch_hand):
    bats = (b.get('bats') or 'S').upper()
    base_avg = _num(b.get('sv_xba'), _num(b.get('avg'), 0.245))
    base_ops = _num(b.get('ops'), 0.720)
    split_avg = None
    split_ops = None
    if pitch_hand == 'L':
        split_avg = _num(b.get('vs_l_avg'), 0)
        split_ops = _num(b.get('vs_l_ops'), 0)
    elif pitch_hand == 'R':
        split_avg = _num(b.get('vs_r_avg'), 0)
        split_ops = _num(b.get('vs_r_ops'), 0)
    hit_adj = 0.0; hr_adj = 0.0; k_adj = 0.0
    if split_avg > 0 and split_ops > 0:
        hit_adj += (split_avg - base_avg) * 0.65
        hr_adj += (split_ops - base_ops) * 0.10
        k_adj -= (split_avg - base_avg) * 1.6
    else:
        if bats == 'S':
            hit_adj += 0.008; hr_adj += 0.0015; k_adj -= 0.008
        elif (bats == 'L' and pitch_hand == 'R') or (bats == 'R' and pitch_hand == 'L'):
            hit_adj += 0.010; hr_adj += 0.0020; k_adj -= 0.012
        else:
            hit_adj -= 0.008; hr_adj -= 0.0015; k_adj += 0.010
    return hit_adj, hr_adj, k_adj


# ── BAT X for Simulation ──────────────────────────────────────────────────────
# Lightweight wrapper: computes per-batter composite + component adjustments
# **once** before the simulation loop (not every PA) and returns a multiplier dict
# that _derive_probs_batx() applies to PA outcome probabilities.
def _batx_for_sim(batter, opp_pitcher, park, weather):
    """
    Returns a dict with:
      composite   float  overall multiplier (e.g. 1.08 = 8% above neutral)
      hit_mult    float  hit probability multiplier
      hr_mult     float  HR probability multiplier
      walk_mult   float  walk probability multiplier
      k_mult      float  strikeout probability multiplier
    All clamped conservatively so no single batter swings MC by more than ±25%.
    """
    try:
        opp_fg = fg_pitcher(opp_pitcher.get('name', ''))
        opp_sv = sv_pitcher(opp_pitcher.get('name', ''))
        pitcher_hand = (opp_pitcher.get('pitchHand') or 'R').upper()
        proj = _project_batter_batx(
            batter, opp_pitcher.get('name', ''), opp_fg, opp_sv,
            park, weather or {}, pitcher_hand=pitcher_hand,
            opp_pitcher_id=opp_pitcher.get('id')
        )
        adj = proj.get('adjustments', {})
        comp = _clamp(proj.get('composite', 1.0), 0.70, 1.30)

        # Contact quality → hit rate
        contact  = adj.get('contact',    0.0)
        power    = adj.get('power',       0.0)
        disc     = adj.get('discipline',  0.0)
        platoon  = adj.get('platoon',     0.0)
        form     = adj.get('form',        0.0)
        bvp      = adj.get('bvp',         0.0)
        pitcher  = adj.get('pitcher',     0.0)

        hit_boost  = _clamp(1.0 + contact*3.5 + platoon*2.0 + form*2.5 + bvp*2.0 + pitcher*1.5, 0.75, 1.25)
        hr_boost   = _clamp(1.0 + power*4.0   + platoon*1.5 + form*2.0 + bvp*1.5,               0.60, 1.40)
        walk_boost = _clamp(1.0 + disc*4.0    + platoon*1.0,                                     0.75, 1.25)
        k_boost    = _clamp(1.0 - disc*3.0    - contact*2.0 - form*1.5,                          0.78, 1.22)

        return {
            'composite': comp,
            'hit_mult':  hit_boost,
            'hr_mult':   hr_boost,
            'walk_mult': walk_boost,
            'k_mult':    k_boost,
        }
    except Exception:
        return {'composite': 1.0, 'hit_mult': 1.0, 'hr_mult': 1.0, 'walk_mult': 1.0, 'k_mult': 1.0}


def _derive_probs(b, p, park=1.0, batx=None):
    avg = _num(b.get('sv_xba'), _num(b.get('avg'), 0.245))
    obp = _num(b.get('obp'), max(avg + 0.060, 0.290))
    slg = _num(b.get('sv_xslg'), _num(b.get('slg'), 0.400))
    xwoba = _num(b.get('sv_xwoba'), _num(b.get('fg_woba'), 0.320))
    ev = _num(b.get('sv_ev'), 87.5)
    hh = _num(b.get('sv_hh_pct'), 37.0)
    brl = _num(b.get('sv_brl_pct'), 5.5)
    wrc = _num(b.get('fg_wrc'), 100.0)
    sb_total = _num(b.get('fg_sb'), 6)

    era = _num(p.get('era'), 4.25)
    whip = _num(p.get('whip'), 1.28)
    k9 = _num(p.get('k9'), 8.4)
    bb9 = _num(p.get('bb9'), 3.2)
    hr9 = _num(p.get('hr9'), 1.10)
    pitch_hand = (p.get('pitchHand') or 'R').upper()

    hand_hit, hand_hr, hand_k = _platoon_adjustments(b, pitch_hand)

    hit_rate = avg + (xwoba - 0.320) * 0.30 + (ev - 87.5) * 0.003 + (hh - 37.0) * 0.0016 + (wrc - 100) * 0.00035
    hit_rate += (whip - 1.28) * 0.055 - (era - 4.25) * 0.010 + (park - 1.0) * 0.030 + hand_hit
    hit_rate = _clamp(hit_rate, 0.13, 0.35)

    walk_rate = max(obp - avg, 0.045) + (bb9 - 3.2) * 0.010
    if pitch_hand == 'L' and (b.get('bats') or 'S') == 'L':
        walk_rate += 0.002
    walk_rate = _clamp(walk_rate, 0.04, 0.15)

    hr_rate = 0.018 + max(0, brl - 6.0) * 0.0035 + max(0, ev - 89.0) * 0.0017 + max(0, slg - 0.420) * 0.060
    hr_rate += (hr9 - 1.10) * 0.020 + (park - 1.0) * 0.050 + hand_hr
    hr_rate = _clamp(hr_rate, 0.005, min(0.095, hit_rate * 0.45))

    dbl_rate = 0.040 + max(0, slg - avg - 0.150) * 0.12 + max(0, ev - 88.0) * 0.002
    dbl_rate = _clamp(dbl_rate, 0.020, min(0.110, hit_rate * 0.40))

    trp_rate = _clamp(0.004 + max(0, avg - 0.270) * 0.04 + (park - 1.0) * 0.005, 0.001, 0.020)
    trp_rate = min(trp_rate, max(0.001, hit_rate - hr_rate - dbl_rate - 0.02))

    single_rate = max(0.05, hit_rate - hr_rate - dbl_rate - trp_rate)
    k_rate = 0.175 + (k9 - 8.2) * 0.018 - (avg - 0.245) * 0.35 - (hh - 37) * 0.002 + hand_k
    k_rate = _clamp(k_rate, 0.09, 0.36)

    steal_rate = 0.010 + max(0, sb_total - 8) * 0.002 + max(0, wrc - 100) * 0.00015
    if (b.get('bats') or 'S') == 'L':
        steal_rate += 0.003
    steal_rate = _clamp(steal_rate, 0.003, 0.075)
    steal_success = _clamp(0.63 + max(0, sb_total - 8) * 0.01 + max(0, avg - 0.250) * 0.4, 0.58, 0.88)

    out_rate = 1.0 - (walk_rate + single_rate + dbl_rate + trp_rate + hr_rate)
    if out_rate < 0.28:
        scale = (1.0 - 0.28) / max(0.01, 1.0 - out_rate)
        walk_rate *= scale; single_rate *= scale; dbl_rate *= scale; trp_rate *= scale; hr_rate *= scale
        out_rate = 1.0 - (walk_rate + single_rate + dbl_rate + trp_rate + hr_rate)
    k_share = _clamp(k_rate / max(out_rate, 0.001), 0.18, 0.72)

    # ── BAT X Integration ─────────────────────────────────────────────────────
    # Apply matchup-aware multipliers computed once per batter from the BAT X engine.
    # This makes every simulated PA reflect platoon, park, form, BvP, and pitcher
    # resistance instead of only season averages.
    if batx:
        hit_m  = batx.get('hit_mult',  1.0)
        hr_m   = batx.get('hr_mult',   1.0)
        walk_m = batx.get('walk_mult', 1.0)
        k_m    = batx.get('k_mult',    1.0)
        # Scale hit types preserving internal ratios
        new_single = _clamp(single_rate * hit_m, 0.03, 0.30)
        new_dbl    = _clamp(dbl_rate    * hit_m, 0.01, 0.12)
        new_trp    = _clamp(trp_rate    * hit_m, 0.001, 0.022)
        new_hr     = _clamp(hr_rate     * hr_m,  0.003, 0.10)
        new_bb     = _clamp(walk_rate   * walk_m, 0.04, 0.16)
        new_out    = max(0.28, 1.0 - (new_single + new_dbl + new_trp + new_hr + new_bb))
        new_k_share = _clamp(k_share * k_m, 0.18, 0.75)
        return {
            'bb': new_bb, '1b': new_single, '2b': new_dbl, '3b': new_trp, 'hr': new_hr,
            'out': new_out, 'kshare': new_k_share, 'steal_rate': steal_rate, 'steal_success': steal_success
        }

    return {
        'bb': walk_rate, '1b': single_rate, '2b': dbl_rate, '3b': trp_rate, 'hr': hr_rate,
        'out': out_rate, 'kshare': k_share, 'steal_rate': steal_rate, 'steal_success': steal_success
    }


def _pick_event(probs, rng):
    r = rng.random(); acc = 0.0
    for ev in ['bb', '1b', '2b', '3b', 'hr', 'out']:
        acc += probs[ev]
        if r <= acc:
            if ev == 'out' and rng.random() < probs['kshare']:
                return 'k'
            return ev
    return 'out'


def _normalize_pa_probs(probs):
    normalized = dict(probs or {})
    non_out_total = (
        float(normalized.get('bb', 0.0) or 0.0)
        + float(normalized.get('1b', 0.0) or 0.0)
        + float(normalized.get('2b', 0.0) or 0.0)
        + float(normalized.get('3b', 0.0) or 0.0)
        + float(normalized.get('hr', 0.0) or 0.0)
    )
    normalized['out'] = max(0.28, 1.0 - non_out_total)
    normalized['kshare'] = _clamp(float(normalized.get('kshare', 0.18) or 0.18), 0.18, 0.78)
    return normalized


def _blank_batter_line(b):
    return {'id': b.get('id'), 'name': b.get('name', ''), 'slot': b.get('slot', 0), 'pos': b.get('pos', ''), 'bats': b.get('bats', 'S'), 'pa': 0, 'ab': 0, 'h': 0, '1b': 0, '2b': 0, '3b': 0, 'hr': 0, 'rbi': 0, 'r': 0, 'bb': 0, 'k': 0, 'tb': 0, 'sb': 0, 'cs': 0}


def _blank_pitcher_line(name):
    return {'name': name, 'outs': 0, 'h': 0, 'er': 0, 'bb': 0, 'k': 0, 'hr': 0}


def _advance_walk(bases, batter_idx, stats, pstats):
    runs = 0
    if bases[0] is not None and bases[1] is not None and bases[2] is not None:
        ridx = bases[2]; stats[ridx]['r'] += 1; stats[batter_idx]['rbi'] += 1; runs += 1; pstats['er'] += 1
    third = bases[1] if (bases[0] is not None and bases[1] is not None) else bases[2]
    second = bases[0] if bases[0] is not None else bases[1]
    first = batter_idx
    if bases[0] is not None and bases[1] is not None and bases[2] is not None:
        bases[:] = [first, second, third]
    elif bases[0] is not None and bases[1] is not None:
        bases[:] = [first, bases[0], bases[1]]
    elif bases[0] is not None:
        bases[:] = [first, bases[0], bases[2]]
    else:
        bases[0] = first
    return runs


def _advance_hit(event, bases, batter_idx, stats, pstats, rng, outs_before):
    runs = 0
    def score_runner(idx):
        nonlocal runs
        if idx is not None:
            stats[idx]['r'] += 1; stats[batter_idx]['rbi'] += 1; runs += 1; pstats['er'] += 1
    if event == '1b':
        if bases[2] is not None: score_runner(bases[2]); bases[2] = None
        if bases[1] is not None:
            if outs_before == 2 or rng.random() < 0.60:
                score_runner(bases[1]); bases[1] = None
        new_third = None
        if bases[0] is not None:
            if rng.random() < 0.38:
                new_third = bases[0]; bases[0] = None
            else:
                bases[1] = bases[0]; bases[0] = None
        if bases[1] is not None and new_third is None:
            bases[2] = bases[1]; bases[1] = None
        elif new_third is not None:
            bases[2] = new_third
        bases[0] = batter_idx
    elif event == '2b':
        if bases[2] is not None: score_runner(bases[2])
        if bases[1] is not None: score_runner(bases[1])
        new_third = None
        if bases[0] is not None:
            if rng.random() < 0.58: score_runner(bases[0])
            else: new_third = bases[0]
        bases[:] = [None, batter_idx, new_third]
    elif event == '3b':
        for idx in list(bases):
            if idx is not None: score_runner(idx)
        bases[:] = [None, None, batter_idx]
    elif event == 'hr':
        for idx in list(bases):
            if idx is not None: score_runner(idx)
        score_runner(batter_idx)
        bases[:] = [None, None, None]
    return runs


# ── Catcher Pop Time (Baseball Savant) ───────────────────────────────────────
# Pop time = seconds from pitch contact → catcher release → 2B glove.
# League average ~2.00s. Sub-1.90s = elite (e.g. Contreras). 2.10s+ = weak.
# Loaded from Baseball Savant catcher pop time leaderboard at startup.
# key: lowercase catcher name  →  value: float avg pop time
_CATCHER_POP_CACHE: dict[str, float] = {}
_LEAGUE_POP_AVG = 2.00

def _catcher_sb_mult(catcher_name: str) -> float:
    """
    Scale SB success probability based on catcher pop time.
    Fast arm (1.88s): suppresses SB ~30%. Slow arm (2.12s): boosts ~30%.
    Neutral (2.00s): returns 1.0.
    """
    pop = _CATCHER_POP_CACHE.get((catcher_name or '').lower().strip(), _LEAGUE_POP_AVG)
    delta = _LEAGUE_POP_AVG - pop   # positive = fast arm = suppresses steals
    return min(1.35, max(0.65, 1.0 - delta * 2.5))

def _maybe_steal(bases, lineup, stats, rng, probs_map, outs):
    if outs >= 2:
        return 0
    if bases[0] is not None and bases[1] is None:
        ridx = bases[0]
        pr = probs_map.get(ridx, {})
        if rng.random() < pr.get('steal_rate', 0.0):
            _catcher = pr.get('opp_catcher', '')
            _pop_mult = _catcher_sb_mult(_catcher)
            _adj_success = min(0.95, max(0.35, pr.get('steal_success', 0.72) * _pop_mult))
            if rng.random() < _adj_success:
                bases[1] = ridx; bases[0] = None; stats[ridx]['sb'] += 1
                return 1
            else:
                bases[0] = None; stats[ridx]['cs'] += 1
                return -1
    if bases[1] is not None and bases[2] is None and outs == 0:
        ridx = bases[1]
        pr = probs_map.get(ridx, {})
        if rng.random() < pr.get('steal_rate', 0.0) * 0.30:
            _catcher2 = pr.get('opp_catcher', '')
            _pop_mult2 = _catcher_sb_mult(_catcher2)
            if rng.random() < min(0.90, max(0.30, (pr.get('steal_success', 0.72) - 0.08) * _pop_mult2)):
                bases[2] = ridx; bases[1] = None; stats[ridx]['sb'] += 1
                return 1
            else:
                bases[1] = None; stats[ridx]['cs'] += 1
                return -1
    return 0


def _select_relief_tier(inning, runs_allowed, starter_outs, tiers):
    if inning >= 8 and runs_allowed <= 4:
        return tiers['closer']
    if inning >= 7 and runs_allowed <= 5:
        return tiers['setup']
    if starter_outs >= 18 and inning >= 6:
        return tiers['setup']
    return tiers['middle']


def _team_relief_fatigue_woba(team_id: int) -> float:
    """
    Best-effort computation of team bullpen fatigue wOBA penalty.
    Uses the last 3 days of relief appearances; returns 0.0 on any error.
    """
    try:
        recent = _team_recent_games(team_id, 3)
        if not recent:
            return 0.0
        report = _build_bullpen_fatigue(team_id, '', recent)
        penalties = []
        for rel in report.get('relievers', []):
            dp = rel.get('days_pitched') or []
            pl = rel.get('pitches_total') or 0
            p = relief_fatigue_penalty(dp, min(pl, 35))
            if p > 0:
                penalties.append(p)
        if not penalties:
            return 0.0
        return round(sum(penalties) / len(penalties), 4)
    except Exception:
        return 0.0































































































































_TRACKER_CAPTURE_LOCK = threading.Lock()
_TRACKER_CAPTURE_JOBS = {}
_TRACKER_AUTO_SYNC_LOCK = threading.Lock()
_TRACKER_AUTO_SYNC_STARTED = False






















# ── Phase 12 Auto-Calibration Layer ───────────────────────────────────────────
CALIBRATION_TARGETS = {
    'batter_hits': 0.56,
    'batter_total_bases': 0.55,
    'batter_home_runs': 0.52,
    'batter_rbis': 0.53,
    'batter_runs_scored': 0.53,
    'batter_hits_runs_rbis': 0.54,
    'batter_stolen_bases': 0.52,
    'pitcher_strikeouts': 0.55,
    'nrfi': 0.53,
    'yrfi': 0.50,
}


























































@app.route('/api/consistency/today')
def api_consistency_today():
    date_str = request.args.get('date') or datetime.now(ET).strftime('%Y-%m-%d')
    refresh = str(request.args.get('refresh') or '').strip().lower() in ('1', 'true', 'yes')
    try:
        return jsonify(_consistency_payload(date_str, refresh=refresh))
    except Exception as ex:
        print(f'[api_consistency_today] {traceback.format_exc()}')
        return jsonify({'success': False, 'error': str(ex)}), 500





















# ── Phase 14 Closing-Line Value + ROI Simulation ──────────────────────────────












# ── Phase 15 Bankroll + Bet Sizing Layer ──────────────────────────────────────
















# ── Phase 16 Portfolio Constraints Layer ──────────────────────────────────────






# ── Phase 2: Batter vs Pitcher Grade ────────────────────────────────────────





# ── Phase 17 Bet Slip Builder + Final Card Output ─────────────────────────────












# ── Phase 18 Bankroll Curve + Card Audit Reporting ────────────────────────────












# ── Phase 19 CLV Attribution + Realized Edge Reporting ────────────────────────












TEAM_HEADSHOT_BASE = "https://img.mlbstatic.com/mlb-photos/image/upload/w_180,q_auto:best/v1/people/{player_id}/headshot/67/current"

# /api/teams/overview is ~200KB and makes 30+ MLB-API calls — cache it.
# 10-min TTL keeps it fresh while shielding the worker from repeat hits.
_teams_overview_cache = {"data": None, "ts": 0.0}
_teams_overview_lock  = threading.Lock()
_TEAMS_OVERVIEW_TTL   = 600  # seconds

@app.route('/api/teams/overview')
def api_teams_overview():
    """Load all 30 MLB teams with rosters. Uses only cached in-memory FG/Savant
    data for player stats — no per-player MLB API calls to avoid timeouts."""
    # Serve from cache if fresh.
    with _teams_overview_lock:
        cached = _teams_overview_cache["data"]
        ts     = _teams_overview_cache["ts"]
    if cached and (time.time() - ts) < _TEAMS_OVERVIEW_TTL:
        return jsonify(cached)
    try:
        teams_resp = requests.get(f"{MLB_API}/teams?sportId=1&activeStatus=Y&sportId=1", timeout=12)
        teams_resp.raise_for_status()
        teams_raw = [t for t in teams_resp.json().get('teams', []) if t.get('sport', {}).get('id') == 1]
        # Fetch all 30 rosters concurrently using threads
        import concurrent.futures
        def fetch_roster(t):
            # ZERO per-player API calls — all stats from pre-cached FG/Savant
            tid = t.get('id')
            roster = _get_active_roster(tid)
            players = []
            for r in roster[:40]:
                try:
                    person = r.get('person', {})
                    pid    = person.get('id')
                    name   = person.get('fullName', 'Unknown')
                    pos    = (r.get('position', {}) or {}).get('abbreviation', '?')
                    if pos == 'P':
                        fgp = fg_pitcher(name) or {}
                        svp = sv_pitcher(name) or {}
                        stat_line = {
                            'label1': 'ERA', 'value1': fgp.get('fg_era')  or svp.get('sv_xera') or '—',
                            'label2': 'FIP', 'value2': fgp.get('fg_fip')  or fgp.get('fg_xfip') or '—',
                            'label3': 'K%',  'value3': fgp.get('fg_kpct') or svp.get('sv_k_pct') or '—',
                        }
                    else:
                        fgb = fg_batter(name) or {}
                        svb = sv_batter(name) or {}
                        wrc = fgb.get('fg_wrc')
                        if not wrc:
                            try:
                                woba = float(fgb.get('fg_woba') or svb.get('sv_xwoba') or 0)
                                wrc  = round((woba - 0.320) / 0.047 * 100 + 100) if woba > 0 else None
                            except Exception:
                                wrc = None
                        stat_line = {
                            'label1': 'AVG',  'value1': fgb.get('fg_avg')  or svb.get('sv_xba')   or '—',
                            'label2': 'wOBA', 'value2': fgb.get('fg_woba') or svb.get('sv_xwoba') or '—',
                            'label3': 'wRC+', 'value3': wrc or '—',
                        }
                    players.append({
                        'id':    pid,
                        'name':  name,
                        'pos':   pos,
                        'image': TEAM_HEADSHOT_BASE.format(player_id=pid) if pid else '',
                        **stat_line,
                    })
                except Exception:
                    pass
            return {
                'id':      tid,
                'abbr':    t.get('abbreviation', '?'),
                'name':    t.get('name', ''),
                'logo':    LOGO_BASE.format(team_id=tid),
                'players': players,
            }
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(fetch_roster, sorted(teams_raw, key=lambda x: x.get('abbreviation', ''))))
        payload = {'success': True, 'teams': [r for r in results if r]}
        with _teams_overview_lock:
            _teams_overview_cache["data"] = payload
            _teams_overview_cache["ts"]   = time.time()
        return jsonify(payload)
    except Exception as ex:
        # If we have a stale cached payload, serve it rather than failing.
        if cached:
            return jsonify(cached)
        return jsonify({'success': False, 'error': str(ex), 'teams': []}), 500


@app.route('/api/teams/pitching-rankings')
def api_teams_pitching_rankings():
    try:
        rows, _ = _get_team_pitching_rankings(force=False)
        return jsonify({'success': True, 'rankings': rows, 'count': len(rows), 'updatedAt': datetime.now(timezone.utc).isoformat()})
    except Exception as ex:
        print('[api_teams_pitching_rankings]', traceback.format_exc())
        return jsonify({'success': False, 'error': str(ex), 'rankings': []}), 500


# ── Cheatsheet cache (Sprint 3.1) ───────────────────────────────────────────
_cheatsheet_cache_lock = threading.Lock()
_cheatsheet_cache = {'data': None, 'ts': 0.0, 'signature': None, 'date': None}
_cheatsheet_refreshing = False
_CHEATSHEET_TTL = 30 * 60


def _cheatsheet_signature(schedule_rows):
    parts = []
    for g in sorted(schedule_rows or [], key=lambda x: x.get('gamePk', 0)):
        lineups = g.get('lineups') or {}
        away = [((p.get('fullName') or p.get('name') or '').strip()) for p in (lineups.get('awayBatters') or [])[:9]]
        home = [((p.get('fullName') or p.get('name') or '').strip()) for p in (lineups.get('homeBatters') or [])[:9]]
        parts.append(f"{g.get('gamePk')}|{','.join(away)}|{','.join(home)}")
    return '||'.join(parts)


def _l10_hit_pct_for_player(player_id, memo):
    if not player_id:
        return None
    if player_id in memo:
        return memo[player_id]
    try:
        t = _build_player_trends(int(player_id), False)
        pct = ((t.get('over_rates') or {}).get('hits', {}).get('0.5', {}).get('l10', {}) or {}).get('pct')
        memo[player_id] = pct
        return pct
    except Exception:
        memo[player_id] = None
        return None


def _compute_cheatsheets_today(date_str):
    full_sched = fetch_schedule(date_str)
    # Remove postponed, cancelled, and suspended games — no lineup data available.
    sched = [
        g for g in full_sched
        if not any(tok in str((g.get("status") or {}).get("detailedState") or "").lower()
                   for tok in ("postponed", "cancelled", "canceled", "suspended"))
    ]
    if not sched:
        return {
            'success': True,
            'date': date_str,
            'hitsBoard': {'rows': []},
            'battingOrderMatchups': {'rows': []},
            'pitcherWeakspots': {'cards': []},
            'generatedAt': datetime.now(timezone.utc).isoformat(),
            'signature': '',
            'games': 0,
        }

    l10_memo = {}
    hits_rows = []
    matchup_rows = []
    weakspot_cards = []

    def _bvp_points(grade):
        g = str(grade or 'D').upper()
        return {'A+': 1.0, 'A': 0.9, 'B': 0.75, 'C': 0.55, 'D': 0.35}.get(g, 0.45)

    def _pitch_adv_points(status):
        s = str(status or 'neutral').lower()
        return {'favorable': 0.85, 'neutral': 0.50, 'unfavorable': 0.20}.get(s, 0.50)

    def _weakspot_card(p_name, p_id, p_team, opp_bats, p_fg, p_sv, p_hand, matchup, matchup_key,
                       gpk, is_dh, game_number, game_time_sort='9999', game_time_display=''):
        raw_pitcher_name = (p_name or '').strip()
        is_tbd_pitcher = not raw_pitcher_name or raw_pitcher_name == 'TBD'
        p_display_name = raw_pitcher_name or 'TBD'
        p_fg = p_fg or {}
        p_sv = p_sv or {}

        opp_scores = []
        if not is_tbd_pitcher:
            for b in (opp_bats or [])[:9]:
                try:
                    sc = _matchup_score(b, p_fg, p_sv, pitcher_hand=p_hand)
                    opp_scores.append({'slot': b.get('slot', 0), 'score': sc.get('score', 50), 'batter': b})
                except Exception:
                    continue
            opp_scores.sort(key=lambda x: x.get('score', 0), reverse=True)

        top_slots = sorted([x.get('slot') for x in opp_scores[:3] if x.get('slot')])
        weak_slots = ', '.join(str(x) for x in top_slots) if top_slots else ('pending' if is_tbd_pitcher else 'n/a')

        top_batters = []
        for entry in opp_scores[:3]:
            b = entry.get('batter', {})
            b_name = b.get('name', '')
            b_fg = fg_batter(b_name) or {}
            top_batters.append({
                'name': b_name,
                'slot': entry.get('slot', 0),
                'score': round(entry.get('score', 50), 1),
                'avg': b_fg.get('fg_avg', b.get('avg', '')),
            })

        if is_tbd_pitcher:
            pitch_vuln = 'Awaiting probable pitcher'
            form_label = 'PENDING PROBABLE'
            recent = {}
        else:
            with _sv_lock:
                name_key = _sv_key(p_display_name) if p_display_name else ''
                arsenal = dict(_sv_arsenal_pct.get(name_key, {}) or {})
                if not arsenal and name_key:
                    match_name = difflib.get_close_matches(name_key, _sv_arsenal_pct.keys(), n=1, cutoff=0.72)
                    if match_name:
                        arsenal = dict(_sv_arsenal_pct.get(match_name[0], {}) or {})
            primary_pitch, primary_pct = ('Unknown', 0)
            if arsenal:
                primary_pitch, primary_pct = max(arsenal.items(), key=lambda kv: kv[1])
            pitch_label = PITCH_LABELS.get(primary_pitch, primary_pitch)
            pitch_vuln = f"{pitch_label} ({round(float(primary_pct or 0), 1)}% usage)"

            recent = _pitcher_recent_form(p_id) if p_id else {}
            season_era = _safe_f((pitcher_stats_mlb(p_id) or {}).get('era'), 4.20) if p_id else 4.20
            recent_era = _safe_f((recent or {}).get('era_recent'), season_era)
            if recent and recent_era <= max(2.70, season_era - 0.4):
                form_label = f"DEALING ({recent_era:.2f} ERA last {recent.get('n_starts', 0)} starts)"
            elif recent and recent_era >= min(6.50, season_era + 0.5):
                form_label = f"STRUGGLING ({recent_era:.2f} ERA last {recent.get('n_starts', 0)} starts)"
            else:
                era_src = 'recent' if recent else 'season'
                form_label = f"STABLE ({recent_era:.2f} ERA {era_src})"

        k_prop_display = None
        if not is_tbd_pitcher:
            try:
                p_fg_k = fg_pitcher(p_display_name) or {}
                k9_season = _safe_f(p_fg_k.get('fg_k9'), 0.0)
                k9_recent = _safe_f((recent or {}).get('k9_recent'), k9_season)
                k9_blended = round(0.6 * k9_season + 0.4 * k9_recent, 1) if k9_recent else round(k9_season, 1)
                xfip = _safe_f(p_fg_k.get('fg_xfip') or p_fg_k.get('fg_fip'), 4.0)
                total_ip = _safe_f(p_fg_k.get('fg_ip'), 0.0)
                total_gs = _safe_f(p_fg_k.get('fg_gs') or p_fg_k.get('fg_g'), 1.0)
                k_per_start = round(k9_season * (total_ip / max(1.0, total_gs)) / 9.0, 1) if total_ip > 0 else 0.0
                if k9_blended >= 7.5:
                    k_line = 4.5 if k9_blended < 8.5 else 5.5
                    k_prop_display = {
                        'line': k_line,
                        'k9Blended': k9_blended,
                        'xfip': round(xfip, 2),
                        'kStartRecent': k_per_start,
                    }
            except Exception:
                pass

        avg_top_score = sum(x.get('score', 50) for x in opp_scores[:4]) / max(1, len(opp_scores[:4]))
        if is_tbd_pitcher:
            rec = 'Wait for the listed starter before attacking this game'
        elif 'STRUGGLING' in form_label:
            rec = 'Target top-order hits/TB overs'
        elif 'DEALING' in form_label:
            rec = 'Play selectively by lineup slot and price'
        elif avg_top_score >= 62:
            rec = 'Target top-order hits/TB overs'
        elif avg_top_score <= 44:
            rec = 'Consider fading opposing batter overs'
        else:
            rec = 'Play selectively by lineup slot and price'

        return {
            'pitcherName': p_display_name,
            'pitcherId': p_id,
            'team': p_team,
            'weakSlots': weak_slots,
            'topBatters': top_batters,
            'pitchVulnerability': pitch_vuln,
            'formLabel': form_label,
            'recommendation': rec,
            'matchup': matchup,
            'matchupKey': matchup_key,
            'gamePk': gpk,
            'isDoubleHeader': is_dh,
            'gameNumber': game_number,
            'gameTimeSort': game_time_sort,
            'gameTime': game_time_display,
            'kProp': k_prop_display,
        }

    def _process_game(g):
        local_hits, local_matchups, local_weakspots = [], [], []
        gpk = g.get('gamePk')
        matchup = ''

        # Stage 1: fetch game data (hard fail — nothing to build without it)
        try:
            gdata, away_bats, home_bats, away_t, home_t, pitchers = _props_fetch_game(
                gpk, date_hint=date_str, gdata_override=g
            )
            if not gdata:
                print(f"[CHEATSHEET] no gdata for {gpk}")
                return local_hits, local_matchups, local_weakspots
        except Exception as ex:
            print(f"[CHEATSHEET] _props_fetch_game failed for {gpk}: {ex}")
            return local_hits, local_matchups, local_weakspots

        away_team = away_t.get('team', {}) if isinstance(away_t, dict) else {}
        home_team = home_t.get('team', {}) if isinstance(home_t, dict) else {}
        away_abbr = away_team.get('abbreviation', 'AWAY')
        home_abbr = home_team.get('abbreviation', 'HOME')
        home_tid = home_team.get('id')

        is_dh = str(g.get('doubleHeader') or 'N').upper() == 'Y'
        game_number = int(g.get('gameNumber') or 1)
        matchup = f"{away_abbr} @ {home_abbr}"
        matchup_key = f"{away_abbr}-{home_abbr}-{gpk}"

        raw_game_dt = g.get('gameDate', '')
        try:
            dt_utc = datetime.fromisoformat(raw_game_dt.replace('Z', '+00:00'))
            game_time_sort = dt_utc.astimezone(ET).strftime('%H%M')
            game_time_display = dt_utc.astimezone(ET).strftime('%-I:%M %p ET')
        except Exception:
            game_time_sort = '9999'
            game_time_display = ''

        park = PARK_FACTORS.get(home_tid, 1.0)

        ap = (pitchers or {}).get('ap') or {}
        hp = (pitchers or {}).get('hp') or {}
        ap_name = ap.get('fullName', 'TBD')
        hp_name = hp.get('fullName', 'TBD')
        ap_id = ap.get('id')
        hp_id = hp.get('id')

        ap_fg = fg_pitcher(ap_name) or {}
        hp_fg = fg_pitcher(hp_name) or {}
        ap_sv = sv_pitcher(ap_name) or {}
        hp_sv = sv_pitcher(hp_name) or {}

        ap_stats = pitcher_stats_mlb(ap_id) if ap_id else {}
        hp_stats = pitcher_stats_mlb(hp_id) if hp_id else {}
        ap_hand = ap_stats.get('pitchHand') or 'R'
        hp_hand = hp_stats.get('pitchHand') or 'R'

        def _side_rows(batters, team_abbr, opp_name, opp_id, opp_hand, opp_fg, opp_sv):
            rows = []
            for b in (batters or [])[:9]:
                name = b.get('name')
                pid = b.get('id')
                if not name:
                    continue

                score = _matchup_score(b, opp_fg, opp_sv, pitcher_hand=opp_hand)
                l10_pct = _l10_hit_pct_for_player(pid, l10_memo)
                bvp_data = _fetch_bvp(pid, opp_id) if pid and opp_id else None
                bvp_grade = _compute_bvp_grade(bvp_data) if bvp_data else 'D'
                matchup_grade = _cheatsheet_matchup_grade(
                    score.get('tier'),
                    pitch_adv={'status': 'neutral'},
                    bvp_grade=bvp_grade,
                    bvp_pa=(bvp_data or {}).get('pa', 0),
                )

                split_ops = platoon_blend_v2(b, opp_hand, 'ops')
                split_score = max(0.0, min(1.0, (split_ops - 0.550) / 0.350))
                park_score = max(0.0, min(1.0, (park - 0.90) / 0.25))
                l10_score = l10_pct if l10_pct is not None else 0.50
                comp = (
                    _bvp_points(bvp_grade) * 0.30
                    + l10_score * 0.25
                    + split_score * 0.20
                    + park_score * 0.15
                    + _pitch_adv_points('neutral') * 0.10
                )
                comp_pct = round(comp * 100.0, 1)
                model_prob = max(0.01, min(0.99, (score.get('score') or 50) / 100.0))
                edge = model_prob - 0.50
                hub = _hub_rating(model_prob, edge, l10_score)

                rows.append({
                    'gamePk': gpk,
                    'matchup': matchup,
                    'player': name,
                    'playerId': pid,
                    'team': team_abbr,
                    'slot': b.get('slot'),
                    'marketKey': 'batterhits',
                    'line': 0.5,
                    'vsHand': opp_hand,
                    'oppPitcher': opp_name,
                    'l10Pct': l10_pct,
                    'bvpGrade': bvp_grade,
                    'matchupGrade': matchup_grade,
                    'hubRating': hub,
                    'evPct': round(edge * 100.0, 1),
                    'matchupScore': score.get('score'),
                    'composite': comp_pct,
                })
            return rows

        # Stage 2: hits/matchups rows (soft fail — weakspot cards can still be built)
        try:
            away_rows = _side_rows(away_bats, away_abbr, hp_name, hp_id, hp_hand, hp_fg, hp_sv)
            home_rows = _side_rows(home_bats, home_abbr, ap_name, ap_id, ap_hand, ap_fg, ap_sv)
            local_hits.extend(away_rows)
            local_hits.extend(home_rows)

            ranked = sorted(away_rows + home_rows, key=lambda x: (x.get('composite') or 0), reverse=True)
            for i, row in enumerate(ranked, start=1):
                local_matchups.append({
                    'gamePk': row.get('gamePk'),
                    'matchup': row.get('matchup'),
                    'rank': i,
                    'player': row.get('player'),
                    'team': row.get('team'),
                    'slot': row.get('slot'),
                    'matchupScore': row.get('composite'),
                    'bvpGrade': row.get('bvpGrade'),
                    'matchupGrade': row.get('matchupGrade'),
                    'l10Pct': row.get('l10Pct'),
                    'hubRating': row.get('hubRating'),
                })
        except Exception as ex:
            print(f"[CHEATSHEET] hits/matchups failed for {gpk} {matchup}: {ex}")

        # Stage 3: weakspot cards (soft fail — hits/matchups already collected)
        try:
            c1 = _weakspot_card(
                ap_name, ap_id, away_abbr, home_bats, ap_fg, ap_sv, ap_hand,
                matchup, matchup_key, gpk, is_dh, game_number, game_time_sort, game_time_display
            )
            c2 = _weakspot_card(
                hp_name, hp_id, home_abbr, away_bats, hp_fg, hp_sv, hp_hand,
                matchup, matchup_key, gpk, is_dh, game_number, game_time_sort, game_time_display
            )
            if c1:
                local_weakspots.append(c1)
            if c2:
                local_weakspots.append(c2)
        except Exception as ex:
            print(f"[CHEATSHEET] _weakspot_card failed for {gpk} {matchup}: {ex}")

        print(f"[CHEATSHEET] {gpk} {matchup} -> hits:{len(local_hits)} matchups:{len(local_matchups)} weakspots:{len(local_weakspots)}")
        return local_hits, local_matchups, local_weakspots

    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(_process_game, g) for g in sched]
        for fut in as_completed(futs):
            h, m, w = fut.result()
            hits_rows.extend(h)
            matchup_rows.extend(m)
            weakspot_cards.extend(w)

    hits_rows.sort(key=lambda x: (x.get('hubRating') or 0, x.get('matchupScore') or 0), reverse=True)
    matchup_rows.sort(key=lambda x: (x.get('matchup'), x.get('rank')))
    weakspot_cards.sort(key=lambda x: (x.get('gameTimeSort', '9999'), x.get('matchup', ''), x.get('team', '')))

    return {
        'success': True,
        'date': date_str,
        'hitsBoard': {'rows': hits_rows[:450]},
        'battingOrderMatchups': {'rows': matchup_rows[:450]},
        'pitcherWeakspots': {'cards': weakspot_cards},
        'generatedAt': datetime.now(timezone.utc).isoformat(),
        'signature': _cheatsheet_signature(full_sched),
        'games': len(sched),
    }


def _get_cheatsheets_today(force=False):
    today = datetime.now(ET).strftime('%Y-%m-%d')
    sched = fetch_schedule(today)
    signature = _cheatsheet_signature(sched)
    now = time.time()

    with _cheatsheet_cache_lock:
        cached = _cheatsheet_cache.get('data')
        ts = float(_cheatsheet_cache.get('ts') or 0)
        cdate = _cheatsheet_cache.get('date')
        csig = _cheatsheet_cache.get('signature')
        refreshing = _cheatsheet_refreshing

    if (not force) and cached and cdate == today and csig == signature and (now - ts) < _CHEATSHEET_TTL:
        out = dict(cached)
        out['cacheAgeSec'] = int(now - ts)
        out['cached'] = True
        out['computing'] = False
        return out

    if force:
        fresh = _compute_cheatsheets_today(today)
        with _cheatsheet_cache_lock:
            _cheatsheet_cache['data'] = fresh
            _cheatsheet_cache['ts'] = now
            _cheatsheet_cache['date'] = today
            _cheatsheet_cache['signature'] = fresh.get('signature')
        out = dict(fresh)
        out['cacheAgeSec'] = 0
        out['cached'] = False
        out['computing'] = False
        return out

    if cached:
        if not refreshing:
            _trigger_cheatsheet_refresh_async(reason='stale_cache')
        out = dict(cached)
        out['cacheAgeSec'] = int(now - ts)
        out['cached'] = True
        out['computing'] = True
        out['message'] = 'Refreshing in background'
        return out

    if not refreshing:
        _trigger_cheatsheet_refresh_async(reason='cold_start')
    return {
        'success': True,
        'date': today,
        'hitsBoard': {'rows': []},
        'battingOrderMatchups': {'rows': []},
        'pitcherWeakspots': {'cards': []},
        'generatedAt': datetime.now(timezone.utc).isoformat(),
        'signature': signature,
        'games': len(sched or []),
        'cacheAgeSec': 0,
        'cached': False,
        'computing': True,
        'message': 'Computing... auto-refresh in 20s',
    }


def _trigger_cheatsheet_refresh_async(reason='manual'):
    global _cheatsheet_refreshing
    with _cheatsheet_cache_lock:
        if _cheatsheet_refreshing:
            return
        _cheatsheet_refreshing = True

    def _runner():
        global _cheatsheet_refreshing
        try:
            _get_cheatsheets_today(force=True)
            print(f"[cheatsheets] refreshed ({reason})")
        except Exception:
            print('[cheatsheets] refresh failed', traceback.format_exc())
        finally:
            with _cheatsheet_cache_lock:
                _cheatsheet_refreshing = False

    threading.Thread(target=_runner, daemon=True).start()


@app.route('/api/cheatsheets/today')
def api_cheatsheets_today():
    try:
        force = request.args.get('refresh') == '1'
        payload = _get_cheatsheets_today(force=force)
        return jsonify(payload)
    except Exception as ex:
        print('[api_cheatsheets_today]', traceback.format_exc())
        return jsonify({'success': False, 'error': str(ex)}), 500


# ── Monte Carlo background cache ──────────────────────────────────────────────
_mc_cache_lock  = threading.Lock()
_mc_cache_data  = None
_mc_cache_ts    = None
_mc_computing   = False
_mc_started_ts  = None   # watchdog: when _mc_computing was last set True
_MC_COMPUTE_TIMEOUT_SEC = 300  # 5 min watchdog — reset stuck flag after this

def _mc_grade(edge):
    if edge >= 0.15: return 'A+'
    if edge >= 0.10: return 'A'
    if edge >= 0.06: return 'B'
    if edge >= 0.02: return 'C'
    return 'D'

def _mc_rec(edge):
    if edge >= 0.15: return 'STRONG PLAY'
    if edge >= 0.10: return 'PLAY'
    if edge >= 0.06: return 'LEAN'
    if edge >= 0.02: return 'WATCH'
    return 'SKIP'

def _mc_compute_background():
    global _mc_cache_data, _mc_cache_ts, _mc_computing, _mc_started_ts
    try:
        # Ensure stat caches are fresh before computing
        _maybe_refresh_fg()
        _maybe_refresh_savant()
        _fetch_injury_status(force=False)

        # Pre-warm the odds snapshot once here (before workers) so that each
        # _compute_game worker can read from cache without triggering a fresh
        # N+1 sequential HTTP fetch inside the thread pool.
        has_odds = bool(ODDS_API_KEY)
        if has_odds:
            try:
                _ensure_daily_odds_snapshot()
            except Exception:
                print(f"[mc_bg] odds snapshot pre-warm failed: {traceback.format_exc()}")

        date_str = datetime.now(ET).strftime('%Y-%m-%d')
        url = (f"{MLB_API}/schedule?sportId=1&date={date_str}"
               "&hydrate=team,probablePitcher,lineups")
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        dates = resp.json().get('dates', [])
        raw   = dates[0].get('games', []) if dates else []

        games, ranked = [], []

        def _compute_game(g):
            game_pk   = g.get('gamePk')
            away_t    = g.get('teams', {}).get('away', {})
            home_t    = g.get('teams', {}).get('home', {})
            away_team = away_t.get('team', {})
            home_team = home_t.get('team', {})
            away      = away_team.get('abbreviation', '?')
            home      = home_team.get('abbreviation', '?')
            away_tid  = away_team.get('id')
            home_tid  = home_team.get('id')
            matchup   = f'{away} @ {home}'
            away_p    = (away_t.get('probablePitcher') or {})
            home_p    = (home_t.get('probablePitcher') or {})
            top_props = []
            try:
                lineups      = g.get('lineups') or {}
                away_hitters = lineups.get('awayBatters') or []
                home_hitters = lineups.get('homeBatters') or []

                def _parse(hitters, side):
                    out = []
                    for i, p in enumerate(hitters, start=1):
                        name = (p.get('fullName') or p.get('name') or '').strip()
                        if name: out.append({'name': name, 'slot': i, 'side': side})
                    return out

                away_lu = _parse(away_hitters, 'away')
                home_lu = _parse(home_hitters, 'home')

                def _roster(tid, side):
                    out = []
                    for e in _get_active_roster(tid):
                        pos  = ((e.get('position') or {}).get('abbreviation') or '?')
                        if pos in ('P', 'SP', 'RP', 'CP'):
                            continue
                        name = ((e.get('person') or {}).get('fullName') or '').strip()
                        if name:
                            out.append({'name': name, 'slot': 0, 'side': side})
                        if len(out) >= 15:
                            break
                    return out

                if len(away_lu) < 5: away_lu = _roster(away_tid, 'away')
                if len(home_lu) < 5: home_lu = _roster(home_tid, 'home')

                all_batters = away_lu + home_lu
                print(f"[mc_bg] {matchup}: {len(away_lu)}a + {len(home_lu)}h")

                if has_odds:
                    valid_names = {b['name'] for b in all_batters if b.get('name')}
                    if away_p.get('fullName'): valid_names.add(away_p['fullName'])
                    if home_p.get('fullName'): valid_names.add(home_p['fullName'])
                    event, _ = _find_odds_event(away_team.get('name',''), home_team.get('name',''))
                    props_books = _load_event_odds(event.get('id') if event else None, featured_only=False) if event else []
                    props_raw = _parse_prop_markets(props_books, valid_names)
                    for p in props_raw:
                        op = p.get('over_implied'); up = p.get('under_implied')
                        if op is None or up is None: continue
                        vig = op + up
                        if vig <= 0: continue
                        fair = op / vig; price = p.get('over_price')
                        if price is None: continue
                        book_impl = _american_to_implied(price)
                        edge = round(fair - book_impl, 4) if book_impl else 0
                        top_props.append({
                            'player': p.get('player'), 'market': p.get('market_key'),
                            'line': p.get('line'), 'bookmaker': p.get('bookmaker'),
                            'price': price, 'edge': round(edge,4), 'prob': round(fair,4),
                            'source': 'odds', 'matchup': matchup,
                            'grade': _mc_grade(edge), 'recommendation': _mc_rec(edge),
                            'reasoning': '',
                        })
                    top_props = sorted(top_props, key=lambda x: x['edge'], reverse=True)[:12]
                else:
                    for b in all_batters:
                        name = b.get('name',''); slot = b.get('slot') or 5; side = b.get('side','')
                        if not name: continue
                        svb = sv_batter(name); fgb = fg_batter(name)
                        xba  = _safe_f(svb.get('sv_xba')    or fgb.get('fg_avg'),  0.250)
                        woba = _safe_f(svb.get('sv_xwoba')  or fgb.get('fg_woba'), 0.320)
                        brl  = _safe_f(svb.get('sv_brl_pct'), 0.0)
                        ev   = _safe_f(svb.get('sv_ev'),    88.0)
                        slg  = _safe_f(fgb.get('fg_slg'),   0.380)
                        wrc  = int(_safe_f(fgb.get('fg_wrc'), 100))
                        obp  = _safe_f(fgb.get('fg_obp'),   0.320)
                        lm = 1.06 if slot<=2 else (1.03 if slot<=4 else (0.97 if slot>=8 else 1.0))
                        for mk, line, prob, reason in [
                            ('batter_hits',        0.5, min(0.92, xba *3.2*lm),
                             f"xBA {xba:.3f} · wOBA {woba:.3f} · wRC+ {wrc} → {min(0.92,xba*3.2*lm)*100:.0f}% hit prob; bats {slot} ({side})"),
                            ('batter_total_bases', 1.5, min(0.88, woba*2.4*lm),
                             f"xwOBA {woba:.3f} · SLG {slg:.3f} · EV {ev:.1f} mph → {min(0.88,woba*2.4*lm)*100:.0f}% TB prob vs 1.5"),
                            ('batter_home_runs',   0.5, min(0.50, brl/100*1.8*lm),
                             f"Barrel% {brl:.1f} · EV {ev:.1f} mph · wOBA {woba:.3f} → {min(0.50,brl/100*1.8*lm)*100:.0f}% HR prob vs 0.5"),
                            ('batter_rbis',        0.5, min(0.72, woba*2.0*lm),
                             f"wOBA {woba:.3f} · OBP {obp:.3f} · bats {slot} ({side}) → {min(0.72,woba*2.0*lm)*100:.0f}% RBI prob vs 0.5"),
                        ]:
                            if prob < 0.20: continue
                            edge = round(prob - 0.50, 4)
                            top_props.append({'player': name, 'market': mk, 'line': line,
                                'bookmaker': 'Model', 'price': None, 'source': 'simulation',
                                'matchup': matchup, 'edge': edge, 'prob': round(prob,4),
                                'grade': _mc_grade(edge), 'recommendation': _mc_rec(edge),
                                'reasoning': reason})

                    for pit in [away_p, home_p]:
                        pname = (pit.get('fullName') or '').strip()
                        if not pname: continue
                        fgp = fg_pitcher(pname); svp = sv_pitcher(pname)
                        k9    = _safe_f(fgp.get('fg_k9'),  0.0)
                        xfip  = _safe_f(fgp.get('fg_xfip') or fgp.get('fg_fip'), 4.0)
                        whiff = _safe_f(svp.get('sv_whiff'), 0.0)
                        bb9   = _safe_f(fgp.get('fg_bb9'),  3.5)
                        ip    = _safe_f(fgp.get('fg_ip'),   0.0)
                        k_prob = min(0.87, max(0.25,
                            (k9/9*0.85 if k9>0 else 0.50)
                            + ((whiff-22)/100*0.4 if whiff>22 else 0)
                            + ((4.10-xfip)/100*0.3)))
                        if k_prob < 0.30: continue
                        edge   = round(k_prob - 0.50, 4)
                        reason = (f"K/9 {k9:.1f} · xFIP {xfip:.2f} · Whiff% {whiff:.1f}% · BB/9 {bb9:.1f}"
                                  + (f" · {ip:.0f} IP" if ip>0 else '') + f" → {k_prob*100:.0f}% K prob vs 5.5")
                        top_props.append({'player': pname, 'market': 'pitcher_strikeouts', 'line': 5.5,
                            'bookmaker': 'Model', 'price': None, 'source': 'simulation',
                            'matchup': matchup, 'edge': edge, 'prob': round(k_prob,4),
                            'grade': _mc_grade(edge), 'recommendation': _mc_rec(edge),
                            'reasoning': reason})

                    top_props = sorted(top_props, key=lambda x: x['edge'], reverse=True)

            except Exception:
                print(f"[mc_bg] {matchup}: {traceback.format_exc()}")
            return {'gamePk': game_pk, 'matchup': matchup, 'topProps': top_props}, top_props

        # Total timeout for all workers: allow up to 90 s for the entire game
        # batch so that a single stalled network call cannot hang the whole
        # background thread forever.
        _MC_FUTURES_TIMEOUT = 90
        workers = min(6, max(1, len(raw)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(_compute_game, g) for g in raw]
            try:
                for fut in as_completed(futs, timeout=_MC_FUTURES_TIMEOUT):
                    game_row, top_props = fut.result()
                    games.append(game_row)
                    if top_props:
                        ranked.extend(top_props)
            except TimeoutError:
                print(f"[mc_bg] WARNING: as_completed timed out after {_MC_FUTURES_TIMEOUT}s — using partial results")
                for fut in futs:
                    if fut.done() and not fut.cancelled():
                        try:
                            game_row, top_props = fut.result()
                            if game_row not in games:
                                games.append(game_row)
                            if top_props:
                                ranked.extend(top_props)
                        except Exception:
                            pass

        ranked = sorted(ranked, key=lambda x: x.get('edge', 0), reverse=True)
        games = sorted(games, key=lambda x: x.get('matchup') or '')
        result = {'success': True, 'date': date_str, 'hasOdds': has_odds,
                  'games': games, 'topProps': ranked[:500], 'computing': False}
        with _mc_cache_lock:
            _mc_cache_data = result
            _mc_cache_ts   = datetime.now()
        print(f"[mc_bg] done — {len(ranked)} props, {len(games)} games")
    except Exception:
        print(f"[mc_bg] FATAL: {traceback.format_exc()}")
    finally:
        with _mc_cache_lock:
            _mc_computing  = False
            _mc_started_ts = None


def _mc_maybe_refresh(force=False):
    global _mc_computing, _mc_started_ts
    with _mc_cache_lock:
        running    = _mc_computing
        started_at = _mc_started_ts
        age        = (datetime.now() - _mc_cache_ts).total_seconds() if _mc_cache_ts else 9999
        has_data   = _mc_cache_data is not None

    if running:
        # Watchdog: if the background thread has been marked as computing for
        # longer than the allowed ceiling, it has likely hung.  Reset the flag
        # so the next poll can launch a fresh thread.
        if started_at and (datetime.now() - started_at).total_seconds() > _MC_COMPUTE_TIMEOUT_SEC:
            print(f"[mc_bg] watchdog: resetting stuck _mc_computing flag "
                  f"(running for >{_MC_COMPUTE_TIMEOUT_SEC}s)")
            with _mc_cache_lock:
                _mc_computing  = False
                _mc_started_ts = None
            # Fall through to start a new thread below
        elif not force:
            return  # Still within timeout and not a forced refresh — wait

    if not force and has_data and age < 1800:
        return

    with _mc_cache_lock:
        _mc_computing  = True
        _mc_started_ts = datetime.now()
    threading.Thread(target=_mc_compute_background, daemon=True).start()




@app.route('/api/lineup/<int:game_pk>')
def api_lineup(game_pk):
    try:
        # ── Step 1: Try live boxscore (official confirmed batting order) ──────
        away, home = [], []
        try:
            r = requests.get(f"{MLB_API}/game/{game_pk}/boxscore", timeout=10)
            d = r.json().get('teams', {})
            away = get_batters_from_boxscore(d.get('away', {}), 'away')
            home = get_batters_from_boxscore(d.get('home', {}), 'home')
        except Exception:
            pass

        away_source = 'confirmed' if len(away) >= 9 else None
        home_source = 'confirmed' if len(home) >= 9 else None

        # ── Step 2: Fallback to schedule-hydrated projected lineups ──────────
        if not away_source or not home_source:
            try:
                gdata = fetch_schedule_game(game_pk)
                if gdata:
                    lineups = gdata.get('lineups') or {}
                    def _parse_sched_lu(hitters):
                        out = []
                        for i, p in enumerate(hitters or [], start=1):
                            name = (p.get('fullName') or p.get('name') or '').strip()
                            pid  = p.get('id') or p.get('playerId')
                            pos  = (p.get('primaryPosition') or {}).get('abbreviation', '?')
                            if not name:
                                continue
                            fgb = fg_batter(name); svb = sv_batter(name)
                            bio = _bio_cache.get(pid) or {}
                            out.append({
                                'slot': i, 'id': pid, 'name': name, 'pos': pos,
                                'bats': bio.get('bats', 'R'),
                                'avg': fgb.get('fg_avg', '.---'), 'obp': fgb.get('fg_obp', '.---'),
                                'slg': fgb.get('fg_slg', '.---'), 'ops': fgb.get('fg_ops', '.---'),
                                'fg_woba': fgb.get('fg_woba', 'N/A'), 'fg_wrc': fgb.get('fg_wrc', 'N/A'),
                                'sv_xwoba': svb.get('sv_xwoba', 'N/A'),
                                'ab': 0, 'hits': 0, 'hr': 0, 'rbi': 0,
                            })
                        return out
                    if not away_source:
                        proj = _parse_sched_lu(lineups.get('awayBatters', []))
                        if proj:
                            away = proj
                            away_source = 'projected'
                    if not home_source:
                        proj = _parse_sched_lu(lineups.get('homeBatters', []))
                        if proj:
                            home = proj
                            home_source = 'projected'
            except Exception:
                pass

        # ── Step 3: Last resort — active roster top position players ─────────
        def _roster_fallback_lu(team_id):
            try:
                out = []
                for entry in _get_active_roster(team_id):
                    pos = ((entry.get('position') or {}).get('abbreviation') or '?')
                    if pos in ('P', 'SP', 'RP', 'CP'):
                        continue
                    name = ((entry.get('person') or {}).get('fullName') or '').strip()
                    pid  = ((entry.get('person') or {}).get('id'))
                    if not name:
                        continue
                    fgb = fg_batter(name); svb = sv_batter(name)
                    out.append({
                        'slot': len(out) + 1, 'id': pid, 'name': name, 'pos': pos, 'bats': 'R',
                        'avg': fgb.get('fg_avg', '.---'), 'obp': fgb.get('fg_obp', '.---'),
                        'slg': fgb.get('fg_slg', '.---'), 'ops': fgb.get('fg_ops', '.---'),
                        'fg_woba': fgb.get('fg_woba', 'N/A'), 'fg_wrc': fgb.get('fg_wrc', 'N/A'),
                        'sv_xwoba': svb.get('sv_xwoba', 'N/A'),
                        'ab': 0, 'hits': 0, 'hr': 0, 'rbi': 0,
                    })
                    if len(out) >= 9:
                        break
                return out
            except Exception:
                return []

        if not away_source:
            # Need team ID — fetch game feed
            try:
                gf = requests.get(f"https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live", timeout=8).json()
                away_tid = gf.get('gameData', {}).get('teams', {}).get('away', {}).get('id')
                if away_tid:
                    away = _roster_fallback_lu(away_tid)
                    if away: away_source = 'roster'
            except Exception:
                pass

        if not home_source:
            try:
                if 'gf' not in dir():
                    gf = requests.get(f"https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live", timeout=8).json()
                home_tid = gf.get('gameData', {}).get('teams', {}).get('home', {}).get('id')
                if home_tid:
                    home = _roster_fallback_lu(home_tid)
                    if home: home_source = 'roster'
            except Exception:
                pass

        return jsonify({
            'success': True, 'gamePk': game_pk,
            'away': away[:9], 'home': home[:9],
            'awayConfirmed': away_source == 'confirmed',
            'homeConfirmed': home_source == 'confirmed',
            'awaySource': away_source or 'none',
            'homeSource': home_source or 'none',
        })
    except Exception as ex:
        return jsonify({'success': False, 'gamePk': game_pk, 'away': [], 'home': [],
                        'awayConfirmed': False, 'homeConfirmed': False,
                        'awaySource': 'none', 'homeSource': 'none', 'error': str(ex)})


# ── Slate Capture & Parlays ───────────────────────────────────────────────────
@app.route('/api/capture-daily-slate/<date_str>')
def api_capture_daily_slate(date_str):
    """Capture all AI projections for the day as a slate snapshot."""
    try:
        year = datetime.now().year
        # Get all games for this date
        raw = fetch_schedule(date_str)
        if not raw:
            return jsonify({'success': False, 'error': 'No games found for this date', 'slate': None})
        
        slate = {
            'date': date_str,
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'games': [],
            'summary': {'total_games': len(raw), 'projections_captured': 0}
        }
        
        # Fetch AI projections for each game
        for game in raw:
            game_pk = game.get('gamePk')
            if not game_pk:
                continue
            
            try:
                ai_data = _get_ai_boxscore_data(game_pk)
                if ai_data.get('success'):
                    slate['games'].append({
                        'gamePk': game_pk,
                        'matchup': ai_data.get('matchup'),
                        'away_team': game.get('teams', {}).get('away', {}).get('team', {}).get('name'),
                        'home_team': game.get('teams', {}).get('home', {}).get('team', {}).get('name'),
                        'projections': ai_data.get('projections'),
                        'weather': ai_data.get('weather'),
                        'venue': ai_data.get('venue'),
                        'pitching': ai_data.get('pitching_matchup'),
                        'captured_at': datetime.now(timezone.utc).isoformat()
                    })
                    slate['summary']['projections_captured'] += 1
            except Exception as ex:
                print(f"[capture_slate] Failed to get AI projections for game {game_pk}: {traceback.format_exc()}")
                continue
        
        # Store slate in data directory
        slate_dir = os.path.join(DATA_DIR, 'slates')
        os.makedirs(slate_dir, exist_ok=True)
        slate_file = os.path.join(slate_dir, f"{date_str}_slate.json")
        with open(slate_file, 'w') as f:
            json.dump(slate, f, indent=2)
        
        return jsonify({
            'success': True,
            'slate': slate,
            'saved_to': slate_file
        })
    except Exception as ex:
        print(f"[capture_daily_slate] {traceback.format_exc()}")
        return jsonify({'success': False, 'error': str(ex), 'slate': None}), 500


@app.route('/api/parlay/build', methods=['POST'])
def api_build_parlay():
    """Build a parlay from selected game props."""
    try:
        req = request.get_json() or {}
        selections = req.get('selections', [])  # List of {game_pk, player, market, projection, side}
        
        if not selections:
            return jsonify({'success': False, 'error': 'No selections provided', 'parlay': None})
        
        parlay = {
            'id': datetime.now().isoformat().replace(':', '').replace('.', ''),
            'created_at': datetime.now(timezone.utc).isoformat(),
            'selections': [],
            'implied_odds': 1.0,
            'american_odds': 100,
            'summary': {'leg_count': 0, 'break_even_prob': 0.0}
        }
        
        # Build each leg
        prob_product = 1.0
        for sel in selections:
            game_pk = sel.get('game_pk')
            player = sel.get('player')
            market = sel.get('market')
            raw_proj = sel.get('projection', 0)
            try:
                # Handle 'N/A', None, or other non-numeric projections
                if raw_proj in (None, '', '-.--', '.---', 'N/A'):
                    projection = 0.0
                else:
                    projection = float(raw_proj)
            except Exception:
                projection = 0.0
            side = sel.get('side', 'Over')
            
            # Estimate win probability
            if market in ('batter_hits', 'batter_home_runs', 'batter_rbis'):
                # Simple heuristic: projection value → probability
                base_prob = 0.55 if projection >= 0.5 else 0.45
            elif market == 'pitcher_strikeouts':
                base_prob = 0.56 if projection >= 6.5 else 0.45
            else:
                base_prob = 0.52
            
            parlay['selections'].append({
                'game_pk': game_pk,
                'player': player,
                'market': market,
                'projection': projection,
                'side': side,
                'win_probability': base_prob,
                'american_odds': _prob_to_american(base_prob)
            })
            
            prob_product *= base_prob
        
        # Calculate parlay odds
        parlay['summary']['leg_count'] = len(parlay['selections'])
        parlay['summary']['break_even_prob'] = round(prob_product, 4)
        parlay['implied_odds'] = round(prob_product, 4)
        parlay['american_odds'] = _prob_to_american(prob_product)
        
        return jsonify({
            'success': True,
            'parlay': parlay
        })
    except Exception as ex:
        print(f"[build_parlay] {traceback.format_exc()}")
        return jsonify({'success': False, 'error': str(ex), 'parlay': None}), 500


@app.route('/api/parlay/send-to-tracker', methods=['POST'])
def api_parlay_to_tracker():
    """Send a parlay to the daily tracker."""
    try:
        req = request.get_json() or {}
        parlay = req.get('parlay')
        date_str = req.get('date', datetime.now().strftime('%Y-%m-%d'))
        notes = req.get('notes', '')
        
        if not parlay:
            return jsonify({'success': False, 'error': 'No parlay provided'})
        
        # Create tracker entries for this parlay
        store = _tracker_store()
        day_entries = store.get(date_str, {'entries': []}).get('entries', [])
        
        parlay_entry = {
            'id': parlay.get('id'),
            'date': date_str,
            'type': 'parlay',
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'selections': parlay.get('selections', []),
            'american_odds': parlay.get('american_odds'),
            'break_even_prob': parlay.get('summary', {}).get('break_even_prob'),
            'notes': notes,
            'status': 'pending',
            'grade': 'pending'
        }
        
        day_entries.append(parlay_entry)
        store[date_str] = {'entries': day_entries}
        
        # Save to file
        with open(TRACKER_STORE, 'w') as f:
            json.dump(store, f, indent=2)
        
        return jsonify({
            'success': True,
            'message': f'Parlay {parlay.get("id")} added to tracker for {date_str}',
            'entry_id': parlay.get('id')
        })
    except Exception as ex:
        print(f"[parlay_to_tracker] {traceback.format_exc()}")
        return jsonify({'success': False, 'error': str(ex)}), 500


@app.route('/api/model-upgrade/suggestions/<date_str>')
def api_model_upgrade_suggestions(date_str):
    """Get model upgrade suggestions based on daily performance grades."""
    try:
        store = _tracker_store()
        day = store.get(date_str, {})
        entries = day.get('entries', [])
        
        # Analyze performance
        graded = [e for e in entries if e.get('grade') in ('win', 'loss', 'push')]
        if not graded:
            return jsonify({
                'success': True,
                'date': date_str,
                'suggestions': [],
                'message': 'No graded entries yet for this date'
            })
        
        wins = sum(1 for e in graded if e.get('grade') == 'win')
        losses = sum(1 for e in graded if e.get('grade') == 'loss')
        hit_rate = round(wins / max(1, wins + losses), 3)
        
        suggestions = []
        
        # Rule 1: High hit rate (>60%) → increase model confidence
        if hit_rate >= 0.60:
            suggestions.append({
                'type': 'increase_confidence',
                'title': 'Boost Model Confidence',
                'description': f'Hit rate of {hit_rate*100:.1f}% suggests model is accurate. Increase confidence weighting.',
                'impact': 'Higher conviction on similar projections',
                'priority': 'high'
            })
        
        # Rule 2: Low hit rate (<40%) → decrease model confidence
        elif hit_rate < 0.40:
            suggestions.append({
                'type': 'decrease_confidence',
                'title': 'Reduce Model Confidence',
                'description': f'Hit rate of {hit_rate*100:.1f}% indicates model needs calibration. Reduce weight.',
                'impact': 'Lower conviction, increase filtering threshold',
                'priority': 'high'
            })
        
        # Rule 3: Specific market underperformance
        market_performance = {}
        for e in graded:
            if e.get('type') == 'parlay':
                for sel in e.get('selections', []):
                    mk = sel.get('market')
                    market_performance.setdefault(mk, {'wins': 0, 'losses': 0})
                    if e.get('grade') == 'win':
                        market_performance[mk]['wins'] += 1
                    else:
                        market_performance[mk]['losses'] += 1
        
        for market, perf in market_performance.items():
            total = perf['wins'] + perf['losses']
            mk_hit_rate = perf['wins'] / max(1, total)
            if mk_hit_rate < 0.35 and total >= 3:
                suggestions.append({
                    'type': 'market_adjustment',
                    'title': f'Adjust {market}',
                    'description': f'{market} has {mk_hit_rate*100:.1f}% hit rate across {total} bets.',
                    'impact': f'Consider skipping or reducing {market} projections',
                    'priority': 'medium'
                })
        
        return jsonify({
            'success': True,
            'date': date_str,
            'performance': {
                'graded': len(graded),
                'wins': wins,
                'losses': losses,
                'hit_rate': hit_rate
            },
            'suggestions': suggestions
        })
    except Exception as ex:
        print(f"[model_upgrade_suggestions] {traceback.format_exc()}")
        return jsonify({'success': False, 'error': str(ex)}), 500


def _prob_to_american(probability):
    """Convert decimal probability to American odds."""
    if probability <= 0 or probability >= 1:
        return 0
    if probability >= 0.5:
        return round(-100 * probability / (1 - probability))
    else:
        return round(100 * (1 - probability) / probability)


def _local_boxscore_projections(game_pk, context, away_bats, home_bats, ap_name, hp_name,
                                ap_fg, hp_fg, ap_sv, hp_sv, ap_stats, hp_stats, pf, wx,
                                away_t, home_t):
    def parse_float(value, fallback=0.0):
        try:
            return float(value)
        except Exception:
            return fallback

    def best_era(sv, fg, mlb):
        # FIX: Require min 15 IP before trusting xERA (avoids April small-sample noise)
        xera = sv.get('sv_xera')
        ip = fg.get('fg_ip', 0) or 0
        if xera and float(ip or 0) >= 15:
            try:
                f = float(xera)
                if 0 < f < 12:
                    return f
            except Exception:
                pass
        for v in (fg.get('fg_era'), mlb.get('era')):
            try:
                f = float(v)
                if 0 < f < 12:
                    return f
            except Exception:
                pass
        return 4.50

    def best_fip(fg, fallback):
        try:
            f = float(fg.get('fg_fip', 0))
            if 0 < f < 12:
                return f
        except Exception:
            pass
        return fallback

    def lineup_xwoba(bats):
        vals = []
        for b in bats:
            if not b:
                continue
            for key in ('sv_xwoba', 'fg_woba'):
                try:
                    f = parse_float(b.get(key, 0))
                except Exception:
                    f = 0.0
                if 0.1 < f < 0.6:
                    vals.append(f)
                    break
            else:
                vals.append(0.320)
        return round(sum(vals) / len(vals), 3) if vals else 0.320

    away_pit_era = best_era(hp_sv, hp_fg, hp_stats)
    home_pit_era = best_era(ap_sv, ap_fg, ap_stats)
    away_pit_fip = best_fip(hp_fg, away_pit_era)
    home_pit_fip = best_fip(ap_fg, home_pit_era)
    away_xwoba = lineup_xwoba(away_bats)
    home_xwoba = lineup_xwoba(home_bats)

    away_blend = 0.6 * away_pit_era + 0.4 * away_pit_fip
    home_blend = 0.6 * home_pit_era + 0.4 * home_pit_fip
    away_runs = 4.50 * (away_blend / 4.50) * (away_xwoba / 0.320) * pf  # FIX: corrected ERA direction
    home_runs = 4.50 * (home_blend / 4.50) * (home_xwoba / 0.320) * pf  # FIX: corrected ERA direction

    wx_adj = 0.0
    if not wx.get('dome'):
        try:
            temp = float(wx.get('temp', '70'))
            if temp > 82:
                wx_adj = 0.20
            elif temp > 76:
                wx_adj = 0.10
            elif temp < 48:
                wx_adj = -0.20
            elif temp < 56:
                wx_adj = -0.10
        except Exception:
            pass

    away_runs = round(max(2.0, min(8.0, away_runs + wx_adj)), 1)
    home_runs = round(max(2.0, min(8.0, home_runs + wx_adj)), 1)
    total_runs = round(away_runs + home_runs, 1)

    def build_reasoning(team_abbr, runs, xwoba, pitcher_name, opponent_name):
        return (
            f"{team_abbr} should score {runs} runs against {opponent_name} given the matchup, "
            f"their lineup xwOBA of {xwoba:.3f}, and the current weather/park profile."
        )

    away_reasoning = build_reasoning(context['away_abbr'], away_runs, away_xwoba,
                                     context['away_pitcher']['name'], context['home_pitcher']['name'])
    home_reasoning = build_reasoning(context['home_abbr'], home_runs, home_xwoba,
                                     context['home_pitcher']['name'], context['away_pitcher']['name'])

    confidence = 'HIGH' if total_runs > 10 or total_runs < 7 else 'MEDIUM'
    if not away_bats or not home_bats:
        confidence = 'LOW'

    def top_batter_props(bats, side):
        scored = []
        for b in bats:
            name = b.get('name')
            if not name:
                continue
            woba = parse_float(b.get('sv_xwoba') or b.get('fg_woba') or 0.320)
            hr = parse_float(b.get('hr') or b.get('fg_hr') or 0)
            avg = parse_float(b.get('avg') or b.get('fg_avg') or 0.240)
            scored.append((woba, name, avg, hr))
        return sorted(scored, key=lambda x: x[0], reverse=True)[:2]

    props = []
    for bats, side in ((away_bats, context['away_abbr']), (home_bats, context['home_abbr'])):
        best = top_batter_props(bats, side)
        if not best:
            continue
        name = best[0][1]
        woba = best[0][0]
        hr = best[0][3]
        if hr > 0:
            projection = round(min(0.42, 0.14 + woba * 0.25), 3)
            prop_type = 'hr'
            reasoning = f"{name} has strong contact and power metrics against this matchup."
        else:
            projection = round(min(0.76, max(0.32, woba * 2.0)), 3)
            prop_type = 'hits'
            reasoning = f"{name} profiles as a top contact bat with a good chance for multiple hits."
        props.append({
            'player': name,
            'prop': prop_type,
            'projection': projection,
            'reasoning': reasoning
        })

    for pitcher_name, pitcher_data in ((ap_name, ap_fg), (hp_name, hp_fg)):
        k9 = parse_float(pitcher_data.get('fg_k9') or pitcher_data.get('k9') or 6.0)
        k_prob = round(min(0.76, max(0.40, k9 / 9 * 0.72 + 0.18)), 3)
        props.append({
            'player': pitcher_name,
            'prop': 'k',
            'projection': k_prob,
            'reasoning': f"{pitcher_name} is likely to generate strikeouts based on his K/9 profile."
        })

    return {
        'away_runs': int(round(away_runs)),
        'home_runs': int(round(home_runs)),
        'away_hits': int(round(max(6, min(15, away_runs * 1.8)))),
        'home_hits': int(round(max(6, min(15, home_runs * 1.8)))),
        'total_runs': int(round(total_runs)),
        'away_reasoning': away_reasoning,
        'home_reasoning': home_reasoning,
        'key_factors': [
            f"Pitching matchup: {context['away_pitcher']['name']} vs {context['home_pitcher']['name']}",
            f"Lineup strength: {context['away_abbr']} xwOBA {away_xwoba}, {context['home_abbr']} xwOBA {home_xwoba}",
            f"Weather/Park: {wx.get('temp', 'N/A')}°F, {wx.get('condition', 'N/A')} at PF {pf}"
        ],
        'confidence': confidence,
        'notable_props': props[:3]
    }


def _get_ai_boxscore_data(game_pk):
    try:
        result = api_ai_boxscore(game_pk)
        if isinstance(result, tuple):
            result = result[0]
        if hasattr(result, 'get_json'):
            return result.get_json()
        return result
    except Exception as ex:
        print(f"[internal_ai_boxscore] {traceback.format_exc()}")
        return {'success': False, 'error': str(ex), 'projections': None}


@app.route('/api/ai-boxscore/<int:game_pk>')
def api_ai_boxscore(game_pk):
    """AI-powered box score projections using weather, player stats, and recent performance."""
    try:
        # Fetch game data
        gdata = fetch_schedule_game(game_pk)
        if not gdata:
            return jsonify({'success': False, 'error': 'Game not found', 'projections': None})
        
        # Team & weather info
        away_t = gdata.get("teams",{}).get("away",{})
        home_t = gdata.get("teams",{}).get("home",{})
        away_name = away_t.get("team",{}).get("name","Unknown")
        home_name = home_t.get("team",{}).get("name","Unknown")
        away_abbr = away_t.get("team",{}).get("abbreviation","AWAY")
        home_abbr = home_t.get("team",{}).get("abbreviation","HOME")
        
        # Weather
        ven = gdata.get("venue", {})
        venue_name = ven.get("name", "Unknown")
        venue_id = ven.get("id")
        vloc = ven.get("location", {}) or {}
        coords = vloc.get("defaultCoordinates", {}) or {}
        lat = coords.get("latitude")
        lon = coords.get("longitude")
        try:
            dt_utc_wx = datetime.fromisoformat(gdata.get("gameDate","").replace("Z","+00:00"))
            game_hour = dt_utc_wx.astimezone(ET).hour
        except Exception:
            game_hour = 13
        
        wx = get_weather(lat, lon, game_hour, venue_id=venue_id)
        if wx.get('temp') in (None, 'N/A'):
            raw_weather = gdata.get('weather', {}) or {}
            if raw_weather:
                wx = {
                    'temp': raw_weather.get('temp', 'N/A'),
                    'condition': raw_weather.get('condition', 'N/A'),
                    'wind': raw_weather.get('wind', 'N/A'),
                }
        
        # Pitchers
        ap = away_t.get("probablePitcher",{}); hp = home_t.get("probablePitcher",{})
        ap_name = ap.get("fullName","TBD"); hp_name = hp.get("fullName","TBD")
        ap_id = ap.get("id"); hp_id = hp.get("id")
        
        # Get pitcher stats
        ap_stats = pitcher_stats_mlb(ap_id) if ap_id else {}
        hp_stats = pitcher_stats_mlb(hp_id) if hp_id else {}
        ap_fg = fg_pitcher(ap_name); hp_fg = fg_pitcher(hp_name)
        ap_sv = sv_pitcher(ap_name); hp_sv = sv_pitcher(hp_name)
        
        # Get lineups
        try:
            r = requests.get(f"{MLB_API}/game/{game_pk}/boxscore", timeout=10)
            r.raise_for_status()
            box = r.json().get("teams",{})
            away_bats = get_batters_from_boxscore(box.get("away",{}), "away")
            home_bats = get_batters_from_boxscore(box.get("home",{}), "home")
        except Exception as ex:
            print(f'[ai_boxscore] boxscore fetch failed for {game_pk}: {ex}')
            away_bats = []; home_bats = []

        # Park factor
        hid = home_t.get("team",{}).get("id")
        pf = PARK_FACTORS.get(hid, 1.0)
        
        # Build context for AI
        context = {
            'away_team': away_name,
            'away_abbr': away_abbr,
            'home_team': home_name,
            'home_abbr': home_abbr,
            'weather': {
                'temp': wx.get('temp', 'N/A'),
                'condition': wx.get('condition', ''),
                'wind': wx.get('wind', 'N/A'),
            },
            'venue': {
                'name': venue_name,
                'park_factor': pf,
            },
            'away_pitcher': {
                'name': ap_name,
                'era': ap_fg.get('fg_era') or ap_stats.get('era', 'N/A'),
                'whip': ap_fg.get('fg_whip') or ap_stats.get('whip', 'N/A'),
                'k9': ap_fg.get('fg_k9') or ap_stats.get('k9', 'N/A'),
                'xera': ap_sv.get('sv_xera', 'N/A'),
            },
            'home_pitcher': {
                'name': hp_name,
                'era': hp_fg.get('fg_era') or hp_stats.get('era', 'N/A'),
                'whip': hp_fg.get('fg_whip') or hp_stats.get('whip', 'N/A'),
                'k9': hp_fg.get('fg_k9') or hp_stats.get('k9', 'N/A'),
                'xera': hp_sv.get('sv_xera', 'N/A'),
            },
            'away_lineup': [
                {
                    'slot': b.get('slot'),
                    'name': b.get('name'),
                    'pos': b.get('pos'),
                    'avg': b.get('avg'),
                    'obp': b.get('obp'),
                    'slg': b.get('slg'),
                    'woba': b.get('fg_woba'),
                    'xwoba': b.get('sv_xwoba'),
                    'ev': b.get('sv_ev'),
                    'hr': b.get('hr'),
                }
                for b in away_bats[:9]
            ],
            'home_lineup': [
                {
                    'slot': b.get('slot'),
                    'name': b.get('name'),
                    'pos': b.get('pos'),
                    'avg': b.get('avg'),
                    'obp': b.get('obp'),
                    'slg': b.get('slg'),
                    'woba': b.get('fg_woba'),
                    'xwoba': b.get('sv_xwoba'),
                    'ev': b.get('sv_ev'),
                    'hr': b.get('hr'),
                }
                for b in home_bats[:9]
            ],
        }

        ai_projections = None
        claude_error = None
        try:
            import anthropic
            api_key = os.environ.get('ANTHROPIC_API_KEY')
            if api_key:
                client = anthropic.Anthropic(api_key=api_key)
                prompt = (
                    f"Game: {context['away_team']} ({context['away_pitcher']['name']}, ERA {context['away_pitcher']['era']}) "
                    f"at {context['home_team']} ({context['home_pitcher']['name']}, ERA {context['home_pitcher']['era']}).\n"
                    f"Venue: {context['venue']['name']} (park factor {context['venue']['park_factor']}).\n"
                    f"Weather: {context['weather']['temp']}°F, {context['weather']['condition']}, wind {context['weather']['wind']}.\n"
                    f"Away lineup xwOBA top 3: " +
                    ", ".join(f"{b['name']} {b.get('xwoba','N/A')}" for b in context['away_lineup'][:3]) + ".\n"
                    f"Home lineup xwOBA top 3: " +
                    ", ".join(f"{b['name']} {b.get('xwoba','N/A')}" for b in context['home_lineup'][:3]) + ".\n"
                    "Return JSON with keys: away_runs (int), home_runs (int), away_hits (int), home_hits (int), "
                    "total_runs (int), away_reasoning (str), home_reasoning (str), key_factors (list of str), "
                    "confidence (HIGH|MEDIUM|LOW), notable_props (list of {player, prop, projection, reasoning})."
                )
                response = client.messages.create(
                    model="claude-sonnet-4-20250514",
                    max_tokens=2000,
                    temperature=0.7,
                    system="You are an expert MLB analyst providing detailed game and player projections based on comprehensive statistical analysis. Respond ONLY with valid JSON. No preamble, no markdown, no backticks — raw JSON only.",
                    messages=[{"role": "user", "content": prompt}]
                )
                response_text = response.content[0].text
                import json as json_lib
                clean = response_text.strip().lstrip('```json').lstrip('```').rstrip('```').strip()
                json_start = clean.find('{')
                json_end = clean.rfind('}') + 1
                if json_start >= 0 and json_end > json_start:
                    ai_projections = json_lib.loads(clean[json_start:json_end])
                else:
                    raise ValueError('Unable to parse Claude JSON response')
            else:
                claude_error = 'ANTHROPIC_API_KEY not configured'
        except Exception as ex:
            claude_error = str(ex)

        if ai_projections is None:
            ai_projections = _local_boxscore_projections(
                game_pk, context, away_bats, home_bats, ap_name, hp_name,
                ap_fg, hp_fg, ap_sv, hp_sv, ap_stats, hp_stats, pf, wx,
                away_t, home_t
            )
            ai_projections['source'] = 'local_fallback'
            ai_projections['fallback_reason'] = claude_error or 'Claude unavailable'
        
        return jsonify({
            'success': True,
            'gamePk': game_pk,
            'matchup': f"{away_abbr} vs {home_abbr}",
            'weather': context['weather'],
            'venue': context['venue'],
            'pitching_matchup': {
                'away_pitcher': context['away_pitcher'],
                'home_pitcher': context['home_pitcher']
            },
            'projections': ai_projections,
            'timestamp': datetime.now(timezone.utc).isoformat(),
        })
        
    except Exception as ex:
        print(f"[api_ai_boxscore] {game_pk}: {traceback.format_exc()}")
        return jsonify({
            'success': False,
            'error': str(ex),
            'projections': None
        }), 500

# ── Shared helper: fetch game + lineup ───────────────────────────────────────


# ── Platoon blend helper ──────────────────────────────────────────────────────





# ── Pitcher recent form cache ─────────────────────────────────────────────────
_pitcher_recent_cache = {}   # pid → {ts, era_recent, k9_recent, whip_recent, starts}



# ── Pitcher projection engine (v2 — recent form weighted) ────────────────────




# ── Matchup scoring engine (v2 — platoon-aware) ───────────────────────────────


# ── Route: Prop projections ───────────────────────────────────────────────────








# ── Route: Matchup scores (v2 — platoon-aware) ────────────────────────────────


# ── Route: Tracker entries for the value bets panel ───────────────────────────


from concurrent.futures import ThreadPoolExecutor, as_completed

# ── League averages (2025 baseline) ──────────────────────────────────────────
LG_K_PER_TEAM_PER_GAME  = 8.5   # strikeouts per team per game
LG_BB_PER_TEAM_PER_GAME = 3.0
LG_R_PER_TEAM_PER_GAME  = 4.5
LG_TOTAL_K_PER_GAME     = 17.0  # both teams combined

# ── Umpire cache ──────────────────────────────────────────────────────────────
_ump_lock  = threading.Lock()
_ump_cache = {}   # ump_id (int) → {"data": {...}, "date": date}

# Common prop lines to score trends against
BATTER_LINES = {
    "hits":  [0.5, 1.5, 2.5],
    "hr":    [0.5],
    "tb":    [1.5, 2.5, 3.5],
    "rbi":   [0.5, 1.5],
}
PITCHER_LINES = {
    "k":   [3.5, 4.5, 5.5, 6.5, 7.5],
    "bb":  [1.5, 2.5],
}


# ── Helper: fetch schedule with officials hydration ───────────────────────────
def _fetch_schedule_with_officials(start_date, end_date):
    """Returns all regular-season games in a date range with umpire data."""
    try:
        r = requests.get(f"{MLB_API}/schedule", params={
            "sportId": 1,
            "startDate": start_date,
            "endDate": end_date,
            "hydrate": "officials,linescore",
            "gameType": "R",
        }, timeout=20)
        r.raise_for_status()
        games = []
        for d in r.json().get("dates", []):
            games.extend(d.get("games", []))
        return games
    except Exception as ex:
        print(f"[ump_schedule] {ex}")
        return []


def _get_hp_umpire(game):
    """Extract home plate umpire from a game dict (needs officials hydration)."""
    for off in game.get("officials", []):
        if off.get("officialType") == "Home Plate":
            return off.get("official", {})
    return {}


def _fetch_boxscore_ump_stats(game_pk):
    """Fetch K/BB totals from a single boxscore."""
    try:
        r = requests.get(f"{MLB_API}/game/{game_pk}/boxscore", timeout=8)
        r.raise_for_status()
        teams = r.json().get("teams", {})
        total_k = 0; total_bb = 0; total_r = 0
        for side in ("away", "home"):
            t = teams.get(side, {})
            ts = t.get("teamStats", {})
            bat = ts.get("batting", {})
            pit = ts.get("pitching", {})
            total_k  += int(pit.get("strikeOuts", 0))
            total_bb += int(pit.get("baseOnBalls", 0))
            total_r  += int(bat.get("runs", 0))
        return {"pk": game_pk, "k": total_k, "bb": total_bb, "r": total_r, "ok": True}
    except Exception as ex:
        return {"pk": game_pk, "ok": False, "error": str(ex)}


def _build_ump_stats(ump_id, game_pks):
    """
    Given a list of gamePks where this umpire was HP, fetch boxscores concurrently
    and compute K/BB/run averages + zone rating.
    """
    pks = game_pks[-20:]   # limit to last 20 for performance
    results = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(_fetch_boxscore_ump_stats, pk): pk for pk in pks}
        for fut in as_completed(futs):
            res = fut.result()
            if res.get("ok"):
                results.append(res)

    if not results:
        return None

    n          = len(results)
    avg_k      = round(sum(r["k"] for r in results) / n, 1)
    avg_bb     = round(sum(r["bb"] for r in results) / n, 1)
    avg_r      = round(sum(r["r"] for r in results) / n, 1)

    # Per-team averages
    avg_k_per_team  = round(avg_k / 2, 1)
    avg_bb_per_team = round(avg_bb / 2, 1)
    avg_r_per_team  = round(avg_r / 2, 1)

    # vs league average (delta)
    k_vs_avg  = round(avg_k_per_team  - LG_K_PER_TEAM_PER_GAME,  1)
    bb_vs_avg = round(avg_bb_per_team - LG_BB_PER_TEAM_PER_GAME, 1)
    r_vs_avg  = round(avg_r_per_team  - LG_R_PER_TEAM_PER_GAME,  1)

    # Zone rating: 0–100.  Higher = more pitcher-friendly (more K, fewer runs)
    # Formula: 50 baseline + k bonus (up to +25) + run penalty (up to -25)
    k_score  = min(25, max(-25, k_vs_avg  *  4.0))
    r_score  = min(25, max(-25, r_vs_avg  * -3.0))
    bb_score = min(10, max(-10, bb_vs_avg * -2.5))
    zone_raw = 50 + k_score + r_score + bb_score
    zone     = int(min(100, max(0, round(zone_raw))))

    if zone >= 65:
        tendency = "PITCHER FRIENDLY"
        tendency_color = "var(--m)"
    elif zone <= 35:
        tendency = "HITTER FRIENDLY"
        tendency_color = "var(--g)"
    else:
        tendency = "NEUTRAL ZONE"
        tendency_color = "var(--mu)"

    return {
        "games_sampled":    n,
        "avg_total_k":      avg_k,
        "avg_k_per_team":   avg_k_per_team,
        "avg_total_bb":     avg_bb,
        "avg_bb_per_team":  avg_bb_per_team,
        "avg_total_r":      avg_r,
        "avg_r_per_team":   avg_r_per_team,
        "k_vs_avg":         f"+{k_vs_avg}" if k_vs_avg >= 0 else str(k_vs_avg),
        "bb_vs_avg":        f"+{bb_vs_avg}" if bb_vs_avg >= 0 else str(bb_vs_avg),
        "r_vs_avg":         f"+{r_vs_avg}"  if r_vs_avg  >= 0 else str(r_vs_avg),
        "zone_rating":      zone,
        "tendency":         tendency,
        "tendency_color":   tendency_color,
    }


def _load_ump_data(ump_id, ump_name):
    """Full umpire history load — called once per ump per day, cached."""
    today     = datetime.now(ET).date()
    season    = today.year
    start     = f"{season}-03-01"
    end_dt    = today - timedelta(days=1)
    end       = end_dt.strftime("%Y-%m-%d")

    games = _fetch_schedule_with_officials(start, end)

    # Filter games where this ump was HP
    hp_pks = []
    for g in games:
        u = _get_hp_umpire(g)
        if u.get("id") == ump_id:
            hp_pks.append(g["gamePk"])

    if not hp_pks:
        return None

    stats = _build_ump_stats(ump_id, hp_pks)
    if stats:
        stats["name"]     = ump_name
        stats["id"]       = ump_id
        stats["games_hp"] = len(hp_pks)
    return stats


def _get_cached_ump(ump_id, ump_name):
    today = datetime.now(ET).date()
    with _ump_lock:
        cached = _ump_cache.get(ump_id)
    if cached and cached.get("date") == today:
        return cached["data"]
    # Load in background — return None if not ready yet (caller handles gracefully)
    def _loader():
        data = _load_ump_data(ump_id, ump_name)
        if data:
            with _ump_lock:
                _ump_cache[ump_id] = {"data": data, "date": today}
        print(f"[ump_cache] loaded {ump_name} id={ump_id}: {data}")
    threading.Thread(target=_loader, daemon=True).start()
    return None


# ── Route: Umpire data for a game ─────────────────────────────────────────────
@app.route("/api/umpire/<int:game_pk>")
def api_umpire(game_pk):
    """
    Returns home plate umpire assignment + historical K/BB/run tendencies.
    On first call the cache is being built — returns loading=True so the
    frontend can poll once more after 3 seconds.
    """
    try:
        # Search today ± 1 day
        for delta in (0, -1, 1):
            date_str = (datetime.now(ET) + timedelta(days=delta)).strftime("%Y-%m-%d")
            games = _fetch_schedule_with_officials(date_str, date_str)
            gdata = next((g for g in games if g.get("gamePk") == game_pk), None)
            if gdata:
                break

        if not gdata:
            return jsonify({"success": False, "error": "Game not found"}), 404

        ump = _get_hp_umpire(gdata)
        if not ump or not ump.get("id"):
            return jsonify({
                "success": True,
                "umpire": None,
                "message": "Umpire assignment not yet posted",
            })

        ump_id   = ump["id"]
        ump_name = ump.get("fullName", "Unknown")

        # Try cache first — kick off load if needed
        today = datetime.now(ET).date()
        with _ump_lock:
            cached = _ump_cache.get(ump_id)

        if cached and cached.get("date") == today:
            stats = cached["data"]
            return jsonify({
                "success": True,
                "loading": False,
                "umpire": {
                    "id":       ump_id,
                    "name":     ump_name,
                    **stats,
                },
            })

        # Not cached — start background load, return partial response
        _get_cached_ump(ump_id, ump_name)
        return jsonify({
            "success": True,
            "loading": True,
            "umpire": {
                "id":   ump_id,
                "name": ump_name,
                "zone_rating": None,
                "tendency": "LOADING",
                "games_sampled": 0,
            },
        })

    except Exception as ex:
        print(f"[api_umpire] {traceback.format_exc()}")
        return jsonify({"success": False, "error": str(ex)}), 500


# ── L5/L10 helpers ────────────────────────────────────────────────────────────
def _fetch_batter_gamelog(player_id, season=None, limit=10):
    """Returns recent game log entries for a batter, newest-last order."""
    if season is None:
        season = datetime.now().year
    try:
        r = requests.get(
            f"{MLB_API}/people/{player_id}/stats",
            params={"stats": "gameLog", "season": season, "group": "hitting", "gameType": "R"},
            timeout=8,
        )
        r.raise_for_status()
        splits = r.json().get("stats", [{}])[0].get("splits", [])
        out = []
        chosen = splits if limit is None else splits[-int(limit):]
        for s in chosen:
            st = s.get("stat", {})
            out.append({
                "date": s.get("date", ""),
                "opp":  s.get("opponent", {}).get("abbreviation", ""),
                "ab":   int(st.get("atBats", 0)),
                "h":    int(st.get("hits", 0)),
                "hr":   int(st.get("homeRuns", 0)),
                "rbi":  int(st.get("rbi", 0)),
                "bb":   int(st.get("baseOnBalls", 0)),
                "tb":   int(st.get("totalBases", 0)),
                "r":    int(st.get("runs", 0)),
                "sb":   int(st.get("stolenBases", 0)),
            })
        return out
    except Exception as ex:
        print(f"[batter_gamelog] pid={player_id} {ex}")
        return []


def _fetch_pitcher_gamelog(player_id, season=None, limit=10):
    """Returns recent game log entries for a pitcher, newest-last order."""
    if season is None:
        season = datetime.now().year
    try:
        r = requests.get(
            f"{MLB_API}/people/{player_id}/stats",
            params={"stats": "gameLog", "season": season, "group": "pitching", "gameType": "R"},
            timeout=8,
        )
        r.raise_for_status()
        splits = r.json().get("stats", [{}])[0].get("splits", [])
        out = []
        chosen = splits if limit is None else splits[-int(limit):]
        for s in chosen:
            st = s.get("stat", {})
            ip_raw = st.get("inningsPitched", "0.0")
            try:
                whole, third = str(ip_raw).split(".")
                ip_dec = int(whole) + int(third) / 3
            except Exception:
                ip_dec = _safe_f(ip_raw, 0)
            out.append({
                "date": s.get("date", ""),
                "opp":  s.get("opponent", {}).get("abbreviation", ""),
                "ip":   round(ip_dec, 2),
                "k":    int(st.get("strikeOuts", 0)),
                "bb":   int(st.get("baseOnBalls", 0)),
                "h":    int(st.get("hits", 0)),
                "er":   int(st.get("earnedRuns", 0)),
            })
        return out
    except Exception as ex:
        print(f"[pitcher_gamelog] pid={player_id} {ex}")
        return []


def _compute_over_rates(game_log, stat_key, lines):
    """
    For a list of game log dicts and a stat key (e.g. 'h', 'hr', 'k'),
    compute over% for each line across L5 and L10.
    Returns dict: { line (str) -> {"l5": {"over":n,"total":n,"pct":f}, "l10": {...}} }
    """
    vals = [g[stat_key] for g in game_log if stat_key in g]
    result = {}
    for line in lines:
        l10_vals = vals[-10:]
        l5_vals  = vals[-5:]
        def _rate(vs):
            over = sum(1 for v in vs if v > line)
            return {"over": over, "total": len(vs), "pct": round(over / len(vs), 3) if vs else None}
        result[str(line)] = {"l5": _rate(l5_vals), "l10": _rate(l10_vals)}
    return result


def _consistency_stat_value(row, market_key):
    if market_key == 'batter_hits':
        return int(row.get('h', 0) or 0)
    if market_key == 'batter_total_bases':
        return int(row.get('tb', 0) or 0)
    if market_key == 'batter_home_runs':
        return int(row.get('hr', 0) or 0)
    if market_key == 'batter_rbis':
        return int(row.get('rbi', 0) or 0)
    if market_key == 'batter_runs_scored':
        return int(row.get('r', 0) or 0)
    if market_key == 'batter_hits_runs_rbis':
        return int(row.get('h', 0) or 0) + int(row.get('r', 0) or 0) + int(row.get('rbi', 0) or 0)
    if market_key == 'batter_stolen_bases':
        return int(row.get('sb', 0) or 0)
    if market_key == 'pitcher_strikeouts':
        return int(row.get('k', 0) or 0)
    return None


def _consistency_window_summary(values, line, limit=None):
    sample = values if limit is None else values[-int(limit):]
    sample = [v for v in sample if v is not None]
    total = len(sample)
    if total == 0:
        return {'over': 0, 'total': 0, 'pct': None}
    over = sum(1 for v in sample if float(v) > float(line))
    return {'over': over, 'total': total, 'pct': round(over / total, 3)}


def _empty_consistency_payload(date_str):
    return {
        'success': True,
        'date': date_str,
        'generatedAt': datetime.now(timezone.utc).isoformat(),
        'cached': False,
        'cacheAgeSec': 0,
        'games': [],
        'teams': [],
        'slots': [],
        'markets': [],
    }


def _compute_consistency_payload(date_str):
    now = time.time()
    raw_games = fetch_schedule(date_str)
    parsed_games = [parse_game(g) for g in raw_games]
    parsed_games = [g for g in parsed_games if g]
    game_meta = {g.get('gamePk'): g for g in parsed_games}
    adjustments = _get_adjustments()
    rows = []

    def _consistency_game_rows(game):
        game_pk = game.get('gamePk')
        if not game_pk:
            return []
        try:
            return _build_tracker_rows_for_game(int(game_pk), date_str, adjustments, _sched=raw_games, include_odds=False) or []
        except Exception:
            print(f'[consistency_rows {game_pk}] {traceback.format_exc()}')
            return []

    with ThreadPoolExecutor(max_workers=min(6, max(1, len(raw_games)))) as _cex:
        for _game_rows in _cex.map(_consistency_game_rows, raw_games):
            rows.extend(_game_rows)

    supported_markets = {
        'batter_hits', 'batter_total_bases', 'batter_home_runs', 'batter_rbis',
        'batter_runs_scored', 'batter_hits_runs_rbis', 'batter_stolen_bases', 'pitcher_strikeouts'
    }
    deduped = {}
    for row in rows:
        market_key = row.get('marketKey')
        if market_key not in supported_markets:
            continue
        key = (row.get('playerId'), market_key, float(row.get('line') or 0))
        current = deduped.get(key)
        if current is None or float(row.get('hubRating') or 0) > float(current.get('hubRating') or 0):
            deduped[key] = dict(row)
    base_rows = list(deduped.values())

    player_tasks = {}
    for row in base_rows:
        player_id = row.get('playerId')
        if not player_id:
            continue
        player_tasks[player_id] = (row.get('marketKey') == 'pitcher_strikeouts')

    logs_by_player = {}
    with ThreadPoolExecutor(max_workers=12) as ex:
        fut_map = {}
        for player_id, is_pitcher in player_tasks.items():
            if is_pitcher:
                fut_map[ex.submit(_fetch_pitcher_gamelog, int(player_id), None, None)] = player_id
            else:
                fut_map[ex.submit(_fetch_batter_gamelog, int(player_id), None, None)] = player_id
        for fut in as_completed(fut_map):
            player_id = fut_map[fut]
            try:
                logs_by_player[player_id] = fut.result() or []
            except Exception:
                logs_by_player[player_id] = []

    market_labels = {
        'batter_hits': 'Hits', 'batter_total_bases': 'Total Bases', 'batter_home_runs': 'Home Runs',
        'batter_rbis': 'RBIs', 'batter_runs_scored': 'Runs', 'batter_hits_runs_rbis': 'H+R+RBI',
        'batter_stolen_bases': 'Stolen Bases', 'pitcher_strikeouts': 'Pitcher Strikeouts'
    }
    sheets = {mk: {'marketKey': mk, 'marketLabel': label, 'rows': []} for mk, label in market_labels.items()}
    teams = set()
    slots = set()
    game_filters = []
    for g in parsed_games:
        game_filters.append({'gamePk': g.get('gamePk'), 'label': f"{g.get('awayAbbr')} @ {g.get('homeAbbr')}"})

    for row in base_rows:
        player_id = row.get('playerId')
        market_key = row.get('marketKey')
        logs = logs_by_player.get(player_id, [])
        values = [_consistency_stat_value(log_row, market_key) for log_row in logs]
        values = [v for v in values if v is not None]
        game = game_meta.get(row.get('gamePk')) or {}
        slot = row.get('slot')
        teams.add(row.get('team') or '')
        if slot is not None:
            slots.add(int(slot))
        sheet_row = {
            'player': row.get('player'),
            'playerId': player_id,
            'team': row.get('team'),
            'opp': row.get('opp') or ((game.get('homeAbbr') if row.get('team') == game.get('awayAbbr') else game.get('awayAbbr')) if game else ''),
            'slot': slot,
            'gamePk': row.get('gamePk'),
            'gameLabel': f"{game.get('awayAbbr')} @ {game.get('homeAbbr')}" if game else '',
            'line': row.get('line'),
            'hubRating': row.get('hubRating'),
            'evPct': row.get('evPct'),
            'l5': _consistency_window_summary(values, row.get('line'), 5),
            'l10': _consistency_window_summary(values, row.get('line'), 10),
            'l20': _consistency_window_summary(values, row.get('line'), 20),
            'season': _consistency_window_summary(values, row.get('line'), None),
            'sampleSize': len(values),
        }
        sheets[market_key]['rows'].append(sheet_row)

    for market_key, sheet in sheets.items():
        sheet['rows'].sort(key=lambda x: (
            -(x.get('l10', {}).get('pct') if x.get('l10', {}).get('pct') is not None else -1),
            -(x.get('l5', {}).get('pct') if x.get('l5', {}).get('pct') is not None else -1),
            -(float(x.get('hubRating') or 0)),
            x.get('player') or ''
        ))

    payload = {
        'success': True,
        'date': date_str,
        'generatedAt': datetime.now(timezone.utc).isoformat(),
        'cached': False,
        'cacheAgeSec': 0,
        'games': game_filters,
        'teams': sorted(t for t in teams if t),
        'slots': sorted(slots),
        'markets': list(sheets.values()),
    }
    with _consistency_cache_lock:
        _CONSISTENCY_CACHE[date_str] = {'ts': now, 'payload': payload}
    return payload


def _trigger_consistency_refresh_async(date_str, reason='auto'):
    global _consistency_refreshing
    with _consistency_cache_lock:
        if _consistency_refreshing:
            return False
        _consistency_refreshing = True

    def _runner():
        global _consistency_refreshing
        try:
            _compute_consistency_payload(date_str)
            print(f'[consistency] refreshed ({reason})')
        except Exception:
            print(f'[consistency] refresh failed {traceback.format_exc()}')
        finally:
            with _consistency_cache_lock:
                _consistency_refreshing = False

    threading.Thread(target=_runner, daemon=True).start()
    return True


def _consistency_payload(date_str, refresh=False):
    now = time.time()
    with _consistency_cache_lock:
        cached = _CONSISTENCY_CACHE.get(date_str)
        refreshing = _consistency_refreshing
    ts = float((cached or {}).get('ts') or 0)
    age = int(now - ts) if ts else 0

    if cached and not refresh and (now - ts) < _CONSISTENCY_TTL:
        payload = dict(cached.get('payload') or {})
        payload['cacheAgeSec'] = age
        payload['cached'] = True
        payload['computing'] = False
        return payload

    if refresh:
        return _compute_consistency_payload(date_str)

    if cached:
        if not refreshing:
            _trigger_consistency_refresh_async(date_str, reason='stale_cache')
        payload = dict(cached.get('payload') or {})
        payload['cacheAgeSec'] = age
        payload['cached'] = True
        payload['computing'] = True
        payload['message'] = 'Refreshing in background'
        return payload

    if not refreshing:
        _trigger_consistency_refresh_async(date_str, reason='cold_start')
    payload = _empty_consistency_payload(date_str)
    payload['computing'] = True
    payload['message'] = 'Computing... auto-refresh in 20s'
    return payload


def _build_player_trends(player_id, is_pitcher):
    """Build the full trend dict for one player."""
    if is_pitcher:
        log  = _fetch_pitcher_gamelog(player_id)
        if not log:
            return {"log": [], "over_rates": {}, "streak": None}
        over_rates = {}
        for stat, lines in PITCHER_LINES.items():
            over_rates[stat] = _compute_over_rates(log, stat, lines)
        # Streak: consecutive games over/under 4.5 Ks
        streak = _compute_streak(log, "k", 4.5)
    else:
        log  = _fetch_batter_gamelog(player_id)
        if not log:
            return {"log": [], "over_rates": {}, "streak": None}
        over_rates = {}
        for stat, lines in BATTER_LINES.items():
            stat_key = {"hits": "h", "hr": "hr", "tb": "tb", "rbi": "rbi"}[stat]
            over_rates[stat] = _compute_over_rates(log, stat_key, lines)
        streak = _compute_streak(log, "h", 0.5)

    return {
        "log":        log,
        "over_rates": over_rates,
        "streak":     streak,
        "games":      len(log),
    }


def _compute_streak(log, stat_key, line):
    """
    Returns current consecutive over/under streak for the stat vs line.
    e.g. {"direction": "over", "length": 5}
    """
    if not log:
        return None
    vals   = [g.get(stat_key, 0) for g in log]
    if not vals:
        return None
    last_dir = "over" if vals[-1] > line else "under"
    length   = 0
    for v in reversed(vals):
        d = "over" if v > line else "under"
        if d == last_dir:
            length += 1
        else:
            break
    return {"direction": last_dir, "length": length}




# ── Route: L5/L10 trends for all players in a game ───────────────────────────


# ── Route: Quick props for dashboard inline strip ─────────────────────────────


# ── Route: Single player trends (used by deepdive player modal) ───────────────
@app.route("/api/player/trends/<int:player_id>")
def api_player_trends(player_id):
    """
    Returns L5/L10 trends for a single player.
    Accepts ?type=batter|pitcher (default: auto-detect from MLB API).
    """
    try:
        is_pitcher_param = request.args.get("type", "").lower()
        if is_pitcher_param == "pitcher":
            is_pitcher = True
        elif is_pitcher_param == "batter":
            is_pitcher = False
        else:
            # Auto-detect from MLB people endpoint
            try:
                r = requests.get(f"{MLB_API}/people/{player_id}", timeout=6)
                r.raise_for_status()
                pos = r.json().get("people", [{}])[0].get("primaryPosition", {}).get("code", "")
                is_pitcher = pos == "1"
            except Exception:
                is_pitcher = False

        data = _build_player_trends(player_id, is_pitcher)
        return jsonify({"success": True, "player_id": player_id, **data})

    except Exception as ex:
        print(f"[api_player_trends] {traceback.format_exc()}")
        return jsonify({"success": False, "error": str(ex)}), 500

from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed


# ── Helper: fetch team schedule for last N days ───────────────────────────────
def _team_recent_games(team_id, n_days=3):
    """Returns gamePks for a team's last n_days completed games."""
    today    = datetime.now(ET).date()
    start_dt = today - timedelta(days=n_days + 2)   # buffer for off-days
    try:
        r = requests.get(f"{MLB_API}/schedule", params={
            "sportId": 1, "teamId": team_id,
            "startDate": start_dt.strftime("%Y-%m-%d"),
            "endDate":   (today - timedelta(days=1)).strftime("%Y-%m-%d"),
            "gameType": "R",
        }, timeout=10)
        r.raise_for_status()
        games = []
        for d in r.json().get("dates", []):
            for g in d.get("games", []):
                status = g.get("status", {}).get("detailedState", "")
                if "Final" in status or "Completed" in status:
                    games.append({
                        "gamePk":  g["gamePk"],
                        "date":    d["date"],
                        "teamId":  team_id,
                    })
        return sorted(games, key=lambda x: x["date"], reverse=True)[:n_days]
    except Exception as ex:
        print(f"[team_recent_games] team={team_id}: {ex}")
        return []


def _bullpen_from_boxscore(game_pk, team_id):
    """
    Returns list of relievers who appeared for team_id in game_pk:
    {name, id, outs, pitches_est, date, days_ago}
    """
    try:
        r = requests.get(f"{MLB_API}/game/{game_pk}/boxscore", timeout=8)
        r.raise_for_status()
        box   = r.json().get("teams", {})
        today = datetime.now(ET).date()

        # Find team side
        for side in ("home", "away"):
            team_data = box.get(side, {})
            if team_data.get("team", {}).get("id") == team_id:
                players  = team_data.get("players", {})
                pitchers = team_data.get("pitchers", [])
                # First pitcher is the starter — skip
                reliever_ids = pitchers[1:] if len(pitchers) > 1 else []
                out = []
                for pid in reliever_ids:
                    pdata = players.get(f"ID{pid}", {})
                    name  = pdata.get("person", {}).get("fullName", "")
                    st    = pdata.get("stats", {}).get("pitching", {})
                    ip_s  = st.get("inningsPitched", "0.0")
                    try:
                        whole, thirds = str(ip_s).split(".")
                        outs = int(whole) * 3 + int(thirds)
                    except Exception:
                        outs = 0
                    pitches_est = max(0, int(outs * 5.2))   # ~5.2 pitches per out
                    out.append({
                        "id":          pid,
                        "name":        name,
                        "outs":        outs,
                        "pitches_est": pitches_est,
                        "game_pk":     game_pk,
                    })
                return out
    except Exception as ex:
        print(f"[bullpen_boxscore] game={game_pk} team={team_id}: {ex}")
    return []


def _build_bullpen_fatigue(team_id, team_abbr, recent_games):
    """
    Aggregates reliever appearances over last 3 games into a fatigue report.
    Returns dict with per-reliever status and team stress score (0–100).
    """
    today = datetime.now(ET).date()

    # Collect appearances concurrently
    appearances = {}   # reliever_id → {name, days_pitched: [0,1,2,...], pitches_by_day}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {
            ex.submit(_bullpen_from_boxscore, g["gamePk"], team_id): g
            for g in recent_games
        }
        for fut in _as_completed(futs):
            g       = futs[fut]
            game_dt = datetime.strptime(g["date"], "%Y-%m-%d").date()
            days_ago = (today - game_dt).days
            try:
                relievers = fut.result()
                for rel in relievers:
                    pid = rel["id"]
                    if pid not in appearances:
                        appearances[pid] = {
                            "name":        rel["name"],
                            "id":          pid,
                            "days_pitched": [],
                            "pitches":     [],
                            "outs":        [],
                        }
                    appearances[pid]["days_pitched"].append(days_ago)
                    appearances[pid]["pitches"].append(rel["pitches_est"])
                    appearances[pid]["outs"].append(rel["outs"])
            except Exception:
                pass

    # Build per-reliever status
    relievers_out = []
    total_stress  = 0.0

    for pid, data in appearances.items():
        if not data["name"]:
            continue
        days   = sorted(data["days_pitched"])
        pitches_total = sum(data["pitches"])
        outs_total    = sum(data["outs"])

        # Rest days since last appearance
        rest_days = days[0] if days else 3   # minimum days_ago

        # Status classification
        if rest_days == 1 and pitches_total >= 25:
            status = "GASSED"; status_col = "#f44336"; stress_pts = 22
        elif rest_days == 1 and pitches_total >= 10:
            status = "TIRED";  status_col = "#ff9800"; stress_pts = 14
        elif rest_days == 1:
            status = "LIGHT";  status_col = "#ffd740"; stress_pts = 8
        elif rest_days == 2 and pitches_total >= 40:
            status = "TIRED";  status_col = "#ff9800"; stress_pts = 10
        elif rest_days == 2:
            status = "OK";     status_col = "#00b8d4"; stress_pts = 4
        else:
            status = "FRESH";  status_col = "#00e676"; stress_pts = 0

        total_stress += stress_pts
        # Back-to-back penalty
        if len(days) >= 2 and days[0] == 1 and days[1] == 2:
            total_stress += 8

        relievers_out.append({
            "name":          data["name"],
            "id":            pid,
            "appearances":   len(days),
            "days_pitched":  days,
            "pitches_total": pitches_total,
            "outs_total":    outs_total,
            "rest_days":     rest_days,
            "status":        status,
            "status_color":  status_col,
        })

    # Sort: most fatigued first
    relievers_out.sort(key=lambda x: x["rest_days"])

    # Team stress score 0–100
    stress_score  = int(min(100, total_stress))
    stress_label  = "HIGH" if stress_score >= 60 else "MODERATE" if stress_score >= 30 else "FRESH"
    stress_color  = "#f44336" if stress_score >= 60 else "#ff9800" if stress_score >= 30 else "#00e676"

    return {
        "team_id":      team_id,
        "team_abbr":    team_abbr,
        "relievers":    relievers_out,
        "stress_score": stress_score,
        "stress_label": stress_label,
        "stress_color": stress_color,
        "games_sampled": len(recent_games),
    }


# ── Route: Bullpen Fatigue ────────────────────────────────────────────────────
@app.route("/api/bullpen/fatigue/<int:game_pk>")
def api_bullpen_fatigue(game_pk):
    """
    Returns bullpen fatigue status for both teams:
    per-reliever rest/pitch counts + team stress score.
    """
    try:
        # Find game
        gdata = None
        for delta in (0, -1, 1):
            ds    = (datetime.now(ET) + timedelta(days=delta)).strftime("%Y-%m-%d")
            raw   = fetch_schedule(ds)
            gdata = next((g for g in raw if g.get("gamePk") == game_pk), None)
            if gdata:
                break
        if not gdata:
            return jsonify({"success": False, "error": "Game not found"}), 404

        away_t    = gdata["teams"]["away"]["team"]
        home_t    = gdata["teams"]["home"]["team"]
        away_id   = away_t["id"];  away_abbr = away_t.get("abbreviation", "AWAY")
        home_id   = home_t["id"];  home_abbr = home_t.get("abbreviation", "HOME")

        # Fetch recent games for both teams concurrently
        with ThreadPoolExecutor(max_workers=2) as ex:
            away_fut = ex.submit(_team_recent_games, away_id, 3)
            home_fut = ex.submit(_team_recent_games, home_id, 3)
            away_recent = away_fut.result()
            home_recent = home_fut.result()

        # Build fatigue reports concurrently
        with ThreadPoolExecutor(max_workers=2) as ex:
            af = ex.submit(_build_bullpen_fatigue, away_id, away_abbr, away_recent)
            hf = ex.submit(_build_bullpen_fatigue, home_id, home_abbr, home_recent)
            away_fatigue = af.result()
            home_fatigue = hf.result()

        return jsonify({
            "success":  True,
            "gamePk":   game_pk,
            "away":     away_fatigue,
            "home":     home_fatigue,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    except Exception as ex:
        print(f"[api_bullpen_fatigue] {traceback.format_exc()}")
        return jsonify({"success": False, "error": str(ex)}), 500


# ── Route: First 5 Innings (F5) Model ────────────────────────────────────────
@app.route("/api/f5/<int:game_pk>")
def api_f5_model(game_pk):
    """
    Projects runs scored in the first 5 innings for each team.
    Uses starter ERA/FIP/xERA + lineup xwOBA + park factor + weather.
    """
    try:
        _maybe_refresh_fg()
        _maybe_refresh_savant()

        gdata = None
        for delta in (0, -1, 1):
            ds    = (datetime.now(ET) + timedelta(days=delta)).strftime("%Y-%m-%d")
            raw   = fetch_schedule(ds)
            gdata = next((g for g in raw if g.get("gamePk") == game_pk), None)
            if gdata:
                break
        if not gdata:
            return jsonify({"success": False, "error": "Game not found"}), 404

        away_t  = gdata["teams"]["away"]
        home_t  = gdata["teams"]["home"]
        home_id = home_t["team"]["id"]
        pf      = PARK_FACTORS.get(home_id, 1.0)

        # Starters
        ap_info = away_t.get("probablePitcher", {})
        hp_info = home_t.get("probablePitcher", {})
        ap_name = ap_info.get("fullName", "TBD")
        hp_name = hp_info.get("fullName", "TBD")
        ap_id   = ap_info.get("id");  hp_id = hp_info.get("id")

        ap_fg = fg_pitcher(ap_name); hp_fg = fg_pitcher(hp_name)
        ap_sv = sv_pitcher(ap_name); hp_sv = sv_pitcher(hp_name)
        ap_st = pitcher_stats_mlb(ap_id) if ap_id else {}
        hp_st = pitcher_stats_mlb(hp_id) if hp_id else {}

        def best_era(sv, fg, mlb):
            for v in [sv.get("sv_xera"), fg.get("fg_era"), mlb.get("era")]:
                try:
                    f = float(v)
                    if 0 < f < 12: return f
                except Exception:
                    pass
            return 4.20

        def best_fip(fg, fallback):
            try:
                f = float(fg.get("fg_fip", 0))
                if 0 < f < 12: return f
            except Exception:
                pass
            return fallback

        ap_era = best_era(ap_sv, ap_fg, ap_st)
        hp_era = best_era(hp_sv, hp_fg, hp_st)
        ap_fip = best_fip(ap_fg, ap_era)
        hp_fip = best_fip(hp_fg, hp_era)

        # Lineup quality from boxscore
        try:
            r = requests.get(f"{MLB_API}/game/{game_pk}/boxscore", timeout=8)
            r.raise_for_status()
            box       = r.json().get("teams", {})
            away_bats = get_batters_from_boxscore(box.get("away", {}), "away")
            home_bats = get_batters_from_boxscore(box.get("home", {}), "home")
        except Exception:
            away_bats = []; home_bats = []

        def lu_xwoba(bats):
            vals = []
            for b in bats:
                for k in ["sv_xwoba", "fg_woba"]:
                    try:
                        f = float(b.get(k, 0))
                        if 0.1 < f < 0.6:
                            vals.append(f); break
                    except Exception:
                        pass
                else:
                    vals.append(0.320)
            return round(sum(vals) / len(vals), 3) if vals else 0.320

        away_xwoba = lu_xwoba(away_bats)
        home_xwoba = lu_xwoba(home_bats)

        # Weather
        ven   = gdata.get("venue", {})
        vid   = ven.get("id")
        vloc  = (ven.get("location") or {})
        coord = (vloc.get("defaultCoordinates") or {})
        lat   = coord.get("latitude"); lon = coord.get("longitude")
        try:
            dt_utc = datetime.fromisoformat(gdata.get("gameDate", "").replace("Z", "+00:00"))
            ghour  = dt_utc.astimezone(ET).hour
        except Exception:
            ghour  = 13
        wx = get_weather(lat, lon, ghour, venue_id=vid)

        # F5 uses only first 5 innings (5/9 of full-game projection)
        # Blended ERA: 60% (xERA/ERA) + 40% FIP
        # away team faces home pitcher (hp)
        away_blend = 0.6 * hp_era + 0.4 * hp_fip
        home_blend = 0.6 * ap_era + 0.4 * ap_fip

        # Base runs model: 4.50 R/G avg, scaled to 5 innings (5/9)
        f5_scale  = 5.0 / 9.0
        # Park factor muted for F5 (less variance in 5 innings)
        pf_f5     = 1.0 + (pf - 1.0) * 0.65

        away_f5   = 4.50 * (4.20 / away_blend) * (away_xwoba / 0.320) * pf_f5 * f5_scale
        home_f5   = 4.50 * (4.20 / home_blend) * (home_xwoba / 0.320) * pf_f5 * f5_scale

        # Weather adj (muted for F5)
        wx_adj = 0.0
        if not wx.get("dome"):
            try:
                t = float(wx.get("temp", 70))
                if t > 82:   wx_adj =  0.08
                elif t > 76: wx_adj =  0.04
                elif t < 48: wx_adj = -0.08
                elif t < 56: wx_adj = -0.04
            except Exception:
                pass

        away_f5 = round(max(0.8, away_f5 + wx_adj), 2)
        home_f5 = round(max(0.8, home_f5 + wx_adj), 2)
        total_f5 = round(away_f5 + home_f5, 2)

        # Signal
        if total_f5 >= 5.0:
            signal = "LEAN OVER"; sig_col = "#00e676"
        elif total_f5 >= 4.5:
            signal = "SLIGHT OVER"; sig_col = "#76ff03"
        elif total_f5 <= 3.2:
            signal = "LEAN UNDER"; sig_col = "#f44336"
        elif total_f5 <= 3.7:
            signal = "SLIGHT UNDER"; sig_col = "#ff9800"
        else:
            signal = "NEUTRAL"; sig_col = "#6a8db0"

        # F5 favorite
        diff = home_f5 - away_f5
        if abs(diff) > 0.25:
            fav     = home_t["team"].get("abbreviation","HOME") if diff > 0 else away_t["team"].get("abbreviation","AWAY")
            fav_col = "#00e5ff"
        else:
            fav = "EVEN"; fav_col = "#6a8db0"

        return jsonify({
            "success":      True,
            "gamePk":       game_pk,
            "awayAbbr":     away_t["team"].get("abbreviation","AWAY"),
            "homeAbbr":     home_t["team"].get("abbreviation","HOME"),
            "awayPitcher":  hp_name,   # home pitcher faces away batters
            "homePitcher":  ap_name,
            "awayF5":       away_f5,
            "homeF5":       home_f5,
            "totalF5":      total_f5,
            "signal":       signal,
            "signalColor":  sig_col,
            "f5Favorite":   fav,
            "favColor":     fav_col,
            "awayEra":      round(hp_era, 2),
            "homeEra":      round(ap_era, 2),
            "awayXwoba":    away_xwoba,
            "homeXwoba":    home_xwoba,
            "parkFactor":   pf,
            "wxAdj":        wx_adj,
            "dome":         wx.get("dome", False),
        })

    except Exception as ex:
        print(f"[api_f5_model] {traceback.format_exc()}")
        return jsonify({"success": False, "error": str(ex)}), 500

# ── Lineup snapshot cache (for change detection) ─────────────────────────────
# Stores the first confirmed lineup seen per gamePk so later polls can diff it
_lineup_snapshots = {}   # gamePk → {"away": [...names...], "home": [...names...], "ts": iso}

def _build_lineup_snapshot(batters, side):
    """Extract ordered name list from batter dicts for diffing."""
    return [
        {"slot": b.get("slot", 0), "name": b.get("name", ""), "pos": b.get("pos", "")}
        for b in sorted(batters, key=lambda x: x.get("slot", 99))
        if b.get("name")
    ]

@app.route("/api/lineup-status/<int:game_pk>")
def api_lineup_status(game_pk):
    """
    Returns current lineup + change flags vs the first snapshot seen.
    Frontend calls this every 10 min to detect late scratches.
    Response:
      confirmed: bool  — lineup has >= 9 batters each side
      away/home: list of {slot, name, pos, status}  (status: 'confirmed'|'added'|'removed'|'moved')
      changes: list of change dicts
      snapshot_age_min: float
    """
    try:
        r = requests.get(f"{MLB_API}/game/{game_pk}/boxscore", timeout=10)
        r.raise_for_status()
        box       = r.json().get("teams", {})
        away_bats = get_batters_from_boxscore(box.get("away", {}), "away")
        home_bats = get_batters_from_boxscore(box.get("home", {}), "home")

        confirmed = len(away_bats) >= 9 and len(home_bats) >= 9
        now_ts    = datetime.now(timezone.utc).isoformat()

        away_snap = _build_lineup_snapshot(away_bats, "away")
        home_snap = _build_lineup_snapshot(home_bats, "home")

        # First time seeing this game — store snapshot, no changes
        if game_pk not in _lineup_snapshots:
            if confirmed:
                _lineup_snapshots[game_pk] = {
                    "away": away_snap, "home": home_snap, "ts": now_ts
                }
                _trigger_cheatsheet_refresh_async(reason='lineup_confirmed_first_seen')
            return jsonify({
                "success":     True,
                "gamePk":      game_pk,
                "confirmed":   confirmed,
                "firstSeen":   True,
                "changes":     [],
                "away":        [dict(p, status="confirmed") for p in away_snap],
                "home":        [dict(p, status="confirmed") for p in home_snap],
                "snapshotAge": 0,
            })

        # Diff against baseline
        baseline   = _lineup_snapshots[game_pk]
        base_ts    = datetime.fromisoformat(baseline["ts"])
        now_utc    = datetime.now(timezone.utc)
        age_min    = round((now_utc - base_ts).total_seconds() / 60, 1)

        def diff_side(base_list, curr_list):
            base_by_slot = {p["slot"]: p["name"] for p in base_list}
            curr_by_slot = {p["slot"]: p["name"] for p in curr_list}
            base_names   = {p["name"] for p in base_list if p["name"]}
            curr_names   = {p["name"] for p in curr_list if p["name"]}
            removed      = base_names - curr_names
            added        = curr_names - base_names
            annotated    = []
            changes      = []
            for p in curr_list:
                name = p["name"]
                if name in added:
                    status = "added"
                    changes.append({"type": "added", "name": name, "slot": p["slot"]})
                else:
                    # Check slot change (moved in order)
                    orig_slot = next((b["slot"] for b in base_list if b["name"] == name), p["slot"])
                    status = "moved" if orig_slot != p["slot"] else "confirmed"
                    if status == "moved":
                        changes.append({"type": "moved", "name": name,
                                        "from_slot": orig_slot, "to_slot": p["slot"]})
                annotated.append({**p, "status": status})
            for name in removed:
                orig = next((b for b in base_list if b["name"] == name), {})
                annotated.append({"slot": orig.get("slot", 0), "name": name,
                                   "pos": orig.get("pos", ""), "status": "removed"})
                changes.append({"type": "removed", "name": name, "slot": orig.get("slot", 0)})
            annotated.sort(key=lambda x: (x["status"] == "removed", x["slot"]))
            return annotated, changes

        away_annotated, away_changes = diff_side(baseline["away"], away_snap)
        home_annotated, home_changes = diff_side(baseline["home"], home_snap)
        all_changes = [dict(c, side="away") for c in away_changes] + \
                      [dict(c, side="home") for c in home_changes]

        # Update snapshot if lineup is now confirmed and was not before
        if confirmed and len(baseline["away"]) < 9:
            _lineup_snapshots[game_pk] = {"away": away_snap, "home": home_snap, "ts": now_ts}
            _trigger_cheatsheet_refresh_async(reason='lineup_confirmed_upgrade')
        elif confirmed and len(all_changes) > 0:
            _trigger_cheatsheet_refresh_async(reason='lineup_change_detected')

        return jsonify({
            "success":     True,
            "gamePk":      game_pk,
            "confirmed":   confirmed,
            "firstSeen":   False,
            "changes":     all_changes,
            "hasChanges":  len(all_changes) > 0,
            "away":        away_annotated,
            "home":        home_annotated,
            "snapshotAge": age_min,
            "snapshotTs":  baseline["ts"],
        })

    except Exception as ex:
        print(f"[api_lineup_status] {traceback.format_exc()}")
        return jsonify({"success": False, "error": str(ex)}), 500


# =============================================================
# GAMESIDE DEEP DIVE — reference-style layered matchup view
# =============================================================
@app.route("/gameside-deepdive/<int:game_pk>")
def gameside_deepdive_page(game_pk):
    """Layered deep-dive UI matching the reference mockup:
       run-env score, pitcher arsenal card with L/R splits,
       top batter prop picks with PF scores."""
    return GAMESIDE_DEEPDIVE_HTML


@app.route("/api/gameside-deepdive/<int:game_pk>")
def api_gameside_deepdive(game_pk):
    """Composite endpoint: game meta, pitchers, ranked batters, wind/run environment."""
    _maybe_refresh_fg()
    _maybe_refresh_savant()
    try:
        raw = fetch_schedule(datetime.now(ET).strftime("%Y-%m-%d"))

        # Find game and probable pitchers from schedule
        ap_data = hp_data = {}
        for raw_g in raw:
            if raw_g.get("gamePk") == game_pk:
                teams = raw_g.get("teams", {})
                ap_data = (teams.get("away", {}).get("probablePitcher") or {})
                hp_data = (teams.get("home", {}).get("probablePitcher") or {})
                gmeta = parse_game(raw_g)
                break
        else:
            return jsonify({"success": False, "error": "Game not found"}), 404

        # Fetch boxscore for lineups (teams nesting)
        box_teams = {}
        try:
            br = requests.get(f"{MLB_API}/game/{game_pk}/boxscore", timeout=10)
            br.raise_for_status()
            box_teams = br.json().get("teams", {})
        except Exception:
            pass
        away_bats = get_batters_from_boxscore(box_teams.get("away", {}), "away")
        home_bats = get_batters_from_boxscore(box_teams.get("home", {}), "home")

        def build_pitcher(name, pid):
            mlb_s = pitcher_stats_mlb(pid) if pid else {}
            fg_s  = fg_pitcher(name) or {}
            sv_s  = sv_pitcher(name) or {}
            out   = {}
            out.update(mlb_s)
            out.update(fg_s)
            for k, v in sv_s.items():
                if k not in ("sv_arsenal_pct", "sv_arsenal_velo"):
                    out[k] = v
            out["sv_arsenal_pct"]  = sv_s.get("sv_arsenal_pct", {})
            out["sv_arsenal_velo"] = sv_s.get("sv_arsenal_velo", {})
            return out

        an = ap_data.get("fullName", "TBD")
        hn = hp_data.get("fullName", "TBD")
        away_p = {"id": ap_data.get("id"), "name": an, "stats": build_pitcher(an, ap_data.get("id"))}
        home_p = {"id": hp_data.get("id"), "name": hn, "stats": build_pitcher(hn, hp_data.get("id"))}

        def rank_vs(batters, p_stats):
            out = []
            for b in batters:
                fgb = fg_batter(b.get("name", "")) or {}
                svb = sv_batter(b.get("name", "")) or {}
                merged = {}
                merged.update(b)
                merged.update(fgb)
                merged.update(svb)
                merged["damage_score"] = _damage_score(merged, p_stats)
                out.append(merged)
            out.sort(key=lambda x: x.get("damage_score", 0), reverse=True)
            return out

        away_ranked = rank_vs(away_bats, home_p["stats"])
        home_ranked = rank_vs(home_bats, away_p["stats"])

        return jsonify({
            "success": True,
            "game": gmeta,
            "awayPitcher": away_p,
            "homePitcher": home_p,
            "awayBatters": away_ranked,
            "homeBatters": home_ranked,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as ex:
        print(f"[api_gameside_deepdive] {traceback.format_exc()}")
        return jsonify({"success": False, "error": str(ex)}), 500



# =============================================================
# BREAKOUT DETECTOR — live 2026 Statcast real-vs-fake signals
# =============================================================
@app.route("/breakout-detector")
def breakout_detector_page():
    """Live Statcast breakout signal dashboard."""
    return BREAKOUT_DETECTOR_HTML


@app.route("/api/breakout/candidates")
def api_breakout_candidates():
    """Returns scored breakout candidates from Savant batter stats + FanGraphs.
    Score = weighted EV95 delta, barrel rate, xwOBA alignment, HH%, K% improvement,
    with penalties for BABIP fluke / wRC+ mirage / BA-xBA luck gap.
    """
    _maybe_refresh_fg(); _maybe_refresh_savant()
    try:
        with _sv_lock:
            stat = dict(_sv_bat_statcast)
            xst = dict(_sv_bat_xstats)
        with _fg_lock:
            fg = dict(_fg_bat)

        players = []
        for name_key, sc in stat.items():
            f = fg.get(name_key, {})
            x = xst.get(name_key, {})
            try:
                ev95 = float(sc.get("sv_hh_pct") or 0)  # proxy — your sv_hh_pct = ev95 percent
                brl  = float(sc.get("sv_brl_pct") or 0)
                if brl and brl <= 1: brl *= 100
                hh   = float(sc.get("sv_hh_pct") or 0)
                if hh and hh <= 1: hh *= 100
                xwoba = float(x.get("sv_xwoba") or f.get("fg_woba") or 0)
                woba  = float(f.get("fg_woba") or xwoba)
                xba   = float(x.get("sv_xba") or 0)
                avg   = float(f.get("fg_avg") or xba)
                wrc   = int(f.get("fg_wrc") or 100)
                babip = float(f.get("fg_babip") or .295)
                iso   = float(f.get("fg_iso") or .150)
                kpct  = float(f.get("fg_kpct") or .22) * (100 if (f.get("fg_kpct") or 0) <= 1 else 1)
                # Real 2026 EV95 uses avg_hit_speed proxy; fallback
                actual_ev95 = float(sc.get("sv_ev") or 0) + 13  # approx offset
                pa = int(f.get("fg_pa") or 0)
                if pa < 30: continue  # not enough BIP yet

                # Prior-year proxy: if no cache for 2025, assume population mean
                ev95_prior = actual_ev95 - 0.5  # neutral placeholder
                kpct_prior = kpct + 0.5

                # Compose player object
                players.append({
                    "name": " ".join(p.capitalize() for p in name_key.split()),
                    "team": "",
                    "pos": "",
                    "ev95": round(actual_ev95, 1),
                    "ev95Prior": round(ev95_prior, 1),
                    "brl": round(brl, 1),
                    "xwoba": round(xwoba, 3),
                    "woba": round(woba, 3),
                    "hh": round(hh, 1),
                    "maxev": float(sc.get("sv_max_ev") or 110),
                    "kpct": round(kpct, 1),
                    "kpctPrior": round(kpct_prior, 1),
                    "babip": round(babip, 3),
                    "wrc": wrc,
                    "avg": round(avg, 3),
                    "xba": round(xba, 3),
                    "iso": round(iso, 3),
                })
            except Exception:
                continue

        return jsonify({
            "success": True,
            "players": players[:50],
            "count": len(players),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as ex:
        print(f"[api_breakout_candidates] {traceback.format_exc()}")
        return jsonify({"success": False, "error": str(ex), "players": []}), 500

# =============================================================
# HR ANALYTICS HUB — Propalytics-style HR probability engine
# =============================================================

import numpy as _np_mod

# ── League baseline constants for z-score composite ─────────────────────────
_HR_LEAGUE = {
    "iso_mu": 0.165,      "iso_sd": 0.045,
    "barrel_mu": 8.5,     "barrel_sd": 4.0,
    "hh_mu": 40.0,        "hh_sd": 8.0,
    "hr9_mu": 1.25,       "hr9_sd": 0.55,
    "park_mu": 100.0,     "park_sd": 8.0,
    "mix_mu": 1.0,        "mix_sd": 0.18,
}

# League SLG per pitch type — used as denominator in mix score
_LEAGUE_SLG_BY_PITCH = {
    "4-Seam Fastball": 0.440, "Sinker": 0.420, "Cutter": 0.390,
    "Slider": 0.360,          "Sweeper": 0.350, "Curveball": 0.330,
    "Changeup": 0.350,        "Splitter": 0.340, "Knuckleball": 0.360,
    "Slurve": 0.340,
}

# Scouting report in-memory cache {(batter_id, pitcher_id, date_str): text}
_hr_scouting_cache: dict = {}
_hr_scouting_lock = threading.Lock()

# Daily HR score cache
_hr_daily_cache: dict = {}  # {"date": str, "scores": list}
_hr_daily_lock  = threading.Lock()

# Pitcher H/L handedness splits cache {pitcher_id: (date, {vl_hr9, vr_hr9})}
_hr_hand_cache: dict = {}
_hr_hand_lock  = threading.Lock()


def _fetch_pitcher_hr9_by_hand(pitcher_id):
    """Return {vl_hr9, vr_hr9} from MLB Stats API statSplits sitCodes.
    Falls back to fg_pitcher overall HR/9 if API call fails."""
    today = datetime.now().date()
    with _hr_hand_lock:
        cached = _hr_hand_cache.get(pitcher_id)
        if cached and cached[0] == today:
            return cached[1]

    year = datetime.now().year
    result = {"vl_hr9": 1.25, "vr_hr9": 1.25}
    try:
        r = requests.get(
            f"{MLB_API}/people/{pitcher_id}/stats",
            params={
                "stats": "statSplits",
                "group": "pitching",
                "sitCodes": "vl,vr",
                "season": year,
                "sportId": 1,
            },
            timeout=10,
        )
        r.raise_for_status()
        splits = (r.json().get("stats") or [{}])[0].get("splits", [])
        for sp in splits:
            sc = sp.get("sitCode", "")
            s  = sp.get("stat", {})
            ip  = _parse_ip(s.get("inningsPitched", 0))
            hr  = float(s.get("homeRuns", 0) or 0)
            hr9 = round(hr * 9 / ip, 3) if ip > 0 else 1.25
            if sc == "vl":
                result["vl_hr9"] = hr9
            elif sc == "vr":
                result["vr_hr9"] = hr9
    except Exception:
        pass

    with _hr_hand_lock:
        _hr_hand_cache[pitcher_id] = (today, result)
    return result


def _get_sv_pid_by_id(player_id):
    """Return player_id str for cross-referencing arsenal stats caches."""
    return str(player_id).strip()


def _compute_pitch_mix_score(pit_pid_str, bat_pid_str):
    """Compute pitch-mix matchup score.
    Returns (score, table_rows) where score is a float (1.0 = league avg)
    and table_rows is a list of dicts for the UI table.
    """
    with _sv_lock:
        pit_arsenal = dict(_sv_pit_arsenal_stats.get(pit_pid_str, {}))
        bat_arsenal = dict(_sv_bat_arsenal_stats.get(bat_pid_str, {}))

    if not pit_arsenal:
        return 1.0, []

    score = 0.0
    weight_sum = 0.0
    table_rows = []

    # Sort by pitcher usage descending
    sorted_pitches = sorted(pit_arsenal.items(), key=lambda x: x[1].get("usage", 0), reverse=True)

    for pitch_name, pit_stats in sorted_pitches[:6]:
        usage = pit_stats.get("usage", 0)
        if usage <= 0:
            continue
        pit_slg = pit_stats.get("slg", 0.400)
        if not isinstance(pit_slg, float) or pit_slg == 0:
            pit_slg = 0.400

        bat_stats = bat_arsenal.get(pitch_name, {})
        bat_slg   = bat_stats.get("slg")
        if not bat_slg or not isinstance(bat_slg, float) or bat_slg == 0:
            bat_slg = None  # no data

        league_slg = _LEAGUE_SLG_BY_PITCH.get(pitch_name, 0.390)

        ratio = (bat_slg / league_slg) if bat_slg else 1.0
        score     += ratio * usage
        weight_sum += usage

        table_rows.append({
            "pitch":       pitch_name,
            "usage":       round(usage, 1),
            "pit_ba":      pit_stats.get("ba"),
            "pit_slg":     pit_stats.get("slg"),
            "pit_woba":    pit_stats.get("woba"),
            "pit_whiff":   pit_stats.get("whiff_pct"),
            "pit_hh":      pit_stats.get("hh_pct"),
            "pit_rv100":   pit_stats.get("run_val"),
            "bat_slg":     bat_stats.get("slg"),
            "bat_woba":    bat_stats.get("woba"),
            "bat_whiff":   bat_stats.get("whiff_pct"),
            "bat_hh":      bat_stats.get("hh_pct"),
            "ratio":       round(ratio, 3),
        })

    final_score = (score / weight_sum) if weight_sum > 0 else 1.0
    return round(final_score, 3), table_rows


def _p_hr_per_ab(iso, barrel_pct, hh_pct, hr9_vs_hand, park_hr_idx, mix_score):
    """Compute per-AB HR probability from weighted input factors.
    Calibrated to 2024 MLB-wide HR rate ~3.2% (1 HR per 31 AB).
    """
    base = 0.032
    iso_f    = (max(0.01, iso)    / 0.165) ** 0.6
    barrel_f = (max(0.1, barrel_pct) / 8.5)  ** 0.5
    hh_f     = (max(5.0, hh_pct)  / 40.0) ** 0.3
    pit_f    = (max(0.1, hr9_vs_hand) / 1.25) ** 0.7
    park_f   = park_hr_idx / 100.0
    mix_f    = max(0.5, min(2.0, mix_score))
    p = base * iso_f * barrel_f * hh_f * pit_f * park_f * mix_f
    return round(min(p, 0.28), 5)


def _daily_hr_score(iso, barrel_pct, hh_pct, hr9_hand, park_hr_idx, mix_score, platoon_adv=0):
    """Compute 0-100 daily composite HR score via weighted z-scores."""
    def _z(x, mu, sd):
        return (x - mu) / sd if sd > 0 else 0.0

    raw = (
        0.30 * _z(iso,          _HR_LEAGUE["iso_mu"],    _HR_LEAGUE["iso_sd"])    +
        0.25 * _z(barrel_pct,   _HR_LEAGUE["barrel_mu"], _HR_LEAGUE["barrel_sd"]) +
        0.20 * _z(hr9_hand,     _HR_LEAGUE["hr9_mu"],    _HR_LEAGUE["hr9_sd"])    +
        0.10 * _z(park_hr_idx,  _HR_LEAGUE["park_mu"],   _HR_LEAGUE["park_sd"])   +
        0.10 * _z(mix_score,    _HR_LEAGUE["mix_mu"],    _HR_LEAGUE["mix_sd"])    +
        0.05 * platoon_adv
    )
    return max(0, min(100, round(50 + 12 * raw, 1)))


def _run_hr_monte_carlo(p_per_ab, ab_per_game=4, n_sims=1000):
    """Run Monte Carlo for HR in a game. Returns prob and distribution."""
    rng  = _np_mod.random.default_rng()
    sims = rng.binomial(n=ab_per_game, p=p_per_ab, size=n_sims)
    prob_any = float((sims >= 1).mean())
    dist = [int((sims == k).sum()) for k in range(ab_per_game + 1)]
    return round(prob_any, 4), dist


def _gather_batter_hr_inputs(batter_name, batter_id, pitcher_id, home_team_id, batter_hand=None):
    """Collect all inputs needed for HR probability computation.
    Returns a dict of inputs + intermediate values.
    """
    _maybe_refresh_fg()
    _maybe_refresh_savant()

    fgb = fg_batter(batter_name)
    svb = sv_batter(batter_name)

    iso        = _num(fgb.get("fg_iso"), 0.0) or max(0.0, _num(svb.get("sv_xslg"), 0.400) - _num(svb.get("sv_xba"), 0.260))
    barrel_pct = _num(svb.get("sv_brl_pct"), 8.5)
    hh_pct     = _num(svb.get("sv_hh_pct"), 40.0)
    avg_ev     = _num(svb.get("sv_ev"), 88.0)
    max_ev     = _num(svb.get("sv_max_ev"), 103.0)
    xwoba      = _num(svb.get("sv_xwoba"), 0.320)

    if batter_hand is None:
        bio        = player_profile(batter_id)
        batter_hand = bio.get("bats", "R")

    hand_splits = _fetch_pitcher_hr9_by_hand(pitcher_id)
    hand_key    = "vl_hr9" if batter_hand == "L" else "vr_hr9"
    hr9_vs_hand = hand_splits.get(hand_key, 1.25)

    park_hr_idx = HR_PARK_FACTORS.get(home_team_id, 100)

    pit_pid_str = _get_sv_pid_by_id(pitcher_id)
    sv_pit = sv_pitcher("") if False else {}  # just to trigger cache load
    pit_svp = sv_pitcher("")
    # Look up pitcher's sv_pid via its name — try FG then direct
    fgp_by_id = {}
    try:
        r = requests.get(f"{MLB_API}/people/{pitcher_id}", timeout=6)
        pitcher_full_name = (r.json().get("people") or [{}])[0].get("fullName", "")
    except Exception:
        pitcher_full_name = ""
    fgp = fg_pitcher(pitcher_full_name) if pitcher_full_name else {}
    svp = sv_pitcher(pitcher_full_name) if pitcher_full_name else {}
    # Resolve pitcher's Savant player_id for arsenal lookup
    pit_pid_str = str(svp.get("sv_pid", "") or pitcher_id).strip()

    bat_pid_str = str(svb.get("sv_pid", "") or batter_id).strip()
    mix_score, pitch_table = _compute_pitch_mix_score(pit_pid_str, bat_pid_str)

    # Platoon advantage: +1 if batter hits same hand as pitcher throws, -1 if opposite, 0 = switch
    pit_hand = "R"
    if pitcher_full_name:
        try:
            pr = requests.get(f"{MLB_API}/people/{pitcher_id}", timeout=5)
            pit_hand = (pr.json().get("people") or [{}])[0].get("pitchHand", {}).get("code", "R")
        except Exception:
            pass
    platoon_adv = 0.5 if batter_hand != pit_hand else -0.3  # same hand = platoon disadvantage

    return {
        "batter_name": batter_name,
        "batter_id": batter_id,
        "batter_hand": batter_hand,
        "pitcher_id": pitcher_id,
        "pitcher_name": pitcher_full_name,
        "pitcher_hand": pit_hand,
        "iso": round(iso, 3),
        "barrel_pct": round(barrel_pct, 1),
        "hh_pct": round(hh_pct, 1),
        "avg_ev": round(avg_ev, 1),
        "max_ev": round(max_ev, 1),
        "xwoba": round(xwoba, 3),
        "hr9_vs_hand": round(hr9_vs_hand, 3),
        "park_hr_idx": park_hr_idx,
        "home_team_id": home_team_id,
        "mix_score": mix_score,
        "platoon_adv": platoon_adv,
        "pitch_table": pitch_table,
        "pitcher_era": _num(fgp.get("fg_era"), 4.50),
        "pitcher_hr9": _num(fgp.get("fg_hr9"), 1.25),
        "fg_hr": int(_num(fgb.get("fg_hr"), 0)),
        "fg_pa": int(_num(fgb.get("fg_pa"), 0)),
    }


def _build_scouting_payload(inputs, bvp, p_per_ab, prob_any, score):
    """Assemble the structured payload sent to Claude for the scouting report."""
    park_name = ""
    try:
        r = requests.get(f"{MLB_API}/teams/{inputs['home_team_id']}/venue", timeout=5)
        park_name = (r.json().get("venues") or [{}])[0].get("name", "")
    except Exception:
        pass
    if not park_name:
        park_name = f"Team {inputs['home_team_id']} park"

    return {
        "batter": {
            "name": inputs["batter_name"],
            "hand": inputs["batter_hand"],
            "iso": inputs["iso"],
            "barrel_pct": inputs["barrel_pct"],
            "hh_pct": inputs["hh_pct"],
            "avg_ev": inputs["avg_ev"],
            "xwoba": inputs["xwoba"],
            "season_hr": inputs["fg_hr"],
            "season_pa": inputs["fg_pa"],
        },
        "pitcher": {
            "name": inputs["pitcher_name"],
            "hand": inputs["pitcher_hand"],
            "hr9_vs_this_hand": inputs["hr9_vs_hand"],
            "hr9_overall": inputs["pitcher_hr9"],
            "era": inputs["pitcher_era"],
        },
        "park": {
            "name": park_name,
            "hr_index": inputs["park_hr_idx"],
        },
        "h2h": {
            "pa": bvp.get("pa", 0),
            "hr": bvp.get("hr", 0),
            "avg": bvp.get("avg"),
            "ops": bvp.get("ops"),
            "sample_note": bvp.get("note", ""),
        },
        "model": {
            "p_per_ab": round(p_per_ab * 100, 2),
            "prob_hr_today_pct": round(prob_any * 100, 2),
            "score_0_100": score,
            "mix_score": inputs["mix_score"],
            "platoon": "advantage" if inputs["platoon_adv"] > 0 else "disadvantage",
        },
    }


def _call_claude_scouting_report(payload):
    """Call Claude to generate a HR scouting report. Returns text or None."""
    import anthropic
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None
    client = anthropic.Anthropic(api_key=api_key)
    batter = payload["batter"]["name"]
    pitcher = payload["pitcher"]["name"]
    park    = payload["park"]["name"]
    prompt = (
        f"You are an MLB advance-scouting analyst. Write a 120-150 word scouting paragraph "
        f"for a Home Run prop bet on {batter} vs {pitcher} at {park} today. "
        f"Lead with the bottom line (BET / LEAN / PASS), then justify with the data. "
        f"Cite specific numbers from the DATA block. Do not invent stats. "
        f"Style: confident analyst, no hedging adverbs. Mention park factor and platoon "
        f"only if they materially move the number.\n\nDATA:\n"
        + json.dumps(payload, indent=2)
    )
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        return (resp.content[0].text or "").strip()
    except Exception as ex:
        print(f"[HR scouting] Claude call failed: {ex}")
        return None


# ── HR Analytics routes ───────────────────────────────────────────────────────

@app.route("/hr-analytics")
def hr_analytics_page():
    return HR_ANALYTICS_HTML


@app.route("/api/hr-analytics/simulator")
def api_hr_analytics_simulator():
    """HR probability simulator for a batter vs pitcher matchup.
    Query params: batter_id, pitcher_id, game_pk, batter_name (optional)
    """
    try:
        batter_id   = int(request.args.get("batter_id", 0) or 0)
        pitcher_id  = int(request.args.get("pitcher_id", 0) or 0)
        game_pk     = int(request.args.get("game_pk", 0) or 0)
        batter_name = (request.args.get("batter_name") or "").strip()

        if not batter_id or not pitcher_id:
            return jsonify({"success": False, "error": "batter_id and pitcher_id required"}), 400

        # Resolve batter name if not provided
        if not batter_name:
            try:
                r = requests.get(f"{MLB_API}/people/{batter_id}", timeout=6)
                batter_name = (r.json().get("people") or [{}])[0].get("fullName", "")
            except Exception:
                pass
        if not batter_name:
            return jsonify({"success": False, "error": "Could not resolve batter name"}), 404

        # Resolve home team from game
        home_team_id = 0
        if game_pk:
            try:
                gdata = fetch_schedule_game(game_pk)
                if gdata:
                    home_team_id = ((gdata.get("teams") or {}).get("home") or {}).get("team", {}).get("id", 0)
            except Exception:
                pass

        inputs = _gather_batter_hr_inputs(batter_name, batter_id, pitcher_id, home_team_id)
        p_per_ab = _p_hr_per_ab(
            inputs["iso"], inputs["barrel_pct"], inputs["hh_pct"],
            inputs["hr9_vs_hand"], inputs["park_hr_idx"], inputs["mix_score"],
        )
        prob_any, mc_dist = _run_hr_monte_carlo(p_per_ab)
        score = _daily_hr_score(
            inputs["iso"], inputs["barrel_pct"], inputs["hh_pct"],
            inputs["hr9_vs_hand"], inputs["park_hr_idx"], inputs["mix_score"],
            inputs["platoon_adv"],
        )
        prob_closed = round(1 - (1 - p_per_ab) ** 4, 4)

        return jsonify({
            "success": True,
            "batter":       batter_name,
            "batter_id":    batter_id,
            "pitcher_id":   pitcher_id,
            "pitcher":      inputs["pitcher_name"],
            "inputs":       {k: v for k, v in inputs.items() if k != "pitch_table"},
            "p_per_ab":     p_per_ab,
            "prob_hr_today":    prob_any,
            "prob_hr_closed":   prob_closed,
            "mc_distribution":  mc_dist,
            "score_0_100":      score,
            "pitch_table":  inputs["pitch_table"],
        })
    except Exception as ex:
        print(f"[api_hr_analytics_simulator] {traceback.format_exc()}")
        return jsonify({"success": False, "error": str(ex)}), 500


@app.route("/api/hr-analytics/pitch-mix/<int:batter_id>/<int:pitcher_id>")
def api_hr_pitch_mix(batter_id, pitcher_id):
    """Pitch mix matchup table for a batter vs pitcher."""
    try:
        _maybe_refresh_savant()
        batter_name  = ""
        pitcher_name = ""
        try:
            rb = requests.get(f"{MLB_API}/people/{batter_id}", timeout=6)
            batter_name = (rb.json().get("people") or [{}])[0].get("fullName", "")
            rp = requests.get(f"{MLB_API}/people/{pitcher_id}", timeout=6)
            pitcher_name = (rp.json().get("people") or [{}])[0].get("fullName", "")
        except Exception:
            pass

        svb = sv_batter(batter_name) if batter_name else {}
        svp = sv_pitcher(pitcher_name) if pitcher_name else {}
        bat_pid_str = str(svb.get("sv_pid", "") or batter_id).strip()
        pit_pid_str = str(svp.get("sv_pid", "") or pitcher_id).strip()

        mix_score, table_rows = _compute_pitch_mix_score(pit_pid_str, bat_pid_str)

        return jsonify({
            "success": True,
            "batter_id": batter_id,
            "pitcher_id": pitcher_id,
            "batter": batter_name,
            "pitcher": pitcher_name,
            "mix_score": mix_score,
            "pitch_table": table_rows,
            "note": "No pitch arsenal data" if not table_rows else "",
        })
    except Exception as ex:
        return jsonify({"success": False, "error": str(ex)}), 500


@app.route("/api/hr-analytics/scouting/<int:batter_id>/<int:pitcher_id>/<int:game_pk>")
def api_hr_scouting(batter_id, pitcher_id, game_pk):
    """Claude-powered HR scouting report. Cached per (batter, pitcher, date)."""
    try:
        date_str = datetime.now(ET).strftime("%Y-%m-%d")
        cache_key = (batter_id, pitcher_id, date_str)
        with _hr_scouting_lock:
            cached = _hr_scouting_cache.get(cache_key)
        if cached:
            return jsonify({"success": True, "report": cached, "cached": True})

        batter_name = ""
        try:
            r = requests.get(f"{MLB_API}/people/{batter_id}", timeout=6)
            batter_name = (r.json().get("people") or [{}])[0].get("fullName", "")
        except Exception:
            pass
        if not batter_name:
            return jsonify({"success": False, "error": "Could not resolve batter"}), 404

        home_team_id = 0
        if game_pk:
            try:
                gdata = fetch_schedule_game(game_pk)
                if gdata:
                    home_team_id = ((gdata.get("teams") or {}).get("home") or {}).get("team", {}).get("id", 0)
            except Exception:
                pass

        inputs  = _gather_batter_hr_inputs(batter_name, batter_id, pitcher_id, home_team_id)
        bvp     = _fetch_bvp(batter_id, pitcher_id) or {}
        p_per_ab = _p_hr_per_ab(
            inputs["iso"], inputs["barrel_pct"], inputs["hh_pct"],
            inputs["hr9_vs_hand"], inputs["park_hr_idx"], inputs["mix_score"],
        )
        prob_any, _ = _run_hr_monte_carlo(p_per_ab, n_sims=500)
        score = _daily_hr_score(
            inputs["iso"], inputs["barrel_pct"], inputs["hh_pct"],
            inputs["hr9_vs_hand"], inputs["park_hr_idx"], inputs["mix_score"],
        )

        payload = _build_scouting_payload(inputs, bvp, p_per_ab, prob_any, score)
        report  = _call_claude_scouting_report(payload)
        if not report:
            report = (
                f"Insufficient API access for AI scouting report. "
                f"Model score: {score}/100. HR probability: {round(prob_any*100,1)}%. "
                f"Key factors: ISO {inputs['iso']}, Barrel% {inputs['barrel_pct']}, "
                f"Park HR index {inputs['park_hr_idx']}."
            )

        with _hr_scouting_lock:
            _hr_scouting_cache[cache_key] = report

        return jsonify({
            "success": True,
            "report": report,
            "cached": False,
            "payload": payload,
        })
    except Exception as ex:
        print(f"[api_hr_scouting] {traceback.format_exc()}")
        return jsonify({"success": False, "error": str(ex)}), 500


@app.route("/api/hr-analytics/daily-scores")
def api_hr_daily_scores():
    """Ranked 0-100 HR scores for every starting batter in today's games."""
    try:
        date_str = datetime.now(ET).strftime("%Y-%m-%d")
        with _hr_daily_lock:
            cached = _hr_daily_cache.get("date")
            if cached == date_str and _hr_daily_cache.get("scores"):
                return jsonify({
                    "success": True,
                    "date": date_str,
                    "scores": _hr_daily_cache["scores"],
                    "cached": True,
                })

        _maybe_refresh_fg()
        _maybe_refresh_savant()

        games_raw = fetch_schedule(date_str) or []
        scores = []

        for game in games_raw:
            game_pk = game.get("gamePk")
            if not game_pk:
                continue
            teams     = game.get("teams") or {}
            home_info = (teams.get("home") or {}).get("team") or {}
            away_info = (teams.get("away") or {}).get("team") or {}
            home_tid  = home_info.get("id", 0)
            away_tid  = away_info.get("id", 0)
            home_name = home_info.get("name", "")
            away_name = away_info.get("name", "")

            # Probable pitchers
            home_prob = (teams.get("home") or {}).get("probablePitcher") or {}
            away_prob = (teams.get("away") or {}).get("probablePitcher") or {}

            game_time = (game.get("gameDate") or "")[-8:-3] if game.get("gameDate") else ""

            # Fetch lineup for this game
            lineup_data = {}
            try:
                lr = requests.get(
                    f"http://localhost:{os.environ.get('PORT', 10000)}/api/lineup/{game_pk}",
                    timeout=8,
                )
                if lr.ok:
                    lineup_data = lr.json()
            except Exception:
                pass

            for side in ("away", "home"):
                batters   = lineup_data.get(side, [])
                opp_prob  = home_prob if side == "away" else away_prob
                opp_pid   = opp_prob.get("id", 0)
                opp_name  = opp_prob.get("fullName", "TBD")
                team_tid  = away_tid if side == "away" else home_tid
                team_name = away_name if side == "away" else home_name
                opp_team  = home_name if side == "away" else away_name

                if not opp_pid:
                    continue

                for b in batters[:9]:
                    bid    = b.get("id") or b.get("playerId")
                    bname  = (b.get("name") or b.get("fullName") or "").strip()
                    bhand  = b.get("bats", "R")
                    slot   = b.get("slot", 0)
                    if not bid or not bname:
                        continue

                    try:
                        fgb = fg_batter(bname)
                        svb = sv_batter(bname)
                        iso        = _num(fgb.get("fg_iso"), 0.0) or max(0.0, _num(svb.get("sv_xslg"), 0.380) - _num(svb.get("sv_xba"), 0.250))
                        barrel_pct = _num(svb.get("sv_brl_pct"), 8.5)
                        hh_pct     = _num(svb.get("sv_hh_pct"), 40.0)
                        fgp_hr9    = _num(fg_pitcher(opp_name).get("fg_hr9"), 1.25) if opp_name else 1.25

                        hand_splits = _fetch_pitcher_hr9_by_hand(opp_pid)
                        hand_key    = "vl_hr9" if bhand == "L" else "vr_hr9"
                        hr9_vs_hand = hand_splits.get(hand_key, fgp_hr9)

                        park_hr_idx = HR_PARK_FACTORS.get(home_tid, 100)
                        bat_pid_str = str(svb.get("sv_pid", "") or bid).strip()
                        svp = sv_pitcher(opp_name) if opp_name else {}
                        pit_pid_str = str(svp.get("sv_pid", "") or opp_pid).strip()
                        mix_score, _ = _compute_pitch_mix_score(pit_pid_str, bat_pid_str)

                        pit_hand = "R"
                        try:
                            bio_p = player_profile(opp_pid)
                            pit_hand = bio_p.get("throws", "R")
                        except Exception:
                            pass
                        platoon_adv = 0.5 if bhand != pit_hand else -0.3

                        p_per_ab = _p_hr_per_ab(iso, barrel_pct, hh_pct, hr9_vs_hand, park_hr_idx, mix_score)
                        prob_any  = round(1 - (1 - p_per_ab) ** 4, 4)
                        score     = _daily_hr_score(iso, barrel_pct, hh_pct, hr9_vs_hand, park_hr_idx, mix_score, platoon_adv)

                        scores.append({
                            "batter_id":   bid,
                            "batter":      bname,
                            "batter_hand": bhand,
                            "slot":        slot,
                            "team":        team_name,
                            "opp_team":    opp_team,
                            "pitcher_id":  opp_pid,
                            "pitcher":     opp_name,
                            "game_pk":     game_pk,
                            "game_time":   game_time,
                            "home_team_id": home_tid,
                            "score":       score,
                            "prob_hr":     prob_any,
                            "p_per_ab":    p_per_ab,
                            "iso":         round(iso, 3),
                            "barrel_pct":  round(barrel_pct, 1),
                            "hh_pct":      round(hh_pct, 1),
                            "hr9_vs_hand": round(hr9_vs_hand, 3),
                            "park_hr_idx": park_hr_idx,
                            "mix_score":   mix_score,
                        })
                    except Exception:
                        continue

        scores.sort(key=lambda x: x["score"], reverse=True)

        with _hr_daily_lock:
            _hr_daily_cache["date"]   = date_str
            _hr_daily_cache["scores"] = scores

        return jsonify({
            "success": True,
            "date": date_str,
            "scores": scores,
            "cached": False,
        })
    except Exception as ex:
        print(f"[api_hr_daily_scores] {traceback.format_exc()}")
        return jsonify({"success": False, "error": str(ex), "scores": []}), 500


# ── Preload caches at startup ───────────────────────────────────────────
# Load FG and Savant data before serving requests to ensure data is available
# on first access. This runs in background threads to avoid blocking startup.
def _preload_caches():
    """Preload FG and Savant caches at startup in background threads."""
    def load_fg():
        try:
            _load_fg_data()
            print("[STARTUP] FG cache preload complete")
        except Exception as ex:
            print(f"[STARTUP] FG cache preload failed: {ex}")
    
    def load_sv():
        try:
            _load_savant_data()
            print("[STARTUP] Savant cache preload complete")
        except Exception as ex:
            print(f"[STARTUP] Savant cache preload failed: {ex}")
    
    # Start cache loads in parallel background threads
    threading.Thread(target=load_fg, daemon=True).start()
    threading.Thread(target=load_sv, daemon=True).start()

    def _prewarm_when_ready():
        # Wait for FG data to be available before prewarming dependent caches.
        deadline = time.time() + 60
        while time.time() < deadline:
            with _fg_lock:
                if _fg_loaded:
                    break
            time.sleep(2)
        prewarm_today_caches({
            "fetch_schedule": fetch_schedule,
            "_fetch_bvp": _fetch_bvp,
            "_pitcher_recent_form": _pitcher_recent_form,
            "_get_cached_ump": _get_cached_ump,
        })

    threading.Thread(target=_prewarm_when_ready, daemon=True).start()

    def _load_brain_overlays_when_ready():
        """Load brain overlays after FG cache is ready (brain merges into FG data)."""
        deadline = time.time() + 90
        while time.time() < deadline:
            with _fg_lock:
                if _fg_loaded:
                    break
            time.sleep(3)
        try:
            load_brain_overlays()
        except Exception as ex:
            print(f"[STARTUP] brain overlay load failed: {ex}")
    threading.Thread(target=_load_brain_overlays_when_ready, daemon=True).start()

    _start_odds_snapshot_worker()

# _preload_caches() is now triggered via the gunicorn post_fork hook in gunicorn_conf.py
# so that port 8080 is bound before any heavy network I/O begins.

configure_pitcher_stats_context(globals())
configure_simulation_context(globals())
configure_odds_context(globals())
initialize_odds_module()
configure_tracker_context(globals())
configure_props_context(globals())

# Start hourly injury refresh worker once routes/helpers are loaded.
_start_injury_worker()
_start_tracker_auto_sync_worker()
_start_mlb_memory_worker()

# Start daily pipeline scheduler (runs at 8 AM ET + on boot)
if _PIPELINE_AVAILABLE:
    start_scheduler()
    logging.info("[pipeline] Scheduler armed — fires at 09:00 ET daily.")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
