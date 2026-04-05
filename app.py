#!/usr/bin/env python3
"""
MLB Analytics Hub — Flask Backend
Full app.py with Eastern Time fix for UTC rollover bug.
"""
import os, re, traceback, requests
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask, jsonify, send_from_directory, abort

ET = ZoneInfo("America/New_York")

MLB_API  = "https://statsapi.mlb.com/api/v1"
FG_URL   = "https://www.fangraphs.com/api/leaders/major-league/data"
SV_URL   = "https://baseballsavant.mlb.com/leaderboard/custom"

PARK_FACTORS = {
    "Coors Field": 1.17, "Great American Ball Park": 1.08,
    "American Family Field": 1.05, "Fenway Park": 1.05,
    "Citizens Bank Park": 1.06, "Globe Life Field": 1.03,
    "Chase Field": 1.04, "Wrigley Field": 1.02,
    "Oracle Park": 0.93, "Petco Park": 0.92,
    "Dodger Stadium": 0.95, "loanDepot park": 0.93,
    "T-Mobile Park": 0.95, "Kauffman Stadium": 0.97,
    "PNC Park": 0.97, "Truist Park": 1.00,
    "Nationals Park": 1.01, "Progressive Field": 1.00,
    "Minute Maid Park": 1.01, "Guaranteed Rate Field": 1.03,
    "Target Field": 0.98, "Busch Stadium": 0.97,
    "Angel Stadium": 0.97, "Oakland Coliseum": 0.94,
    "Camden Yards": 1.01, "Yankee Stadium": 1.04,
    "Citi Field": 0.97, "Tropicana Field": 0.96,
    "Rogers Centre": 1.02, "Comerica Park": 0.97,
}

DOME_PARKS = {
    "Tropicana Field", "Rogers Centre", "loanDepot park",
    "American Family Field", "Globe Life Field", "Chase Field",
    "Minute Maid Park",
}

app = Flask(__name__, static_folder="static")

# ── Helpers ──────────────────────────────────────────────────────────────────

def safe_float(v, default=None):
    try:
        if v in (None, "", "N/A", "---", "-.--", ".---"): return default
        return float(str(v).replace(",", ""))
    except: return default

def fmt_stat(v, decimals=3):
    n = safe_float(v)
    return f"{n:.{decimals}f}" if n is not None else "N/A"

def fg_pitcher(name):
    try:
        r = requests.get(FG_URL, params={
            "pos": "P", "stats": "pit", "lg": "all", "qual": "0",
            "season": datetime.now(ET).year, "season1": datetime.now(ET).year,
            "pageitems": 2000, "pagenum": 1, "ind": 0,
        }, timeout=8)
        r.raise_for_status()
        rows = r.json().get("data", [])
        name_lower = name.lower()
        for row in rows:
            rn = (row.get("PlayerName") or row.get("Name") or "").lower()
            if name_lower in rn or rn in name_lower:
                return {
                    "fg_era": fmt_stat(row.get("ERA"), 2),
                    "fg_fip": fmt_stat(row.get("FIP"), 2),
                    "fg_xfip": fmt_stat(row.get("xFIP"), 2),
                    "fg_whip": fmt_stat(row.get("WHIP"), 2),
                    "fg_k9": fmt_stat(row.get("K/9"), 1),
                    "fg_bb9": fmt_stat(row.get("BB/9"), 1),
                    "fg_kpct": fmt_stat(safe_float(row.get("K%")), 3),
                    "fg_bbpct": fmt_stat(safe_float(row.get("BB%")), 3),
                    "fg_war": fmt_stat(row.get("WAR"), 1),
                    "fg_babip": fmt_stat(row.get("BABIP"), 3),
                    "fg_lob": fmt_stat(safe_float(row.get("LOB%")), 3),
                    "fg_g": row.get("G", 0), "fg_gs": row.get("GS", 0),
                    "fg_ip": fmt_stat(row.get("IP"), 1),
                    "fg_w": row.get("W", 0), "fg_l": row.get("L", 0),
                }
    except: pass
    return {}

