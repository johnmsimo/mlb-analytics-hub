import joblib
import pandas as pd
import os, threading, traceback, difflib, io, csv as csvmod, json, re
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
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
PROPS_HTML = open(os.path.join(_HERE, 'props.html')).read()
DATA_DIR = os.path.join(_HERE, 'data')
os.makedirs(DATA_DIR, exist_ok=True)
TRACKER_STORE = os.path.join(DATA_DIR, 'daily_tracker.json')
ADJUST_STORE = os.path.join(DATA_DIR, 'model_adjustments.json')
CAL_HISTORY_STORE = os.path.join(DATA_DIR, 'calibration_history.json')
VALUE_HISTORY_STORE = os.path.join(DATA_DIR, 'value_history.json')
hits_model = joblib.load("models/hits_model.pkl")
hits_features = joblib.load("models/hits_model_features.pkl")

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
        df = pb.batting_stats(year, qual=0)
        bat = {}
        for _, r in df.iterrows():
            k = str(r.get("Name","")).strip().lower()
            if k:
                bat[k] = {
                    "fg_avg":  round(float(r.get("AVG")   or 0), 3),
                    "fg_obp":  round(float(r.get("OBP")   or 0), 3),
                    "fg_slg":  round(float(r.get("SLG")   or 0), 3),
                    "fg_ops":  round(float(r.get("OPS")   or 0), 3),
                    "fg_woba": round(float(r.get("wOBA")  or 0), 3),
                    "fg_wrc":  int(r.get("wRC+") or 0),
                    "fg_pa":   int(r.get("PA")   or 0),
                    "fg_r":    int(r.get("R")    or 0),
                    "fg_hr":   int(r.get("HR")   or 0),
                    "fg_rbi":  int(r.get("RBI")  or 0),
                    "fg_sb":   int(r.get("SB")   or 0),
                    "fg_war":  round(float(r.get("WAR")   or 0), 1),
                    "fg_babip":round(float(r.get("BABIP")  or 0), 3),
                    "fg_bbpct":round(float(r.get("BB%")    or 0), 3),
                    "fg_kpct": round(float(r.get("K%")     or 0), 3),
                    "fg_iso":  round(float(r.get("ISO")    or 0), 3),
                }
        with _fg_lock: _fg_bat = bat
        print(f"[FG] Batting: {len(bat)}")
    except Exception as ex:
        print("[FG] Batting failed:", ex)
    try:
        import pybaseball as pb
        df = pb.pitching_stats(year, qual=0)
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
    m = difflib.get_close_matches(k, cache.keys(), n=1, cutoff=0.78)
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
                "sv_xera":    _sv_f(row.get("xera")),
                "sv_era_p":   _sv_f(row.get("era")),
                "sv_xwoba_p": _sv_f(row.get("est_woba")),
                "sv_k_pct":   _sv_f(row.get("k_percent")),
                "sv_bb_pct":  _sv_f(row.get("bb_percent")),
                "sv_whiff":   _sv_f(row.get("whiff_percent")),
                "sv_pid":     row.get("player_id",""),
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
                "sv_k_pct": _sv_f(row.get("k_percent")),
                "sv_bb_pct":_sv_f(row.get("bb_percent")),
                "sv_pid":   row.get("player_id",""),
            }
        with _sv_lock: _sv_bat_xstats = d
        print(f"[Savant] Batter xStats: {len(d)}")
    except Exception as ex:
        print("[Savant] Batter xStats failed:", ex)

    # 3. Statcast batter EV / HH% / Barrel%
    try:
        rows = _fetch_sv_csv(f"{BASE}/leaderboard/statcast?type=batter&year={y}&position=&team=&min=1&csv=true")
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
           "&hydrate=team,probablePitcher,linescore,venue(location),weather")
    r = requests.get(url, timeout=10); r.raise_for_status()
    dates = r.json().get("dates", [])
    return dates[0].get("games", []) if dates else []


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
        return {
            "temp":       round(temps[idx]) if idx < len(temps) else "N/A",
            "rain_chance":precip[idx] if idx < len(precip) else "N/A",
            "wind_speed": wind_speed,
            "wind_dir":   compass,
            "wind":       wind_str,
            "condition":  wcode_map.get(wcode, "Clear"),
        }
    except Exception as ex:
        print(f"[get_weather] lat={lat} lon={lon} hour={game_hour} venue={venue_id} err={ex}")
        return {"temp":"N/A","rain_chance":"N/A","wind_speed":"N/A","wind_dir":"","wind":"N/A","condition":"N/A"}

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
        except:
            game_hour_local = 13
        wx = get_weather(lat, lon, game_hour_local, venue_id=venue_id)
        # Fallback: use MLB schedule weather when Open-Meteo fails or returns unavailable data.
        if (wx.get("temp") in (None, "N/A") or wx.get("condition") in (None, "", "N/A")):
            raw_weather = g.get("weather", {}) or {}
            if raw_weather:
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
            "temp": wx.get("temp","N/A"), "wind": wx.get("wind", f"{wx.get('wind_speed','?')} mph {wx.get('wind_dir','')}").strip(),
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

@app.route('/props')
def props_page():
    return PROPS_HTML

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
        date_str = datetime.now(ET).strftime("%Y-%m-%d")
        raw   = fetch_schedule(date_str)
        games = [g for g in [parse_game(x) for x in raw] if g]
        return jsonify({"success":True,"games":games,"count":len(games)})
    except Exception as ex:
        print("[api_games_today]", traceback.format_exc())
        return jsonify({"success":False,"error":str(ex),"games":[]}), 500

