import os, threading, traceback, difflib, io, csv as csvmod
import requests
from datetime import datetime, timezone
from flask import Flask, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

_HERE = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_HTML = open(os.path.join(_HERE, 'dashboard.html')).read()
DEEP_DIVE_HTML = open(os.path.join(_HERE, 'deepdive.html')).read()

MLB_API   = "https://statsapi.mlb.com/api/v1"
WX_API    = "https://api.open-meteo.com/v1/forecast"
LOGO_BASE = "https://www.mlbstatic.com/team-logos/{team_id}.svg"
PARK_FACTORS = {
    133:1.08,144:0.92,110:0.97,111:1.04,112:0.97,137:0.95,109:1.06,
    145:1.03,116:1.00,158:0.97,142:1.00,147:0.97,143:1.03,140:1.05,
    146:0.95,121:0.97,136:0.93,138:1.02,141:0.98,139:0.99,108:0.96,
    117:0.97,135:0.98,120:0.98,134:0.97,119:0.95,118:1.02,114:1.01,
    113:0.94,115:1.00,158:0.97
}

# ── FanGraphs Cache ───────────────────────────────────────────────────────────
_fg_lock = threading.Lock()
_fg_bat  = {}
_fg_pit  = {}
_fg_loaded    = False
_fg_load_date = None

def _load_fg_data():
    global _fg_bat, _fg_pit, _fg_loaded, _fg_load_date
    year = datetime.now().year
    try:
        import pybaseball as pb
        pb.cache.enable()
        df = pb.batting_stats(year, qual=1)
        bat = {}
        for _, r in df.iterrows():
            k = str(r.get("Name","")).strip().lower()
            if k:
                bat[k] = {
                    "fg_avg": round(float(r.get("AVG") or 0),3),
                    "fg_obp": round(float(r.get("OBP") or 0),3),
                    "fg_slg": round(float(r.get("SLG") or 0),3),
                    "fg_ops": round(float(r.get("OPS") or 0),3),
                    "fg_woba":round(float(r.get("wOBA") or 0),3),
                    "fg_wrc": int(r.get("wRC+") or 0),
                    "fg_pa":  int(r.get("PA")   or 0),
                    "fg_r":   int(r.get("R")    or 0),
                    "fg_hr":  int(r.get("HR")   or 0),
                    "fg_rbi": int(r.get("RBI")  or 0),
                    "fg_sb":  int(r.get("SB")   or 0),
                    "fg_war": round(float(r.get("WAR") or 0),1),
                }
        with _fg_lock: _fg_bat = bat
        print(f"[FG] Batting: {len(bat)}")
    except Exception as ex:
        print("[FG] Batting failed:", ex)
    try:
        import pybaseball as pb
        df = pb.pitching_stats(year, qual=1)
        pit = {}
        for _, r in df.iterrows():
            k = str(r.get("Name","")).strip().lower()
            if k:
                pit[k] = {
                    "fg_era":  round(float(r.get("ERA")  or 0),2),
                    "fg_fip":  round(float(r.get("FIP")  or 0),2),
                    "fg_xfip": round(float(r.get("xFIP") or 0),2),
                    "fg_whip": round(float(r.get("WHIP") or 0),2),
                    "fg_k9":   round(float(r.get("K/9")  or 0),2),
                    "fg_bb9":  round(float(r.get("BB/9") or 0),2),
                    "fg_hr9":  round(float(r.get("HR/9") or 0),2),
                    "fg_kpct": round(float(r.get("K%")   or 0),3),
                    "fg_bbpct":round(float(r.get("BB%")  or 0),3),
                    "fg_babip":round(float(r.get("BABIP") or 0),3),
                    "fg_lob":  round(float(r.get("LOB%") or 0),3),
                    "fg_war":  round(float(r.get("WAR")  or 0),1),
                    "fg_ip":   round(float(r.get("IP")   or 0),1),
                    "fg_g":    int(r.get("G")  or 0),
                    "fg_gs":   int(r.get("GS") or 0),
                    "fg_w":    int(r.get("W")  or 0),
                    "fg_l":    int(r.get("L")  or 0),
                }
        with _fg_lock: _fg_pit = pit
        print(f"[FG] Pitching: {len(pit)}")
    except Exception as ex:
        print("[FG] Pitching failed:", ex)
    with _fg_lock:
        _fg_loaded    = True
        _fg_load_date = datetime.now().date()