def fg_batter(name):
    try:
        r = requests.get(FG_URL, params={
            "pos": "all", "stats": "bat", "lg": "all", "qual": "0",
            "season": datetime.now(ET).year, "season1": datetime.now(ET).year,
            "pageitems": 2000, "pagenum": 1, "ind": 0,
        }, timeout=8)
        r.raise_for_status()
        rows = r.json().get("data", [])
        name_lower = name.lower()
        for row in rows:
            rn = (row.get("PlayerName") or row.get("Name") or "").lower()
            if name_lower in rn or rn in name_lower:
                return {
                    "fg_avg": fmt_stat(row.get("AVG"), 3),
                    "fg_obp": fmt_stat(row.get("OBP"), 3),
                    "fg_slg": fmt_stat(row.get("SLG"), 3),
                    "fg_ops": fmt_stat(row.get("OPS"), 3),
                    "fg_woba": fmt_stat(row.get("wOBA"), 3),
                    "fg_wrc": fmt_stat(row.get("wRC+"), 0),
                    "fg_war": fmt_stat(row.get("WAR"), 1),
                    "fg_babip": fmt_stat(row.get("BABIP"), 3),
                    "fg_kpct": fmt_stat(safe_float(row.get("K%")), 3),
                    "fg_bbpct": fmt_stat(safe_float(row.get("BB%")), 3),
                }
    except: pass
    return {}

def sv_pitcher(name):
    try:
        r = requests.get("https://baseballsavant.mlb.com/leaderboard/expected_statistics",
            params={"type": "pitcher", "year": datetime.now(ET).year, "min": 0, "pos": "", "team": 0,
                    "xstat": "xera", "csv": "false"}, timeout=8)
        r.raise_for_status()
        rows = r.json()
        name_lower = name.lower()
        for row in rows:
            rn = (row.get("last_name") or "") + " " + (row.get("first_name") or "")
            fn = (row.get("first_name") or "") + " " + (row.get("last_name") or "")
            if name_lower in rn.lower() or name_lower in fn.lower():
                pid = row.get("player_id")
                arsenal = _sv_arsenal(pid) if pid else {}
                return {
                    "sv_xera": fmt_stat(row.get("xera"), 2),
                    "sv_xwoba_p": fmt_stat(row.get("xwoba"), 3),
                    "sv_arsenal_pct": arsenal.get("pct", {}),
                    "sv_arsenal_velo": arsenal.get("velo", {}),
                }
    except: pass
    return {}

def sv_batter(name):
    try:
        r = requests.get("https://baseballsavant.mlb.com/leaderboard/expected_statistics",
            params={"type": "batter", "year": datetime.now(ET).year, "min": 0, "pos": "", "team": 0,
                    "xstat": "xba", "csv": "false"}, timeout=8)
        r.raise_for_status()
        rows = r.json()
        name_lower = name.lower()
        for row in rows:
            rn = (row.get("last_name") or "") + " " + (row.get("first_name") or "")
            fn = (row.get("first_name") or "") + " " + (row.get("last_name") or "")
            if name_lower in rn.lower() or name_lower in fn.lower():
                return {
                    "sv_xba": fmt_stat(row.get("xba"), 3),
                    "sv_xslg": fmt_stat(row.get("xslg"), 3),
                    "sv_xwoba": fmt_stat(row.get("xwoba"), 3),
                    "sv_ev": fmt_stat(row.get("exit_velocity_avg"), 1),
                    "sv_hh_pct": fmt_stat(row.get("hard_hit_percent"), 1),
                    "sv_brl_pct": fmt_stat(row.get("barrel_batted_rate"), 1),
                }
    except: pass
    return {}

def _sv_arsenal(player_id):
    try:
        r = requests.get("https://baseballsavant.mlb.com/leaderboard/pitch-arsenal-stats",
            params={"type": "pitcher", "pitchType": "", "year": datetime.now(ET).year,
                    "team": "", "min": 0, "csv": "false"}, timeout=8)
        r.raise_for_status()
        rows = r.json()
        pct  = {}
        velo = {}
        for row in rows:
            if row.get("player_id") == player_id:
                pt = (row.get("pitch_type") or "").lower()
                if pt:
                    pct[pt]  = round(float(row.get("pitch_usage", 0) or 0), 1)
                    velo[pt] = round(float(row.get("avg_speed", 0) or 0), 1)
        return {"pct": pct, "velo": velo}
    except: pass
    return {}