@app.route("/api/game/<int:game_pk>")
def api_game_detail(game_pk):
    _maybe_refresh_fg()
    _maybe_refresh_savant()
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
    _maybe_refresh_fg()
    _maybe_refresh_savant()
    try:
        raw = fetch_schedule(datetime.now(ET).strftime("%Y-%m-%d"))
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
    _maybe_refresh_fg()
    _maybe_refresh_savant()
    try:
        raw = fetch_schedule(datetime.now(ET).strftime("%Y-%m-%d"))
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


def _build_ai_lines(name, is_pitcher, season, fg, sv, logs):
    """Generate 3-5 plain-text AI scouting sentences from cached + live stats."""
    lines = []
    try:
        first = name.split()[0] if name else "Player"
        if not is_pitcher:
            avg  = float(fg.get("fg_avg")     or season.get("avg")   or 0)
            woba = float(fg.get("fg_woba")    or 0)
            wrc  = int(fg.get("fg_wrc")       or 0)
            xba  = float(sv.get("sv_xba")     or 0)
            ev   = float(sv.get("sv_ev")      or 0)
            brl  = float(sv.get("sv_brl_pct") or 0)
            hh   = float(sv.get("sv_hh_pct")  or 0)
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
            era  = float(fg.get("fg_era")  or season.get("era")  or 0)
            fip  = float(fg.get("fg_fip")  or 0)
            kpct = float(fg.get("fg_kpct") or 0)
            xera = float(sv.get("sv_xera") or 0)
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
        team_abbr  = (p.get("currentTeam") or {}).get("abbreviation", "?")
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
        # Strip underscores: fg_avg→fgavg, sv_hh_pct→svhhpct, sv_arsenal_pct→svarsenalpct
        fg_out = {k.replace("_", ""): v for k, v in fgr.items()}
        sv_out = {k.replace("_", ""): v for k, v in svr.items()}

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
                for sp in (lr.json().get("stats", [{}])[0].get("splits", []))[-10:]:
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
        })
    except Exception as ex:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(ex)}), 500


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


@app.route('/api/simulate/<int:game_pk>')
def api_simulate(game_pk):
    try:
        raw = fetch_schedule(datetime.now(ET).strftime('%Y-%m-%d'))
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


def _find_odds_event(away_name, home_name):
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
        for ev in events:
            if _norm_name(ev.get('away_team')) == na and _norm_name(ev.get('home_team')) == nh:
                return ev, events
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
        raw = fetch_schedule(datetime.now(ET).strftime('%Y-%m-%d'))
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
            # ZERO per-player API calls — all stats from pre-cached FG/Savant
            tid = t.get('id')
            try:
                rr = requests.get(f"{MLB_API}/teams/{tid}/roster?rosterType=active", timeout=8)
                roster = rr.json().get('roster', []) if rr.ok else []
            except Exception:
                roster = []
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
        return jsonify({'success': True, 'teams': [r for r in results if r]})
    except Exception as ex:
        return jsonify({'success': False, 'error': str(ex), 'teams': []}), 500


@app.route('/api/projections/monte-carlo')
def api_projections_monte_carlo():
    """Monte Carlo projection board. Uses Odds API props when key is configured,
    otherwise falls back to simulation-based projections from the existing sim engine."""
    _maybe_refresh_fg()
    _maybe_refresh_savant()
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
            projection = float(sel.get('projection', 0))
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
        for v in (sv.get('sv_xera'), fg.get('fg_era'), mlb.get('era')):
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
    away_runs = 4.50 * (4.50 / away_blend) * (away_xwoba / 0.320) * pf
    home_runs = 4.50 * (4.50 / home_blend) * (home_xwoba / 0.320) * pf

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
        date_str = datetime.now(ET).strftime("%Y-%m-%d")
        raw = fetch_schedule(date_str)
        gdata = next((g for g in raw if g.get("gamePk") == game_pk), None)
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
        except:
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
        except:
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
def _props_fetch_game(game_pk):
    """Fetch schedule entry + boxscore lineups. Searches today ± 1 day."""
    gdata = None
    for delta in (0, -1, 1):
        date_str = (datetime.now(ET) + timedelta(days=delta)).strftime("%Y-%m-%d")
        raw   = fetch_schedule(date_str)
        gdata = next((g for g in raw if g.get("gamePk") == game_pk), None)
        if gdata:
            break
    if not gdata:
        return None, [], [], {}, {}, {}

    away_t  = gdata.get("teams", {}).get("away", {})
    home_t  = gdata.get("teams", {}).get("home", {})
    ap_info = away_t.get("probablePitcher", {})
    hp_info = home_t.get("probablePitcher", {})

    away_bats, home_bats = [], []
    try:
        r = requests.get(f"{MLB_API}/game/{game_pk}/boxscore", timeout=10)
        r.raise_for_status()
        box = r.json().get("teams", {})
        away_bats = get_batters_from_boxscore(box.get("away", {}), "away")
        home_bats = get_batters_from_boxscore(box.get("home", {}), "home")
    except Exception as ex:
        print(f"[props] boxscore error: {ex}")

    return gdata, away_bats, home_bats, away_t, home_t, {"ap": ap_info, "hp": hp_info}


