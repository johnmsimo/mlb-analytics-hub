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
           "&hydrate=team,probablePitcher,linescore,venue(location),weather")
    r = requests.get(url, timeout=10); r.raise_for_status()
    dates = r.json().get("dates", [])
    return dates[0].get("games", []) if dates else []

def _deg_to_compass(d):
    """Convert wind degrees to compass label (N/NE/E/SE/S/SW/W/NW)."""
    if d is None:
        return "N/A"
    dirs = ["N","NE","NE","E","E","SE","SE","S","S","SW","SW","W","W","NW","NW","N"]
    return dirs[int((float(d) + 11.25) / 22.5) % 16]


def get_weather(lat, lon, game_hour=13, venue_id=None):
    # Dome/retractable roof — return indoor stub immediately
    if venue_id and venue_id in DOME_VENUES:
        return {"temp": "DOME", "rain_chance": 0, "wind_speed": 0,
                "wind_dir": "N/A", "condition": "Dome", "dome": True}
    # Fallback: fill coords from hardcoded stadium map if MLB API didn't return them
    if (lat is None or lon is None) and venue_id and venue_id in STADIUM_COORDS:
        lat, lon = STADIUM_COORDS[venue_id]
    if lat is None or lon is None:
        return {"temp": "N/A", "rain_chance": "N/A", "wind_speed": "N/A",
                "wind_dir": "N/A", "condition": "N/A"}
    try:
        # Pin timezone to America/New_York so game_hour_et always aligns
        # with the hourly index regardless of venue location (fixes west coast 3hr offset)
        r = requests.get(WX_API, params={
            "latitude":            lat,
            "longitude":           lon,
            "hourly":              "temperature_2m,precipitation_probability,"
                                   "windspeed_10m,winddirection_10m,weathercode",
            "temperature_unit":    "fahrenheit",
            "windspeed_unit":      "mph",
            "forecast_days":       2,
            "timezone":            "America/New_York",
        }, timeout=8)
        r.raise_for_status()
        h = r.json().get("hourly", {})
        # game_hour is 0-23 in ET; open-meteo returns 48 hourly slots for today+tomorrow
        idx = max(0, min(len(h.get("temperature_2m", [])) - 1, int(game_hour)))
        wcode_map = {
            0: "Clear",       1: "Mainly Clear",  2: "Partly Cloudy", 3: "Overcast",
            45: "Foggy",      48: "Foggy",
            51: "Drizzle",    53: "Drizzle",       55: "Drizzle",
            61: "Rain",       63: "Rain",          65: "Heavy Rain",
            71: "Snow",       73: "Snow",          75: "Snow",
            80: "Showers",    81: "Showers",       82: "Heavy Showers",
            95: "Thunderstorm", 96: "Thunderstorm", 99: "Thunderstorm",
        }
        temps     = h.get("temperature_2m",            [70] * 48)
        precip    = h.get("precipitation_probability", [0]  * 48)
        wind_spd  = h.get("windspeed_10m",             [0]  * 48)
        wind_dirs = h.get("winddirection_10m",         [])
        wcodes    = h.get("weathercode",               [0]  * 48)
        wcode     = wcodes[idx]    if idx < len(wcodes)    else 0
        w_dir_deg = wind_dirs[idx] if idx < len(wind_dirs) else None
        return {
            "temp":        round(temps[idx])    if idx < len(temps)    else "N/A",
            "rain_chance": precip[idx]          if idx < len(precip)   else "N/A",
            "wind_speed":  round(wind_spd[idx]) if idx < len(wind_spd) else "N/A",
            "wind_dir":    _deg_to_compass(w_dir_deg),
            "condition":   wcode_map.get(wcode, "Clear"),
        }
    except Exception as ex:
        print(f"[get_weather] lat={lat} lon={lon} hour={game_hour} venue={venue_id} err={ex}")
        return {"temp": "N/A", "rain_chance": "N/A", "wind_speed": "N/A",
                "wind_dir": "N/A", "condition": "N/A"}


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
    batters = team_data.get("batters", [])
    players = team_data.get("players", {})
    seen = set()
    for pid in batters:
        key = f"ID{pid}"
        p = players.get(key, {})
        name = p.get("person", {}).get("fullName", "")
        if not name or pid in seen:
            continue
        seen.add(pid)
        pos = p.get("position", {}).get("abbreviation", "?")
        ss = p.get("seasonStats", {}).get("batting", {})
        slot = p.get("battingOrder", 0)
        try:
            slot = int(str(slot)[0])
        except:
            slot = 0
        if slot < 1 or slot > 9:
            continue
        fgb = fg_batter(name)
        svb = sv_batter(name)
        out.append({
            "slot": slot,
            "id": pid,
            "name": name,
            "pos": pos or "?",
            "avg": ss.get("avg", fgb.get("fg_avg", ".---")),
            "obp": ss.get("obp", fgb.get("fg_obp", ".---")),
            "slg": ss.get("slg", fgb.get("fg_slg", ".---")),
            "ops": ss.get("ops", fgb.get("fg_ops", ".---")),
            "ab": int(ss.get("atBats", 0) or 0),
            "hr": int(ss.get("homeRuns", 0) or 0),
            "rbi": int(ss.get("rbi", 0) or 0),
            "fg_avg": fgb.get("fg_avg", "N/A"),
            "fg_obp": fgb.get("fg_obp", "N/A"),
            "fg_slg": fgb.get("fg_slg", "N/A"),
            "fg_ops": fgb.get("fg_ops", "N/A"),
            "fg_woba": fgb.get("fg_woba", "N/A"),
            "fg_wrc": fgb.get("fg_wrc", "N/A"),
            "fg_pa": fgb.get("fg_pa", "N/A"),
            "fg_r": fgb.get("fg_r", "N/A"),
            "fg_sb": fgb.get("fg_sb", "N/A"),
            "fg_war": fgb.get("fg_war", "N/A"),
            "sv_xba": svb.get("sv_xba", "N/A"),
            "sv_xslg": svb.get("sv_xslg", "N/A"),
            "sv_xwoba": svb.get("sv_xwoba", "N/A"),
            "sv_ev": svb.get("sv_ev", "N/A"),
            "sv_hh_pct": svb.get("sv_hh_pct", "N/A"),
            "sv_brl_pct": svb.get("sv_brl_pct", "N/A"),
            "bats": (p.get("person", {}).get("batSide", {}) or {}).get("code", "S"),
        })
    out.sort(key=lambda x: (x.get("slot", 99), x.get("name", "")))
    return out