def _maybe_refresh_fg():
    with _fg_lock:
        loaded = _fg_loaded; date = _fg_load_date
    if not loaded or date != datetime.now().date():
        threading.Thread(target=_load_fg_data, daemon=True).start()

def _fuzzy_lookup(name, cache):
    if not name or not cache: return {}
    k = name.strip().lower()
    if k in cache: return cache[k]
    m = difflib.get_close_matches(k, cache.keys(), n=1, cutoff=0.82)
    return cache[m[0]] if m else {}

def fg_batter(name):
    with _fg_lock: c = dict(_fg_bat)
    return _fuzzy_lookup(name, c)

def fg_pitcher(name):
    with _fg_lock: c = dict(_fg_pit)
    return _fuzzy_lookup(name, c)

# ── Baseball Savant Cache ─────────────────────────────────────────────────────
_sv_lock         = threading.Lock()
_sv_pit_xstats   = {}
_sv_bat_xstats   = {}
_sv_bat_statcast = {}
_sv_arsenal_pct  = {}
_sv_arsenal_velo = {}
_sv_loaded    = False
_sv_load_date = None

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
    except: return "N/A"

def _fetch_sv_csv(url):
    r = requests.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=15)
    r.raise_for_status()
    text = r.text.lstrip("\ufeff")
    return list(csvmod.DictReader(io.StringIO(text)))

def _load_savant_data():
    global _sv_pit_xstats, _sv_bat_xstats, _sv_bat_statcast
    global _sv_arsenal_pct, _sv_arsenal_velo, _sv_loaded, _sv_load_date
    y = datetime.now().year
    BASE = "https://baseballsavant.mlb.com"

    # 1. Pitcher xERA
    try:
        rows = _fetch_sv_csv(f"{BASE}/leaderboard/expected_statistics?type=pitcher&year={y}&position=&team=&min=1&csv=true")
        d = {}
        for row in rows:
            raw = row.get("last_name, first_name","").strip()
            if not raw: continue
            d[_sv_key(raw)] = {
                "sv_xera":  _sv_f(row.get("xera")),
                "sv_era_p": _sv_f(row.get("era")),
                "sv_xwoba_p": _sv_f(row.get("est_woba")),
                "sv_pid":   row.get("player_id",""),
            }
        with _sv_lock: _sv_pit_xstats = d
        print(f"[Savant] Pitcher xStats: {len(d)}")
    except Exception as ex:
        print("[Savant] Pitcher xStats failed:", ex)

    # 2. Batter xBA/xSLG/xwOBA
    try:
        rows = _fetch_sv_csv(f"{BASE}/leaderboard/expected_statistics?type=batter&year={y}&position=&team=&min=1&csv=true")
        d = {}
        for row in rows:
            raw = row.get("last_name, first_name","").strip()
            if not raw: continue
            d[_sv_key(raw)] = {
                "sv_xba":   _sv_f(row.get("est_ba")),
                "sv_xslg":  _sv_f(row.get("est_slg")),
                "sv_xwoba": _sv_f(row.get("est_woba")),
                "sv_pid":   row.get("player_id",""),
            }
        with _sv_lock: _sv_bat_xstats = d
        print(f"[Savant] Batter xStats: {len(d)}")
    except Exception as ex:
        print("[Savant] Batter xStats failed:", ex)

    # 3. Statcast batter EV / HH% / Barrel%
    try:
        rows = _fetch_sv_csv(f"{BASE}/leaderboard/statcast?type=batter&year={y}&position=&team=&min=q&csv=true")
        d = {}
        for row in rows:
            raw = row.get("last_name, first_name","").strip()
            if not raw: continue
            d[_sv_key(raw)] = {
                "sv_ev":     _sv_f(row.get("avg_hit_speed")),
                "sv_hh_pct": _sv_f(row.get("ev95percent")),
                "sv_brl_pct":_sv_f(row.get("brl_percent")),
                "sv_brl_pa": _sv_f(row.get("brl_pa")),
                "sv_la":     _sv_f(row.get("avg_hit_angle")),
                "sv_ss_pct": _sv_f(row.get("anglesweetspotpercent")),
                "sv_max_ev": _sv_f(row.get("max_hit_speed")),
            }
        with _sv_lock: _sv_bat_statcast = d
        print(f"[Savant] Batter Statcast: {len(d)}")
    except Exception as ex:
        print("[Savant] Batter Statcast failed:", ex)

    # 4. Pitch arsenal % usage
    try:
        rows = _fetch_sv_csv(f"{BASE}/leaderboard/pitch-arsenals?year={y}&min=1&type=n_&hand=&csv=true")
        d = {}
        for row in rows:
            raw = row.get("last_name, first_name","").strip()
            if not raw: continue
            pitches = {}
            for pt in PITCH_ORDER:
                v = row.get("n_" + pt,"").strip()
                if v:
                    try: pitches[pt] = round(float(v), 1)
                    except: pass
            if pitches: d[_sv_key(raw)] = pitches
        with _sv_lock: _sv_arsenal_pct = d
        print(f"[Savant] Arsenal %: {len(d)}")
    except Exception as ex:
        print("[Savant] Arsenal % failed:", ex)

    # 5. Pitch arsenal velocities
    try:
        rows = _fetch_sv_csv(f"{BASE}/leaderboard/pitch-arsenals?year={y}&min=1&type=avg_speed&hand=&csv=true")
        d = {}
        for row in rows:
            raw = row.get("last_name, first_name","").strip()
            if not raw: continue
            velos = {}
            for pt in PITCH_ORDER:
                v = row.get(pt + "_avg_speed","").strip()
                if v:
                    try: velos[pt] = round(float(v), 1)
                    except: pass
            if velos: d[_sv_key(raw)] = velos
        with _sv_lock: _sv_arsenal_velo = d
        print(f"[Savant] Arsenal velo: {len(d)}")
    except Exception as ex:
        print("[Savant] Arsenal velo failed:", ex)

    with _sv_lock:
        _sv_loaded    = True
        _sv_load_date = datetime.now().date()
    print("[Savant] All caches ready")