# ── Platoon blend helper ──────────────────────────────────────────────────────
def _platoon_blend(batter, pitcher_hand, stat):
    """
    Returns a platoon-blended value for a given stat key ('avg', 'ops', 'slg', 'obp').
    Blends split stat with season stat weighted by PA sample size:
      >= 100 PA  → 80% split / 20% season
      >= 50  PA  → 65% split / 35% season
      >= 25  PA  → 45% split / 55% season
      <  25  PA  → season only
    Falls back gracefully when splits are missing.
    pitcher_hand: 'L' or 'R' (or 'S' treated as 'R')
    """
    hand = (pitcher_hand or 'R').upper()
    if hand not in ('L', 'R'):
        hand = 'R'

    split_key = f"vs_{'l' if hand == 'L' else 'r'}_{stat}"
    split_val = _safe_f(batter.get(split_key), 0.0)

    # Retrieve split PA — stored as vs_l_pa / vs_r_pa if present, else infer from ops
    pa_key    = f"vs_{'l' if hand == 'L' else 'r'}_pa"
    split_pa  = int(batter.get(pa_key, 0) or 0)

    # Season value
    season_map = {
        'avg': batter.get('avg') or batter.get('fg_avg'),
        'ops': batter.get('ops'),
        'slg': batter.get('slg') or batter.get('fg_slg'),
        'obp': batter.get('obp') or batter.get('fg_obp'),
    }
    season_val = _safe_f(season_map.get(stat), _STAT_DEFAULTS.get(stat, 0.0))

    # No split data — return season
    if split_val <= 0.0:
        return season_val

    # Blend weights by sample size
    if split_pa >= 100:
        w_split = 0.80
    elif split_pa >= 50:
        w_split = 0.65
    elif split_pa >= 25:
        w_split = 0.45
    else:
        w_split = 0.30   # small sample — lean season

    w_season  = 1.0 - w_split
    blended   = round(w_split * split_val + w_season * season_val, 4)
    return blended


# Stat defaults used as fallback when both split and season are missing
_STAT_DEFAULTS = {
    'avg': 0.245,
    'obp': 0.315,
    'slg': 0.390,
    'ops': 0.705,
}


def _batter_hand_note(batter, pitcher_hand):
    """
    Returns a short string describing the platoon matchup, e.g. 'LHB vs RHP (+)'
    Used for UI display on the projection card.
    """
    bat  = (batter.get('bats') or 'S').upper()
    pit  = (pitcher_hand or 'R').upper()
    if bat == 'S':
        return f"SB vs {'RHP' if pit == 'R' else 'LHP'} (switch)"
    favorable = (bat == 'L' and pit == 'R') or (bat == 'R' and pit == 'L')
    direction = '+' if favorable else '−'
    return f"{'LHB' if bat == 'L' else 'RHB'} vs {'RHP' if pit == 'R' else 'LHP'} ({direction})"


