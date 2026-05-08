import os, threading, traceback, difflib, io, csv as csvmod, json, re, time, uuid, unicodedata, logging
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


app = Flask(__name__)
CORS(app)

from nrfi_odds_routes import register_nrfi_odds_routes
register_nrfi_odds_routes(app)

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


def _parse_ip(raw):
    """Convert MLB API inningsPitched string to true decimal innings.

    The MLB API encodes outs as a decimal digit, not a fraction:
      '45.2'  → 45 innings + 2 outs  = 45 + 2/3 = 45.6667
      '6.1'   →  6 innings + 1 out   =  6 + 1/3 =  6.3333
      '7.0'   →  7 innings + 0 outs  =  7.0
    Using _safe_num() directly treats '45.2' as the float 45.2,
    which inflates BB/9, K/9, and ERA by ~1-2%.
    """
    try:
        s = str(raw or '').strip()
        if not s or s in ('N/A', '-.--', '.---'):
            return 0.0
        if '.' in s:
            whole, outs = s.split('.', 1)
            return int(whole) + int(outs[0]) / 3.0
        return float(s)
    except Exception:
        return 0.0


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
        
        # FIX: was svbatter(player_name) — undefined NameError. sv_batter() is the
        # local function defined further down in this file.
        savant_data = sv_batter(player_name) or {}
        barrel_pct = _safe_num(savant_data.get("sv_brl_pct"), 0)
        hard_hit_pct = _safe_num(savant_data.get("sv_hh_pct"), 0)
        
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
            badges.append({"type": "COLD_STREAK", "label": f".{int(l5_avg*1000):03d} L5", "color": "red", "props": []})
        
        if l5_hr >= 3 or (l5_hr >= 2 and barrel_pct > 12):
            badges.append({"type": "POWER_SURGE", "label": f"{l5_hr} HR L5", "color": "orange", "props": ["HR", "TB"]})
        
        if l5_k_rate < 0.15 and l5_avg > 0.280:
            badges.append({"type": "CONTACT_MODE", "label": f".{int(l5_avg*1000):03d}/{int(l5_k_rate*100)}%K", "color": "blue", "props": ["HIT", "RBI"]})
        
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
        
        # FIX: use _parse_ip() so '6.2' (= 6⅔ IP) is 6.667, not 6.2.
        # _safe_num() treats the outs digit as a decimal fraction, inflating per-9 stats.
        l5_ip = sum(_parse_ip(g.get("stat", {}).get("inningsPitched")) for g in last_5)
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
            badges.append({"type": "VULNERABLE_LHB", "label": f"LHB .{int(vs_lhb_ops*1000):03d}", "color": "orange", "props": ["TARGET_LHB"]})
        elif pitcher_hand == "L" and vs_rhb_ops > 0.850:
            badges.append({"type": "VULNERABLE_RHB", "label": f"RHB .{int(vs_rhb_ops*1000):03d}", "color": "orange", "props": ["TARGET_RHB"]})
        
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