def _maybe_refresh_savant():
    with _sv_lock:
        loaded = _sv_loaded; date = _sv_load_date
    if not loaded or date != datetime.now().date():
        threading.Thread(target=_load_savant_data, daemon=True).start()

def sv_pitcher(name):
    with _sv_lock:
        xs = dict(_sv_pit_xstats)
        ap = dict(_sv_arsenal_pct)
        av = dict(_sv_arsenal_velo)
    r = dict(_fuzzy_lookup(name, xs))
    r["sv_arsenal_pct"]  = _fuzzy_lookup(name, ap)
    r["sv_arsenal_velo"] = _fuzzy_lookup(name, av)
    return r

def sv_batter(name):
    with _sv_lock:
        xs = dict(_sv_bat_xstats)
        sc = dict(_sv_bat_statcast)
    r = dict(_fuzzy_lookup(name, xs))
    r.update(_fuzzy_lookup(name, sc))
    return r

# ── MLB API Helpers ───────────────────────────────────────────────────────────
def fetch_schedule(date_str):
    url = (f"{MLB_API}/schedule?sportId=1&date={date_str}"
           "&hydrate=team,probablePitcher,linescore,venue,weather")
    r = requests.get(url, timeout=10); r.raise_for_status()
    dates = r.json().get("dates", [])
    return dates[0].get("games", []) if dates else []