# ── Batter projection engine (v2 — platoon-aware) ────────────────────────────
def _project_batter(batter, opp_pitcher_name, opp_pitcher_fg, opp_pitcher_sv,
                    park_factor, weather, pitcher_hand='R'):
    """
    Returns per-prop projections for one batter.
    Uses platoon-blended avg/obp/slg/ops as base rates when split PA is available.
    """
    name = batter.get("name", "")
    fg   = fg_batter(name)
    sv   = sv_batter(name)

    # ── Platoon-blended base rates ────────────────────────────────────────────
    avg  = _platoon_blend(batter, pitcher_hand, 'avg')
    obp  = _platoon_blend(batter, pitcher_hand, 'obp')
    slg  = _platoon_blend(batter, pitcher_hand, 'slg')
    ops  = _platoon_blend(batter, pitcher_hand, 'ops')

    # Fallback chain for fg-only stats (not in splits)
    fg_pa  = _safe_f(fg.get("fg_pa"),  200)
    fg_hr  = _safe_f(fg.get("fg_hr"),  3)
    fg_rbi = _safe_f(fg.get("fg_rbi"), 12)
    fg_r   = _safe_f(fg.get("fg_r"),   12)
    hr_r   = fg_hr  / max(fg_pa, 1)
    rbi_r  = fg_rbi / max(fg_pa, 1)
    r_r    = fg_r   / max(fg_pa, 1)

    # Scale HR rate by platoon OPS ratio (strong platoon advantage boosts power)
    season_ops  = _safe_f(batter.get('ops'), _STAT_DEFAULTS['ops'])
    ops_ratio   = ops / max(season_ops, 0.400)            # ≈1.0 neutral, >1 platoon advantage
    hr_r_adj    = hr_r * ops_ratio                        # HR scales with ops split

    xwoba = _safe_f(sv.get("sv_xwoba") or fg.get("fg_woba"), 0.310)
    brl   = _safe_f(sv.get("sv_brl_pct"), 6.0) / 100

    # ── Pitcher resistance multiplier ─────────────────────────────────────────
    opp_era  = _safe_f(opp_pitcher_fg.get("fg_era")  or opp_pitcher_sv.get("sv_era_p"), 4.20)
    opp_xera = _safe_f(opp_pitcher_sv.get("sv_xera"), opp_era)
    opp_kpct = _safe_f(opp_pitcher_fg.get("fg_kpct"), 0.22)
    bat_kpct = _safe_f(fg.get("fg_kpct") or sv.get("sv_k_pct"), 0.22)

    pit_mult = min(1.25, max(0.72, (opp_era + opp_xera) / 2 / 4.20))
    k_adj    = 1.0 - max(0.0, (opp_kpct - bat_kpct) * 0.5)

    # ── Platoon K-rate modifier ───────────────────────────────────────────────
    # Platoon disadvantage → more K exposure
    bat  = (batter.get('bats') or 'S').upper()
    pit  = (pitcher_hand or 'R').upper()
    if bat != 'S':
        platoon_k_mod = 0.96 if ((bat == 'L' and pit == 'R') or (bat == 'R' and pit == 'L')) else 1.04
    else:
        platoon_k_mod = 1.0   # switch hitter, neutral
    k_adj *= platoon_k_mod

    # ── Weather multiplier ────────────────────────────────────────────────────
    dome      = weather.get("dome", False)
    wx_mult   = 1.0
    if not dome:
        temp_f   = _safe_f(weather.get("temp"), 72)
        wind_spd = _safe_f(weather.get("wind_speed"), 0)
        wx_mult  = 1.0 + (temp_f - 72) * 0.003 + min(0.06, float(wind_spd) * 0.003)

    # ── Expected PA by slot ───────────────────────────────────────────────────
    slot   = int(batter.get("slot") or 5)
    exp_pa = round(4.35 - (slot - 1) * 0.095, 2)

    # ── Projections ───────────────────────────────────────────────────────────
    # Hits: platoon avg replaces season avg as base
    hits_proj = round(max(0.05, avg * exp_pa * pit_mult * k_adj * park_factor * wx_mult), 3)

    # Total Bases: built from platoon slg
    tb_proj   = round(max(0.08, slg * exp_pa * pit_mult * k_adj * park_factor * wx_mult), 3)

    # HR: platoon-adjusted hr rate + barrel bonus + park + weather
    hr_pf     = min(1.30, park_factor * 1.08)
    hr_proj   = round(max(0.005,
        hr_r_adj * exp_pa * hr_pf * (1.0 + (brl - 0.06) * 0.8) * wx_mult
    ), 4)

    # RBI: correlated to hits+HR, slot bonus
    slot_rbi_bonus = max(0.8, 1.0 + (4 - abs(slot - 4)) * 0.03)
    rbi_proj  = round(max(0.05, rbi_r * exp_pa * pit_mult * park_factor * slot_rbi_bonus), 3)

    # Runs: lead-off slots score more
    slot_r_bonus = max(0.8, 1.1 - abs(slot - 1.5) * 0.025)
    r_proj    = round(max(0.04, r_r * exp_pa * pit_mult * slot_r_bonus), 3)

    # H+R+RBI combo
    hrr_proj  = round(hits_proj + r_proj + rbi_proj, 3)

    return {
        "hits":        hits_proj,
        "hr":          hr_proj,
        "tb":          tb_proj,
        "rbi":         rbi_proj,
        "r":           r_proj,
        "hrr":         hrr_proj,
        "expected_pa": exp_pa,
        # expose the blended rates so the frontend can show them
        "split_avg":   round(avg, 3),
        "split_ops":   round(ops, 3),
        "platoon_note": _batter_hand_note(batter, pitcher_hand),
    }


# ── Pitcher recent form cache ─────────────────────────────────────────────────
_pitcher_recent_cache = {}   # pid → {ts, era_recent, k9_recent, whip_recent, starts}

def _pitcher_recent_form(pitcher_id, n_starts=5):
    """
    Fetch last n_starts game logs for a pitcher and return blended weighted stats.
    Caches per pitcher per calendar day.
    Returns dict with era_recent, k9_recent, whip_recent, n_starts_found.
    """
    if not pitcher_id:
        return {}
    cache_key = f"{pitcher_id}_{datetime.now(ET).strftime('%Y-%m-%d')}"
    if cache_key in _pitcher_recent_cache:
        return _pitcher_recent_cache[cache_key]
    try:
        yr = datetime.now().year
        r  = requests.get(
            f"{MLB_API}/people/{pitcher_id}/stats",
            params={"stats": "gameLog", "group": "pitching", "season": yr},
            timeout=8,
        )
        r.raise_for_status()
        all_splits = r.json().get("stats", [{}])[0].get("splits", [])
        # Only real starts (IP >= 3.0)
        starts = [
            sp for sp in all_splits
            if _safe_f(sp.get("stat", {}).get("inningsPitched", "0"), 0) >= 3.0
        ][-n_starts:]
        if not starts:
            _pitcher_recent_cache[cache_key] = {}
            return {}
        total_ip = total_er = total_k = total_bb = total_h = 0.0
        for sp in starts:
            st  = sp.get("stat", {})
            ip_s = str(st.get("inningsPitched", "0.0"))
            try:
                whole, thirds = ip_s.split(".")
                ip = int(whole) + int(thirds) / 3
            except Exception:
                ip = _safe_f(ip_s, 0)
            total_ip   += ip
            total_er   += _safe_f(st.get("earnedRuns"),   0)
            total_k    += _safe_f(st.get("strikeOuts"),   0)
            total_bb   += _safe_f(st.get("baseOnBalls"),  0)
            total_h    += _safe_f(st.get("hits"),         0)

        if total_ip < 1:
            _pitcher_recent_cache[cache_key] = {}
            return {}

        era_recent  = round((total_er  / total_ip) * 9, 2)
        k9_recent   = round((total_k   / total_ip) * 9, 2)
        bb9_recent  = round((total_bb  / total_ip) * 9, 2)
        whip_recent = round((total_h + total_bb) / total_ip, 3)

        out = {
            "era_recent":   _clamp(era_recent,  1.5, 12.0),
            "k9_recent":    _clamp(k9_recent,   2.0, 16.0),
            "bb9_recent":   _clamp(bb9_recent,  0.5,  9.0),
            "whip_recent":  _clamp(whip_recent, 0.6,  2.4),
            "n_starts":     len(starts),
            "total_ip":     round(total_ip, 1),
            "total_er":     int(total_er),
            "total_k":      int(total_k),
        }
        _pitcher_recent_cache[cache_key] = out
        return out
    except Exception as ex:
        print(f"[pitcher_recent_form] pid={pitcher_id}: {ex}")
        _pitcher_recent_cache[cache_key] = {}
        return {}