def _build_game(g):
    teams   = g.get("teams", {})
    away_t  = teams.get("away", {}).get("team", {})
    home_t  = teams.get("home", {}).get("team", {})
    venue   = g.get("venue", {}).get("name", "")
    pf      = PARK_FACTORS.get(venue, 1.00)

    status_obj = g.get("status", {})
    ab_code = status_obj.get("abstractGameCode", "")
    detail  = status_obj.get("detailedState", "Scheduled")
    if ab_code == "L":
        status = "Live"
    elif ab_code == "F":
        status = "Final"
    else:
        status = "Scheduled"

    game_time = ""
    raw_time = g.get("gameDate", "")
    if raw_time:
        try:
            utc_dt = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
            et_dt  = utc_dt.astimezone(ET)
            game_time = et_dt.strftime("%-I:%M %p ET")
        except: game_time = raw_time

    away_sp_name = ""
    home_sp_name = ""
    try:
        away_sp = teams.get("away", {}).get("probablePitcher", {})
        home_sp = teams.get("home", {}).get("probablePitcher", {})
        away_sp_name = away_sp.get("fullName", "TBD")
        home_sp_name = home_sp.get("fullName", "TBD")
    except: pass

    away_id = away_t.get("id", 0)
    home_id = home_t.get("id", 0)

    pf_adj = (pf - 1.0) * 100
    edge = round(5.0 + pf_adj * 2, 1)
    edge = max(0.5, min(25.0, edge))
    bar_pct = min(100, int(edge * 4))

    return {
        "gamePk": g.get("gamePk"),
        "status": status,
        "detailedState": detail,
        "gameTime": game_time,
        "venue": venue,
        "parkFactor": pf,
        "edge": edge,
        "barPct": bar_pct,
        "awayId": away_id, "awayAbbr": away_t.get("abbreviation", ""),
        "awayName": away_t.get("name", ""),
        "awayLogo": f"https://www.mlbstatic.com/team-logos/{away_id}.svg",
        "homeLogo": f"https://www.mlbstatic.com/team-logos/{home_id}.svg",
        "homeId": home_id, "homeAbbr": home_t.get("abbreviation", ""),
        "homeName": home_t.get("name", ""),
        "awayPitcher": away_sp_name,
        "homePitcher": home_sp_name,
        "temp": "N/A", "wind": "N/A", "condition": "N/A", "rainChance": "N/A",
        "isDome": venue in DOME_PARKS,
    }

# ── Static / HTML routes ─────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(".", "dashboard.html")

@app.route("/deepdive/<int:game_pk>")
def deepdive(game_pk):
    return send_from_directory(".", "deepdive.html")

@app.route("/parlays")
def parlays():
    return send_from_directory(".", "parlays.html")

@app.route("/tracker")
def tracker():
    return send_from_directory(".", "tracker.html")

@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory("static", filename)

# ── API: Today's Games ───────────────────────────────────────────────────────

@app.route("/api/games/today")
def api_games_today():
    try:
        date_str = datetime.now(ET).strftime("%Y-%m-%d")
        r = requests.get(f"{MLB_API}/schedule",
            params={"sportId": 1, "date": date_str,
                    "hydrate": "team,venue,probablePitcher,status,linescore",
                    "fields": "dates,games,gamePk,gameDate,status,teams,venue,linescore"},
            timeout=10)
        r.raise_for_status()
        dates = r.json().get("dates", [])
        games_raw = dates[0].get("games", []) if dates else []
        games = [_build_game(g) for g in games_raw]
        for game in games:
            if game["isDome"]:
                game["temp"] = "DOME"
        return jsonify({"success": True, "date": date_str, "games": games})
    except Exception as ex:
        print("[api_games_today]", traceback.format_exc())
        return jsonify({"success": False, "error": str(ex)}), 500

# ── API: Game Lineup ─────────────────────────────────────────────────────────