def get_weather(lat, lon):
    try:
        r = requests.get(WX_API, params={
            "latitude":lat,"longitude":lon,
            "hourly":"temperature_2m,precipitation_probability,windspeed_10m,weathercode",
            "temperature_unit":"fahrenheit","windspeed_unit":"mph",
            "forecast_days":1,"timezone":"auto"
        }, timeout=6)
        r.raise_for_status()
        h = r.json().get("hourly",{})
        idx = 13
        wcode_map = {0:"Clear",1:"Mainly Clear",2:"Partly Cloudy",3:"Overcast",
                     45:"Foggy",48:"Foggy",51:"Drizzle",53:"Drizzle",55:"Drizzle",
                     61:"Rain",63:"Rain",65:"Heavy Rain",71:"Snow",73:"Snow",75:"Snow",
                     80:"Showers",81:"Showers",82:"Heavy Showers",
                     95:"Thunderstorm",96:"Thunderstorm",99:"Thunderstorm"}
        wcode = h.get("weathercode",[0]*24)[idx]
        return {
            "temp":       round(h.get("temperature_2m",[70]*24)[idx]),
            "rain_chance":h.get("precipitation_probability",[0]*24)[idx],
            "wind_speed": round(h.get("windspeed_10m",[0]*24)[idx]),
            "condition":  wcode_map.get(wcode, "Clear"),
        }
    except: return {"temp":"N/A","rain_chance":"N/A","wind_speed":"N/A","condition":"N/A"}

def pitcher_stats_mlb(player_id):
    try:
        r = requests.get(f"{MLB_API}/people/{player_id}/stats?stats=season&group=pitching&season={datetime.now().year}", timeout=8)
        r.raise_for_status()
        splits = r.json().get("stats",[{}])[0].get("splits",[])
        if not splits: return {}
        s = splits[0].get("stat",{})
        return {
            "era":  s.get("era","N/A"), "whip": s.get("whip","N/A"),
            "ip":   s.get("inningsPitched","N/A"),
            "wins": s.get("wins",0), "losses": s.get("losses",0),
            "g":    s.get("gamesPlayed",0), "gs": s.get("gamesStarted",0),
            "k9":   round(float(s.get("strikeoutsPer9Inn",0) or 0),2),
            "bb9":  round(float(s.get("walksPer9Inn",0) or 0),2),
            "hr9":  round(float(s.get("homeRunsPer9",0) or 0),2),
        }
    except: return {}

def get_batters_from_boxscore(team_data, side):
    out = []
    batters = team_data.get("batters",[])
    players = team_data.get("players",{})
    for pid in batters:
        key = f"ID{pid}"
        p   = players.get(key,{})
        name= p.get("person",{}).get("fullName","")
        pos = p.get("position",{}).get("abbreviation","?")
        s   = p.get("stats",{}).get("batting",{})
        ss  = p.get("seasonStats",{}).get("batting",{})
        slot= p.get("battingOrder",0)
        try: slot = int(str(slot)[0])
        except: slot = 0
        fgb = fg_batter(name)
        svb = sv_batter(name)
        out.append({
            "slot": slot, "id": pid, "name": name, "pos": pos,
            "avg":  ss.get("avg",  fgb.get("fg_avg",".---")),
            "obp":  ss.get("obp",  fgb.get("fg_obp",".---")),
            "slg":  ss.get("slg",  fgb.get("fg_slg",".---")),
            "ops":  ss.get("ops",  fgb.get("fg_ops",".---")),
            "ab":   s.get("atBats",0), "hits": s.get("hits",0),
            "hr":   s.get("homeRuns",0), "rbi": s.get("rbi",0),
            # FanGraphs
            "fg_pa":  fgb.get("fg_pa","N/A"), "fg_r":  fgb.get("fg_r","N/A"),
            "fg_sb":  fgb.get("fg_sb","N/A"), "fg_woba":fgb.get("fg_woba","N/A"),
            "fg_wrc": fgb.get("fg_wrc","N/A"), "fg_war":fgb.get("fg_war","N/A"),
            # Baseball Savant
            "sv_xba":    svb.get("sv_xba","N/A"),
            "sv_xslg":   svb.get("sv_xslg","N/A"),
            "sv_xwoba":  svb.get("sv_xwoba","N/A"),
            "sv_ev":     svb.get("sv_ev","N/A"),
            "sv_hh_pct": svb.get("sv_hh_pct","N/A"),
            "sv_brl_pct":svb.get("sv_brl_pct","N/A"),
            "sv_la":     svb.get("sv_la","N/A"),
        })
    return out