# ── Pitcher projection engine (v2 — recent form weighted) ────────────────────
def _project_pitcher(pitcher_name, pitcher_id, pitcher_fg, pitcher_sv, pitcher_stats,
                     opp_batters, park_factor, weather):
    fg   = pitcher_fg
    sv   = pitcher_sv

    # ── Season stats ──────────────────────────────────────────────────────────
    k9_season   = _safe_f(fg.get("fg_k9"),   8.5)
    bb9_season  = _safe_f(fg.get("fg_bb9"),  3.0)
    era_season  = _safe_f(fg.get("fg_era") or pitcher_stats.get("era"), 4.20)
    whip_season = _safe_f(fg.get("fg_whip") or pitcher_stats.get("whip"), 1.28)
    kpct        = _safe_f(fg.get("fg_kpct") or sv.get("sv_k_pct"), 0.22)

    # ── Recent form (last 3-5 starts) — 40% weight ────────────────────────────
    recent = _pitcher_recent_form(pitcher_id, n_starts=5)
    if recent:
        W_RECENT = 0.40;  W_SEASON = 0.60
        era  = W_SEASON * era_season  + W_RECENT * recent["era_recent"]
        k9   = W_SEASON * k9_season   + W_RECENT * recent["k9_recent"]
        bb9  = W_SEASON * bb9_season  + W_RECENT * recent["bb9_recent"]
        whip = W_SEASON * whip_season + W_RECENT * recent["whip_recent"]
    else:
        era  = era_season
        k9   = k9_season
        bb9  = bb9_season
        whip = whip_season

    # ── Form trend flag (for UI) ──────────────────────────────────────────────
    # "struggling" = recent ERA 1.5+ runs worse than season
    # "dealing"    = recent ERA 1.5+ runs better than season
    form_flag = "neutral"
    if recent:
        delta = recent["era_recent"] - era_season
        if delta >= 1.5:
            form_flag = "struggling"
        elif delta <= -1.5:
            form_flag = "dealing"

    # ── Expected IP (quality-start model) ─────────────────────────────────────
    base_ip = 5.3
    ip_adj  = 1.0 + (4.20 - era) * 0.10
    exp_ip  = round(min(8.0, max(3.5, base_ip + ip_adj)), 1)

    # ── Opponent quality adjustment ───────────────────────────────────────────
    opp_wobas, opp_kpcts = [], []
    for b in opp_batters[:9]:
        b_fg = fg_batter(b.get("name", ""))
        b_sv = sv_batter(b.get("name", ""))
        opp_wobas.append(_safe_f(b_fg.get("fg_woba") or b_sv.get("sv_xwoba"), 0.310))
        opp_kpcts.append(_safe_f(b_fg.get("fg_kpct") or b_sv.get("sv_k_pct"), 0.22))

    avg_opp_woba = sum(opp_wobas) / len(opp_wobas) if opp_wobas else 0.310
    avg_opp_kpct = sum(opp_kpcts) / len(opp_kpcts) if opp_kpcts else 0.22
    opp_quality  = _clamp(avg_opp_woba / 0.320, 0.85, 1.15)
    k_opp_adj    = 1.0 + (avg_opp_kpct - 0.22) * 0.4

    k_proj  = round(max(0.5, (k9 / 9) * exp_ip * k_opp_adj / opp_quality), 2)
    bb_proj = round(max(0.1, (bb9 / 9) * exp_ip * opp_quality), 2)

    if not weather.get("dome", False):
        temp_f = _safe_f(weather.get("temp"), 72)
        if temp_f > 88:
            k_proj = round(k_proj * 0.97, 2)

    return {
        "k":          k_proj,
        "bb":         bb_proj,
        "expected_ip": exp_ip,
        # Form metadata exposed to frontend
        "era_season":  round(era_season, 2),
        "era_recent":  round(recent.get("era_recent", era_season), 2) if recent else None,
        "era_blended": round(era, 2),
        "k9_recent":   round(recent.get("k9_recent",  k9_season),  2) if recent else None,
        "form_flag":   form_flag,
        "recent_starts": recent.get("n_starts", 0),
        "recent_ip":   recent.get("total_ip"),
        "recent_er":   recent.get("total_er"),
        "recent_k":    recent.get("total_k"),
    }


def _safe_f(val, default=0.0):
    try:
        v = float(val)
        return v if v == v else default   # NaN guard
    except (TypeError, ValueError):
        return default


