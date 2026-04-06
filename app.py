import os, threading, traceback, difflib, io, csv as csvmod, json, re
import requests
from datetime import datetime, timezone
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo
ET = ZoneInfo("America/New_York")
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

_HERE = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_HTML = open(os.path.join(_HERE, 'dashboard.html')).read()
DEEP_DIVE_HTML = open(os.path.join(_HERE, 'deepdive.html')).read()
TRACKER_HTML = open(os.path.join(_HERE, 'tracker.html')).read()
DATA_DIR = os.path.join(_HERE, 'data')
os.makedirs(DATA_DIR, exist_ok=True)
TRACKER_STORE = os.path.join(DATA_DIR, 'daily_tracker.json')
ADJUST_STORE = os.path.join(DATA_DIR, 'model_adjustments.json')
CAL_HISTORY_STORE = os.path.join(DATA_DIR, 'calibration_history.json')
VALUE_HISTORY_STORE = os.path.join(DATA_DIR, 'value_history.json')

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
    2394: (42.3391151,  -83.048695),    # Comerica Park, Detroit (retractable)
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
DOME_VENUES = {
    12,    # Tropicana Field (fixed dome)
    14,    # Rogers Centre (retractable)
    15,    # Chase Field (retractable)
    32,    # American Family Field (retractable)
    680,   # T-Mobile Park (retractable)
    2392,  # Daikin Park / Minute Maid (retractable)
    2394,  # Comerica Park (retractable)
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


def current_et_date_str():
    return datetime.now(ET).strftime("%Y-%m-%d")

def fetch_schedule(date_str):
    url = (f"{MLB_API}/schedule?sportId=1&date={date_str}"
           "&hydrate=team,probablePitcher,linescore,venue,weather")
    r = requests.get(url, timeout=10); r.raise_for_status()
    dates = r.json().get("dates", [])
    return dates[0].get("games", []) if dates else []

def get_weather(lat, lon, game_hour=13, venue_id=None):
    # Dome/retractable roof: return indoor conditions immediately
    if venue_id and venue_id in DOME_VENUES:
        return {"temp":"DOME","rain_chance":0,"wind_speed":0,"condition":"Dome","dome":True}
    # Last-resort: fill coords from hardcoded stadium map
    if (lat is None or lon is None) and venue_id and venue_id in STADIUM_COORDS:
        lat, lon = STADIUM_COORDS[venue_id]
    if lat is None or lon is None:
        return {"temp":"N/A","rain_chance":"N/A","wind_speed":"N/A","condition":"N/A"}
    try:
        r = requests.get(WX_API, params={
            "latitude":lat,"longitude":lon,
            "hourly":"temperature_2m,precipitation_probability,windspeed_10m,weathercode",
            "temperature_unit":"fahrenheit","windspeed_unit":"mph",
            "forecast_days":2,"timezone":"auto"
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
        wcodes  = h.get("weathercode",[0]*48)
        wcode   = wcodes[idx] if idx < len(wcodes) else 0
        return {
            "temp":       round(temps[idx]) if idx < len(temps) else "N/A",
            "rain_chance":precip[idx] if idx < len(precip) else "N/A",
            "wind_speed": round(wind[idx]) if idx < len(wind) else "N/A",
            "condition":  wcode_map.get(wcode, "Clear"),
        }
    except Exception as ex:
        print(f"[get_weather] lat={lat} lon={lon} hour={game_hour} venue={venue_id} err={ex}")
        return {"temp":"N/A","rain_chance":"N/A","wind_speed":"N/A","condition":"N/A"}

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
        game_num = g.get("gameNumber", 1)
        is_dh    = g.get("doubleHeader", "N") == "Y"
        stat = g.get("status",{}).get("detailedState","Scheduled")
        st   = "Live" if "Progress" in stat else ("Final" if "Final" in stat else "Scheduled")
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
            dt_utc_wx = datetime.fromisoformat(g.get("gameDate","").replace("Z","+00:00"))
            game_hour_et = dt_utc_wx.astimezone(ET).hour
        except: game_hour_et = 13
        wx = get_weather(lat, lon, game_hour_et, venue_id=venue_id)
        gt   = g.get("gameDate","")
        try:
            dt_utc = datetime.fromisoformat(gt.replace("Z","+00:00"))
            dt_et  = dt_utc.astimezone(ET)
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
            "gameNumber":   game_num,
            "doubleHeader": is_dh,
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
        date_str = (request.args.get("date") or "").strip() or current_et_date_str()
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
        raw = fetch_schedule(current_et_date_str())
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
        raw = fetch_schedule(current_et_date_str())
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
        wx_adj = 0.0
        if not wx.get("dome"):
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





# ── Phase 6 Monte Carlo Simulation ────────────────────────────────────────────
import random, statistics, math

BULLPEN_BASE = {"era":4.05, "whip":1.28, "k9":8.6, "bb9":3.2, "hr9":1.10}
_bio_cache = {}
_hit_split_cache = {}
_team_pitch_cache = {}


def _num(v, d=0.0):
    try:
        if v in (None, "", "N/A", "---"):
            return float(d)
        return float(v)
    except:
        return float(d)


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
    except:
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
    except:
        out = {}
    _hit_split_cache[player_id] = out
    return out


def team_pitching_context(team_id):
    if not team_id:
        return {}
    key = (team_id, current_et_date_str())
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
    except:
        out = dict(BULLPEN_BASE)
    _team_pitch_cache[key] = out
    return out


def pitcher_stats_mlb(player_id):
    try:
        r = requests.get(f"{MLB_API}/people/{player_id}/stats?stats=season&group=pitching&season={datetime.now().year}", timeout=8)
        r.raise_for_status()
        splits = r.json().get("stats", [{}])[0].get("splits", [])
        prof = player_profile(player_id)
        if not splits:
            return {'pitchHand': prof.get('throws', 'R')}
        s = splits[0].get("stat", {})
        return {
            "era": s.get("era", "N/A"), "whip": s.get("whip", "N/A"),
            "ip": s.get("inningsPitched", "N/A"),
            "wins": s.get("wins", 0), "losses": s.get("losses", 0),
            "g": s.get("gamesPlayed", 0), "gs": s.get("gamesStarted", 0),
            "k9": round(float(s.get("strikeoutsPer9Inn", 0) or 0), 2),
            "bb9": round(float(s.get("walksPer9Inn", 0) or 0), 2),
            "hr9": round(float(s.get("homeRunsPer9", 0) or 0), 2),
            "pitchHand": prof.get('throws', 'R'),
        }
    except:
        prof = player_profile(player_id)
        return {'pitchHand': prof.get('throws', 'R')}


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
        slot = p.get("battingOrder", 0)
        try:
            slot = int(str(slot)[0])
        except:
            slot = 0
        fgb = fg_batter(name)
        svb = sv_batter(name)
        prof = player_profile(pid)
        spl = hitter_split_profile(pid)
        out.append({
            "slot": slot, "id": pid, "name": name, "pos": pos,
            "avg": ss.get("avg", fgb.get("fg_avg", ".---")),
            "obp": ss.get("obp", fgb.get("fg_obp", ".---")),
            "slg": ss.get("slg", fgb.get("fg_slg", ".---")),
            "ops": ss.get("ops", fgb.get("fg_ops", ".---")),
            "ab": s.get("atBats", 0), "hits": s.get("hits", 0),
            "hr": s.get("homeRuns", 0), "rbi": s.get("rbi", 0),
            "bats": prof.get('bats', 'S'),
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
            "sv_la": svb.get("sv_la", "N/A"),
        })
    return out


def _pitcher_model(name, pid=None, team_id=None):
    mlb = pitcher_stats_mlb(pid) if pid else {}
    fg = fg_pitcher(name)
    sv = sv_pitcher(name)
    era = _num(sv.get('sv_xera'), None)
    if era is None or era == 0:
        era = _num(fg.get('fg_era'), _num(mlb.get('era'), 4.50))
    whip = _num(fg.get('fg_whip'), _num(mlb.get('whip'), 1.30))
    k9 = _num(fg.get('fg_k9'), _num(mlb.get('k9'), 8.2))
    bb9 = _num(fg.get('fg_bb9'), _num(mlb.get('bb9'), 3.1))
    hr9 = _num(fg.get('fg_hr9'), _num(mlb.get('hr9'), 1.10))
    return {
        'name': name, 'id': pid, 'team_id': team_id,
        'era': _clamp(era, 2.0, 8.5), 'whip': _clamp(whip, 0.9, 1.9),
        'k9': _clamp(k9, 4.0, 14.0), 'bb9': _clamp(bb9, 1.0, 6.0), 'hr9': _clamp(hr9, 0.4, 2.5),
        'pitchHand': (mlb.get('pitchHand') or player_profile(pid).get('throws', 'R')),
    }


def _tier_blend(tm, starter, w_tm, w_base, w_sp, mods):
    era = _clamp(w_tm * _num(tm.get('era'), BULLPEN_BASE['era']) + w_base * BULLPEN_BASE['era'] + w_sp * starter.get('era', 4.2) + mods.get('era', 0), 2.6, 5.8)
    whip = _clamp(w_tm * _num(tm.get('whip'), BULLPEN_BASE['whip']) + w_base * BULLPEN_BASE['whip'] + w_sp * starter.get('whip', 1.3) + mods.get('whip', 0), 0.98, 1.58)
    k9 = _clamp(w_tm * _num(tm.get('k9'), BULLPEN_BASE['k9']) + w_base * BULLPEN_BASE['k9'] + w_sp * starter.get('k9', 8.2) + mods.get('k9', 0), 6.0, 12.8)
    bb9 = _clamp(w_tm * _num(tm.get('bb9'), BULLPEN_BASE['bb9']) + w_base * BULLPEN_BASE['bb9'] + w_sp * starter.get('bb9', 3.2) + mods.get('bb9', 0), 1.8, 5.2)
    hr9 = _clamp(w_tm * _num(tm.get('hr9'), BULLPEN_BASE['hr9']) + w_base * BULLPEN_BASE['hr9'] + w_sp * starter.get('hr9', 1.1) + mods.get('hr9', 0), 0.55, 1.9)
    return {'era': era, 'whip': whip, 'k9': k9, 'bb9': bb9, 'hr9': hr9}


def _bullpen_tiers(starter, team_id=None):
    tm = team_pitching_context(team_id)
    closer = _tier_blend(tm, starter, 0.65, 0.20, 0.15, {'era': -0.28, 'whip': -0.06, 'k9': 0.80, 'bb9': -0.15, 'hr9': -0.08})
    setup = _tier_blend(tm, starter, 0.62, 0.23, 0.15, {'era': -0.12, 'whip': -0.03, 'k9': 0.35, 'bb9': -0.05, 'hr9': -0.03})
    middle = _tier_blend(tm, starter, 0.58, 0.25, 0.17, {'era': 0.18, 'whip': 0.04, 'k9': -0.25, 'bb9': 0.10, 'hr9': 0.08})
    for name, model in [('Closer', closer), ('Setup', setup), ('Middle', middle)]:
        model['name'] = name
        model['pitchHand'] = starter.get('pitchHand', 'R')
    return {'closer': closer, 'setup': setup, 'middle': middle}


def _starter_outs_target(starter, rng):
    mean = 16.5 + (4.1 - starter['era']) * 1.2 + (starter['k9'] - 8.2) * 0.20 - max(0, starter['whip'] - 1.25) * 2.8
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


def _derive_probs(b, p, park=1.0):
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


def _maybe_steal(bases, lineup, stats, rng, probs_map, outs):
    if outs >= 2:
        return 0
    if bases[0] is not None and bases[1] is None:
        ridx = bases[0]
        pr = probs_map.get(ridx, {})
        if rng.random() < pr.get('steal_rate', 0.0):
            if rng.random() < pr.get('steal_success', 0.72):
                bases[1] = ridx; bases[0] = None; stats[ridx]['sb'] += 1
                return 1
            else:
                bases[0] = None; stats[ridx]['cs'] += 1
                return -1
    if bases[1] is not None and bases[2] is None and outs == 0:
        ridx = bases[1]
        pr = probs_map.get(ridx, {})
        if rng.random() < pr.get('steal_rate', 0.0) * 0.30:
            if rng.random() < pr.get('steal_success', 0.72) - 0.08:
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


def _simulate_offense(lineup, opp_starter, opp_team_id, park, rng):
    stats = [_blank_batter_line(b) for b in lineup]
    starter_line = _blank_pitcher_line(opp_starter['name'])
    tiers = _bullpen_tiers(opp_starter, opp_team_id)
    relief_lines = {'Closer': _blank_pitcher_line('Closer'), 'Setup': _blank_pitcher_line('Setup'), 'Middle': _blank_pitcher_line('Middle')}
    starter_target = _starter_outs_target(opp_starter, rng)
    runs = 0; batter_ptr = 0
    probs_cache = {}
    for i, b in enumerate(lineup):
        probs_cache[i] = _derive_probs(b, opp_starter, park)
    for inning in range(1, 10):
        outs = 0
        bases = [None, None, None]
        while outs < 3:
            steal_outcome = _maybe_steal(bases, lineup, stats, rng, probs_cache, outs)
            if steal_outcome == -1:
                outs += 1
                if starter_line['outs'] < starter_target:
                    starter_line['outs'] += 1
                else:
                    tier0 = _select_relief_tier(inning, runs, starter_line['outs'], tiers)
                    relief_lines[tier0['name']]['outs'] += 1
                if outs >= 3:
                    break
            b = lineup[batter_ptr]
            s = stats[batter_ptr]
            use_starter = starter_line['outs'] < starter_target
            if use_starter:
                pm = opp_starter
                pl = starter_line
            else:
                pm = _select_relief_tier(inning, runs, starter_line['outs'], tiers)
                pl = relief_lines[pm['name']]
            probs = _derive_probs(b, pm, park)
            probs_cache[batter_ptr] = probs
            ev = _pick_event(probs, rng)
            s['pa'] += 1
            if ev == 'bb':
                s['bb'] += 1; pl['bb'] += 1
                runs += _advance_walk(bases, batter_ptr, stats, pl)
            elif ev in ('1b', '2b', '3b', 'hr'):
                s['ab'] += 1; s['h'] += 1; pl['h'] += 1
                if ev == '1b': s['1b'] += 1; s['tb'] += 1
                elif ev == '2b': s['2b'] += 1; s['tb'] += 2
                elif ev == '3b': s['3b'] += 1; s['tb'] += 3
                elif ev == 'hr': s['hr'] += 1; s['tb'] += 4; pl['hr'] += 1
                runs += _advance_hit(ev, bases, batter_ptr, stats, pl, rng, outs)
            else:
                s['ab'] += 1; outs += 1; pl['outs'] += 1
                if ev == 'k': s['k'] += 1; pl['k'] += 1
            batter_ptr = (batter_ptr + 1) % len(lineup)
    bullpen_tot = _blank_pitcher_line('Bullpen')
    for rl in relief_lines.values():
        bullpen_tot['outs'] += rl['outs']; bullpen_tot['h'] += rl['h']; bullpen_tot['er'] += rl['er']; bullpen_tot['bb'] += rl['bb']; bullpen_tot['k'] += rl['k']; bullpen_tot['hr'] += rl['hr']
    return {
        'batters': stats,
        'starter': starter_line,
        'bullpen': bullpen_tot,
        'relief_lines': relief_lines,
        'bullpen_models': tiers,
        'runs': runs,
        'hits': sum(x['h'] for x in stats),
        'bb': sum(x['bb'] for x in stats),
        'k': sum(x['k'] for x in stats),
        'tb': sum(x['tb'] for x in stats),
        'sb': sum(x['sb'] for x in stats),
    }


def _summarize_player(lines):
    def arr(k): return [x[k] for x in lines]
    hits = arr('h'); hr = arr('hr'); rbi = arr('rbi'); runs = arr('r'); bb = arr('bb'); k = arr('k'); tb = arr('tb'); sb = arr('sb')
    return {
        'mean_hits': round(statistics.mean(hits), 3), 'median_hits': statistics.median(hits),
        'mean_hr': round(statistics.mean(hr), 3), 'mean_rbi': round(statistics.mean(rbi), 3),
        'mean_runs': round(statistics.mean(runs), 3), 'mean_bb': round(statistics.mean(bb), 3),
        'mean_k': round(statistics.mean(k), 3), 'mean_tb': round(statistics.mean(tb), 3), 'mean_sb': round(statistics.mean(sb), 3),
        'p10_hits': round(_pct(hits, 0.10), 2), 'p50_hits': round(_pct(hits, 0.50), 2), 'p90_hits': round(_pct(hits, 0.90), 2),
        'p10_tb': round(_pct(tb, 0.10), 2), 'p50_tb': round(_pct(tb, 0.50), 2), 'p90_tb': round(_pct(tb, 0.90), 2),
        'p10_k': round(_pct(k, 0.10), 2), 'p50_k': round(_pct(k, 0.50), 2), 'p90_k': round(_pct(k, 0.90), 2),
        'p_1plus_hit': round(sum(1 for x in hits if x >= 1) / len(hits), 3),
        'p_2plus_hit': round(sum(1 for x in hits if x >= 2) / len(hits), 3),
        'p_1plus_hr': round(sum(1 for x in hr if x >= 1) / len(hr), 3),
        'p_1plus_rbi': round(sum(1 for x in rbi if x >= 1) / len(rbi), 3),
        'p_1plus_run': round(sum(1 for x in runs if x >= 1) / len(runs), 3),
        'p_2plus_tb': round(sum(1 for x in tb if x >= 2) / len(tb), 3),
        'p_1plus_bb': round(sum(1 for x in bb if x >= 1) / len(bb), 3),
        'p_1plus_k': round(sum(1 for x in k if x >= 1) / len(k), 3),
        'p_1plus_sb': round(sum(1 for x in sb if x >= 1) / len(sb), 3),
    }


def _summarize_pitcher(lines):
    def arr(k): return [x[k] for x in lines]
    outs = arr('outs'); er = arr('er'); ks = arr('k'); hs = arr('h'); bbs = arr('bb')
    return {
        'mean_outs': round(statistics.mean(outs), 2), 'median_outs': statistics.median(outs),
        'mean_er': round(statistics.mean(er), 2), 'mean_k': round(statistics.mean(ks), 2),
        'mean_h': round(statistics.mean(hs), 2), 'mean_bb': round(statistics.mean(bbs), 2),
        'p10_outs': round(_pct(outs, 0.10), 2), 'p50_outs': round(_pct(outs, 0.50), 2), 'p90_outs': round(_pct(outs, 0.90), 2),
        'p10_k': round(_pct(ks, 0.10), 2), 'p50_k': round(_pct(ks, 0.50), 2), 'p90_k': round(_pct(ks, 0.90), 2),
        'p10_er': round(_pct(er, 0.10), 2), 'p50_er': round(_pct(er, 0.50), 2), 'p90_er': round(_pct(er, 0.90), 2),
        'p_12plus_outs': round(sum(1 for x in outs if x >= 12)/len(outs), 3),
        'p_15plus_outs': round(sum(1 for x in outs if x >= 15)/len(outs), 3),
        'p_18plus_outs': round(sum(1 for x in outs if x >= 18)/len(outs), 3),
        'p_4plus_k': round(sum(1 for x in ks if x >= 4)/len(ks), 3),
        'p_5plus_k': round(sum(1 for x in ks if x >= 5)/len(ks), 3),
        'p_6plus_k': round(sum(1 for x in ks if x >= 6)/len(ks), 3),
        'p_2plus_er': round(sum(1 for x in er if x >= 2)/len(er), 3),
        'p_3plus_er': round(sum(1 for x in er if x >= 3)/len(er), 3),
    }




# ── Player Profile: Game Logs, Platoon Splits, Statcast, AI Scout ────────────
def _ai_scout_report(name, pos, season, platoon, recent, fg, sv, is_pitcher):
    PITCH_NAMES = {"ff":"4-Seam FB","si":"Sinker","fc":"Cutter","st":"Sweeper",
                   "sl":"Slider","cu":"Curveball","ch":"Changeup","fs":"Splitter","kn":"Knuckleball"}
    lines = []
    l7 = recent[-7:] if recent else []
    if not is_pitcher:
        l7_ab = sum(g.get("ab",0) for g in l7)
        l7_h  = sum(g.get("h",0)  for g in l7)
        l7_hr = sum(g.get("hr",0) for g in l7)
        l7_avg = round(l7_h/l7_ab,3) if l7_ab > 0 else None
        if l7_avg is not None:
            if l7_avg >= .310:
                lines.append(f"🔥 <b>HOT STREAK</b>: {name} batting <b>.{int(l7_avg*1000):03d}</b> over last 7 games" + (f" with {l7_hr} HR" if l7_hr else "") + " — prime target for hits/TB props.")
            elif l7_avg < .160 and l7_ab >= 12:
                lines.append(f"❄️ <b>COLD STREAK</b>: {name} hitting just .{int(l7_avg*1000):03d} over last 7 — fade on hits props until signs of life return.")
            else:
                lines.append(f"📊 <b>Recent Form</b>: {name} is batting .{int(l7_avg*1000):03d} over the last 7 games.")
        try:
            xba = float(sv.get("sv_xba") or 0); avg = float(fg.get("fg_avg") or season.get("avg","0") or 0)
            if xba and avg:
                gap = round(xba - avg, 3)
                if gap >= 0.030:
                    lines.append(f"📈 <b>Positive Regression Alert</b>: xBA of {xba:.3f} is +{gap:.3f} above actual AVG — Statcast says {name} is hitting it better than results show.")
                elif gap <= -0.030:
                    lines.append(f"📉 <b>Negative Regression Risk</b>: xBA {xba:.3f} is {gap:.3f} below actual AVG — results are outpacing contact quality.")
        except: pass
        try:
            ev = float(sv.get("sv_ev") or 0); brl = float(sv.get("sv_brl_pct") or 0)
            if ev >= 91:
                lines.append(f"💥 <b>Elite EV</b>: {ev:.1f} mph avg exit velo" + (f" + {brl:.1f}% barrel rate" if brl >= 8 else "") + " — consistent hard contact profile.")
            if brl >= 12:
                lines.append(f"🎯 <b>Barrel Machine</b>: {brl:.1f}% barrel rate — elite HR and extra-base upside.")
        except: pass
        try:
            kpct = float(sv.get("sv_k_pct") or fg.get("fg_kpct") or 0)
            if kpct >= 0.28: lines.append(f"⚡ <b>Strikeout Risk</b>: {kpct*100:.0f}% K rate caps the hits prop floor.")
            elif kpct and kpct <= 0.14: lines.append(f"🎯 <b>Contact Hitter</b>: {kpct*100:.0f}% K rate gives a consistent hits floor.")
        except: pass
        vl = platoon.get("vl",{}); vr = platoon.get("vr",{})
        try:
            ops_vl = float(vl.get("ops","0") or 0); ops_vr = float(vr.get("ops","0") or 0)
            if ops_vl > 0 and ops_vr > 0 and abs(ops_vl-ops_vr) >= 0.080:
                fav = "LHP" if ops_vl > ops_vr else "RHP"
                lines.append(f"🆚 <b>Platoon Edge vs {fav}</b>: OPS {max(ops_vl,ops_vr):.3f} vs {fav} vs {min(ops_vl,ops_vr):.3f} opposite — check tonight's pitcher hand.")
        except: pass
        try:
            wrc = int(fg.get("fg_wrc") or 0)
            if wrc >= 130: lines.append(f"⭐ <b>Elite Offense</b>: wRC+ of {wrc} — premium prop target on good matchups.")
            elif wrc and wrc <= 80: lines.append(f"📉 <b>Below Average</b>: wRC+ of {wrc} — avoid props unless facing weak pitching.")
        except: pass
        lines.append("🎰 <b>Betting Angle</b>: Best on hits/TB props — confirm platoon matchup and opposing pitcher hand before placing.")
    else:
        try:
            era = float(fg.get("fg_era") or season.get("era","4.5") or 4.5)
            xera = float(sv.get("sv_xera") or 0); fip = float(fg.get("fg_fip") or 0)
            if xera and era - xera >= 0.70:
                lines.append(f"📈 <b>Unlucky ERA</b>: xERA {xera:.2f} vs ERA {era:.2f} — Statcast says {name} is pitching better than results. Lean into K props.")
            if fip and era - fip >= 0.55:
                lines.append(f"🔵 <b>FIP Divergence</b>: FIP ({fip:.2f}) well below ERA ({era:.2f}) — defense is inflating ERA.")
        except: pass
        try:
            k9 = float(fg.get("fg_k9") or 0)
            if k9 >= 10: lines.append(f"🔥 <b>Strikeout Weapon</b>: {k9:.1f} K/9 — hammer K overs aggressively when lines are reasonable.")
            elif k9 and k9 <= 6.5: lines.append(f"⚠️ <b>Contact Pitcher</b>: {k9:.1f} K/9 — low strikeout ceiling limits K prop upside.")
        except: pass
        try:
            bb9 = float(fg.get("fg_bb9") or 0)
            if bb9 >= 4.5: lines.append(f"⚡ <b>Control Issues</b>: {bb9:.1f} BB/9 — free baserunners elevate opponent scoring props.")
        except: pass
        try:
            arsenal = sv.get("sv_arsenal_pct") or {}
            if arsenal:
                top = max(arsenal.items(), key=lambda x: x[1])
                lines.append(f"⚾ <b>Primary Weapon</b>: {PITCH_NAMES.get(top[0], top[0].upper())} at {top[1]:.0f}% usage.")
        except: pass
        lines.append("🎰 <b>Betting Angle</b>: Target K props aggressively — hammer K overs on days with reasonable lines.")
    return lines


@app.route("/api/player/<int:player_id>")
def api_player_profile(player_id):
    try:
        year = datetime.now(ET).year
        info_r = requests.get(f"{MLB_API}/people/{player_id}", params={"hydrate":"currentTeam"}, timeout=8)
        info_r.raise_for_status()
        person = info_r.json().get("people",[{}])[0]
        name      = person.get("fullName","?")
        pos       = person.get("primaryPosition",{}).get("abbreviation","?")
        team      = person.get("currentTeam",{}).get("name","?")
        team_abbr = person.get("currentTeam",{}).get("abbreviation","?")
        bats      = person.get("batSide",{}).get("code","?")
        throws    = person.get("pitchHand",{}).get("code","?")
        is_pitcher = pos in ("P","SP","RP","CL")
        group     = "pitching" if is_pitcher else "hitting"
        season_stats = {}
        try:
            sr = requests.get(f"{MLB_API}/people/{player_id}/stats",
                params={"stats":"season","group":group,"season":year}, timeout=8)
            splits = sr.json().get("stats",[{}])
            if splits and splits[0].get("splits"):
                season_stats = splits[0]["splits"][-1].get("stat",{})
        except: pass
        game_logs = []
        try:
            lr = requests.get(f"{MLB_API}/people/{player_id}/stats",
                params={"stats":"gameLog","group":group,"season":year}, timeout=8)
            all_games = lr.json().get("stats",[{}])[0].get("splits",[])
            for sp in (all_games[-15:] if len(all_games) >= 15 else all_games):
                s = sp.get("stat",{})
                if group == "hitting":
                    game_logs.append({"date":sp.get("date","")[:10],"opp":sp.get("opponent",{}).get("abbreviation",""),
                        "ab":s.get("atBats",0),"h":s.get("hits",0),"hr":s.get("homeRuns",0),
                        "rbi":s.get("rbi",0),"k":s.get("strikeOuts",0),"bb":s.get("baseOnBalls",0),
                        "avg":s.get("avg","---"),"tb":s.get("totalBases",0)})
                else:
                    game_logs.append({"date":sp.get("date","")[:10],"opp":sp.get("opponent",{}).get("abbreviation",""),
                        "ip":s.get("inningsPitched","0"),"er":s.get("earnedRuns",0),"k":s.get("strikeOuts",0),
                        "bb":s.get("baseOnBalls",0),"h":s.get("hits",0),"era":s.get("era","---")})
        except: pass
        platoon = {}
        try:
            pr = requests.get(f"{MLB_API}/people/{player_id}/stats",
                params={"stats":"statSplits","group":group,"season":year,"sitCodes":"vl,vr"}, timeout=8)
            for sp in pr.json().get("stats",[{}])[0].get("splits",[]):
                code = sp.get("split",{}).get("code",""); s = sp.get("stat",{})
                if code in ("vl","vr"):
                    platoon[code] = {"avg":s.get("avg","---"),"obp":s.get("obp","---"),
                        "slg":s.get("slg","---"),"ops":s.get("ops","---"),
                        "pa":s.get("plateAppearances",0),"hr":s.get("homeRuns",0),
                        "k":s.get("strikeOuts",0),"bb":s.get("baseOnBalls",0),"woba":s.get("woba","---")}
        except: pass
        fg = fg_pitcher(name) if is_pitcher else fg_batter(name)
        sv = sv_pitcher(name) if is_pitcher else sv_batter(name)
        ai_lines = _ai_scout_report(name, pos, season_stats, platoon, game_logs, fg, sv, is_pitcher)
        return jsonify({"success":True,"playerId":player_id,"name":name,"pos":pos,
            "team":team,"teamAbbr":team_abbr,"bats":bats,"throws":throws,
            "isPitcher":is_pitcher,"season":season_stats,"gameLogs":game_logs,
            "platoon":platoon,"fg":fg,"sv":sv,"aiLines":ai_lines})
    except Exception as ex:
        return jsonify({"success":False,"error":str(ex)}), 500

@app.route('/api/simulate/<int:game_pk>')
def api_simulate(game_pk):
    try:
        raw = fetch_schedule(current_et_date_str())
        g = next((x for x in raw if x.get('gamePk') == game_pk), None)
        if not g:
            return jsonify({'success': False, 'error': 'Game not found'}), 404
        box = requests.get(f"{MLB_API}/game/{game_pk}/boxscore", timeout=10).json().get('teams', {})
        away_lineup = get_batters_from_boxscore(box.get('away', {}), 'away')
        home_lineup = get_batters_from_boxscore(box.get('home', {}), 'home')
        if not away_lineup or not home_lineup:
            return jsonify({'success': False, 'error': 'Lineups not posted yet'}), 400

        away_team = g.get('teams', {}).get('away', {}).get('team', {})
        home_team = g.get('teams', {}).get('home', {}).get('team', {})
        away_team_id = away_team.get('id')
        home_team_id = home_team.get('id')
        away_abbr = away_team.get('abbreviation', 'AWAY')
        home_abbr = home_team.get('abbreviation', 'HOME')

        away_p = g.get('teams', {}).get('away', {}).get('probablePitcher', {})
        home_p = g.get('teams', {}).get('home', {}).get('probablePitcher', {})
        away_pitcher = _pitcher_model(away_p.get('fullName', 'Away SP'), away_p.get('id'), away_team_id)
        home_pitcher = _pitcher_model(home_p.get('fullName', 'Home SP'), home_p.get('id'), home_team_id)
        park = PARK_FACTORS.get(home_team_id, 1.0)

        sims = 1000
        rng = random.Random(game_pk + int(datetime.now().strftime('%Y%m%d')) + 6)

        away_store = {i: [] for i in range(len(away_lineup))}
        home_store = {i: [] for i in range(len(home_lineup))}
        away_team_runs, home_team_runs, totals = [], [], []
        away_starter_lines, home_starter_lines = [], []
        away_bullpen_lines, home_bullpen_lines = [], []
        away_relief = {'Closer': [], 'Setup': [], 'Middle': []}
        home_relief = {'Closer': [], 'Setup': [], 'Middle': []}
        away_win = 0; home_win = 0; ties = 0
        sample = None

        for sim in range(sims):
            away_off = _simulate_offense(away_lineup, home_pitcher, home_team_id, park, rng)
            home_off = _simulate_offense(home_lineup, away_pitcher, away_team_id, park, rng)
            if sample is None:
                sample = {
                    'away': away_off, 'home': home_off,
                    'away_abbr': away_abbr, 'home_abbr': home_abbr,
                    'away_pitcher': away_pitcher['name'], 'home_pitcher': home_pitcher['name']
                }
            for i, line in enumerate(away_off['batters']): away_store[i].append(line)
            for i, line in enumerate(home_off['batters']): home_store[i].append(line)
            home_starter_lines.append(away_off['starter'])
            away_starter_lines.append(home_off['starter'])
            home_bullpen_lines.append(away_off['bullpen'])
            away_bullpen_lines.append(home_off['bullpen'])
            for k in away_relief.keys():
                away_relief[k].append(home_off['relief_lines'][k])
                home_relief[k].append(away_off['relief_lines'][k])
            away_team_runs.append(away_off['runs']); home_team_runs.append(home_off['runs']); totals.append(away_off['runs'] + home_off['runs'])
            if away_off['runs'] > home_off['runs']: away_win += 1
            elif home_off['runs'] > away_off['runs']: home_win += 1
            else: ties += 1

        away_props = []
        for i, b in enumerate(away_lineup):
            s = _summarize_player(away_store[i]); s.update({'id': b.get('id'), 'name': b.get('name'), 'slot': b.get('slot'), 'pos': b.get('pos'), 'bats': b.get('bats', 'S')}); away_props.append(s)
        home_props = []
        for i, b in enumerate(home_lineup):
            s = _summarize_player(home_store[i]); s.update({'id': b.get('id'), 'name': b.get('name'), 'slot': b.get('slot'), 'pos': b.get('pos'), 'bats': b.get('bats', 'S')}); home_props.append(s)

        away_pitch_summary = _summarize_pitcher(away_starter_lines)
        away_pitch_summary.update({'name': away_pitcher['name'], 'pitchHand': away_pitcher['pitchHand']})
        home_pitch_summary = _summarize_pitcher(home_starter_lines)
        home_pitch_summary.update({'name': home_pitcher['name'], 'pitchHand': home_pitcher['pitchHand']})
        away_bullpen_summary = _summarize_pitcher(away_bullpen_lines)
        away_bullpen_summary.update({'name': away_abbr + ' Bullpen'})
        home_bullpen_summary = _summarize_pitcher(home_bullpen_lines)
        home_bullpen_summary.update({'name': home_abbr + ' Bullpen'})

        away_tiers = {k.lower(): _summarize_pitcher(v) for k, v in away_relief.items()}
        home_tiers = {k.lower(): _summarize_pitcher(v) for k, v in home_relief.items()}

        away_l = sum(1 for b in away_lineup if (b.get('bats') or 'S') == 'L')
        away_r = sum(1 for b in away_lineup if (b.get('bats') or 'S') == 'R')
        away_s = sum(1 for b in away_lineup if (b.get('bats') or 'S') == 'S')
        home_l = sum(1 for b in home_lineup if (b.get('bats') or 'S') == 'L')
        home_r = sum(1 for b in home_lineup if (b.get('bats') or 'S') == 'R')
        home_s = sum(1 for b in home_lineup if (b.get('bats') or 'S') == 'S')

        return jsonify({
            'success': True,
            'meta': {'sims': sims, 'awayAbbr': away_abbr, 'homeAbbr': home_abbr, 'parkFactor': park},
            'team': {
                'away_mean_runs': round(statistics.mean(away_team_runs), 2),
                'home_mean_runs': round(statistics.mean(home_team_runs), 2),
                'mean_total': round(statistics.mean(totals), 2),
                'median_total': statistics.median(totals),
                'p_8plus_total': round(sum(1 for x in totals if x >= 8)/len(totals), 3),
                'p_9plus_total': round(sum(1 for x in totals if x >= 9)/len(totals), 3),
                'p_10plus_total': round(sum(1 for x in totals if x >= 10)/len(totals), 3),
                'away_win_pct': round(away_win / sims, 3),
                'home_win_pct': round(home_win / sims, 3),
                'tie_pct': round(ties / sims, 3),
            },
            'handedness': {
                'awayLineup': {'L': away_l, 'R': away_r, 'S': away_s},
                'homeLineup': {'L': home_l, 'R': home_r, 'S': home_s},
                'awayStarterHand': away_pitcher['pitchHand'],
                'homeStarterHand': home_pitcher['pitchHand'],
            },
            'playerProps': {'away': away_props, 'home': home_props},
            'pitcherProps': {
                'awayStarter': away_pitch_summary, 'homeStarter': home_pitch_summary,
                'awayBullpen': away_bullpen_summary, 'homeBullpen': home_bullpen_summary,
                'awayBullpenTiers': away_tiers, 'homeBullpenTiers': home_tiers,
            },
            'sampleBoxscore': sample,
        })
    except Exception as ex:
        print('[api_simulate]', traceback.format_exc())
        return jsonify({'success': False, 'error': str(ex)}), 500



# ── Phase 7 Odds / Lineup / Edge Infrastructure ──────────────────────────────
ODDS_API_KEY = (os.getenv('ODDS_API_KEY') or '').strip()
ODDS_REGION = (os.getenv('ODDS_REGION') or 'us').strip()


def _norm_name(s):
    return re.sub(r'[^a-z0-9]+', '', (s or '').lower())


def _american_to_implied(price):
    try:
        p = float(price)
        if p > 0:
            return round(100.0 / (p + 100.0), 4)
        if p < 0:
            return round((-p) / ((-p) + 100.0), 4)
        return None
    except:
        return None


def _find_odds_event(away_name, home_name, game_number=1):
    if not ODDS_API_KEY:
        return None, []
    try:
        r = requests.get(
            'https://api.the-odds-api.com/v4/sports/baseball_mlb/events',
            params={'apiKey': ODDS_API_KEY, 'dateFormat': 'iso'},
            timeout=12,
        )
        r.raise_for_status()
        events = r.json() or []
        na = _norm_name(away_name)
        nh = _norm_name(home_name)
        matches = []
        for ev in events:
            if _norm_name(ev.get('away_team')) == na and _norm_name(ev.get('home_team')) == nh:
                matches.append(ev)
        if matches:
            return matches[min(game_number - 1, len(matches) - 1)], events
        return None, events
    except:
        return None, []


def _best_moneyline(bookmakers, away_name, home_name):
    best = {'away': None, 'home': None}
    for bk in bookmakers or []:
        for m in bk.get('markets', []) or []:
            if m.get('key') != 'h2h':
                continue
            for o in m.get('outcomes', []) or []:
                nm = o.get('name')
                item = {'bookmaker': bk.get('title'), 'price': o.get('price'), 'implied': _american_to_implied(o.get('price'))}
                if nm == away_name:
                    if best['away'] is None or float(o.get('price', -9999)) > float(best['away']['price']):
                        best['away'] = item
                elif nm == home_name:
                    if best['home'] is None or float(o.get('price', -9999)) > float(best['home']['price']):
                        best['home'] = item
    return best


def _best_total(bookmakers):
    best = None
    for bk in bookmakers or []:
        for m in bk.get('markets', []) or []:
            if m.get('key') != 'totals':
                continue
            outs = m.get('outcomes', []) or []
            over = next((x for x in outs if str(x.get('name')).lower() == 'over'), None)
            under = next((x for x in outs if str(x.get('name')).lower() == 'under'), None)
            if not over or not under:
                continue
            cand = {
                'bookmaker': bk.get('title'),
                'line': over.get('point'),
                'over_price': over.get('price'), 'under_price': under.get('price'),
                'over_implied': _american_to_implied(over.get('price')),
                'under_implied': _american_to_implied(under.get('price')),
            }
            if best is None:
                best = cand
            else:
                # prefer widely used standard line nearest consensus price
                if abs(float(cand['over_price'] or 0)) < abs(float(best['over_price'] or 0)):
                    best = cand
    return best


def _load_event_odds(event_id, featured_only=False):
    if not ODDS_API_KEY or not event_id:
        return []
    markets = 'h2h,totals' if featured_only else 'batter_hits,batter_total_bases,batter_home_runs,batter_rbis,batter_runs_scored,batter_stolen_bases,pitcher_strikeouts'
    try:
        r = requests.get(
            f'https://api.the-odds-api.com/v4/sports/baseball_mlb/events/{event_id}/odds',
            params={'apiKey': ODDS_API_KEY, 'regions': ODDS_REGION, 'markets': markets, 'oddsFormat': 'american', 'dateFormat': 'iso'},
            timeout=15,
        )
        r.raise_for_status()
        return r.json().get('bookmakers', []) or []
    except:
        return []


def _parse_prop_markets(bookmakers, valid_names):
    grouped = {}
    for bk in bookmakers or []:
        bkt = bk.get('title')
        for m in bk.get('markets', []) or []:
            mk = m.get('key')
            outs = m.get('outcomes', []) or []
            for o in outs:
                player = o.get('description') or o.get('name')
                if mk.startswith('pitcher_'):
                    player = o.get('description') or o.get('name')
                if not player or player not in valid_names:
                    continue
                side = str(o.get('name', '')).lower()
                if side not in ('over', 'under'):
                    continue
                point = o.get('point')
                key = (player, mk, point, bkt)
                if key not in grouped:
                    grouped[key] = {'player': player, 'market_key': mk, 'line': point, 'bookmaker': bkt, 'over_price': None, 'under_price': None}
                grouped[key][f'{side}_price'] = o.get('price')
    out = []
    for item in grouped.values():
        item['over_implied'] = _american_to_implied(item.get('over_price'))
        item['under_implied'] = _american_to_implied(item.get('under_price'))
        out.append(item)
    return out


@app.route('/api/market/<int:game_pk>')
def api_market(game_pk):
    try:
        raw = fetch_schedule(current_et_date_str())
        g = next((x for x in raw if x.get('gamePk') == game_pk), None)
        if not g:
            return jsonify({'success': False, 'error': 'Game not found'}), 404

        away_team = g.get('teams', {}).get('away', {}).get('team', {})
        home_team = g.get('teams', {}).get('home', {}).get('team', {})
        away_name = away_team.get('name', '')
        home_name = home_team.get('name', '')
        away_abbr = away_team.get('abbreviation', 'AWAY')
        home_abbr = home_team.get('abbreviation', 'HOME')

        box = requests.get(f"{MLB_API}/game/{game_pk}/boxscore", timeout=10).json().get('teams', {})
        away_lineup = get_batters_from_boxscore(box.get('away', {}), 'away')
        home_lineup = get_batters_from_boxscore(box.get('home', {}), 'home')
        away_confirmed = len(away_lineup) >= 9
        home_confirmed = len(home_lineup) >= 9

        valid_names = set([x.get('name') for x in away_lineup + home_lineup if x.get('name')])
        away_p = g.get('teams', {}).get('away', {}).get('probablePitcher', {})
        home_p = g.get('teams', {}).get('home', {}).get('probablePitcher', {})
        if away_p.get('fullName'): valid_names.add(away_p.get('fullName'))
        if home_p.get('fullName'): valid_names.add(home_p.get('fullName'))

        event, events = _find_odds_event(away_name, home_name)
        featured = _load_event_odds(event.get('id') if event else None, featured_only=True) if event else []
        props_books = _load_event_odds(event.get('id') if event else None, featured_only=False) if event else []
        props = _parse_prop_markets(props_books, valid_names)

        market = {
            'moneyline': _best_moneyline(featured, away_name, home_name),
            'total': _best_total(featured),
        }

        return jsonify({
            'success': True,
            'meta': {
                'oddsApiConfigured': bool(ODDS_API_KEY),
                'oddsEventFound': bool(event),
                'eventId': event.get('id') if event else None,
                'bookmakersFeatured': len(featured),
                'bookmakersProps': len(props_books),
                'awayAbbr': away_abbr,
                'homeAbbr': home_abbr,
            },
            'lineup': {
                'awayConfirmed': away_confirmed,
                'homeConfirmed': home_confirmed,
                'awayCount': len(away_lineup),
                'homeCount': len(home_lineup),
                'status': g.get('status', {}).get('detailedState', 'Scheduled'),
            },
            'lines': market,
            'playerProps': props[:220],
        })
    except Exception as ex:
        print('[api_market]', traceback.format_exc())
        return jsonify({'success': False, 'error': str(ex)}), 500



# ── Phase 10 Daily Projection Tracker ─────────────────────────────────────────

def _load_json(path, default):
    try:
        if os.path.exists(path):
            with open(path, 'r') as f:
                return json.load(f)
    except:
        pass
    return default


def _save_json(path, obj):
    with open(path, 'w') as f:
        json.dump(obj, f, indent=2)


def _append_calibration_history(event_type, adjustments, meta=None):
    meta = meta or {}
    hist = _load_json(CAL_HISTORY_STORE, [])
    hist.append({
        'timestamp': datetime.now().isoformat(),
        'eventType': event_type,
        'date': meta.get('date'),
        'window': meta.get('window'),
        'applied': meta.get('applied', []),
        'note': meta.get('note'),
        'adjustments': adjustments,
    })
    _save_json(CAL_HISTORY_STORE, hist[-800:])


def _history_in_window(end_date_str, window_days):
    dates = set(_dates_in_window(end_date_str, window_days))
    hist = _load_json(CAL_HISTORY_STORE, [])
    out = []
    for row in hist:
        ts = (row.get('timestamp') or '')[:10]
        if ts in dates:
            out.append(row)
    return out


def _daily_series(end_date_str, window_days, market_key=None):
    store = _tracker_store()
    dates = list(reversed(_dates_in_window(end_date_str, window_days)))
    series = []
    for ds in dates:
        rows = list((store.get(ds) or {}).get('entries', []) or [])
        if market_key:
            rows = [r for r in rows if r.get('marketKey') == market_key]
        active = [r for r in rows if r.get('grade') in ('win', 'loss')]
        wins = sum(1 for r in active if r.get('grade') == 'win')
        losses = sum(1 for r in active if r.get('grade') == 'loss')
        hit_rate = round(wins / max(1, wins + losses), 4) if active else None
        avg_edge = round(sum(float(r.get('edge') or 0) for r in active) / max(1, len(active)), 4) if active else None
        series.append({
            'date': ds, 'graded': len(active), 'wins': wins, 'losses': losses,
            'hit_rate': hit_rate, 'avg_edge': avg_edge
        })
    return series


def _multiplier_history(end_date_str, window_days, market_key):
    hist = _history_in_window(end_date_str, window_days)
    points = []
    for row in hist:
        adj = row.get('adjustments', {}) or {}
        mult = ((adj.get('market_multipliers') or {}).get(market_key))
        if mult is None:
            continue
        points.append({
            'timestamp': row.get('timestamp'),
            'multiplier': float(mult),
            'eventType': row.get('eventType'),
            'note': row.get('note'),
            'applied': row.get('applied', []),
        })
    return points


def _default_adjustments():
    return {
        'captured_per_game': 14,
        'best_edge_threshold': 0.03,
        'best_prob_threshold': 0.58,
        'bankroll': 1000.0,
        'kelly_fraction': 0.50,
        'unit_size_pct': 0.01,
        'max_bet_pct': 0.03,
        'max_daily_risk_pct': 0.12,
        'max_team_exposure_pct': 0.05,
        'max_market_exposure_pct': 0.05,
        'max_game_exposure_pct': 0.08,
        'market_multipliers': {
            'batter_hits': 1.00,
            'batter_total_bases': 1.00,
            'batter_home_runs': 1.00,
            'batter_rbis': 1.00,
            'batter_runs_scored': 1.00,
            'batter_stolen_bases': 1.00,
            'pitcher_strikeouts': 1.00,
        }
    }


def _get_adjustments():
    obj = _load_json(ADJUST_STORE, _default_adjustments())
    d = _default_adjustments()
    d.update({k: v for k, v in obj.items() if k != 'market_multipliers'})
    d['market_multipliers'].update(obj.get('market_multipliers', {}))
    return d


def _market_mult(market_key, adjustments):
    return float((adjustments or {}).get('market_multipliers', {}).get(market_key, 1.0) or 1.0)


def _clamp01(v):
    return _clamp(v, 0.01, 0.99)


def _tracker_stat_from_boxscore(player_obj, market_key):
    if not player_obj:
        return None
    if market_key == 'pitcher_strikeouts':
        p = player_obj.get('stats', {}).get('pitching', {})
        return int(p.get('strikeOuts', 0) or 0)
    b = player_obj.get('stats', {}).get('batting', {})
    hits = int(b.get('hits', 0) or 0)
    doubles = int(b.get('doubles', 0) or 0)
    triples = int(b.get('triples', 0) or 0)
    hr = int(b.get('homeRuns', 0) or 0)
    singles = max(0, hits - doubles - triples - hr)
    mapping = {
        'batter_hits': hits,
        'batter_total_bases': singles + 2 * doubles + 3 * triples + 4 * hr,
        'batter_home_runs': hr,
        'batter_rbis': int(b.get('rbi', 0) or 0),
        'batter_runs_scored': int(b.get('runs', 0) or 0),
        'batter_stolen_bases': int(b.get('stolenBases', 0) or 0),
    }
    return mapping.get(market_key)


def _grade_over(actual, line):
    if actual is None:
        return 'pending'
    if float(actual) > float(line):
        return 'win'
    if float(actual) < float(line):
        return 'loss'
    return 'push'


def _projection_reason_short(player, market_key, adj_prob, edge, opp_name=''):
    lbl = market_key.replace('batter_', '').replace('pitcher_', '').replace('_', ' ')
    if edge is not None:
        return f"{player} rates well for {lbl}; model {adj_prob:.1%} with edge {edge:.1%} versus market."
    if opp_name:
        return f"{player} rates well for {lbl}; model {adj_prob:.1%} against {opp_name}."
    return f"{player} rates well for {lbl}; model probability {adj_prob:.1%}."


def _build_tracker_rows_for_game(game_pk, capture_date, adjustments=None):
    adjustments = adjustments or _get_adjustments()
    raw = fetch_schedule(capture_date)
    g = next((x for x in raw if x.get('gamePk') == game_pk), None)
    if not g:
        return []

    box = requests.get(f"{MLB_API}/game/{game_pk}/boxscore", timeout=10).json().get('teams', {})
    away_lineup = get_batters_from_boxscore(box.get('away', {}), 'away')
    home_lineup = get_batters_from_boxscore(box.get('home', {}), 'home')
    if not away_lineup or not home_lineup:
        return []

    away_team = g.get('teams', {}).get('away', {}).get('team', {})
    home_team = g.get('teams', {}).get('home', {}).get('team', {})
    away_team_id = away_team.get('id')
    home_team_id = home_team.get('id')
    away_abbr = away_team.get('abbreviation', 'AWAY')
    home_abbr = home_team.get('abbreviation', 'HOME')
    park = PARK_FACTORS.get(home_team_id, 1.0)

    away_p = g.get('teams', {}).get('away', {}).get('probablePitcher', {})
    home_p = g.get('teams', {}).get('home', {}).get('probablePitcher', {})
    away_pitcher = _pitcher_model(away_p.get('fullName', 'Away SP'), away_p.get('id'), away_team_id)
    home_pitcher = _pitcher_model(home_p.get('fullName', 'Home SP'), home_p.get('id'), home_team_id)

    sims = 260
    rng = random.Random(game_pk + int(capture_date.replace('-', '')) + 10)
    away_store = {i: [] for i in range(len(away_lineup))}
    home_store = {i: [] for i in range(len(home_lineup))}
    away_starter_lines, home_starter_lines = [], []

    for _ in range(sims):
        away_off = _simulate_offense(away_lineup, home_pitcher, home_team_id, park, rng)
        home_off = _simulate_offense(home_lineup, away_pitcher, away_team_id, park, rng)
        for i, line in enumerate(away_off['batters']): away_store[i].append(line)
        for i, line in enumerate(home_off['batters']): home_store[i].append(line)
        home_starter_lines.append(away_off['starter'])
        away_starter_lines.append(home_off['starter'])

    away_props, home_props = [], []
    for i, b in enumerate(away_lineup):
        s = _summarize_player(away_store[i]); s.update({'id': b.get('id'), 'name': b.get('name'), 'slot': b.get('slot'), 'pos': b.get('pos'), 'bats': b.get('bats', 'S')}); away_props.append(s)
    for i, b in enumerate(home_lineup):
        s = _summarize_player(home_store[i]); s.update({'id': b.get('id'), 'name': b.get('name'), 'slot': b.get('slot'), 'pos': b.get('pos'), 'bats': b.get('bats', 'S')}); home_props.append(s)

    away_sp = _summarize_pitcher(away_starter_lines); away_sp.update({'name': away_pitcher['name'], 'id': away_pitcher.get('id'), 'pitchHand': away_pitcher['pitchHand']})
    home_sp = _summarize_pitcher(home_starter_lines); home_sp.update({'name': home_pitcher['name'], 'id': home_pitcher.get('id'), 'pitchHand': home_pitcher['pitchHand']})

    event, _ = _find_odds_event(away_team.get('name', ''), home_team.get('name', ''))
    props_books = _load_event_odds(event.get('id') if event else None, featured_only=False) if event else []
    valid_names = set([x.get('name') for x in away_lineup + home_lineup if x.get('name')])
    if away_pitcher.get('name'): valid_names.add(away_pitcher.get('name'))
    if home_pitcher.get('name'): valid_names.add(home_pitcher.get('name'))
    market_props = _parse_prop_markets(props_books, valid_names)

    def find_market(player, mk, line):
        for item in market_props:
            if item.get('player') == player and item.get('market_key') == mk and float(item.get('line')) == float(line):
                return item
        return None

    rows = []
    hit_defs = [
        ('batter_hits', 0.5, 'p_1plus_hit', 'mean_hits'),
        ('batter_hits', 1.5, 'p_2plus_hit', 'mean_hits'),
        ('batter_total_bases', 1.5, 'p_2plus_tb', 'mean_tb'),
        ('batter_home_runs', 0.5, 'p_1plus_hr', 'mean_hr'),
        ('batter_rbis', 0.5, 'p_1plus_rbi', 'mean_rbi'),
        ('batter_runs_scored', 0.5, 'p_1plus_run', 'mean_runs'),
        ('batter_stolen_bases', 0.5, 'p_1plus_sb', 'mean_sb'),
    ]

    def process_hitters(arr, team_abbr, opp_name):
        for p in arr:
            for mk, line, prob_field, mean_field in hit_defs:
                raw_prob = float(p.get(prob_field, 0) or 0)
                if raw_prob < 0.10:
                    continue
                adj_prob = _clamp01(raw_prob * _market_mult(mk, adjustments))
                market = find_market(p.get('name'), mk, line)
                edge = (adj_prob - market.get('over_implied')) if market and market.get('over_implied') is not None else None
                score = (edge * 100.0 if edge is not None else 0) + adj_prob
                rows.append({
                    'date': capture_date, 'gamePk': game_pk, 'team': team_abbr, 'player': p.get('name'), 'playerId': p.get('id'), 'marketKey': mk, 'line': line, 'recommendedSide': 'Over',
                    'rawProb': round(raw_prob, 4), 'adjProb': round(adj_prob, 4), 'modelMean': round(float(p.get(mean_field, 0) or 0), 3), 'edge': round(edge, 4) if edge is not None else None,
                    'bookmaker': market.get('bookmaker') if market else None, 'marketPrice': market.get('over_price') if market else None, 'marketImplied': market.get('over_implied') if market else None,
                    'score': round(score, 4), 'opp': opp_name, 'reason': _projection_reason_short(p.get('name'), mk, adj_prob, edge, opp_name), 'status': 'pending', 'actual': None, 'grade': 'pending', 'openingPrice': market.get('over_price') if market else None, 'openingImplied': market.get('over_implied') if market else None, 'closingPrice': None, 'closingImplied': None, 'closingBookmaker': None, 'closingCapturedAt': None, 'clvEdge': None, 'profitUnits': None
                })

    process_hitters(away_props, away_abbr, home_pitcher.get('name'))
    process_hitters(home_props, home_abbr, away_pitcher.get('name'))

    for sp, team_abbr in [(away_sp, away_abbr), (home_sp, home_abbr)]:
        for line, prob_field in [(3.5, 'p_4plus_k'), (4.5, 'p_5plus_k'), (5.5, 'p_6plus_k')]:
            raw_prob = float(sp.get(prob_field, 0) or 0)
            if raw_prob < 0.12:
                continue
            adj_prob = _clamp01(raw_prob * _market_mult('pitcher_strikeouts', adjustments))
            market = find_market(sp.get('name'), 'pitcher_strikeouts', line)
            edge = (adj_prob - market.get('over_implied')) if market and market.get('over_implied') is not None else None
            score = (edge * 100.0 if edge is not None else 0) + adj_prob
            rows.append({
                'date': capture_date, 'gamePk': game_pk, 'team': team_abbr, 'player': sp.get('name'), 'playerId': sp.get('id'), 'marketKey': 'pitcher_strikeouts', 'line': line, 'recommendedSide': 'Over',
                'rawProb': round(raw_prob, 4), 'adjProb': round(adj_prob, 4), 'modelMean': round(float(sp.get('mean_k', 0) or 0), 3), 'edge': round(edge, 4) if edge is not None else None,
                'bookmaker': market.get('bookmaker') if market else None, 'marketPrice': market.get('over_price') if market else None, 'marketImplied': market.get('over_implied') if market else None,
                'score': round(score, 4), 'opp': '', 'reason': _projection_reason_short(sp.get('name'), 'pitcher_strikeouts', adj_prob, edge), 'status': 'pending', 'actual': None, 'grade': 'pending', 'openingPrice': market.get('over_price') if market else None, 'openingImplied': market.get('over_implied') if market else None, 'closingPrice': None, 'closingImplied': None, 'closingBookmaker': None, 'closingCapturedAt': None, 'clvEdge': None, 'profitUnits': None
            })

    rows.sort(key=lambda x: x.get('score', 0), reverse=True)
    keep = int((adjustments or {}).get('captured_per_game', 14) or 14)
    return rows[:keep]


def _tracker_summary(entries):
    total = len(entries)
    graded = [x for x in entries if x.get('grade') in ('win', 'loss', 'push')]
    wins = sum(1 for x in graded if x.get('grade') == 'win')
    losses = sum(1 for x in graded if x.get('grade') == 'loss')
    pushes = sum(1 for x in graded if x.get('grade') == 'push')
    hit_rate = round(wins / max(1, wins + losses), 3) if graded else 0.0
    by_market = {}
    for x in entries:
        mk = x.get('marketKey')
        by_market.setdefault(mk, {'picks': 0, 'wins': 0, 'losses': 0, 'pushes': 0})
        by_market[mk]['picks'] += 1
        if x.get('grade') == 'win': by_market[mk]['wins'] += 1
        elif x.get('grade') == 'loss': by_market[mk]['losses'] += 1
        elif x.get('grade') == 'push': by_market[mk]['pushes'] += 1
    return {'picks': total, 'graded': len(graded), 'wins': wins, 'losses': losses, 'pushes': pushes, 'hit_rate': hit_rate, 'by_market': by_market}


@app.route('/tracker')
def tracker_page():
    return TRACKER_HTML


@app.route('/api/tracker/adjustments', methods=['GET', 'POST'])
def api_tracker_adjustments():
    if request.method == 'POST':
        payload = request.get_json(silent=True) or {}
        base = _get_adjustments()
        for key in ['captured_per_game', 'best_edge_threshold', 'best_prob_threshold', 'bankroll', 'kelly_fraction', 'unit_size_pct', 'max_bet_pct', 'max_daily_risk_pct', 'max_team_exposure_pct', 'max_market_exposure_pct', 'max_game_exposure_pct']:
            if key in payload:
                base[key] = payload[key]
        if 'market_multipliers' in payload and isinstance(payload['market_multipliers'], dict):
            base['market_multipliers'].update(payload['market_multipliers'])
        _save_json(ADJUST_STORE, base)
        _append_calibration_history('manual_save', base, {'note': 'Manual adjustment save'})
        return jsonify({'success': True, 'adjustments': base})
    return jsonify({'success': True, 'adjustments': _get_adjustments()})


@app.route('/api/tracker/date/<date_str>')
def api_tracker_date(date_str):
    store = _load_json(TRACKER_STORE, {})
    day = store.get(date_str, {'entries': [], 'capturedAt': None, 'gradedAt': None})
    day['entries'] = _recalc_tracker_entries(day.get('entries', []))
    return jsonify({'success': True, 'date': date_str, 'adjustments': _get_adjustments(), 'capturedAt': day.get('capturedAt'), 'gradedAt': day.get('gradedAt'), 'closingCapturedAt': day.get('closingCapturedAt'), 'entries': day.get('entries', []), 'summary': _tracker_summary(day.get('entries', []))})


@app.route('/api/tracker/capture/<date_str>', methods=['POST'])
def api_tracker_capture(date_str):
    adjustments = _get_adjustments()
    sched = fetch_schedule(date_str)
    entries = []
    for g in sched:
        try:
            entries.extend(_build_tracker_rows_for_game(g.get('gamePk'), date_str, adjustments))
        except Exception:
            print('[tracker_capture_game]', traceback.format_exc())
    store = _load_json(TRACKER_STORE, {})
    entries = _recalc_tracker_entries(entries)
    store[date_str] = {'capturedAt': datetime.now().isoformat(), 'gradedAt': None, 'closingCapturedAt': None, 'entries': entries}
    _save_json(TRACKER_STORE, store)
    return jsonify({'success': True, 'date': date_str, 'entries': entries, 'summary': _tracker_summary(entries), 'capturedAt': store[date_str]['capturedAt']})


@app.route('/api/tracker/grade/<date_str>', methods=['POST'])
def api_tracker_grade(date_str):
    store = _load_json(TRACKER_STORE, {})
    day = store.get(date_str)
    if not day:
        return jsonify({'success': False, 'error': 'No captured tracker data for this date'}), 404
    sched = fetch_schedule(date_str)
    games = {g.get('gamePk'): g for g in sched}
    for row in day.get('entries', []):
        gpk = row.get('gamePk')
        g = games.get(gpk)
        if not g:
            continue
        status = ((g.get('status') or {}).get('detailedState') or '').lower()
        if 'final' not in status:
            continue
        try:
            box = requests.get(f"{MLB_API}/game/{gpk}/boxscore", timeout=10).json().get('teams', {})
            players = {}
            for side in ['away', 'home']:
                players.update((box.get(side) or {}).get('players', {}))
            pobj = None
            pid = row.get('playerId')
            if pid:
                pobj = players.get(f'ID{pid}')
            if not pobj:
                for v in players.values():
                    if (v.get('person', {}).get('fullName') or '').lower() == (row.get('player') or '').lower():
                        pobj = v; break
            actual = _tracker_stat_from_boxscore(pobj, row.get('marketKey'))
            row['actual'] = actual
            row['grade'] = _grade_over(actual, row.get('line'))
            row['status'] = 'graded'
        except Exception:
            print('[tracker_grade_row]', traceback.format_exc())
    day['entries'] = _recalc_tracker_entries(day.get('entries', []))
    day['gradedAt'] = datetime.now().isoformat()
    store[date_str] = day
    _save_json(TRACKER_STORE, store)
    return jsonify({'success': True, 'date': date_str, 'entries': day.get('entries', []), 'summary': _tracker_summary(day.get('entries', [])), 'gradedAt': day.get('gradedAt')})



# ── Phase 12 Auto-Calibration Layer ───────────────────────────────────────────
CALIBRATION_TARGETS = {
    'batter_hits': 0.56,
    'batter_total_bases': 0.55,
    'batter_home_runs': 0.52,
    'batter_rbis': 0.53,
    'batter_runs_scored': 0.53,
    'batter_stolen_bases': 0.52,
    'pitcher_strikeouts': 0.55,
}


def _tracker_store():
    return _load_json(TRACKER_STORE, {})


def _dates_in_window(end_date_str, window_days):
    try:
        end_dt = datetime.strptime(end_date_str, '%Y-%m-%d').date()
    except:
        end_dt = datetime.now().date()
    return [(end_dt - timedelta(days=i)).isoformat() for i in range(max(1, int(window_days)))]


def _collect_window_entries(end_date_str, window_days):
    store = _tracker_store()
    dates = set(_dates_in_window(end_date_str, window_days))
    rows = []
    for ds, payload in store.items():
        if ds in dates:
            rows.extend(payload.get('entries', []))
    return rows


def _market_calibration(entries, current_adj):
    by_market = {}
    for row in entries:
        mk = row.get('marketKey')
        if not mk:
            continue
        by_market.setdefault(mk, [])
        if row.get('grade') in ('win', 'loss'):
            by_market[mk].append(row)

    out = []
    for mk, rows in by_market.items():
        graded = len(rows)
        wins = sum(1 for r in rows if r.get('grade') == 'win')
        losses = sum(1 for r in rows if r.get('grade') == 'loss')
        hit_rate = wins / max(1, graded)
        avg_edge = sum(float(r.get('edge') or 0) for r in rows) / max(1, graded)
        avg_prob = sum(float(r.get('adjProb') or r.get('rawProb') or 0) for r in rows) / max(1, graded)
        target = CALIBRATION_TARGETS.get(mk, 0.54)
        current_mult = float(current_adj.get('market_multipliers', {}).get(mk, 1.0) or 1.0)
        delta = hit_rate - target
        if graded < 8:
            confidence = 'LOW SAMPLE'
            suggested = current_mult
            action = 'hold'
        else:
            shift = _clamp(delta * 0.60, -0.08, 0.08)
            suggested = round(_clamp(current_mult + shift, 0.80, 1.20), 3)
            if suggested > current_mult + 0.004:
                action = 'increase'
            elif suggested < current_mult - 0.004:
                action = 'decrease'
            else:
                action = 'hold'
            confidence = 'HIGH' if graded >= 20 else 'MEDIUM'
        rationale = f"{mk}: {wins}-{losses} over last sample, hit rate {hit_rate:.1%} vs target {target:.1%}, avg edge {avg_edge:.1%}."
        out.append({
            'marketKey': mk,
            'graded': graded,
            'wins': wins,
            'losses': losses,
            'hit_rate': round(hit_rate, 4),
            'target_rate': round(target, 4),
            'avg_edge': round(avg_edge, 4),
            'avg_prob': round(avg_prob, 4),
            'current_multiplier': round(current_mult, 3),
            'suggested_multiplier': round(suggested, 3),
            'action': action,
            'confidence': confidence,
            'rationale': rationale,
        })
    out.sort(key=lambda x: (x['action'] == 'hold', -x['graded'], x['marketKey']))
    return out


def _overall_window_summary(entries):
    graded = [x for x in entries if x.get('grade') in ('win', 'loss', 'push')]
    wins = sum(1 for x in graded if x.get('grade') == 'win')
    losses = sum(1 for x in graded if x.get('grade') == 'loss')
    pushes = sum(1 for x in graded if x.get('grade') == 'push')
    active = [x for x in graded if x.get('grade') in ('win', 'loss')]
    hit_rate = round(wins / max(1, wins + losses), 4) if active else 0.0
    avg_edge = round(sum(float(x.get('edge') or 0) for x in active) / max(1, len(active)), 4) if active else 0.0
    return {
        'tracked': len(entries), 'graded': len(graded), 'wins': wins, 'losses': losses,
        'pushes': pushes, 'hit_rate': hit_rate, 'avg_edge': avg_edge
    }


@app.route('/api/tracker/calibration/dashboard/<date_str>')
def api_tracker_calibration_dashboard(date_str):
    window = int(request.args.get('window', 14) or 14)
    adjustments = _get_adjustments()
    markets = list((adjustments.get('market_multipliers') or {}).keys())
    market_series = {mk: _daily_series(date_str, window, mk) for mk in markets}
    multiplier_history = {mk: _multiplier_history(date_str, window, mk) for mk in markets}
    events = _history_in_window(date_str, window)
    return jsonify({
        'success': True,
        'date': date_str,
        'window': window,
        'overallSeries': _daily_series(date_str, window, None),
        'marketSeries': market_series,
        'multiplierHistory': multiplier_history,
        'events': events[-120:],
        'availableMarkets': markets,
        'adjustments': adjustments,
    })


@app.route('/api/tracker/calibration/<date_str>')
def api_tracker_calibration(date_str):
    window = int(request.args.get('window', 7) or 7)
    entries = _collect_window_entries(date_str, window)
    adjustments = _get_adjustments()
    return jsonify({
        'success': True,
        'date': date_str,
        'window': window,
        'summary': _overall_window_summary(entries),
        'markets': _market_calibration(entries, adjustments),
        'adjustments': adjustments,
    })


@app.route('/api/tracker/calibration/apply', methods=['POST'])
def api_tracker_calibration_apply():
    payload = request.get_json(silent=True) or {}
    date_str = payload.get('date') or datetime.now().strftime('%Y-%m-%d')
    window = int(payload.get('window', 7) or 7)
    selected = payload.get('markets') or []
    current = _get_adjustments()
    suggestions = _market_calibration(_collect_window_entries(date_str, window), current)
    chosen = [s for s in suggestions if (not selected or s['marketKey'] in selected) and s['action'] != 'hold']
    for row in chosen:
        current['market_multipliers'][row['marketKey']] = row['suggested_multiplier']
    _save_json(ADJUST_STORE, current)
    _append_calibration_history('auto_apply', current, {'date': date_str, 'window': window, 'applied': chosen, 'note': 'Auto-calibration apply'})
    return jsonify({'success': True, 'applied': chosen, 'adjustments': current, 'window': window, 'date': date_str})



# ── Phase 14 Closing-Line Value + ROI Simulation ──────────────────────────────

def _profit_units_from_american(price):
    try:
        p = float(price)
        if p > 0:
            return round(p / 100.0, 4)
        if p < 0:
            return round(100.0 / abs(p), 4)
    except:
        pass
    return None


def _recalc_tracker_entry(row):
    if row.get('openingPrice') is None and row.get('marketPrice') is not None:
        row['openingPrice'] = row.get('marketPrice')
    row['openingImplied'] = _american_to_implied(row.get('openingPrice'))
    if row.get('closingPrice') is not None:
        row['closingImplied'] = _american_to_implied(row.get('closingPrice'))
    if row.get('openingImplied') is not None and row.get('closingImplied') is not None:
        row['clvEdge'] = round(float(row['closingImplied']) - float(row['openingImplied']), 4)
    else:
        row['clvEdge'] = None
    if row.get('grade') in ('win', 'loss', 'push'):
        if row.get('grade') == 'win':
            row['profitUnits'] = _profit_units_from_american(row.get('openingPrice'))
        elif row.get('grade') == 'loss':
            row['profitUnits'] = -1.0
        else:
            row['profitUnits'] = 0.0
    else:
        row['profitUnits'] = None
    return row


def _recalc_tracker_entries(entries):
    for row in entries or []:
        _recalc_tracker_entry(row)
    return entries or []


def _daily_value_series(end_date_str, window_days, market_key=None):
    store = _tracker_store()
    dates = list(reversed(_dates_in_window(end_date_str, window_days)))
    series = []
    for ds in dates:
        rows = list((store.get(ds) or {}).get('entries', []) or [])
        if market_key:
            rows = [r for r in rows if r.get('marketKey') == market_key]
        graded = [r for r in rows if r.get('grade') in ('win', 'loss', 'push')]
        staked = len(graded)
        units = round(sum(float(r.get('profitUnits') or 0) for r in graded if r.get('profitUnits') is not None), 4)
        roi = round(units / max(1, staked), 4) if graded else None
        clv = [float(r.get('clvEdge')) for r in graded if r.get('clvEdge') is not None]
        avg_clv = round(sum(clv) / max(1, len(clv)), 4) if clv else None
        clv_pos = round(sum(1 for x in clv if x > 0) / max(1, len(clv)), 4) if clv else None
        series.append({'date': ds, 'staked': staked, 'units': units, 'roi': roi, 'avg_clv': avg_clv, 'clv_pos_rate': clv_pos})
    return series


def _value_summary(entries):
    graded = [r for r in entries if r.get('grade') in ('win', 'loss', 'push')]
    units = round(sum(float(r.get('profitUnits') or 0) for r in graded if r.get('profitUnits') is not None), 4)
    roi = round(units / max(1, len(graded)), 4) if graded else 0.0
    clv = [float(r.get('clvEdge')) for r in graded if r.get('clvEdge') is not None]
    avg_clv = round(sum(clv) / max(1, len(clv)), 4) if clv else 0.0
    clv_pos = round(sum(1 for x in clv if x > 0) / max(1, len(clv)), 4) if clv else 0.0
    return {'units': units, 'roi': roi, 'avg_clv': avg_clv, 'clv_positive_rate': clv_pos, 'graded_with_clv': len(clv)}


def _tracker_summary(entries):
    total = len(entries)
    graded = [x for x in entries if x.get('grade') in ('win', 'loss', 'push')]
    wins = sum(1 for x in graded if x.get('grade') == 'win')
    losses = sum(1 for x in graded if x.get('grade') == 'loss')
    pushes = sum(1 for x in graded if x.get('grade') == 'push')
    hit_rate = round(wins / max(1, wins + losses), 3) if graded else 0.0
    by_market = {}
    for x in entries:
        mk = x.get('marketKey')
        by_market.setdefault(mk, {'picks': 0, 'wins': 0, 'losses': 0, 'pushes': 0, 'units': 0.0})
        by_market[mk]['picks'] += 1
        if x.get('grade') == 'win':
            by_market[mk]['wins'] += 1
        elif x.get('grade') == 'loss':
            by_market[mk]['losses'] += 1
        elif x.get('grade') == 'push':
            by_market[mk]['pushes'] += 1
        if x.get('profitUnits') is not None:
            by_market[mk]['units'] = round(by_market[mk]['units'] + float(x.get('profitUnits') or 0), 4)
    return {
        'picks': total, 'graded': len(graded), 'wins': wins, 'losses': losses, 'pushes': pushes,
        'hit_rate': hit_rate, 'by_market': by_market, 'value': _value_summary(entries)
    }


@app.route('/api/tracker/close/<date_str>', methods=['POST'])
def api_tracker_close(date_str):
    store = _tracker_store()
    day = store.get(date_str)
    if not day:
        return jsonify({'success': False, 'error': 'No captured tracker data for this date'}), 404
    entries = day.get('entries', [])
    by_game = {}
    for row in entries:
        by_game.setdefault(row.get('gamePk'), []).append(row)
    sched = fetch_schedule(date_str)
    games = {g.get('gamePk'): g for g in sched}
    updated = 0
    for gpk, rows in by_game.items():
        g = games.get(gpk)
        if not g:
            continue
        away_name = g.get('teams', {}).get('away', {}).get('team', {}).get('name', '')
        home_name = g.get('teams', {}).get('home', {}).get('team', {}).get('name', '')
        event, _ = _find_odds_event(away_name, home_name)
        if not event:
            continue
        books = _load_event_odds(event.get('id'), featured_only=False)
        valid_names = set([r.get('player') for r in rows if r.get('player')])
        props = _parse_prop_markets(books, valid_names)
        for row in rows:
            m = next((x for x in props if x.get('player') == row.get('player') and x.get('market_key') == row.get('marketKey') and float(x.get('line')) == float(row.get('line'))), None)
            if not m:
                continue
            row['closingPrice'] = m.get('over_price')
            row['closingBookmaker'] = m.get('bookmaker')
            row['closingCapturedAt'] = datetime.now().isoformat()
            _recalc_tracker_entry(row)
            updated += 1
    day['entries'] = _recalc_tracker_entries(entries)
    day['closingCapturedAt'] = datetime.now().isoformat()
    store[date_str] = day
    _save_json(TRACKER_STORE, store)
    return jsonify({'success': True, 'date': date_str, 'updated': updated, 'entries': day.get('entries', []), 'summary': _tracker_summary(day.get('entries', [])), 'closingCapturedAt': day.get('closingCapturedAt')})


@app.route('/api/tracker/value/dashboard/<date_str>')
def api_tracker_value_dashboard(date_str):
    window = int(request.args.get('window', 14) or 14)
    adjustments = _get_adjustments()
    markets = list((adjustments.get('market_multipliers') or {}).keys())
    overall = _daily_value_series(date_str, window, None)
    market_series = {mk: _daily_value_series(date_str, window, mk) for mk in markets}
    entries = _collect_window_entries(date_str, window)
    graded = [r for r in entries if r.get('grade') in ('win', 'loss', 'push')]
    top_clv = sorted([r for r in graded if r.get('clvEdge') is not None], key=lambda x: x.get('clvEdge', 0), reverse=True)[:12]
    worst_clv = sorted([r for r in graded if r.get('clvEdge') is not None], key=lambda x: x.get('clvEdge', 0))[:12]
    top_profit = sorted([r for r in graded if r.get('profitUnits') is not None], key=lambda x: x.get('profitUnits', 0), reverse=True)[:12]
    return jsonify({'success': True, 'date': date_str, 'window': window, 'overallSeries': overall, 'marketSeries': market_series, 'availableMarkets': markets, 'windowSummary': _value_summary(entries), 'topCLV': top_clv, 'worstCLV': worst_clv, 'topProfit': top_profit})



# ── Phase 15 Bankroll + Bet Sizing Layer ──────────────────────────────────────

def _kelly_fraction(prob, price):
    try:
        p = float(prob)
        odds = float(price)
        if odds > 0:
            b = odds / 100.0
        elif odds < 0:
            b = 100.0 / abs(odds)
        else:
            return 0.0
        q = 1.0 - p
        frac = ((b * p) - q) / b
        return round(max(0.0, frac), 6)
    except:
        return 0.0


def _stake_profile(row, adjustments):
    bankroll = float((adjustments or {}).get('bankroll', 1000.0) or 1000.0)
    unit_size_pct = float((adjustments or {}).get('unit_size_pct', 0.01) or 0.01)
    kelly_fraction = float((adjustments or {}).get('kelly_fraction', 0.50) or 0.50)
    max_bet_pct = float((adjustments or {}).get('max_bet_pct', 0.03) or 0.03)
    price = row.get('openingPrice') if row.get('openingPrice') is not None else row.get('marketPrice')
    prob = row.get('adjProb') if row.get('adjProb') is not None else row.get('rawProb')
    full_kelly = _kelly_fraction(prob, price) if price is not None and prob is not None else 0.0
    sized_pct = min(max_bet_pct, max(0.0, full_kelly * kelly_fraction))
    unit_dollars = bankroll * unit_size_pct
    stake_dollars = round(bankroll * sized_pct, 2)
    stake_units = round(stake_dollars / max(0.01, unit_dollars), 3) if stake_dollars > 0 else 0.0
    return {
        'bankroll': bankroll,
        'unit_dollars': round(unit_dollars, 2),
        'full_kelly_pct': round(full_kelly, 4),
        'stake_pct': round(sized_pct, 4),
        'stake_dollars': stake_dollars,
        'stake_units': round(stake_units, 3),
    }


def _recalc_tracker_entry(row):
    if row.get('openingPrice') is None and row.get('marketPrice') is not None:
        row['openingPrice'] = row.get('marketPrice')
    row['openingImplied'] = _american_to_implied(row.get('openingPrice'))
    if row.get('closingPrice') is not None:
        row['closingImplied'] = _american_to_implied(row.get('closingPrice'))
    if row.get('openingImplied') is not None and row.get('closingImplied') is not None:
        row['clvEdge'] = round(float(row['closingImplied']) - float(row['openingImplied']), 4)
    else:
        row['clvEdge'] = None

    adj = _get_adjustments()
    stake = _stake_profile(row, adj)
    row['fullKellyPct'] = stake['full_kelly_pct']
    row['stakePct'] = stake['stake_pct']
    row['stakeUnits'] = stake['stake_units']
    row['stakeDollars'] = stake['stake_dollars']

    if row.get('grade') in ('win', 'loss', 'push'):
        if row.get('grade') == 'win':
            row['profitUnits'] = _profit_units_from_american(row.get('openingPrice'))
        elif row.get('grade') == 'loss':
            row['profitUnits'] = -1.0
        else:
            row['profitUnits'] = 0.0
        row['profitDollars'] = round(float(row.get('profitUnits') or 0) * float(row.get('stakeDollars') or 0), 2)
    else:
        row['profitUnits'] = None
        row['profitDollars'] = None
    return row


def _bankroll_summary(entries, adjustments):
    bankroll = float((adjustments or {}).get('bankroll', 1000.0) or 1000.0)
    unit_size_pct = float((adjustments or {}).get('unit_size_pct', 0.01) or 0.01)
    max_daily_risk_pct = float((adjustments or {}).get('max_daily_risk_pct', 0.12) or 0.12)
    graded = [r for r in entries if r.get('grade') in ('win', 'loss', 'push')]
    planned = [r for r in entries if float(r.get('stakeDollars') or 0) > 0]
    total_staked = round(sum(float(r.get('stakeDollars') or 0) for r in planned), 2)
    total_profit = round(sum(float(r.get('profitDollars') or 0) for r in graded if r.get('profitDollars') is not None), 2)
    live_bankroll = round(bankroll + total_profit, 2)
    daily_cap = round(bankroll * max_daily_risk_pct, 2)
    return {
        'starting_bankroll': round(bankroll, 2),
        'unit_dollars': round(bankroll * unit_size_pct, 2),
        'planned_stake': total_staked,
        'daily_risk_cap': daily_cap,
        'risk_used_pct': round(total_staked / max(0.01, bankroll), 4),
        'over_cap': total_staked > daily_cap,
        'realized_profit': total_profit,
        'live_bankroll': live_bankroll,
    }


def _value_summary(entries):
    graded = [r for r in entries if r.get('grade') in ('win', 'loss', 'push')]
    units = round(sum(float(r.get('profitUnits') or 0) for r in graded if r.get('profitUnits') is not None), 4)
    dollars = round(sum(float(r.get('profitDollars') or 0) for r in graded if r.get('profitDollars') is not None), 2)
    total_staked = round(sum(float(r.get('stakeDollars') or 0) for r in graded if r.get('stakeDollars') is not None), 2)
    roi = round(dollars / max(0.01, total_staked), 4) if graded else 0.0
    clv = [float(r.get('clvEdge')) for r in graded if r.get('clvEdge') is not None]
    avg_clv = round(sum(clv) / max(1, len(clv)), 4) if clv else 0.0
    clv_pos = round(sum(1 for x in clv if x > 0) / max(1, len(clv)), 4) if clv else 0.0
    return {'units': units, 'dollars': dollars, 'staked': total_staked, 'roi': roi, 'avg_clv': avg_clv, 'clv_positive_rate': clv_pos, 'graded_with_clv': len(clv)}


def _daily_value_series(end_date_str, window_days, market_key=None):
    store = _tracker_store()
    dates = list(reversed(_dates_in_window(end_date_str, window_days)))
    series = []
    for ds in dates:
        rows = list((store.get(ds) or {}).get('entries', []) or [])
        if market_key:
            rows = [r for r in rows if r.get('marketKey') == market_key]
        graded = [r for r in rows if r.get('grade') in ('win', 'loss', 'push')]
        staked = round(sum(float(r.get('stakeDollars') or 0) for r in graded if r.get('stakeDollars') is not None), 2)
        units = round(sum(float(r.get('profitUnits') or 0) for r in graded if r.get('profitUnits') is not None), 4)
        dollars = round(sum(float(r.get('profitDollars') or 0) for r in graded if r.get('profitDollars') is not None), 2)
        roi = round(dollars / max(0.01, staked), 4) if graded and staked > 0 else None
        clv = [float(r.get('clvEdge')) for r in graded if r.get('clvEdge') is not None]
        avg_clv = round(sum(clv) / max(1, len(clv)), 4) if clv else None
        clv_pos = round(sum(1 for x in clv if x > 0) / max(1, len(clv)), 4) if clv else None
        series.append({'date': ds, 'staked': staked, 'units': units, 'dollars': dollars, 'roi': roi, 'avg_clv': avg_clv, 'clv_pos_rate': clv_pos})
    return series


def _tracker_summary(entries):
    total = len(entries)
    graded = [x for x in entries if x.get('grade') in ('win', 'loss', 'push')]
    wins = sum(1 for x in graded if x.get('grade') == 'win')
    losses = sum(1 for x in graded if x.get('grade') == 'loss')
    pushes = sum(1 for x in graded if x.get('grade') == 'push')
    hit_rate = round(wins / max(1, wins + losses), 3) if graded else 0.0
    by_market = {}
    for x in entries:
        mk = x.get('marketKey')
        by_market.setdefault(mk, {'picks': 0, 'wins': 0, 'losses': 0, 'pushes': 0, 'units': 0.0, 'dollars': 0.0})
        by_market[mk]['picks'] += 1
        if x.get('grade') == 'win':
            by_market[mk]['wins'] += 1
        elif x.get('grade') == 'loss':
            by_market[mk]['losses'] += 1
        elif x.get('grade') == 'push':
            by_market[mk]['pushes'] += 1
        if x.get('profitUnits') is not None:
            by_market[mk]['units'] = round(by_market[mk]['units'] + float(x.get('profitUnits') or 0), 4)
        if x.get('profitDollars') is not None:
            by_market[mk]['dollars'] = round(by_market[mk]['dollars'] + float(x.get('profitDollars') or 0), 2)
    adjustments = _get_adjustments()
    return {
        'picks': total, 'graded': len(graded), 'wins': wins, 'losses': losses, 'pushes': pushes,
        'hit_rate': hit_rate, 'by_market': by_market, 'value': _value_summary(entries), 'bankroll': _bankroll_summary(entries, adjustments)
    }



# ── Phase 16 Portfolio Constraints Layer ──────────────────────────────────────

def _portfolio_plan(entries, adjustments):
    bankroll = float((adjustments or {}).get('bankroll', 1000.0) or 1000.0)
    daily_cap = bankroll * float((adjustments or {}).get('max_daily_risk_pct', 0.12) or 0.12)
    team_cap = bankroll * float((adjustments or {}).get('max_team_exposure_pct', 0.05) or 0.05)
    market_cap = bankroll * float((adjustments or {}).get('max_market_exposure_pct', 0.05) or 0.05)
    game_cap = bankroll * float((adjustments or {}).get('max_game_exposure_pct', 0.08) or 0.08)
    edge_gate = float((adjustments or {}).get('best_edge_threshold', 0.03) or 0.03)
    prob_gate = float((adjustments or {}).get('best_prob_threshold', 0.58) or 0.58)

    ranked = sorted(entries or [], key=lambda x: (float(x.get('edge') or -999), float(x.get('adjProb') or 0), float(x.get('stakeDollars') or 0)), reverse=True)
    accepted, rejected = [], []
    team_risk, market_risk, game_risk = {}, {}, {}
    total_risk = 0.0

    for row in ranked:
        stake = float(row.get('stakeDollars') or 0)
        edge = row.get('edge')
        prob = float(row.get('adjProb') or row.get('rawProb') or 0)
        game_key = str(row.get('gamePk'))
        team_key = row.get('team') or 'NA'
        market_key = row.get('marketKey') or 'NA'
        reason = None

        if stake <= 0:
            reason = 'No positive Kelly stake.'
        elif edge is not None:
            if float(edge) < edge_gate:
                reason = f'Edge below best-bet threshold ({edge_gate:.1%}).'
        elif prob < prob_gate:
            reason = f'Probability below fallback gate ({prob_gate:.1%}).'
        elif total_risk + stake > daily_cap + 1e-9:
            reason = 'Would breach daily risk cap.'
        elif team_risk.get(team_key, 0.0) + stake > team_cap + 1e-9:
            reason = 'Would breach team exposure cap.'
        elif market_risk.get(market_key, 0.0) + stake > market_cap + 1e-9:
            reason = 'Would breach market exposure cap.'
        elif game_risk.get(game_key, 0.0) + stake > game_cap + 1e-9:
            reason = 'Would breach game exposure cap.'

        item = dict(row)
        if reason is None:
            total_risk += stake
            team_risk[team_key] = round(team_risk.get(team_key, 0.0) + stake, 2)
            market_risk[market_key] = round(market_risk.get(market_key, 0.0) + stake, 2)
            game_risk[game_key] = round(game_risk.get(game_key, 0.0) + stake, 2)
            item['portfolioStatus'] = 'accepted'
            accepted.append(item)
        else:
            item['portfolioStatus'] = 'rejected'
            item['portfolioReason'] = reason
            rejected.append(item)

    accepted_profit = round(sum(float(x.get('profitDollars') or 0) for x in accepted if x.get('profitDollars') is not None), 2)
    accepted_graded = [x for x in accepted if x.get('grade') in ('win', 'loss', 'push')]
    accepted_roi = round(accepted_profit / max(0.01, sum(float(x.get('stakeDollars') or 0) for x in accepted_graded)), 4) if accepted_graded else 0.0

    def _top_exposure(d):
        return sorted([{'key': k, 'risk': round(v,2), 'risk_pct': round(v/max(0.01, bankroll),4)} for k,v in d.items()], key=lambda x: x['risk'], reverse=True)[:12]

    return {
        'summary': {
            'accepted_count': len(accepted),
            'rejected_count': len(rejected),
            'accepted_risk': round(total_risk, 2),
            'remaining_risk': round(max(0.0, daily_cap - total_risk), 2),
            'daily_cap': round(daily_cap, 2),
            'accepted_profit': accepted_profit,
            'accepted_roi': accepted_roi,
            'team_cap': round(team_cap, 2),
            'market_cap': round(market_cap, 2),
            'game_cap': round(game_cap, 2),
        },
        'accepted': accepted[:40],
        'rejected': rejected[:40],
        'exposure': {
            'teams': _top_exposure(team_risk),
            'markets': _top_exposure(market_risk),
            'games': _top_exposure(game_risk),
        }
    }


@app.route('/api/tracker/portfolio/<date_str>')
def api_tracker_portfolio(date_str):
    store = _tracker_store()
    day = store.get(date_str, {'entries': []})
    entries = _recalc_tracker_entries(day.get('entries', []))
    adjustments = _get_adjustments()
    return jsonify({
        'success': True,
        'date': date_str,
        'adjustments': adjustments,
        'portfolio': _portfolio_plan(entries, adjustments),
    })



# ── Phase 17 Bet Slip Builder + Final Card Output ─────────────────────────────

def _confidence_tier(row):
    edge = float(row.get('edge') or 0)
    prob = float(row.get('adjProb') or row.get('rawProb') or 0)
    clv = float(row.get('clvEdge') or 0) if row.get('clvEdge') is not None else None
    if edge >= 0.09 and prob >= 0.67:
        return 'A'
    if edge >= 0.06 and prob >= 0.62:
        return 'B'
    if edge >= 0.04 and prob >= 0.58:
        return 'C'
    return 'D'


def _card_label(row):
    return f"{row.get('player')} OVER {row.get('line')} {row.get('marketKey')}"


def _market_sort_key(row):
    return (-float(row.get('edge') or 0), -float(row.get('adjProb') or 0), -float(row.get('stakeDollars') or 0))


def _build_bet_slip(entries, adjustments):
    plan = _portfolio_plan(entries, adjustments)
    accepted = list(plan.get('accepted', []))
    accepted.sort(key=_market_sort_key)
    singles = []
    for rank, row in enumerate(accepted, start=1):
        item = dict(row)
        item['rank'] = rank
        item['confidenceTier'] = _confidence_tier(item)
        item['cardLabel'] = _card_label(item)
        singles.append(item)

    core = [x for x in singles if x.get('confidenceTier') in ('A', 'B')][:5]
    flex = [x for x in singles if x.get('confidenceTier') in ('C', 'D')][:8]

    top2 = singles[:2]
    top3 = singles[:3]
    top4 = singles[:4]

    def _parlay(items, name):
        if len(items) < 2:
            return None
        total_risk = round(sum(float(x.get('stakeDollars') or 0) for x in items), 2)
        avg_edge = round(sum(float(x.get('edge') or 0) for x in items) / max(1, len(items)), 4)
        avg_prob = round(sum(float(x.get('adjProb') or 0) for x in items) / max(1, len(items)), 4)
        return {
            'name': name,
            'legs': [{'player': x.get('player'), 'marketKey': x.get('marketKey'), 'line': x.get('line'), 'team': x.get('team')} for x in items],
            'avg_edge': avg_edge,
            'avg_prob': avg_prob,
            'proxy_risk': total_risk,
        }

    parlays = [x for x in [_parlay(top2, 'Top 2 Lean Pair'), _parlay(top3, 'Top 3 Ladder'), _parlay(top4, 'Top 4 Longshot Mix')] if x]

    total_risk = round(sum(float(x.get('stakeDollars') or 0) for x in singles), 2)
    total_profit = round(sum(float(x.get('profitDollars') or 0) for x in singles if x.get('profitDollars') is not None), 2)
    by_tier = {}
    for x in singles:
        t = x.get('confidenceTier')
        by_tier.setdefault(t, {'count': 0, 'risk': 0.0})
        by_tier[t]['count'] += 1
        by_tier[t]['risk'] = round(by_tier[t]['risk'] + float(x.get('stakeDollars') or 0), 2)

    return {
        'summary': {
            'recommended_bets': len(singles),
            'core_bets': len(core),
            'flex_bets': len(flex),
            'total_risk': total_risk,
            'realized_profit': total_profit,
            'avg_edge': round(sum(float(x.get('edge') or 0) for x in singles) / max(1, len(singles)), 4) if singles else 0.0,
            'avg_prob': round(sum(float(x.get('adjProb') or 0) for x in singles) / max(1, len(singles)), 4) if singles else 0.0,
            'by_tier': by_tier,
        },
        'singles': singles[:20],
        'core': core,
        'flex': flex,
        'parlays': parlays,
        'portfolio': plan,
    }


@app.route('/api/tracker/betslip/<date_str>')
def api_tracker_betslip(date_str):
    store = _tracker_store()
    day = store.get(date_str, {'entries': []})
    entries = _recalc_tracker_entries(day.get('entries', []))
    adjustments = _get_adjustments()
    return jsonify({
        'success': True,
        'date': date_str,
        'adjustments': adjustments,
        'betslip': _build_bet_slip(entries, adjustments),
    })



# ── Phase 18 Bankroll Curve + Card Audit Reporting ────────────────────────────

def _audit_bucket_init():
    return {'bets': 0, 'graded': 0, 'wins': 0, 'losses': 0, 'pushes': 0, 'risk': 0.0, 'profit': 0.0}


def _audit_bucket_add(bucket, row):
    bucket['bets'] += 1
    bucket['risk'] = round(bucket['risk'] + float(row.get('stakeDollars') or 0), 2)
    if row.get('grade') in ('win', 'loss', 'push'):
        bucket['graded'] += 1
        if row.get('grade') == 'win':
            bucket['wins'] += 1
        elif row.get('grade') == 'loss':
            bucket['losses'] += 1
        elif row.get('grade') == 'push':
            bucket['pushes'] += 1
        bucket['profit'] = round(bucket['profit'] + float(row.get('profitDollars') or 0), 2)


def _audit_bucket_finalize(bucket):
    graded_non_push = bucket['wins'] + bucket['losses']
    bucket['hit_rate'] = round(bucket['wins'] / max(1, graded_non_push), 4) if bucket['graded'] else 0.0
    bucket['roi'] = round(bucket['profit'] / max(0.01, bucket['risk']), 4) if bucket['risk'] > 0 else 0.0
    return bucket


def _bankroll_curve_dashboard(end_date_str, window_days):
    adjustments = _get_adjustments()
    bankroll = float((adjustments or {}).get('bankroll', 1000.0) or 1000.0)
    store = _tracker_store()
    dates = list(reversed(_dates_in_window(end_date_str, window_days)))
    roll = bankroll
    curve = []
    tier_audit = {k: _audit_bucket_init() for k in ['A', 'B', 'C', 'D']}
    card_audit = {k: _audit_bucket_init() for k in ['core', 'flex', 'all_singles']}

    for ds in dates:
        day = store.get(ds, {'entries': []})
        entries = _recalc_tracker_entries(day.get('entries', []))
        slip = _build_bet_slip(entries, adjustments)
        singles = slip.get('singles', [])
        core_ids = set((x.get('player'), x.get('marketKey'), x.get('line')) for x in slip.get('core', []))
        daily_profit = round(sum(float(x.get('profitDollars') or 0) for x in singles if x.get('grade') in ('win', 'loss', 'push') and x.get('profitDollars') is not None), 2)
        daily_risk = round(sum(float(x.get('stakeDollars') or 0) for x in singles), 2)
        graded = [x for x in singles if x.get('grade') in ('win', 'loss', 'push')]
        daily_roi = round(daily_profit / max(0.01, sum(float(x.get('stakeDollars') or 0) for x in graded)), 4) if graded else 0.0
        daily_hit = round(sum(1 for x in graded if x.get('grade') == 'win') / max(1, sum(1 for x in graded if x.get('grade') in ('win', 'loss'))), 4) if graded else 0.0
        roll = round(roll + daily_profit, 2)
        curve.append({'date': ds, 'profit': daily_profit, 'risk': daily_risk, 'roi': daily_roi, 'hit_rate': daily_hit, 'bankroll': roll, 'bets': len(singles)})

        for row in singles:
            tier = row.get('confidenceTier', 'D')
            if tier not in tier_audit:
                tier_audit[tier] = _audit_bucket_init()
            _audit_bucket_add(tier_audit[tier], row)
            _audit_bucket_add(card_audit['all_singles'], row)
            key = (row.get('player'), row.get('marketKey'), row.get('line'))
            if key in core_ids:
                _audit_bucket_add(card_audit['core'], row)
            else:
                _audit_bucket_add(card_audit['flex'], row)

    tier_rows = [{'bucket': k, **_audit_bucket_finalize(v)} for k, v in tier_audit.items()]
    card_rows = [{'bucket': k, **_audit_bucket_finalize(v)} for k, v in card_audit.items()]
    return {
        'summary': {
            'window_days': window_days,
            'start_bankroll': bankroll,
            'end_bankroll': round(curve[-1]['bankroll'], 2) if curve else bankroll,
            'total_profit': round(sum(x['profit'] for x in curve), 2),
            'avg_daily_roi': round(sum(x['roi'] for x in curve) / max(1, len(curve)), 4) if curve else 0.0,
            'active_days': sum(1 for x in curve if x['bets'] > 0),
        },
        'curve': curve,
        'tierAudit': tier_rows,
        'cardAudit': card_rows,
    }


@app.route('/api/tracker/bankroll/dashboard/<date_str>')
def api_tracker_bankroll_dashboard(date_str):
    window = int(request.args.get('window', 14) or 14)
    return jsonify({'success': True, 'date': date_str, 'window': window, 'dashboard': _bankroll_curve_dashboard(date_str, window)})



# ── Phase 19 CLV Attribution + Realized Edge Reporting ────────────────────────

def _attr_bucket_init():
    return {'bets': 0, 'graded': 0, 'wins': 0, 'losses': 0, 'pushes': 0, 'risk': 0.0, 'profit': 0.0, 'clv_sum': 0.0, 'clv_n': 0, 'edge_sum': 0.0, 'prob_sum': 0.0}


def _attr_bucket_add(bucket, row):
    bucket['bets'] += 1
    bucket['risk'] = round(bucket['risk'] + float(row.get('stakeDollars') or 0), 2)
    bucket['edge_sum'] = round(bucket['edge_sum'] + float(row.get('edge') or 0), 6)
    bucket['prob_sum'] = round(bucket['prob_sum'] + float(row.get('adjProb') or row.get('rawProb') or 0), 6)
    if row.get('clvEdge') is not None:
        bucket['clv_sum'] = round(bucket['clv_sum'] + float(row.get('clvEdge') or 0), 6)
        bucket['clv_n'] += 1
    if row.get('grade') in ('win', 'loss', 'push'):
        bucket['graded'] += 1
        if row.get('grade') == 'win':
            bucket['wins'] += 1
        elif row.get('grade') == 'loss':
            bucket['losses'] += 1
        else:
            bucket['pushes'] += 1
        bucket['profit'] = round(bucket['profit'] + float(row.get('profitDollars') or 0), 2)


def _attr_bucket_finalize(name, bucket):
    graded_non_push = bucket['wins'] + bucket['losses']
    return {
        'bucket': name,
        'bets': bucket['bets'],
        'graded': bucket['graded'],
        'wins': bucket['wins'],
        'losses': bucket['losses'],
        'pushes': bucket['pushes'],
        'risk': round(bucket['risk'], 2),
        'profit': round(bucket['profit'], 2),
        'roi': round(bucket['profit'] / max(0.01, bucket['risk']), 4) if bucket['risk'] > 0 else 0.0,
        'hit_rate': round(bucket['wins'] / max(1, graded_non_push), 4) if graded_non_push else 0.0,
        'avg_clv': round(bucket['clv_sum'] / max(1, bucket['clv_n']), 4) if bucket['clv_n'] else 0.0,
        'avg_edge': round(bucket['edge_sum'] / max(1, bucket['bets']), 4) if bucket['bets'] else 0.0,
        'avg_prob': round(bucket['prob_sum'] / max(1, bucket['bets']), 4) if bucket['bets'] else 0.0,
        'clv_samples': bucket['clv_n'],
    }


def _attribution_dashboard(end_date_str, window_days):
    adjustments = _get_adjustments()
    store = _tracker_store()
    dates = list(reversed(_dates_in_window(end_date_str, window_days)))
    market_buckets = {}
    tier_buckets = {k: _attr_bucket_init() for k in ['A', 'B', 'C', 'D']}
    overall = _attr_bucket_init()
    daily = []

    for ds in dates:
        day = store.get(ds, {'entries': []})
        entries = _recalc_tracker_entries(day.get('entries', []))
        slip = _build_bet_slip(entries, adjustments)
        singles = slip.get('singles', [])
        day_bucket = _attr_bucket_init()
        for row in singles:
            _attr_bucket_add(overall, row)
            _attr_bucket_add(day_bucket, row)
            mk = row.get('marketKey') or 'unknown'
            market_buckets.setdefault(mk, _attr_bucket_init())
            _attr_bucket_add(market_buckets[mk], row)
            tier = row.get('confidenceTier', 'D')
            tier_buckets.setdefault(tier, _attr_bucket_init())
            _attr_bucket_add(tier_buckets[tier], row)
        daily.append({'date': ds, **_attr_bucket_finalize(ds, day_bucket)})

    overall_row = _attr_bucket_finalize('overall', overall)
    market_rows = sorted([_attr_bucket_finalize(k, v) for k, v in market_buckets.items()], key=lambda x: (x['profit'], x['avg_clv'], x['bets']), reverse=True)
    tier_rows = [_attr_bucket_finalize(k, tier_buckets.get(k, _attr_bucket_init())) for k in ['A', 'B', 'C', 'D']]
    strongest = [x for x in market_rows if x['avg_clv'] > 0 and x['roi'] > 0][:8]
    weakest = sorted(market_rows, key=lambda x: (x['roi'], x['avg_clv']))[:8]
    return {
        'summary': {
            'graded': overall_row['graded'],
            'bets': overall_row['bets'],
            'risk': overall_row['risk'],
            'profit': overall_row['profit'],
            'roi': overall_row['roi'],
            'avg_clv': overall_row['avg_clv'],
            'positive_clv_rate': round(sum(1 for d in daily if d.get('avg_clv', 0) > 0) / max(1, len(daily)), 4) if daily else 0.0,
            'avg_edge': overall_row['avg_edge'],
            'avg_prob': overall_row['avg_prob'],
        },
        'daily': daily,
        'marketAudit': market_rows,
        'tierAudit': tier_rows,
        'strongestMarkets': strongest,
        'weakestMarkets': weakest,
    }


@app.route('/api/tracker/attribution/dashboard/<date_str>')
def api_tracker_attribution_dashboard(date_str):
    window = int(request.args.get('window', 14) or 14)
    return jsonify({'success': True, 'date': date_str, 'window': window, 'dashboard': _attribution_dashboard(date_str, window)})



TEAM_HEADSHOT_BASE = "https://img.mlbstatic.com/mlb-photos/image/upload/w_180,q_auto:best/v1/people/{player_id}/headshot/67/current"

@app.route('/api/teams/overview')
def api_teams_overview():
    """Load all 30 MLB teams with rosters. Uses only cached in-memory FG/Savant
    data for player stats — no per-player MLB API calls to avoid timeouts."""
    try:
        teams_resp = requests.get(f"{MLB_API}/teams?sportId=1&activeStatus=Y&sportId=1", timeout=12)
        teams_resp.raise_for_status()
        teams_raw = [t for t in teams_resp.json().get('teams', []) if t.get('sport', {}).get('id') == 1]
        # Fetch all 30 rosters concurrently using threads
        import concurrent.futures
        def fetch_roster(t):
            tid = t.get('id')
            try:
                rr = requests.get(f"{MLB_API}/teams/{tid}/roster?rosterType=active", timeout=8)
                roster = rr.json().get('roster', []) if rr.ok else []
            except Exception:
                roster = []
            players = []
            for r in roster[:40]:
                person = r.get('person', {})
                pid = person.get('id')
                name = person.get('fullName', 'Unknown')
                pos = (r.get('position', {}) or {}).get('abbreviation', '?')
                # Use only cached in-memory data — no individual MLB API calls
                if pos == 'P':
                    fgp = fg_pitcher(name)
                    svp = sv_pitcher(name)
                    stat_line = {
                        'label1': 'ERA',
                        'value1': fgp.get('fg_era') or svp.get('sv_xera') or '—',
                        'label2': 'FIP',
                        'value2': fgp.get('fg_fip') or '—',
                        'label3': 'K%',
                        'value3': fgp.get('fg_kpct') or svp.get('sv_k_pct') or '—',
                    }
                else:
                    fgb = fg_batter(name)
                    svb = sv_batter(name)
                    stat_line = {
                        'label1': 'AVG',
                        'value1': fgb.get('fg_avg') or svb.get('sv_xba') or '—',
                        'label2': 'wOBA',
                        'value2': fgb.get('fg_woba') or svb.get('sv_xwoba') or '—',
                        'label3': 'wRC+',
                        'value3': fgb.get('fg_wrc') or '—',
                    }
                players.append({
                    'id': pid,
                    'name': name,
                    'pos': pos,
                    'image': TEAM_HEADSHOT_BASE.format(player_id=pid) if pid else '',
                    **stat_line,
                })
            return {
                'id': tid,
                'abbr': t.get('abbreviation', '?'),
                'name': t.get('name', ''),
                'logo': LOGO_BASE.format(team_id=tid),
                'players': players,
            }
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(fetch_roster, sorted(teams_raw, key=lambda x: x.get('abbreviation', ''))))
        return jsonify({'success': True, 'teams': [r for r in results if r]})
    except Exception as ex:
        return jsonify({'success': False, 'error': str(ex), 'teams': []}), 500


@app.route('/api/projections/monte-carlo')
def api_projections_monte_carlo():
    """Monte Carlo projection board. Uses Odds API props when key is configured,
    otherwise falls back to simulation-based projections from the existing sim engine."""
    try:
        date_str = datetime.now(ET).strftime('%Y-%m-%d')
        raw = fetch_schedule(date_str)
        games = []
        ranked = []
        has_odds = bool(ODDS_API_KEY)
        for g in raw:
            game_pk = g.get('gamePk')
            away_team = g.get('teams', {}).get('away', {}).get('team', {})
            home_team = g.get('teams', {}).get('home', {}).get('team', {})
            away = away_team.get('abbreviation', '?')
            home = home_team.get('abbreviation', '?')
            matchup = f'{away} @ {home}'
            top_props = []
            try:
                away_p = g.get('teams', {}).get('away', {}).get('probablePitcher', {})
                home_p = g.get('teams', {}).get('home', {}).get('probablePitcher', {})
                try:
                    box = requests.get(f"{MLB_API}/game/{game_pk}/boxscore", timeout=8).json().get('teams', {})
                    away_lineup = get_batters_from_boxscore(box.get('away', {}), 'away')
                    home_lineup = get_batters_from_boxscore(box.get('home', {}), 'home')
                except Exception:
                    away_lineup, home_lineup = [], []

                if has_odds:
                    # --- Odds API path ---
                    valid_names = set(x.get('name') for x in away_lineup + home_lineup if x.get('name'))
                    if away_p.get('fullName'): valid_names.add(away_p['fullName'])
                    if home_p.get('fullName'): valid_names.add(home_p['fullName'])
                    event, _ = _find_odds_event(away_team.get('name', ''), home_team.get('name', ''))
                    props_books = _load_event_odds(event.get('id') if event else None, featured_only=False) if event else []
                    props = _parse_prop_markets(props_books, valid_names)
                    scored = []
                    for p in props:
                        op = p.get('over_implied')
                        up = p.get('under_implied')
                        if op is None or up is None: continue
                        vig = op + up
                        if vig <= 0: continue
                        fair = op / vig
                        price = p.get('over_price')
                        if price is None: continue
                        book_implied = _american_to_implied(price)
                        edge = round(fair - book_implied, 4) if book_implied else 0
                        scored.append({
                            'player': p.get('player'),
                            'market': p.get('market_key'),
                            'line': p.get('line'),
                            'bookmaker': p.get('bookmaker'),
                            'price': price,
                            'edge': round(edge, 4),
                            'prob': round(fair, 4),
                            'source': 'odds',
                        })
                    top_props = sorted(scored, key=lambda x: x['edge'], reverse=True)[:12]
                else:
                    # --- Simulation fallback path (no Odds API key needed) ---
                    all_batters = away_lineup + home_lineup
                    hit_defs = [
                        ('batter_hits', 0.5, 'Hit', 'Hits'),
                        ('batter_total_bases', 1.5, 'Total Bases', 'TB'),
                        ('batter_home_runs', 0.5, 'Home Run', 'HR'),
                        ('batter_rbis', 0.5, 'RBI', 'RBIs'),
                    ]
                    for b in all_batters[:18]:
                        name = b.get('name', '')
                        if not name: continue
                        svb = sv_batter(name)
                        fgb = fg_batter(name)
                        # estimate hit probability from xBA + wOBA
                        try:
                            xba = float(svb.get('sv_xba') or fgb.get('fg_avg') or 0.250)
                        except: xba = 0.250
                        try:
                            woba = float(svb.get('sv_xwoba') or fgb.get('fg_woba') or 0.320)
                        except: woba = 0.320
                        pa_prob_hit = min(0.95, xba * 3.2)
                        pa_prob_tb  = min(0.85, woba * 2.4)
                        pa_prob_hr  = min(0.45, float(svb.get('sv_brl_pct') or 0) / 100 * 1.8)
                        pa_prob_rbi = min(0.70, woba * 2.0)
                        probs = [pa_prob_hit, pa_prob_tb, pa_prob_hr, pa_prob_rbi]
                        for (mk, line, label, unit), prob in zip(hit_defs, probs):
                            if prob < 0.35: continue
                            top_props.append({
                                'player': name,
                                'market': mk,
                                'line': line,
                                'bookmaker': 'Model',
                                'price': None,
                                'edge': round(prob - 0.50, 4),
                                'prob': round(prob, 4),
                                'source': 'simulation',
                            })
                    # Pitcher K projections
                    for pit_info in [away_p, home_p]:
                        pname = pit_info.get('fullName', '')
                        if not pname: continue
                        fgp = fg_pitcher(pname)
                        svp = sv_pitcher(pname)
                        try:
                            k9 = float(fgp.get('fg_k9') or svp.get('sv_k_pct') or 0)
                            k_prob = min(0.85, k9 / 9 * 0.85) if k9 > 0 else 0.55
                        except: k_prob = 0.55
                        if k_prob >= 0.40:
                            top_props.append({
                                'player': pname,
                                'market': 'pitcher_strikeouts',
                                'line': 5.5,
                                'bookmaker': 'Model',
                                'price': None,
                                'edge': round(k_prob - 0.50, 4),
                                'prob': round(k_prob, 4),
                                'source': 'simulation',
                            })
                    top_props = sorted(top_props, key=lambda x: x['edge'], reverse=True)[:12]

                for row in top_props:
                    ranked.append({'matchup': matchup, **row})
            except Exception as ex:
                print(f"[mc_game] {game_pk} {ex}")
            games.append({'gamePk': game_pk, 'matchup': matchup, 'topProps': top_props})
        ranked = sorted(ranked, key=lambda x: x.get('edge', 0), reverse=True)
        return jsonify({
            'success': True,
            'date': date_str,
            'hasOdds': has_odds,
            'games': games,
            'topProps': ranked[:60],
        })
    except Exception as ex:
        return jsonify({'success': False, 'error': str(ex), 'games': [], 'topProps': []}), 500


@app.route('/api/lineup/<int:game_pk>')
def api_lineup(game_pk):
    try:
        r = requests.get(f"{MLB_API}/game/{game_pk}/boxscore", timeout=10)
        d = r.json().get('teams', {})
        away = get_batters_from_boxscore(d.get('away', {}), 'away')
        home = get_batters_from_boxscore(d.get('home', {}), 'home')
        away_conf = len(away) >= 9
        home_conf = len(home) >= 9
        return jsonify({'success': True, 'gamePk': game_pk, 'away': away[:9], 'home': home[:9], 'awayConfirmed': away_conf, 'homeConfirmed': home_conf})
    except Exception as ex:
        return jsonify({'success': False, 'gamePk': game_pk, 'away': [], 'home': [], 'awayConfirmed': False, 'homeConfirmed': False, 'error': str(ex)})


# Boot background loaders
threading.Thread(target=_load_fg_data,      daemon=True).start()
threading.Thread(target=_load_savant_data,  daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