def parse_game(g):
    try:
        pk   = g.get("gamePk")
        stat = g.get("status",{}).get("detailedState","Scheduled")
        st   = "Live" if "Progress" in stat else ("Final" if "Final" in stat else "Scheduled")
        away = g.get("teams",{}).get("away",{})
        home = g.get("teams",{}).get("home",{})
        at   = away.get("team",{}); ht = home.get("team",{})
        aid  = at.get("id"); hid = ht.get("id")
        ap   = away.get("probablePitcher",{}); hp = home.get("probablePitcher",{})
        ven  = g.get("venue",{})
        vloc = ven.get("location",{})
        lat  = vloc.get("defaultCoordinates",{}).get("latitude")
        lon  = vloc.get("defaultCoordinates",{}).get("longitude")
        wx   = get_weather(lat, lon) if lat and lon else {}
        gt   = g.get("gameDate","")
        try:
            dt_utc = datetime.fromisoformat(gt.replace("Z","+00:00"))
            dt_et  = dt_utc.astimezone()
            gt_fmt = dt_et.strftime("%-I:%M %p ET")
        except: gt_fmt = "TBD"
        pf   = PARK_FACTORS.get(hid, 1.0)
        ap_n = ap.get("fullName","TBD"); hp_n = hp.get("fullName","TBD")
        fgap = fg_pitcher(ap_n); fghp = fg_pitcher(hp_n)
        era_a = float(fgap.get("fg_era") or 4.50); era_h = float(fghp.get("fg_era") or 4.50)
        edge = round(abs(era_a - era_h) * 2 + (pf - 1.0) * 10, 1)
        bar  = min(100, int(edge * 9))
        wc   = (wx.get("condition","") or "").lower()
        wi   = "🌧" if "rain" in wc else ("⛅" if "cloud" in wc else "☀")
        return {
            "gamePk": pk, "status": st,
            "awayAbbr": at.get("abbreviation","?"), "awayName": at.get("name",""),
            "homeAbbr": ht.get("abbreviation","?"), "homeName": ht.get("name",""),
            "awayLogo": LOGO_BASE.format(team_id=aid) if aid else "",
            "homeLogo": LOGO_BASE.format(team_id=hid) if hid else "",
            "awayPitcher": ap_n, "homePitcher": hp_n,
            "venue": ven.get("name",""), "gameTime": gt_fmt,
            "parkFactor": pf, "edge": edge, "barPct": bar,
            "temp": wx.get("temp","N/A"), "wind": f"{wx.get('wind_speed','?')} mph",
            "condition": wx.get("condition",""), "rainChance": wx.get("rain_chance","N/A"),
            "weatherIcon": wi,
        }
    except Exception as ex:
        print("[parse_game]", ex); return None

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def dashboard():
    return DASHBOARD_HTML

@app.route("/deep-dive/<int:game_pk>")
def deep_dive(game_pk):
    return DEEP_DIVE_HTML

@app.route("/api/status")
def api_status():
    with _fg_lock:
        fgl, fgd, fgb, fgp = _fg_loaded, _fg_load_date, len(_fg_bat), len(_fg_pit)
    with _sv_lock:
        svl, svd = _sv_loaded, _sv_load_date
        svpi, svbi, svsc, svar = len(_sv_pit_xstats), len(_sv_bat_xstats), len(_sv_bat_statcast), len(_sv_arsenal_pct)
    return jsonify({
        "fangraphs": {"loaded":fgl,"date":str(fgd),"batters":fgb,"pitchers":fgp},
        "savant":    {"loaded":svl,"date":str(svd),"pit_xstats":svpi,"bat_xstats":svbi,"statcast":svsc,"arsenals":svar},
    })

@app.route("/api/games/today")
def api_games_today():
    _maybe_refresh_fg()
    _maybe_refresh_savant()
    try:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        raw   = fetch_schedule(date_str)
        games = [g for g in [parse_game(x) for x in raw] if g]
        return jsonify({"success":True,"games":games,"count":len(games)})
    except Exception as ex:
        print("[api_games_today]", traceback.format_exc())
        return jsonify({"success":False,"error":str(ex),"games":[]}), 500