# ── Matchup scoring engine (v2 — platoon-aware) ───────────────────────────────
def _matchup_score(batter, pitcher_fg, pitcher_sv, pitcher_hand='R'):
    """
    Returns a 0–100 matchup score broken into 4 sub-scores.
    Uses platoon-blended contact/power metrics when split data is available.
    """
    name = batter.get("name", "")
    fg   = fg_batter(name)
    sv   = sv_batter(name)

    # ── Contact (25 pts): platoon-blended avg + xBA ───────────────────────────
    avg  = _platoon_blend(batter, pitcher_hand, 'avg')
    xba  = _safe_f(sv.get("sv_xba"), avg)
    con  = round(min(25, max(0, ((avg + xba) / 2 - 0.180) / (0.340 - 0.180) * 25)), 1)

    # ── Power (25 pts): platoon-blended slg + xSLG + iso + barrel% ───────────
    slg  = _platoon_blend(batter, pitcher_hand, 'slg')
    xslg = _safe_f(sv.get("sv_xslg"), slg)
    iso  = _safe_f(fg.get("fg_iso"), 0.145)
    brl  = _safe_f(sv.get("sv_brl_pct"), 6.0) / 100
    pwr  = round(min(25, max(0,
        ((slg + xslg) / 2 - 0.290) / (0.600 - 0.290) * 22 + brl * 15 + iso * 10
    )), 1)

    # ── OBP (25 pts): platoon-blended obp + BB% ───────────────────────────────
    obp   = _platoon_blend(batter, pitcher_hand, 'obp')
    bbpct = _safe_f(fg.get("fg_bbpct"), 0.08)
    obp_s = round(min(25, max(0, (obp - 0.270) / (0.420 - 0.270) * 22 + bbpct * 10)), 1)

    # ── Statcast (25 pts): EV / HH% / xwOBA — pitcher penalty ────────────────
    ev   = _safe_f(sv.get("sv_ev"), 87.0)
    hh   = _safe_f(sv.get("sv_hh_pct") or sv.get("sv_hhpct"), 33.0) / 100
    xwob = _safe_f(sv.get("sv_xwoba") or fg.get("fg_woba"), 0.310)
    opp_kpct = _safe_f(pitcher_fg.get("fg_kpct"), 0.22)
    opp_xera = _safe_f(pitcher_sv.get("sv_xera"), 4.20)
    pit_pen  = max(0.0, (0.22 - opp_kpct) * 10 + (opp_xera - 4.20) * 1.5)
    stc  = round(min(25, max(0,
        (ev - 82) / (98 - 82) * 8 +
        hh * 12 +
        (xwob - 0.270) / (0.420 - 0.270) * 8 +
        pit_pen
    )), 1)

    # ── Platoon bonus / penalty (up to ±4 pts) ────────────────────────────────
    bat  = (batter.get('bats') or 'S').upper()
    pit  = (pitcher_hand or 'R').upper()
    if bat != 'S':
        platoon_bonus = 3.0 if ((bat == 'L' and pit == 'R') or (bat == 'R' and pit == 'L')) else -3.0
    else:
        platoon_bonus = 1.5  # switch hitter slight edge

    total = round(min(100, max(0, con + pwr + obp_s + stc + platoon_bonus)), 1)
    tier  = "A" if total >= 70 else "B" if total >= 55 else "C" if total >= 40 else "D"

    return {
        "score":        total,
        "tier":         tier,
        "contact":      con,
        "power":        round(pwr, 1),
        "obp":          obp_s,
        "statcast":     stc,
        "platoon_bonus": platoon_bonus,
        "platoon_note": _batter_hand_note(batter, pitcher_hand),
    }