@app.route("/api/game/<int:game_pk>")
def api_game(game_pk):
    try:
        r = requests.get(f"{MLB_API}/game/{game_pk}/boxscore", timeout=10)
        r.raise_for_status()
        box = r.json()
        teams = box.get("teams", {})

        def parse_batters(side_data):
            batters = []
            batting_order = side_data.get("battingOrder", [])
            players = side_data.get("players", {})
            for pid in batting_order:
                key = f"ID{pid}"
                p = players.get(key, {})
                person = p.get("person", {})
                pos = p.get("position", {}).get("abbreviation", "")
                stats = p.get("seasonStats", {}).get("batting", {})
                slot = batting_order.index(pid) + 1
                name = person.get("fullName", "Unknown")
                pid_int = person.get("id", 0)
                fg = fg_batter(name)
                sv_stats = sv_batter(name)
                batters.append({
                    "id": pid_int, "name": name, "pos": pos, "slot": slot,
                    "avg": stats.get("avg", ".000"),
                    "obp": stats.get("obp", ".000"),
                    "slg": stats.get("slg", ".000"),
                    "ops": stats.get("ops", ".000"),
                    "hr": stats.get("homeRuns", 0),
                    "rbi": stats.get("rbi", 0),
                    **fg, **sv_stats,
                })
            return batters

        away_batters = parse_batters(teams.get("away", {}))
        home_batters = parse_batters(teams.get("home", {}))
        return jsonify({"success": True, "gamePk": game_pk,
                        "awayBatters": away_batters, "homeBatters": home_batters})
    except Exception as ex:
        print("[api_game]", traceback.format_exc())
        return jsonify({"success": False, "error": str(ex)}), 500

# ── API: Pitchers ────────────────────────────────────────────────────────────

@app.route("/api/pitchers/<int:game_pk>")
def api_pitchers(game_pk):
    try:
        date_str = datetime.now(ET).strftime("%Y-%m-%d")
        r = requests.get(f"{MLB_API}/schedule",
            params={"sportId": 1, "date": date_str,
                    "hydrate": "probablePitcher,team",
                    "gamePk": game_pk},
            timeout=10)
        r.raise_for_status()
        dates = r.json().get("dates", [])
        game = None
        for d in dates:
            for g in d.get("games", []):
                if g.get("gamePk") == game_pk:
                    game = g; break

        def build_pitcher(team_key):
            if not game: return {"name": "TBD", "stats": {}}
            pp = game.get("teams", {}).get(team_key, {}).get("probablePitcher", {})
            pid = pp.get("id")
            name = pp.get("fullName", "TBD")
            if not pid: return {"name": name, "stats": {}}
            sr = requests.get(f"{MLB_API}/people/{pid}/stats",
                params={"stats": "season", "group": "pitching",
                        "season": datetime.now(ET).year}, timeout=8)
            sr.raise_for_status()
            splits = (sr.json().get("stats") or [{}])[0].get("splits", [])
            mlb_stats = splits[0].get("stat", {}) if splits else {}
            fg = fg_pitcher(name)
            sv = sv_pitcher(name)
            combined = {**mlb_stats, **fg, **sv,
                        "era": mlb_stats.get("era", "N/A"),
                        "whip": mlb_stats.get("whip", "N/A"),
                        "k9": mlb_stats.get("strikeoutsPer9Inn", "N/A"),
                        "bb9": mlb_stats.get("walksPer9Inn", "N/A"),
                        "ip": mlb_stats.get("inningsPitched", "N/A"),
                        "g": mlb_stats.get("gamesPlayed", 0),
                        "gs": mlb_stats.get("gamesStarted", 0),
                        "wins": mlb_stats.get("wins", 0),
                        "losses": mlb_stats.get("losses", 0)}
            return {"name": name, "id": pid, "stats": combined}

        return jsonify({"success": True, "gamePk": game_pk,
                        "awayPitcher": build_pitcher("away"),
                        "homePitcher": build_pitcher("home")})
    except Exception as ex:
        print("[api_pitchers]", traceback.format_exc())
        return jsonify({"success": False, "error": str(ex)}), 500

# ── API: Monte Carlo Projections ─────────────────────────────────────────────