@app.route("/api/game/<int:game_pk>")
def api_game_detail(game_pk):
    try:
        r = requests.get(f"{MLB_API}/game/{game_pk}/boxscore", timeout=10)
        r.raise_for_status()
        d = r.json().get("teams",{})
        return jsonify({
            "success": True,
            "awayBatters": get_batters_from_boxscore(d.get("away",{}), "away"),
            "homeBatters": get_batters_from_boxscore(d.get("home",{}), "home"),
        })
    except Exception as ex:
        print("[api_game_detail]", traceback.format_exc())
        return jsonify({"success":False,"error":str(ex),"awayBatters":[],"homeBatters":[]}), 500

@app.route("/api/pitchers/<int:game_pk>")
def api_pitchers(game_pk):
    try:
        raw = fetch_schedule(datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        for g in raw:
            if g.get("gamePk") == game_pk:
                ap = g.get("teams",{}).get("away",{}).get("probablePitcher",{})
                hp = g.get("teams",{}).get("home",{}).get("probablePitcher",{})
                an = ap.get("fullName","TBD"); hn = hp.get("fullName","TBD")
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
                    return s
                return jsonify({
                    "success": True,
                    "awayPitcher": {"id":ap.get("id"),"name":an,"stats":build_pitcher_stats(an,ap.get("id"))},
                    "homePitcher": {"id":hp.get("id"),"name":hn,"stats":build_pitcher_stats(hn,hp.get("id"))},
                })
        return jsonify({"success":False,"error":"Game not found","awayPitcher":{},"homePitcher":{}})
    except Exception as ex:
        print("[api_pitchers]", traceback.format_exc())
        return jsonify({"success":False,"error":str(ex),"awayPitcher":{},"homePitcher":{}}), 500

@app.errorhandler(404)
def e404(e): return jsonify({"error":"Not found"}), 404
@app.errorhandler(500)
def e500(e): return jsonify({"error":str(e)}), 500


# ── Phase 3 Routes ────────────────────────────────────────────────────────────
@app.route("/api/game-projection/<int:game_pk>")
def api_game_projection(game_pk):
    try:
        raw = fetch_schedule(datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        gdata = next((g for g in raw if g.get("gamePk") == game_pk), None)
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
            for v in [sv.get("sv_xera"), fg.get("fg_era"), mlb.get("era")]:
                try:
                    f = float(v)
                    if 0 < f < 12: return f
                except: pass
            return 4.50
        def best_fip(fg, fallback):
            try:
                f = float(fg.get("fg_fip",0))
                if 0 < f < 12: return f
            except: pass
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
        except:
            away_bats = []; home_bats = []
        def lineup_xwoba(bats):
            vals = []
            for b in bats:
                for k in ["sv_xwoba","fg_woba"]:
                    try:
                        f = float(b.get(k,0))
                        if 0.1 < f < 0.6: vals.append(f); break
                    except: pass
                else:
                    vals.append(0.320)
            return round(sum(vals)/len(vals), 3) if vals else 0.320
        away_xwoba = lineup_xwoba(away_bats)
        home_xwoba = lineup_xwoba(home_bats)
        # Blended ERA: 60% xERA/ERA + 40% FIP
        away_blend = 0.6*away_pit_era + 0.4*away_pit_fip
        home_blend = 0.6*home_pit_era + 0.4*home_pit_fip
        # Base runs model (empirical: 4.5 R/G MLB avg)
        away_runs = 4.50 * (4.50/away_blend) * (away_xwoba/0.320) * pf
        home_runs = 4.50 * (4.50/home_blend) * (home_xwoba/0.320) * pf
        # Weather adjustment
        ven = gdata.get("venue",{}); vloc = ven.get("location",{})
        lat = vloc.get("defaultCoordinates",{}).get("latitude")
        lon = vloc.get("defaultCoordinates",{}).get("longitude")
        wx = get_weather(lat, lon) if lat and lon else {}
        wx_adj = 0.0
        try:
            t = float(wx.get("temp","70"))
            if t > 82: wx_adj = 0.20
            elif t > 76: wx_adj = 0.10
            elif t < 48: wx_adj = -0.20
            elif t < 56: wx_adj = -0.10
        except: pass
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
        })
    except Exception as ex:
        print("[api_game_projection]", traceback.format_exc())
        return jsonify({"success":False,"error":str(ex)}), 500

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

# Boot background loaders
threading.Thread(target=_load_fg_data,      daemon=True).start()
threading.Thread(target=_load_savant_data,  daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