# ── Route: Prop projections ───────────────────────────────────────────────────
@app.route('/api/props/projections/<int:game_pk>')
def api_props_projections(game_pk):
    try:
        _maybe_refresh_fg()
        _maybe_refresh_savant()

        gdata, away_bats, home_bats, away_t, home_t, pitchers = _props_fetch_game(game_pk)
        if not gdata:
            return jsonify({"success": False, "error": "Game not found"}), 404

        away_abbr = away_t.get("team", {}).get("abbreviation", "AWAY")
        home_abbr = home_t.get("team", {}).get("abbreviation", "HOME")
        home_id   = home_t.get("team", {}).get("id")
        pf        = PARK_FACTORS.get(home_id, 1.0)

        ap_info = pitchers["ap"]; hp_info = pitchers["hp"]
        ap_name = ap_info.get("fullName", "TBD")
        hp_name = hp_info.get("fullName", "TBD")
        ap_id   = ap_info.get("id");  hp_id  = hp_info.get("id")

        ap_fg = fg_pitcher(ap_name); hp_fg = fg_pitcher(hp_name)
        ap_sv = sv_pitcher(ap_name); hp_sv = sv_pitcher(hp_name)
        ap_st = pitcher_stats_mlb(ap_id) if ap_id else {}
        hp_st = pitcher_stats_mlb(hp_id) if hp_id else {}

        # ── Pitcher hands — the key new inputs ────────────────────────────────
        ap_hand = (ap_st.get("pitchHand") or "R").upper()
        hp_hand = (hp_st.get("pitchHand") or "R").upper()

        # Weather
        ven   = gdata.get("venue", {})
        vid   = ven.get("id")
        vloc  = (ven.get("location") or {})
        coord = vloc.get("defaultCoordinates") or {}
        lat   = coord.get("latitude")
        lon   = coord.get("longitude")
        try:
            dt_utc = datetime.fromisoformat(gdata.get("gameDate", "").replace("Z", "+00:00"))
            ghour  = dt_utc.astimezone(ET).hour
        except Exception:
            ghour  = 13
        wx = get_weather(lat, lon, ghour, venue_id=vid)

        # ── Build batter projections (now passes pitcher_hand) ─────────────────
        def enrich_batters(batters, opp_pfg, opp_psv, opp_pst, opp_abbr, opp_pname):
            opp_hand = (opp_pst.get("pitchHand") or "R").upper()
            result   = []
            for b in batters[:9]:
                name = b.get("name", "")
                bfg  = fg_batter(name)
                bsv  = sv_batter(name)
                proj = _project_batter(
                    b, opp_pname, opp_pfg, opp_psv, pf, wx,
                    pitcher_hand=opp_hand   # ← platoon key
                )
                result.append({
                    "name":         name,
                    "team":         b.get("team", ""),
                    "pos":          b.get("pos", ""),
                    "slot":         b.get("slot", 0),
                    "id":           b.get("id"),
                    "bats":         b.get("bats", ""),
                    "opp_pitcher":  opp_pname,
                    "opp_hand":     opp_hand,
                    "opp_era":      opp_pfg.get("fg_era") or opp_psv.get("sv_era_p"),
                    "opp_k9":       opp_pfg.get("fg_k9"),
                    "avg":          b.get("avg") or bfg.get("fg_avg"),
                    "obp":          b.get("obp") or bfg.get("fg_obp"),
                    "slg":          b.get("slg") or bfg.get("fg_slg"),
                    "fg_woba":      bfg.get("fg_woba"),
                    "sv_xwoba":     bsv.get("sv_xwoba"),
                    "sv_ev":        bsv.get("sv_ev"),
                    # Expose platoon splits for UI
                    "vs_l_avg":     b.get("vs_l_avg"),
                    "vs_r_avg":     b.get("vs_r_avg"),
                    "vs_l_ops":     b.get("vs_l_ops"),
                    "vs_r_ops":     b.get("vs_r_ops"),
                    "proj":         proj,
                })
            return result

        away_proj = enrich_batters(away_bats, hp_fg, hp_sv, hp_st, home_abbr, hp_name)
        home_proj = enrich_batters(home_bats, ap_fg, ap_sv, ap_st, away_abbr, ap_name)
        all_batters = away_proj + home_proj

        # ── Pitcher projections ────────────────────────────────────────────────
        pitchers_out = []
        for pid, pname, pfg, psv, pst, opp_bats, pabbr, phand in [
            (ap_id, ap_name, ap_fg, ap_sv, ap_st, home_bats, away_abbr, ap_hand),
            (hp_id, hp_name, hp_fg, hp_sv, hp_st, away_bats, home_abbr, hp_hand),
        ]:
            if pname == "TBD":
                continue
            proj = _project_pitcher(pname, pid, pfg, psv, pst, opp_bats, pf, wx)
            pitchers_out.append({
                "name":      pname,
                "team":      pabbr,
                "id":        pid,
                "role":      "SP",
                "pitchHand": phand,
                "era":       pfg.get("fg_era") or psv.get("sv_era_p"),
                "whip":      pfg.get("fg_whip"),
                "fip":       pfg.get("fg_fip"),
                "xera":      psv.get("sv_xera"),
                "kpct":      pfg.get("fg_kpct") or psv.get("sv_k_pct"),
                "bbpct":     pfg.get("fg_bbpct") or psv.get("sv_bb_pct"),
                "opp_k_pct": sum(_safe_f(fg_batter(b.get("name","")).get("fg_kpct"), 0.22)
                                 for b in opp_bats[:9]) / max(len(opp_bats[:9]), 1),
                "opp_woba":  sum(_safe_f(fg_batter(b.get("name","")).get("fg_woba") or
                                         sv_batter(b.get("name","")).get("sv_xwoba"), 0.310)
                                 for b in opp_bats[:9]) / max(len(opp_bats[:9]), 1),
                "proj":      proj,
            })

        return jsonify({
            "success":     True,
            "gamePk":      game_pk,
            "matchup":     f"{away_abbr} @ {home_abbr}",
            "batters":     all_batters,
            "pitchers":    pitchers_out,
            "weather":     wx,
            "park_factor": pf,
            "timestamp":   datetime.now(timezone.utc).isoformat(),
        })

    except Exception as ex:
        print(f"[api_props_projections] {traceback.format_exc()}")
        return jsonify({"success": False, "error": str(ex)}), 500