@app.route("/api/monte-carlo/<int:game_pk>")
def api_monte_carlo(game_pk):
    try:
        import random
        date_str = datetime.now(ET).strftime("%Y-%m-%d")
        r = requests.get(f"{MLB_API}/schedule",
            params={"sportId": 1, "date": date_str, "gamePk": game_pk,
                    "hydrate": "team,venue,probablePitcher"}, timeout=10)
        r.raise_for_status()
        dates = r.json().get("dates", [])
        game = None
        for d in dates:
            for g in d.get("games", []):
                if g.get("gamePk") == game_pk:
                    game = g; break

        venue  = game.get("venue", {}).get("name", "") if game else ""
        pf     = PARK_FACTORS.get(venue, 1.00)
        sims   = 10000
        away_wins = 0
        total_runs_list = []

        for _ in range(sims):
            away_runs = 0; home_runs = 0
            for inning in range(9):
                away_runs += max(0, int(random.gauss(0.5 * pf, 0.8)))
                home_runs += max(0, int(random.gauss(0.5 * pf, 0.8)))
            if away_runs > home_runs: away_wins += 1
            total_runs_list.append(away_runs + home_runs)

        home_wins  = sims - away_wins
        avg_total  = sum(total_runs_list) / sims
        over_8     = sum(1 for t in total_runs_list if t > 8.5) / sims * 100
        over_9     = sum(1 for t in total_runs_list if t > 9.5) / sims * 100
        over_7     = sum(1 for t in total_runs_list if t > 7.5) / sims * 100

        away_abbr = game.get("teams", {}).get("away", {}).get("team", {}).get("abbreviation", "AWAY") if game else "AWAY"
        home_abbr = game.get("teams", {}).get("home", {}).get("team", {}).get("abbreviation", "HOME") if game else "HOME"

        return jsonify({
            "success": True, "gamePk": game_pk, "sims": sims,
            "awayWinPct": round(away_wins / sims * 100, 1),
            "homeWinPct": round(home_wins / sims * 100, 1),
            "avgTotal": round(avg_total, 2),
            "over7Pct": round(over_7, 1),
            "over8Pct": round(over_8, 1),
            "over9Pct": round(over_9, 1),
            "awayAbbr": away_abbr, "homeAbbr": home_abbr,
        })
    except Exception as ex:
        print("[api_monte_carlo]", traceback.format_exc())
        return jsonify({"success": False, "error": str(ex)}), 500

# ── API: Live Game Data ───────────────────────────────────────────────────────

@app.route("/api/game/livedata/<int:game_pk>")
def api_game_livedata(game_pk):
    try:
        r = requests.get(
            f"https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live",
            timeout=10)
        r.raise_for_status()
        data = r.json()

        ls       = data.get("liveData", {}).get("linescore", {})
        box      = data.get("liveData", {}).get("boxscore",  {})
        gdata    = data.get("gameData", {})
        status_d = gdata.get("status", {}).get("detailedState", "")

        inning_num  = ls.get("currentInning", 0)
        inning_half = ls.get("inningHalf", "Top")
        if any(x in status_d for x in ["Middle", "Mid ", "Between"]):
            inning_label = f"MID {inning_num}"
        elif inning_half == "Bottom":
            inning_label = f"BOT {inning_num}"
        else:
            inning_label = f"TOP {inning_num}"

        ht = ls.get("teams", {}).get("home", {})
        at = ls.get("teams", {}).get("away", {})
        offense = ls.get("offense", {})
        bases = {
            "first":  bool(offense.get("first")),
            "second": bool(offense.get("second")),
            "third":  bool(offense.get("third")),
        }
        batter_id   = (offense.get("batter")  or {}).get("id")
        batter_name = (offense.get("batter")  or {}).get("fullName", "")
        on_deck     = (offense.get("onDeck")  or {}).get("fullName", "")
        in_hole     = (offense.get("inHole")  or {}).get("fullName", "")
        pitcher_id   = (ls.get("defense", {}).get("pitcher") or {}).get("id")
        pitcher_name = (ls.get("defense", {}).get("pitcher") or {}).get("fullName", "")

        pitcher_ip = "\u2014"; pitcher_er = "\u2014"
        batter_ab  = 0;        batter_h   = 0;  batter_ops = "\u2014"
        box_teams  = box.get("teams", {})
        for side in ("home", "away"):
            players = box_teams.get(side, {}).get("players", {})
            if pitcher_id:
                ps  = players.get(f"ID{pitcher_id}", {})
                pst = ps.get("stats", {}).get("pitching", {})
                if pst:
                    pitcher_ip = pst.get("inningsPitched", "\u2014")
                    pitcher_er = pst.get("earnedRuns", "\u2014")
            if batter_id:
                bs  = players.get(f"ID{batter_id}", {})
                bst = bs.get("stats", {}).get("batting", {})
                bss = bs.get("seasonStats", {}).get("batting", {})
                if bst: batter_ab = bst.get("atBats", 0); batter_h = bst.get("hits", 0)
                if bss: batter_ops = bss.get("ops", "\u2014")

        innings = ls.get("innings", [])
        inning_scores = [{"num": i.get("num"), "away": i.get("away", {}).get("runs", ""),
                          "home": i.get("home", {}).get("runs", "")} for i in innings]

        return jsonify({
            "success": True, "gamePk": game_pk, "statusDetail": status_d,
            "inningLabel": inning_label,
            "balls": ls.get("balls", 0), "strikes": ls.get("strikes", 0), "outs": ls.get("outs", 0),
            "awayRuns": at.get("runs", 0), "awayHits": at.get("hits", 0), "awayErrors": at.get("errors", 0),
            "homeRuns": ht.get("runs", 0), "homeHits": ht.get("hits", 0), "homeErrors": ht.get("errors", 0),
            "bases": bases,
            "pitcher": {"name": pitcher_name, "ip": pitcher_ip, "er": pitcher_er},
            "batter":  {"name": batter_name,  "ab": batter_ab,  "h": batter_h, "ops": batter_ops},
            "dueUp":   [on_deck, in_hole],
            "inningScores": inning_scores,
        })
    except Exception as ex:
        print("[api_game_livedata]", traceback.format_exc())
        return jsonify({"success": False, "error": str(ex)}), 500