# ── Route: Matchup scores (v2 — platoon-aware) ────────────────────────────────
@app.route('/api/props/matchup-scores/<int:game_pk>')
def api_props_matchup_scores(game_pk):
    try:
        _maybe_refresh_fg()
        _maybe_refresh_savant()

        gdata, away_bats, home_bats, away_t, home_t, pitchers = _props_fetch_game(game_pk)
        if not gdata:
            return jsonify({"success": False, "error": "Game not found"}), 404

        away_abbr = away_t.get("team", {}).get("abbreviation", "AWAY")
        home_abbr = home_t.get("team", {}).get("abbreviation", "HOME")

        ap_info = pitchers["ap"]; hp_info = pitchers["hp"]
        ap_name = ap_info.get("fullName", "TBD")
        hp_name = hp_info.get("fullName", "TBD")
        ap_id   = ap_info.get("id");  hp_id  = hp_info.get("id")

        ap_fg = fg_pitcher(ap_name); ap_sv = sv_pitcher(ap_name)
        hp_fg = fg_pitcher(hp_name); hp_sv = sv_pitcher(hp_name)
        ap_st = pitcher_stats_mlb(ap_id) if ap_id else {}
        hp_st = pitcher_stats_mlb(hp_id) if hp_id else {}

        ap_hand = (ap_st.get("pitchHand") or "R").upper()
        hp_hand = (hp_st.get("pitchHand") or "R").upper()

        def score_lineup(batters, opp_pfg, opp_psv, opp_hand):
            out = []
            for b in batters[:9]:
                sc = _matchup_score(b, opp_pfg, opp_psv, pitcher_hand=opp_hand)
                out.append({
                    "name":  b.get("name", ""),
                    "pos":   b.get("pos", ""),
                    "slot":  b.get("slot", 0),
                    "bats":  b.get("bats", ""),
                    "score": sc,
                })
            return sorted(out, key=lambda x: x["slot"])

        # home batters face away pitcher (ap_hand), away batters face home pitcher (hp_hand)
        away_scores = score_lineup(away_bats, hp_fg, hp_sv, hp_hand)
        home_scores = score_lineup(home_bats, ap_fg, ap_sv, ap_hand)

        return jsonify({
            "success": True,
            "gamePk":  game_pk,
            "away": {
                "abbr":         away_abbr,
                "pitcher_name": hp_name,
                "pitcher_hand": hp_hand,
                "pitcher_era":  hp_fg.get("fg_era") or hp_sv.get("sv_era_p"),
                "batters":      home_scores,
            },
            "home": {
                "abbr":         home_abbr,
                "pitcher_name": ap_name,
                "pitcher_hand": ap_hand,
                "pitcher_era":  ap_fg.get("fg_era") or ap_sv.get("sv_era_p"),
                "batters":      away_scores,
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    except Exception as ex:
        print(f"[api_props_matchup_scores] {traceback.format_exc()}")
        return jsonify({"success": False, "error": str(ex)}), 500


# ── Route: Tracker entries for the value bets panel ───────────────────────────
@app.route('/api/tracker/entries')
def api_tracker_entries():
    try:
        date   = request.args.get("date", datetime.now(ET).strftime("%Y-%m-%d"))
        gamePk = request.args.get("gamePk")

        store = {}
        if os.path.exists(TRACKER_STORE):
            with open(TRACKER_STORE) as f:
                store = json.load(f)

        day     = store.get(date, {})
        entries = day.get("entries", [])

        if gamePk:
            try:
                pk_int  = int(gamePk)
                entries = [e for e in entries
                           if e.get("gamePk") == pk_int or str(e.get("gamePk")) == str(pk_int)]
            except (ValueError, TypeError):
                pass

        return jsonify({"success": True, "date": date, "entries": entries, "total": len(entries)})

    except Exception as ex:
        print(f"[api_tracker_entries] {traceback.format_exc()}")


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
def _fetch_batter_gamelog(player_id, season=None):
    """Returns last 10 game log entries for a batter."""
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
        for s in splits[-10:]:
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
            })
        return out
    except Exception as ex:
        print(f"[batter_gamelog] pid={player_id} {ex}")
        return []


def _fetch_pitcher_gamelog(player_id, season=None):
    """Returns last 10 game log entries for a pitcher."""
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
        for s in splits[-10:]:
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
@app.route("/api/props/trends/<int:game_pk>")
def api_props_trends(game_pk):
    """
    Returns L5/L10 over rates for every batter and both starting pitchers in a game.
    Uses concurrent fetching to keep response time under ~4s.
    """
    try:
        # Get lineups + pitchers
        gdata, away_bats, home_bats, away_t, home_t, pitchers = _props_fetch_game(game_pk)
        if not gdata:
            return jsonify({"success": False, "error": "Game not found"}), 404

        all_batters  = away_bats + home_bats
        ap_info      = pitchers["ap"]
        hp_info      = pitchers["hp"]

        # Build task list: (player_id, is_pitcher, name)
        tasks = []
        for b in all_batters:
            pid = b.get("id")
            if pid:
                tasks.append((int(pid), False, b.get("name", "")))
        for pi in [ap_info, hp_info]:
            pid = pi.get("id")
            if pid:
                tasks.append((int(pid), True, pi.get("fullName", "")))

        results = {}
        with ThreadPoolExecutor(max_workers=10) as ex:
            fut_map = {
                ex.submit(_build_player_trends, pid, is_pit): (pid, name)
                for pid, is_pit, name in tasks
            }
            for fut in as_completed(fut_map):
                pid, name = fut_map[fut]
                try:
                    data = fut.result()
                    results[str(pid)] = {"name": name, **data}
                except Exception as fe:
                    results[str(pid)] = {"name": name, "error": str(fe)}

        return jsonify({
            "success":  True,
            "gamePk":   game_pk,
            "players":  results,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    except Exception as ex:
        print(f"[api_props_trends] {traceback.format_exc()}")
        return jsonify({"success": False, "error": str(ex)}), 500


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


# Boot background loaders
threading.Thread(target=_load_fg_data,      daemon=True).start()
threading.Thread(target=_load_savant_data,  daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