# ── API: Player Profile ───────────────────────────────────────────────────────

@app.route("/api/player/<int:player_id>")
def api_player_profile(player_id):
    try:
        year = datetime.now(ET).year
        bio_r = requests.get(f"{MLB_API}/people/{player_id}", timeout=8)
        bio_r.raise_for_status()
        pdata = (bio_r.json().get("people") or [{}])[0]
        name  = pdata.get("fullName", "")
        pos   = pdata.get("primaryPosition", {}).get("abbreviation", "?")
        bats  = pdata.get("batSide", {}).get("code", "S")
        throws = pdata.get("pitchHand", {}).get("code", "R")
        team  = pdata.get("currentTeam", {}).get("name", "")
        headshot = (f"https://img.mlbstatic.com/mlb-photos/image/upload/"
                    f"d_people:generic:headshot:67:current.png/w_213,q_auto:best/"
                    f"v1/people/{player_id}/headshot/67/current")
        is_pitcher = pos in ("P", "SP", "RP", "CL")
        group = "pitching" if is_pitcher else "hitting"
        sr = requests.get(f"{MLB_API}/people/{player_id}/stats",
            params={"stats": "season", "group": group, "season": year}, timeout=8)
        sr.raise_for_status()
        season_sp = (sr.json().get("stats") or [{}])[0].get("splits", [])
        season = season_sp[0].get("stat", {}) if season_sp else {}
        lr = requests.get(f"{MLB_API}/people/{player_id}/stats",
            params={"stats": "gameLog", "group": group, "season": year}, timeout=8)
        lr.raise_for_status()
        all_games = (lr.json().get("stats") or [{}])[0].get("splits", [])
        last15 = all_games[-15:]
        gamelog = []
        if not is_pitcher:
            for sp in last15:
                s = sp.get("stat", {})
                gamelog.append({"date": sp.get("date", ""), "opp": sp.get("opponent", {}).get("abbreviation", ""),
                                 "ab": s.get("atBats", 0), "h": s.get("hits", 0), "hr": s.get("homeRuns", 0),
                                 "rbi": s.get("rbi", 0), "k": s.get("strikeOuts", 0), "bb": s.get("baseOnBalls", 0),
                                 "sb": s.get("stolenBases", 0), "avg": s.get("avg", "---")})
        else:
            for sp in last15:
                s = sp.get("stat", {})
                gamelog.append({"date": sp.get("date", ""), "opp": sp.get("opponent", {}).get("abbreviation", ""),
                                 "ip": s.get("inningsPitched", "0"), "er": s.get("earnedRuns", 0),
                                 "k": s.get("strikeOuts", 0), "bb": s.get("baseOnBalls", 0),
                                 "h": s.get("hits", 0), "hr": s.get("homeRuns", 0)})
        fg = fg_pitcher(name) if is_pitcher else fg_batter(name)
        sv = sv_pitcher(name) if is_pitcher else sv_batter(name)
        return jsonify({
            "success": True,
            "player": {"id": player_id, "name": name, "pos": pos, "bats": bats,
                       "throws": throws, "team": team, "headshot": headshot},
            "season": season, "fg": fg, "savant": sv,
            "gamelog": gamelog, "isPitcher": is_pitcher,
        })
    except Exception as ex:
        print("[api_player_profile]", traceback.format_exc())
        return jsonify({"success": False, "error": str(ex)}), 500

# ── Health check ─────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({"status": "ok", "time_et": datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET")})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
