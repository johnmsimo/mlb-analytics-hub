import os, requests, traceback
from flask import Flask, jsonify
from flask_cors import CORS
from datetime import datetime, timezone, timedelta

app = Flask(__name__)
CORS(app)

MLB_API = "https://statsapi.mlb.com/api/v1"

TEAM_LOGOS = {
    "ARI":"https://www.mlbstatic.com/team-logos/109.svg","ATL":"https://www.mlbstatic.com/team-logos/144.svg",
    "BAL":"https://www.mlbstatic.com/team-logos/110.svg","BOS":"https://www.mlbstatic.com/team-logos/111.svg",
    "CHC":"https://www.mlbstatic.com/team-logos/112.svg","CWS":"https://www.mlbstatic.com/team-logos/145.svg",
    "CIN":"https://www.mlbstatic.com/team-logos/113.svg","CLE":"https://www.mlbstatic.com/team-logos/114.svg",
    "COL":"https://www.mlbstatic.com/team-logos/115.svg","DET":"https://www.mlbstatic.com/team-logos/116.svg",
    "HOU":"https://www.mlbstatic.com/team-logos/117.svg","KC":"https://www.mlbstatic.com/team-logos/118.svg",
    "LAA":"https://www.mlbstatic.com/team-logos/108.svg","LAD":"https://www.mlbstatic.com/team-logos/119.svg",
    "MIA":"https://www.mlbstatic.com/team-logos/146.svg","MIL":"https://www.mlbstatic.com/team-logos/158.svg",
    "MIN":"https://www.mlbstatic.com/team-logos/142.svg","NYM":"https://www.mlbstatic.com/team-logos/121.svg",
    "NYY":"https://www.mlbstatic.com/team-logos/147.svg","OAK":"https://www.mlbstatic.com/team-logos/133.svg",
    "PHI":"https://www.mlbstatic.com/team-logos/143.svg","PIT":"https://www.mlbstatic.com/team-logos/134.svg",
    "SD":"https://www.mlbstatic.com/team-logos/135.svg","SEA":"https://www.mlbstatic.com/team-logos/136.svg",
    "SF":"https://www.mlbstatic.com/team-logos/137.svg","STL":"https://www.mlbstatic.com/team-logos/138.svg",
    "TB":"https://www.mlbstatic.com/team-logos/139.svg","TEX":"https://www.mlbstatic.com/team-logos/140.svg",
    "TOR":"https://www.mlbstatic.com/team-logos/141.svg","WSH":"https://www.mlbstatic.com/team-logos/120.svg",
}

PARK_FACTORS = {
    "COL":{"run":1.18,"hr":1.22},"BOS":{"run":1.08,"hr":1.09},"CIN":{"run":1.07,"hr":1.11},
    "PHI":{"run":1.06,"hr":1.07},"TEX":{"run":1.05,"hr":1.06},"NYY":{"run":1.04,"hr":1.10},
    "CHC":{"run":1.03,"hr":1.05},"MIL":{"run":1.02,"hr":1.04},"LAD":{"run":0.97,"hr":0.96},
    "OAK":{"run":0.96,"hr":0.93},"SF":{"run":0.94,"hr":0.89},"SD":{"run":0.93,"hr":0.91},
    "MIA":{"run":0.92,"hr":0.88},"SEA":{"run":0.95,"hr":0.92},
}

STADIUM_COORDS = {
    "ARI": (33.4455, -112.0667), "ATL": (33.8907, -84.4677), "BAL": (39.2839, -76.6217),
    "BOS": (42.3467, -71.0972), "CHC": (41.9484, -87.6553), "CWS": (41.8300, -87.6338),
    "CIN": (39.0979, -84.5081), "CLE": (41.4962, -81.6852), "COL": (39.7559, -104.9942),
    "DET": (42.3390, -83.0485), "HOU": (29.7573, -95.3555), "KC": (39.0517, -94.4803),
    "LAA": (33.8003, -117.8827), "LAD": (34.0739, -118.2400), "MIA": (25.7781, -80.2197),
    "MIL": (43.0280, -87.9712), "MIN": (44.9817, -93.2776), "NYM": (40.7571, -73.8458),
    "NYY": (40.8296, -73.9262), "PHI": (39.9061, -75.1665), "PIT": (40.4469, -80.0057),
    "SD": (32.7073, -117.1573), "SEA": (47.5914, -122.3325), "SF": (37.7786, -122.3893),
    "STL": (38.6226, -90.1928), "TB": (27.7683, -82.6534), "TEX": (32.7513, -97.0825),
    "TOR": (43.6414, -79.3894), "WSH": (38.8730, -77.0074), "AZ": (33.4455, -112.0667),
}

WX_CODES = {
    0: "Clear", 1: "Mostly Clear", 2: "Partly Cloudy", 3: "Cloudy", 45: "Fog", 48: "Fog",
    51: "Light Drizzle", 53: "Drizzle", 55: "Heavy Drizzle", 61: "Light Rain", 63: "Rain",
    65: "Heavy Rain", 71: "Light Snow", 73: "Snow", 75: "Heavy Snow", 80: "Rain Showers",
    81: "Rain Showers", 82: "Heavy Showers", 95: "Thunderstorm"
}

def deg_to_cardinal(deg):
    dirs = ["N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSW","SW","WSW","W","WNW","NW","NNW"]
    return dirs[round(float(deg) / 22.5) % 16]

def get_hourly_weather(home_abbr, game_time_utc):
    coords = STADIUM_COORDS.get(home_abbr)
    if not coords:
        return {}
    try:
        lat, lon = coords
        game_dt = datetime.fromisoformat(game_time_utc.replace("Z", "+00:00")).replace(minute=0, second=0, microsecond=0)
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "hourly": "temperature_2m,precipitation_probability,weather_code,wind_speed_10m,wind_direction_10m",
                "wind_speed_unit": "mph",
                "temperature_unit": "fahrenheit",
                "precipitation_unit": "inch",
                "timezone": "UTC",
                "forecast_days": 2,
            },
            timeout=8,
        )
        r.raise_for_status()
        j = r.json().get("hourly", {})
        times = [datetime.fromisoformat(t) for t in j.get("time", [])]
        if not times:
            return {}
        idx = min(range(len(times)), key=lambda i: abs(times[i] - game_dt))
        temp = round(j.get("temperature_2m", [70])[idx])
        rain = j.get("precipitation_probability", [0])[idx]
        wind_speed = round(j.get("wind_speed_10m", [0])[idx])
        wind_dir = deg_to_cardinal(j.get("wind_direction_10m", [0])[idx])
        code = j.get("weather_code", [0])[idx]
        return {
            "temp": str(temp),
            "wind": f"{wind_speed} mph {wind_dir}",
            "condition": WX_CODES.get(code, "Unknown"),
            "rainChance": rain,
        }
    except Exception as e:
        print(f"[get_hourly_weather] {e}")
        return {}

def utc_to_et(utc_str):
    """Convert MLB UTC game time to Eastern Time string."""
    try:
        dt_utc = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
        # EST = UTC-5, EDT = UTC-4 (approx Apr-Oct)
        dt_et  = dt_utc - timedelta(hours=4)
        hour   = dt_et.hour
        minute = dt_et.minute
        ampm   = "PM" if hour >= 12 else "AM"
        hour12 = hour % 12 or 12
        return f"{hour12}:{minute:02d} {ampm} ET"
    except Exception:
        return "TBD"

def fetch_schedule(date_str):
    try:
        r = requests.get(f"{MLB_API}/schedule", params={
            "sportId": 1, "date": date_str,
            "hydrate": "team,probablePitcher,weather,venue,linescore"
        }, timeout=10)
        r.raise_for_status()
        games = []
        for d in r.json().get("dates", []):
            games.extend(d.get("games", []))
        return games
    except Exception as e:
        print(f"[schedule] {e}")
        return []

def parse_game(g):
    try:
        pk        = g.get("gamePk", 0)
        status    = g.get("status", {}).get("abstractGameState", "Preview")
        game_time = g.get("gameDate", "")
        away      = g.get("teams", {}).get("away", {}).get("team", {})
        home      = g.get("teams", {}).get("home", {}).get("team", {})
        away_abbr = away.get("abbreviation", "???")
        home_abbr = home.get("abbreviation", "???")
        away_p    = g.get("teams", {}).get("away", {}).get("probablePitcher", {})
        home_p    = g.get("teams", {}).get("home", {}).get("probablePitcher", {})
        weather   = g.get("weather", {})
        pf        = PARK_FACTORS.get(home_abbr, {}).get("run", 1.0)
        wx        = get_hourly_weather(home_abbr, game_time) or {
            "temp": weather.get("temp", "N/A"),
            "wind": weather.get("wind", "N/A"),
            "condition": weather.get("condition", "N/A"),
            "rainChance": "N/A",
        }

        try:
            temp_val  = int(str(wx.get("temp", "70")).strip())
            heat_boost= max(0, (temp_val - 70) * 0.2)
        except Exception:
            temp_val  = 70
            heat_boost= 0

        edge    = round(min(15, max(1, (pf - 1) * 40 + heat_boost + 5)), 1)
        bar_pct = int(min(95, max(8, edge * 6.5)))

        return {
            "gamePk":      pk,
            "status":      status,
            "gameTime":    utc_to_et(game_time),
            "awayAbbr":    away_abbr,
            "homeAbbr":    home_abbr,
            "awayName":    away.get("name", "Away"),
            "homeName":    home.get("name", "Home"),
            "awayLogo":    TEAM_LOGOS.get(away_abbr, ""),
            "homeLogo":    TEAM_LOGOS.get(home_abbr, ""),
            "awayPitcher": away_p.get("fullName", "TBD"),
            "homePitcher": home_p.get("fullName", "TBD"),
            "venue":       g.get("venue", {}).get("name", ""),
            "temp":        wx.get("temp", "N/A"),
            "wind":        wx.get("wind", "N/A"),
            "condition":   wx.get("condition", "N/A"),
            "rainChance":  wx.get("rainChance", "N/A"),
            "parkFactor":  pf,
            "edge":        edge,
            "barPct":      bar_pct,
        }
    except Exception as e:
        print(f"[parse_game] {e}\n{traceback.format_exc()}")
        return None

def pitcher_stats(pid):
    try:
        r = requests.get(f"{MLB_API}/people/{pid}/stats", params={
            "stats": "season", "group": "pitching",
            "season": datetime.now().year
        }, timeout=8)
        r.raise_for_status()
        splits = r.json().get("stats", [{}])[0].get("splits", [])
        if splits:
            s = splits[0].get("stat", {})
            return {"era": s.get("era","N/A"), "whip": s.get("whip","N/A"),
                    "k9": s.get("strikeoutsPer9Inn","N/A"),
                    "ip": s.get("inningsPitched","N/A"),
                    "wins": s.get("wins",0), "losses": s.get("losses",0)}
    except Exception as e:
        print(f"[pitcher_stats] {e}")
    return {"era":"N/A","whip":"N/A","k9":"N/A","ip":"N/A","wins":0,"losses":0}

# ─────────────────────────────────────────
# MAIN DASHBOARD HTML
# ─────────────────────────────────────────
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MLB Analytics Hub</title>
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;700;900&family=Inter:wght@300;400;600&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--c:#00e5ff;--m:#e040fb;--g:#00e676;--bg:#050a18;--card:#0a1628;--b:rgba(0,229,255,.18);--t:#e0f0ff;--mu:#6a8db0}
body{font-family:'Inter',sans-serif;background:var(--bg);color:var(--t);min-height:100vh}
body::before{content:'';position:fixed;inset:0;background:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,229,255,.012) 2px,rgba(0,229,255,.012) 4px);pointer-events:none;z-index:9999}
nav{display:flex;align-items:center;justify-content:space-between;padding:14px 32px;background:rgba(5,10,24,.95);backdrop-filter:blur(16px);border-bottom:1px solid var(--b);position:sticky;top:0;z-index:100;box-shadow:0 0 30px rgba(0,229,255,.1)}
.logo{font-family:'Orbitron',monospace;font-size:1.4rem;font-weight:900;color:var(--c);text-shadow:0 0 20px rgba(0,229,255,.6);letter-spacing:3px}
.nav-r{display:flex;align-items:center;gap:18px}
.live{display:flex;align-items:center;gap:6px;font-size:.7rem;letter-spacing:2px;color:var(--g);font-family:'Orbitron',monospace}
.dot{width:8px;height:8px;border-radius:50%;background:var(--g);box-shadow:0 0 8px var(--g);animation:blink 1.4s ease-in-out infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}
#clk{font-family:'Orbitron',monospace;font-size:.95rem;color:var(--c);min-width:100px;text-align:right}
.strip{display:flex;align-items:center;justify-content:space-between;padding:18px 32px;background:linear-gradient(90deg,rgba(0,229,255,.05),rgba(224,64,251,.04));border-bottom:1px solid var(--b)}
.strip-title{font-family:'Orbitron',monospace;font-size:1rem;color:var(--mu);letter-spacing:5px}
.strip-stats{display:flex;gap:32px}
.ss{text-align:center}
.ss-v{font-family:'Orbitron',monospace;font-size:1.6rem;font-weight:700;color:var(--c);text-shadow:0 0 12px rgba(0,229,255,.4)}
.ss-l{font-size:.65rem;color:var(--mu);letter-spacing:2px;margin-top:2px}
.main{padding:28px 32px;max-width:1600px;margin:0 auto}
.sh{font-family:'Orbitron',monospace;font-size:.78rem;color:var(--mu);letter-spacing:4px;margin-bottom:20px;display:flex;align-items:center;gap:12px}
.sh::after{content:'';flex:1;height:1px;background:linear-gradient(90deg,var(--b),transparent)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:20px}
.card{background:var(--card);border:1px solid var(--b);border-radius:16px;padding:22px;cursor:pointer;transition:all .3s cubic-bezier(.16,1,.3,1);position:relative;overflow:hidden}
.card::after{content:'';position:absolute;inset:0;background:linear-gradient(135deg,rgba(0,229,255,.03),rgba(224,64,251,.03));opacity:0;transition:opacity .3s}
.card:hover{transform:translateY(-6px);border-color:var(--c);box-shadow:0 0 35px rgba(0,229,255,.22),0 20px 40px rgba(0,0,0,.5)}
.card:hover::after{opacity:1}
.ct{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px}
.badge{font-size:.62rem;letter-spacing:2px;padding:3px 10px;border-radius:20px;font-family:'Orbitron',monospace;font-weight:700}
.bs{background:rgba(0,229,255,.08);color:var(--c);border:1px solid rgba(0,229,255,.25)}
.bl{background:rgba(0,230,118,.1);color:var(--g);border:1px solid rgba(0,230,118,.3);animation:pb 2s ease-in-out infinite}
.bf{background:rgba(106,141,176,.1);color:var(--mu);border:1px solid rgba(106,141,176,.2)}
@keyframes pb{0%,100%{box-shadow:0 0 0 0 rgba(0,230,118,.4)}50%{box-shadow:0 0 0 3px rgba(0,230,118,0)}}
.ctime{font-size:.72rem;color:var(--mu);font-family:'Orbitron',monospace}
.mu{display:flex;align-items:center;justify-content:center;gap:14px;margin-bottom:16px}
.tb{display:flex;flex-direction:column;align-items:center;gap:7px;flex:1}
.tlw{width:62px;height:62px;border-radius:50%;border:2px solid var(--b);background:rgba(0,0,0,.4);display:flex;align-items:center;justify-content:center;overflow:hidden;transition:all .3s}
.card:hover .tlw{border-color:var(--c);box-shadow:0 0 16px rgba(0,229,255,.3)}
.tlw img{width:42px;height:42px;object-fit:contain}
.ta{font-family:'Orbitron',monospace;font-size:.82rem;font-weight:700}
.vs{font-family:'Orbitron',monospace;font-size:.68rem;color:var(--mu);letter-spacing:2px}
.pr{display:flex;justify-content:space-between;margin-bottom:12px;padding:9px 12px;background:rgba(0,0,0,.3);border-radius:8px;border:1px solid rgba(255,255,255,.04)}
.pi{text-align:center;flex:1}
.pl{font-size:.58rem;color:var(--mu);letter-spacing:2px;margin-bottom:2px}
.pn{font-size:.7rem;color:var(--t);font-weight:600}
.wr{display:flex;align-items:center;gap:8px;margin-bottom:12px;padding:7px 10px;background:rgba(0,0,0,.2);border-radius:7px}
.wt{font-size:.7rem;color:var(--mu)}
.wt span{color:var(--t);font-weight:600}
.eb{margin-bottom:8px}
.ebl{display:flex;justify-content:space-between;margin-bottom:4px}
.ebl span{font-size:.62rem;color:var(--mu);letter-spacing:1px}
.ebl strong{font-size:.75rem;color:var(--c);font-family:'Orbitron',monospace}
.bar{height:7px;background:rgba(255,255,255,.06);border-radius:4px;overflow:hidden}
.fill{height:100%;border-radius:4px;background:linear-gradient(90deg,var(--c),var(--m));box-shadow:0 0 8px rgba(0,229,255,.3);transition:width 1.2s cubic-bezier(.16,1,.3,1)}
.vn{font-size:.62rem;color:var(--mu);text-align:center;margin-top:8px;letter-spacing:1px}
.ap{display:flex;align-items:center;justify-content:center;gap:6px;margin-top:12px;width:100%;padding:9px;border-radius:8px;background:linear-gradient(90deg,rgba(0,229,255,.1),rgba(224,64,251,.07));border:1px solid rgba(0,229,255,.2);font-size:.7rem;color:var(--c);letter-spacing:2px;font-family:'Orbitron',monospace;font-weight:700;transition:all .2s;cursor:pointer}
.card:hover .ap{background:linear-gradient(90deg,rgba(0,229,255,.2),rgba(224,64,251,.14));border-color:var(--c)}
.sw{display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:280px;gap:14px}
.sp{width:44px;height:44px;border:3px solid rgba(0,229,255,.15);border-top-color:var(--c);border-radius:50%;animation:spin 1s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.st{font-family:'Orbitron',monospace;font-size:.78rem;color:var(--mu);letter-spacing:3px}
.ng{text-align:center;padding:60px 20px}
.ng-i{font-size:3rem;margin-bottom:16px;opacity:.4}
footer{text-align:center;padding:20px;border-top:1px solid var(--b);font-size:.65rem;color:var(--mu);letter-spacing:2px;font-family:'Orbitron',monospace;margin-top:20px}
@media(max-width:768px){nav{padding:12px 16px}.logo{font-size:1.05rem}.strip{flex-direction:column;gap:12px;padding:14px 16px;align-items:flex-start}.strip-stats{gap:20px}.main{padding:16px}}
</style>
</head>
<body>
<nav>
  <div class="logo">&#9889; MLB ANALYTICS HUB</div>
  <div class="nav-r">
    <div id="dt" style="font-size:.72rem;color:var(--mu);font-family:'Orbitron',monospace"></div>
    <div id="clk"></div>
    <div class="live"><div class="dot"></div>LIVE</div>
  </div>
</nav>
<div class="strip">
  <div class="strip-title">COMMAND CENTER</div>
  <div class="strip-stats">
    <div class="ss"><div class="ss-v" id="sg">-</div><div class="ss-l">GAMES TODAY</div></div>
    <div class="ss"><div class="ss-v" id="sl">-</div><div class="ss-l">LIVE NOW</div></div>
    <div class="ss"><div class="ss-v" id="se">-</div><div class="ss-l">AVG EDGE</div></div>
    <div class="ss"><div class="ss-v" id="sh">-</div><div class="ss-l">HIGH CONF</div></div>
  </div>
</div>
<div class="main">
  <div class="sh">TODAY'S GAMES</div>
  <div id="gc"><div class="sw"><div class="sp"></div><div class="st">LOADING GAMES...</div></div></div>
</div>
<footer>MLB ANALYTICS HUB &nbsp;&#124;&nbsp; POWERED BY MLB STATS API &nbsp;&#124;&nbsp; FOR ENTERTAINMENT PURPOSES</footer>
<script>
function tick(){
  const n=new Date(),h=n.getHours(),m=n.getMinutes(),s=n.getSeconds();
  document.getElementById('clk').textContent=`${(h%12||12).toString().padStart(2,'0')}:${m.toString().padStart(2,'0')}:${s.toString().padStart(2,'0')}${h>=12?'PM':'AM'}`;
  document.getElementById('dt').textContent=n.toLocaleDateString('en-US',{weekday:'short',month:'short',day:'numeric'}).toUpperCase();
}
setInterval(tick,1000);tick();

function wicon(c){c=(c||'').toLowerCase();if(c.includes('rain'))return'&#127783;';if(c.includes('cloud'))return'&#9925;';if(c.includes('clear')||c.includes('sun'))return'&#9728;';return'&#127780;';}

function mkCard(g){
  const bc=g.status==='Live'?'bl':g.status==='Final'?'bf':'bs';
  const bl=g.status==='Live'?'&#9679; LIVE':g.status==='Final'?'FINAL':'SCHEDULED';
  const aL=g.awayLogo?`<img src="${g.awayLogo}" alt="${g.awayAbbr}" width="42" height="42" loading="lazy">`:`<span style="font-family:Orbitron;font-size:.9rem;color:var(--c)">${g.awayAbbr}</span>`;
  const hL=g.homeLogo?`<img src="${g.homeLogo}" alt="${g.homeAbbr}" width="42" height="42" loading="lazy">`:`<span style="font-family:Orbitron;font-size:.9rem;color:var(--c)">${g.homeAbbr}</span>`;
  const rain=(g.rainChance!==undefined && g.rainChance!=='N/A')?` &nbsp;&#124;&nbsp; ${g.rainChance}% rain`:'';
  const hW=g.temp&&g.temp!='N/A'?`<div class="wr"><span>${wicon(g.condition)}</span><span class="wt"><span>${g.temp}&deg;F</span> &nbsp;&#124;&nbsp; ${g.wind}${rain}</span></div>`:'';
  return `<div class="card" onclick="location.href='/deep-dive/${g.gamePk}'">
    <div class="ct"><span class="badge ${bc}">${bl}</span><span class="ctime">${g.gameTime}</span></div>
    <div class="mu">
      <div class="tb"><div class="tlw">${aL}</div><div class="ta">${g.awayAbbr}</div></div>
      <div class="vs">VS</div>
      <div class="tb"><div class="tlw">${hL}</div><div class="ta">${g.homeAbbr}</div></div>
    </div>
    <div class="pr">
      <div class="pi"><div class="pl">AWAY SP</div><div class="pn">${g.awayPitcher}</div></div>
      <div style="width:1px;background:rgba(255,255,255,.05)"></div>
      <div class="pi"><div class="pl">HOME SP</div><div class="pn">${g.homePitcher}</div></div>
    </div>
    ${hW}
    <div class="eb">
      <div class="ebl"><span>MODEL EDGE</span><strong>${g.edge}%</strong></div>
      <div class="bar"><div class="fill" style="width:0%" data-w="${g.barPct}%"></div></div>
    </div>
    <div class="vn">${g.venue}</div>
    <div class="ap">&#9889; DEEP DIVE ANALYSIS</div>
  </div>`;
}

async function load(){
  try{
    const r=await fetch('/api/games/today');
    const d=await r.json();
    if(!d.success)throw new Error(d.error||'API error');
    const gs=d.games;
    document.getElementById('sg').textContent=gs.length||'0';
    document.getElementById('sl').textContent=gs.filter(g=>g.status==='Live').length;
    document.getElementById('se').textContent=gs.length?(gs.reduce((s,g)=>s+g.edge,0)/gs.length).toFixed(1)+'%':'--';
    document.getElementById('sh').textContent=gs.filter(g=>g.edge>=8).length;
    if(!gs.length){document.getElementById('gc').innerHTML='<div class="ng"><div class="ng-i">&#9918;</div><h3 style="font-family:Orbitron;color:var(--mu);letter-spacing:2px">NO GAMES TODAY</h3></div>';return;}
    document.getElementById('gc').innerHTML=`<div class="grid">${gs.map(mkCard).join('')}</div>`;
    requestAnimationFrame(()=>{document.querySelectorAll('.fill').forEach(e=>{setTimeout(()=>{e.style.width=e.dataset.w;},150);});});
  }catch(e){
    document.getElementById('gc').innerHTML=`<div class="sw"><div style="background:rgba(224,64,251,.08);border:1px solid rgba(224,64,251,.3);border-radius:12px;padding:24px 32px;text-align:center"><h3 style="color:var(--m);font-family:Orbitron;margin-bottom:8px">CONNECTION ERROR</h3><p style="color:var(--mu);font-size:.82rem">${e.message}</p></div></div>`;
  }
}
load();setInterval(load,5*60*1000);
</script>
</body>
</html>"""

# ─────────────────────────────────────────
# DEEP DIVE HTML
# ─────────────────────────────────────────
DEEP_DIVE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Deep Dive &#8212; MLB Analytics Hub</title>
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;700;900&family=Inter:wght@300;400;600&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--c:#00e5ff;--m:#e040fb;--g:#00e676;--o:#ff9800;--bg:#050a18;--card:#0a1628;--b:rgba(0,229,255,.18);--t:#e0f0ff;--mu:#6a8db0}
body{font-family:'Inter',sans-serif;background:var(--bg);color:var(--t);min-height:100vh}
body::before{content:'';position:fixed;inset:0;background:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,229,255,.012) 2px,rgba(0,229,255,.012) 4px);pointer-events:none;z-index:9999}
nav{display:flex;align-items:center;justify-content:space-between;padding:14px 32px;background:rgba(5,10,24,.95);backdrop-filter:blur(16px);border-bottom:1px solid var(--b);position:sticky;top:0;z-index:100}
.logo{font-family:'Orbitron',monospace;font-size:1.2rem;font-weight:900;color:var(--c);text-shadow:0 0 20px rgba(0,229,255,.5)}
.back{display:flex;align-items:center;gap:8px;padding:8px 20px;background:rgba(0,229,255,.07);border:1px solid rgba(0,229,255,.22);border-radius:8px;color:var(--c);font-family:'Orbitron',monospace;font-size:.68rem;letter-spacing:2px;cursor:pointer;text-decoration:none;transition:all .2s}
.back:hover{background:rgba(0,229,255,.14);border-color:var(--c)}
.mh{padding:28px 32px;background:linear-gradient(135deg,rgba(0,229,255,.04),rgba(224,64,251,.04));border-bottom:1px solid var(--b)}
.mhi{max-width:860px;margin:0 auto;text-align:center}
.mht{display:flex;align-items:center;justify-content:center;gap:28px;margin-bottom:18px}
.mhte{display:flex;flex-direction:column;align-items:center;gap:9px}
.mhl{width:76px;height:76px;border-radius:50%;border:2px solid var(--b);background:rgba(0,0,0,.4);display:flex;align-items:center;justify-content:center;overflow:hidden}
.mhl img{width:54px;height:54px;object-fit:contain}
.mha{font-family:'Orbitron',monospace;font-size:1.1rem;font-weight:700}
.mhfn{font-size:.75rem;color:var(--mu)}
.mhvs{font-family:'Orbitron',monospace;font-size:1.4rem;color:var(--mu)}
.mhm{display:flex;gap:10px;justify-content:center;flex-wrap:wrap}
.bdg{padding:4px 14px;border-radius:20px;font-size:.65rem;letter-spacing:2px;font-family:'Orbitron',monospace}
.bc{background:rgba(0,229,255,.1);color:var(--c);border:1px solid rgba(0,229,255,.22)}
.bg{background:rgba(0,230,118,.1);color:var(--g);border:1px solid rgba(0,230,118,.22)}
.bo{background:rgba(255,152,0,.1);color:var(--o);border:1px solid rgba(255,152,0,.22)}
.dg{display:grid;grid-template-columns:1fr 1fr;gap:18px;padding:22px 32px;max-width:1300px;margin:0 auto}
.fw{grid-column:1/-1}
.pnl{background:var(--card);border:1px solid var(--b);border-radius:14px;padding:20px;position:relative;overflow:hidden}
.pnl::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,var(--c),var(--m))}
.pt{font-family:'Orbitron',monospace;font-size:.68rem;color:var(--mu);letter-spacing:3px;margin-bottom:15px;display:flex;align-items:center;gap:7px}
.en{font-family:'Orbitron',monospace;font-size:3rem;font-weight:900;text-shadow:0 0 30px rgba(0,229,255,.4);line-height:1}
.el{font-size:.68rem;color:var(--mu);letter-spacing:3px;margin-top:5px}
.cb{height:10px;background:rgba(255,255,255,.06);border-radius:5px;overflow:hidden;margin-top:8px}
.cf{height:100%;background:linear-gradient(90deg,var(--c),var(--m));border-radius:5px;transition:width 1.5s cubic-bezier(.16,1,.3,1);box-shadow:0 0 8px rgba(0,229,255,.3)}
.wg{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.ws{background:rgba(0,0,0,.3);border:1px solid rgba(255,255,255,.05);border-radius:8px;padding:12px;text-align:center}
.wi{font-size:1.4rem;margin-bottom:5px}
.wv{font-family:'Orbitron',monospace;font-size:.95rem;font-weight:700}
.wl{font-size:.6rem;color:var(--mu);letter-spacing:1px;margin-top:3px}
.wi2{margin-top:10px;padding:9px 12px;background:rgba(0,229,255,.05);border:1px solid rgba(0,229,255,.12);border-radius:8px;font-size:.72rem;color:var(--c);text-align:center;line-height:1.5}
.pg{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.pc{background:rgba(0,0,0,.3);border:1px solid rgba(255,255,255,.05);border-radius:10px;padding:15px;text-align:center}
.pc h4{font-family:'Orbitron',monospace;font-size:.75rem;color:var(--c);margin-bottom:3px}
.pct{font-size:.62rem;color:var(--mu);margin-bottom:11px;letter-spacing:1px}
.sr{display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid rgba(255,255,255,.04)}
.sr:last-child{border-bottom:none}
.sk{font-size:.68rem;color:var(--mu)}
.sv{font-size:.75rem;font-weight:600;font-family:'Orbitron',monospace}
.sg{color:var(--g)!important}.sw2{color:var(--o)!important}
.pkg{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.ps{background:rgba(0,0,0,.3);border:1px solid rgba(255,255,255,.05);border-radius:8px;padding:12px;text-align:center}
.pv{font-family:'Orbitron',monospace;font-size:1.3rem;font-weight:700}
.plb{font-size:.6rem;color:var(--mu);letter-spacing:1px;margin-top:3px}
.lt{display:flex;gap:8px;margin-bottom:12px}
.tb2{padding:6px 14px;border-radius:6px;font-size:.68rem;letter-spacing:2px;font-family:'Orbitron',monospace;cursor:pointer;border:1px solid rgba(255,255,255,.1);background:transparent;color:var(--mu);transition:all .2s}
.tb2.ac{background:rgba(0,229,255,.1);border-color:var(--c);color:var(--c)}
table{width:100%;border-collapse:collapse}
th{font-size:.6rem;color:var(--mu);letter-spacing:2px;text-align:left;padding:6px 8px;border-bottom:1px solid var(--b);font-family:'Orbitron',monospace}
td{font-size:.75rem;padding:7px 8px;border-bottom:1px solid rgba(255,255,255,.04)}
tr:hover td{background:rgba(0,229,255,.03)}
.on{color:var(--mu);font-family:'Orbitron',monospace;font-size:.68rem}
.pb3{display:inline-block;padding:2px 7px;border-radius:4px;background:rgba(0,229,255,.08);color:var(--c);font-size:.62rem;font-family:'Orbitron',monospace}
.ag{color:var(--g)}.am{color:var(--o)}.al{color:var(--mu)}
.selg{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-bottom:14px}
.selc{background:rgba(0,0,0,.26);border:1px solid rgba(255,255,255,.05);border-radius:10px;padding:14px}
.selh{font-family:'Orbitron',monospace;font-size:.66rem;color:var(--mu);letter-spacing:2px;margin-bottom:10px}
.selc label{display:block;font-size:.64rem;color:var(--mu);letter-spacing:1px;margin:8px 0 5px}
.selc select{width:100%;background:#081220;border:1px solid rgba(0,229,255,.18);color:var(--t);padding:10px 12px;border-radius:8px;font-size:.78rem;outline:none}
.selc select:focus{border-color:var(--c);box-shadow:0 0 0 2px rgba(0,229,255,.12)}
.projg{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.projc{background:rgba(0,0,0,.3);border:1px solid rgba(255,255,255,.05);border-radius:12px;padding:16px}
.projt{font-family:'Orbitron',monospace;font-size:.7rem;color:var(--c);letter-spacing:2px;margin-bottom:10px}
.projsub{font-size:.72rem;color:var(--mu);margin-bottom:10px;line-height:1.5}
.projrow{display:flex;justify-content:space-between;padding:7px 0;border-bottom:1px solid rgba(255,255,255,.04)}
.projrow:last-child{border-bottom:none}
.projk{font-size:.7rem;color:var(--mu)}
.projv{font-size:.78rem;color:var(--t);font-weight:600;font-family:'Orbitron',monospace}
.projnote{margin-top:12px;font-size:.68rem;color:var(--mu);line-height:1.5}
.sp{width:32px;height:32px;border:2px solid rgba(0,229,255,.15);border-top-color:var(--c);border-radius:50%;animation:spin 1s linear infinite;margin:20px auto}
@keyframes spin{to{transform:rotate(360deg)}}
.lt2{text-align:center;font-family:'Orbitron',monospace;font-size:.68rem;color:var(--mu);letter-spacing:3px}
.na{color:var(--mu);font-size:.78rem;padding:16px 0;text-align:center;line-height:1.6}
@media(max-width:860px){.dg{grid-template-columns:1fr;padding:14px 16px}.fw{grid-column:1}.pg{grid-template-columns:1fr}nav{padding:12px 16px}.mh{padding:18px 16px}}
</style>
</head>
<body>
<nav>
  <div class="logo">&#9889; MLB ANALYTICS HUB</div>
  <a class="back" href="/">&#8592; DASHBOARD</a>
</nav>
<div class="mh">
  <div class="mhi">
    <div id="mhTeams" class="mht"><div class="sp" style="margin:0 auto"></div></div>
    <div class="mhm" id="mhMeta"></div>
  </div>
</div>
<div class="dg">
  <div class="pnl"><div class="pt">&#127919; MODEL EDGE SCORE</div><div id="edgeP"><div class="sp"></div></div></div>
  <div class="pnl"><div class="pt">&#127780; WEATHER CONDITIONS</div><div id="wxP"><div class="sp"></div></div></div>
  <div class="pnl fw"><div class="pt">&#9918; STARTING PITCHERS</div><div id="pitP"><div class="sp"></div><div class="lt2" style="margin-top:8px">LOADING PITCHER DATA...</div></div></div>
  <div class="pnl"><div class="pt">&#127960; PARK FACTORS</div><div id="pkP"><div class="sp"></div></div></div>
  <div class="pnl"><div class="pt">&#128202; MATCHUP ANALYSIS</div><div id="maP"><div class="sp"></div></div></div>
  <div class="pnl fw">
    <div class="pt">&#127919; MATCHUP PROJECTIONS</div>
    <div class="selg">
      <div class="selc">
        <div class="selh">BATTER PROJECTION</div>
        <label for="batterSel">Select batter</label>
        <select id="batterSel" onchange="updateProjectionView()"><option value="">Loading hitters...</option></select>
        <label for="batterPitcherSel">Opposing pitcher</label>
        <select id="batterPitcherSel" onchange="updateProjectionView()"><option value="">Loading pitchers...</option></select>
      </div>
      <div class="selc">
        <div class="selh">PITCHER PROJECTION</div>
        <label for="pitcherSel">Select pitcher</label>
        <select id="pitcherSel" onchange="updateProjectionView()"><option value="">Loading pitchers...</option></select>
        <label for="pitcherTargetSel">Opposing lineup anchor</label>
        <select id="pitcherTargetSel" onchange="updateProjectionView()"><option value="">Loading hitters...</option></select>
      </div>
    </div>
    <div id="projP"><p class="na">Choose a batter or pitcher to generate matchup-based projections.</p></div>
  </div>

  <div class="pnl fw">
    <div class="pt">&#128203; LINEUP ANALYSIS</div>
    <div class="lt"><button class="tb2 ac" onclick="showTab('away')">AWAY LINEUP</button><button class="tb2" onclick="showTab('home')">HOME LINEUP</button></div>
    <div id="luP"><div class="sp"></div></div>
  </div>
</div>
<script>
const GPK=parseInt(window.location.pathname.split('/').pop());
let GD=null,PD=null,AL=[],HL=[];

function num(v,d=0){
  const n=parseFloat(String(v).replace(/^\./,'0.'));
  return Number.isFinite(n)?n:d;
}
function batterProjection(b,p){
  const avg=num(b.avg,.240), obp=num(b.obp,avg+.060), slg=num(b.slg,avg*1.6), ops=num(b.ops,obp+slg);
  const era=num((p.stats||{}).era,4.20), whip=num((p.stats||{}).whip,1.30), k9=num((p.stats||{}).k9,8.0);
  const hitProb=Math.max(.12,Math.min(.68, avg + (whip-1.25)*0.08 - (era-4.0)*0.015));
  const hits=(3.9*hitProb).toFixed(2);
  const tb=(Math.max(0.3,3.8*(slg*.72)*(1+(whip-1.2)*0.12-(k9-8)*0.025))).toFixed(2);
  const hrPct=(Math.max(2,Math.min(28, 6 + (slg-.380)*38 + (era-4.0)*2.2))*1).toFixed(1);
  const rbiPct=(Math.max(8,Math.min(55, 18 + (ops-.700)*42 + (era-4.0)*2.5))).toFixed(1);
  const kRisk=(Math.max(8,Math.min(45, 16 + (k9-8)*3.2 - (avg-.250)*55))).toFixed(1);
  return {hits,tb,hrPct,rbiPct,kRisk};
}
function pitcherProjection(p, lineup){
  const era=num((p.stats||{}).era,4.20), whip=num((p.stats||{}).whip,1.30), k9=num((p.stats||{}).k9,8.0);
  const lineupAvg=lineup.length?lineup.reduce((s,x)=>s+num(x.avg,.240),0)/lineup.length:.240;
  const lineupOps=lineup.length?lineup.reduce((s,x)=>s+num(x.ops,.700),0)/lineup.length:.700;
  const outs=Math.max(9,Math.min(21, 16.5 - (era-4.0)*0.9 - (lineupOps-.700)*6.5)).toFixed(1);
  const ks=Math.max(2,Math.min(10, (outs/3)*(k9/9)*(1-(lineupAvg-.250)*1.4))).toFixed(1);
  const er=Math.max(0.5,Math.min(5.5, (outs/3)*(era/9)*(1+(lineupOps-.700)*1.3))).toFixed(1);
  const baserunners=Math.max(3,Math.min(11, (outs/3)*whip*(1+(lineupAvg-.250)*1.1))).toFixed(1);
  return {outs,ks,er,baserunners,lineupAvg:(lineupAvg).toFixed(3),lineupOps:(lineupOps).toFixed(3)};
}
function allBatters(){
  const away=(AL||[]).map(b=>({...b, team:'away', teamLabel:GD?.awayAbbr||'AWAY'}));
  const home=(HL||[]).map(b=>({...b, team:'home', teamLabel:GD?.homeAbbr||'HOME'}));
  return away.concat(home);
}
function allPitchers(){
  if(!PD||!PD.success) return [];
  return [
    {...(PD.awayPitcher||{}), side:'away', teamLabel:GD?.awayAbbr||'AWAY'},
    {...(PD.homePitcher||{}), side:'home', teamLabel:GD?.homeAbbr||'HOME'}
  ].filter(x=>x && x.name);
}
function populateProjectionSelectors(){
  const batSel=document.getElementById('batterSel');
  const batPitSel=document.getElementById('batterPitcherSel');
  const pitSel=document.getElementById('pitcherSel');
  const pitTarSel=document.getElementById('pitcherTargetSel');
  if(!batSel||!batPitSel||!pitSel||!pitTarSel) return;
  const batters=allBatters();
  const pitchers=allPitchers();
  batSel.innerHTML = '<option value="">Select a batter...</option>' + batters.map((b,i)=>`<option value="${i}">${b.teamLabel} — ${b.slot}. ${b.name}</option>`).join('');
  batPitSel.innerHTML = '<option value="">Select pitcher...</option>' + pitchers.map((p,i)=>`<option value="${i}">${p.teamLabel} — ${p.name}</option>`).join('');
  pitSel.innerHTML = '<option value="">Select a pitcher...</option>' + pitchers.map((p,i)=>`<option value="${i}">${p.teamLabel} — ${p.name}</option>`).join('');
  pitTarSel.innerHTML = '<option value="">Select opposing hitter...</option>' + batters.map((b,i)=>`<option value="${i}">${b.teamLabel} — ${b.slot}. ${b.name}</option>`).join('');
  if(!batSel.value && batters.length) batSel.value='0';
  if(!pitSel.value && pitchers.length) pitSel.value='0';
  if(!pitTarSel.value && batters.length) pitTarSel.value='0';
  const selB = batters[batSel.value||0];
  if(selB && pitchers.length){
    const opp = pitchers.findIndex(p=>p.side !== selB.team);
    batPitSel.value = String(opp >= 0 ? opp : 0);
  }
  const selP = pitchers[pitSel.value||0];
  if(selP && batters.length){
    const oppB = batters.findIndex(b=>b.team !== selP.side);
    pitTarSel.value = String(oppB >= 0 ? oppB : 0);
  }
  updateProjectionView();
}
function updateProjectionView(){
  const batters=allBatters(), pitchers=allPitchers();
  const b = batters[document.getElementById('batterSel')?.value];
  const bp = pitchers[document.getElementById('batterPitcherSel')?.value];
  const p = pitchers[document.getElementById('pitcherSel')?.value];
  const tb = batters[document.getElementById('pitcherTargetSel')?.value];
  let left = '<p class="na">Select a batter and opposing pitcher.</p>';
  let right = '<p class="na">Select a pitcher and opposing hitter anchor.</p>';
  if(b && bp){
    const pr = batterProjection(b,bp);
    left = `<div class="projc"><div class="projt">BATTER OUTLOOK</div><div class="projsub">${b.teamLabel} ${b.name} vs ${bp.teamLabel} ${bp.name}</div><div class="projrow"><span class="projk">Projected hits</span><span class="projv">${pr.hits}</span></div><div class="projrow"><span class="projk">Projected total bases</span><span class="projv">${pr.tb}</span></div><div class="projrow"><span class="projk">HR probability</span><span class="projv">${pr.hrPct}%</span></div><div class="projrow"><span class="projk">RBI probability</span><span class="projv">${pr.rbiPct}%</span></div><div class="projrow"><span class="projk">Strikeout risk</span><span class="projv">${pr.kRisk}%</span></div><div class="projnote">Model inputs: batter AVG/OBP/SLG/OPS blended with opposing starter ERA, WHIP, and K/9.</div></div>`;
  }
  if(p){
    const oppLineup = (p.side==='away') ? HL : AL;
    const pr2 = pitcherProjection(p, oppLineup);
    const anchorTxt = tb ? `${tb.teamLabel} ${tb.name}` : 'opposing lineup';
    right = `<div class="projc"><div class="projt">PITCHER OUTLOOK</div><div class="projsub">${p.teamLabel} ${p.name} vs ${p.side==='away' ? GD?.homeAbbr : GD?.awayAbbr} lineup, anchor: ${anchorTxt}</div><div class="projrow"><span class="projk">Projected outs</span><span class="projv">${pr2.outs}</span></div><div class="projrow"><span class="projk">Projected strikeouts</span><span class="projv">${pr2.ks}</span></div><div class="projrow"><span class="projk">Projected earned runs</span><span class="projv">${pr2.er}</span></div><div class="projrow"><span class="projk">Projected baserunners</span><span class="projv">${pr2.baserunners}</span></div><div class="projrow"><span class="projk">Opp lineup AVG / OPS</span><span class="projv">${pr2.lineupAvg} / ${pr2.lineupOps}</span></div><div class="projnote">Model inputs: starter ERA, WHIP, K/9 plus opposing lineup average and OPS from the posted batting order.</div></div>`;
  }
  const projP=document.getElementById('projP');
  if(projP) projP.innerHTML = `<div class="projg">${left}${right}</div>`;
}


function wic(c){c=(c||'').toLowerCase();if(c.includes('rain'))return'&#127783;';if(c.includes('cloud'))return'&#9925;';if(c.includes('clear')||c.includes('sun'))return'&#9728;';return'&#127780;';}
function ec(e){const v=parseFloat(e);if(isNaN(v))return'';return v<3.0?'sg':v<4.5?'':'sw2';}
function ac(a){const v=parseFloat(a);if(isNaN(v))return'al';return v>=.280?'ag':v>=.240?'am':'al';}

async function loadGame(){
  try{
    const r=await fetch('/api/games/today');
    const d=await r.json();
    if(!d.success)throw new Error(d.error||'failed');
    GD=(d.games||[]).find(g=>g.gamePk==GPK);
    if(!GD)throw new Error('Game not found. It may not be scheduled for today.');
    renderHeader();renderEdge();renderWx();renderPark();renderMatchup();
    populateProjectionSelectors();
  }catch(e){
    document.getElementById('mhTeams').innerHTML=`<p style="color:var(--m);font-family:Orbitron;font-size:.85rem">${e.message}</p>`;
    ['edgeP','wxP','pkP','maP'].forEach(id=>{document.getElementById(id).innerHTML=`<p class="na">Data unavailable</p>`;});
  }
}
async function loadPitchers(){
  try{
    const r=await fetch('/api/pitchers/'+GPK);
    PD=await r.json();
    renderPitchers();
    populateProjectionSelectors();
  }catch(e){document.getElementById('pitP').innerHTML=`<p class="na">Pitcher data unavailable</p>`;}
}
async function loadLineups(){
  try{
    const r=await fetch('/api/game/'+GPK);
    const d=await r.json();
    if(d.success){AL=d.awayBatters||[];HL=d.homeBatters||[];}
    showTab('away');
    populateProjectionSelectors();
  }catch(e){document.getElementById('luP').innerHTML=`<p class="na">Lineup not yet posted. Check back ~3 hours before first pitch.</p>`;}
}

function renderHeader(){
  const g=GD;
  const ai=g.awayLogo?`<img src="${g.awayLogo}" alt="${g.awayAbbr}" width="54" height="54" style="object-fit:contain">`:`<span style="font-family:Orbitron;color:var(--c);font-size:1rem">${g.awayAbbr}</span>`;
  const hi=g.homeLogo?`<img src="${g.homeLogo}" alt="${g.homeAbbr}" width="54" height="54" style="object-fit:contain">`:`<span style="font-family:Orbitron;color:var(--c);font-size:1rem">${g.homeAbbr}</span>`;
  const sc=g.status==='Live'?'bg':g.status==='Final'?'':'bc';
  document.getElementById('mhTeams').innerHTML=`<div class="mhte"><div class="mhl">${ai}</div><div class="mha">${g.awayAbbr}</div><div class="mhfn">${g.awayName}</div></div><div class="mhvs">VS</div><div class="mhte"><div class="mhl">${hi}</div><div class="mha">${g.homeAbbr}</div><div class="mhfn">${g.homeName}</div></div>`;
  document.getElementById('mhMeta').innerHTML=`<span class="bdg bc">${g.gameTime}</span><span class="bdg ${sc}">${g.status.toUpperCase()}</span><span class="bdg bo">PARK ${g.parkFactor}x</span>${g.venue?`<span class="bdg bc">${g.venue}</span>`:''}`;
}
function renderEdge(){
  const g=GD,conf=g.edge>=8?'HIGH':g.edge>=5?'MEDIUM':'LOW',col=g.edge>=8?'var(--g)':g.edge>=5?'var(--c)':'var(--mu)';
  document.getElementById('edgeP').innerHTML=`<div style="text-align:center;padding:12px 0">
    <div class="en" style="color:${col}">${g.edge}%</div><div class="el">MODEL EDGE SCORE</div>
    <div style="margin-top:14px">
      <div style="display:flex;justify-content:space-between;margin-bottom:4px"><span style="font-size:.62rem;color:var(--mu);letter-spacing:2px">CONFIDENCE</span><span style="font-family:Orbitron;font-size:.72rem;color:${col}">${conf}</span></div>
      <div class="cb"><div class="cf" style="width:0%" data-w="${g.barPct}%"></div></div>
      <div style="display:flex;justify-content:space-between;margin-top:4px"><span style="font-size:.58rem;color:var(--mu)">LOW</span><span style="font-size:.58rem;color:var(--mu)">MED</span><span style="font-size:.58rem;color:var(--mu)">HIGH</span></div>
    </div></div>`;
  setTimeout(()=>{document.querySelectorAll('.cf').forEach(e=>{e.style.width=e.dataset.w;});},200);
}
function renderWx(){
  const g=GD;
  if(!g.temp||g.temp==='N/A'){document.getElementById('wxP').innerHTML=`<p class="na">Weather data not yet available for this game.</p>`;return;}
  const tv=parseInt(g.temp)||70;
  const imp=tv>80?'&#128293; HOT &mdash; Ball carries well. Hitters favored.':tv<50?'&#129398; COLD &mdash; Ball dies. Pitchers favored.':'&#9989; NEUTRAL &mdash; Standard conditions.';
  document.getElementById('wxP').innerHTML=`<div class="wg" style="grid-template-columns:1fr 1fr 1fr"><div class="ws"><div class="wi">${wic(g.condition)}</div><div class="wv">${g.temp}&deg;F</div><div class="wl">TEMPERATURE</div></div><div class="ws"><div class="wi">&#128168;</div><div class="wv" style="font-size:.82rem">${g.wind||'N/A'}</div><div class="wl">WIND</div></div><div class="ws"><div class="wi">&#127783;</div><div class="wv">${g.rainChance ?? 'N/A'}%</div><div class="wl">RAIN CHANCE</div></div></div><div class="wi2">${imp}</div>`;
}
function renderPitchers(){
  function pc(p,lbl){
    const s=p.stats||{},has=s.era&&s.era!=='N/A';
    const rows=has?`<div class="sr"><span class="sk">ERA</span><span class="sv ${ec(s.era)}">${s.era}</span></div><div class="sr"><span class="sk">WHIP</span><span class="sv">${s.whip}</span></div><div class="sr"><span class="sk">K/9</span><span class="sv">${s.k9}</span></div><div class="sr"><span class="sk">IP</span><span class="sv">${s.ip}</span></div><div class="sr"><span class="sk">W-L</span><span class="sv">${s.wins}-${s.losses}</span></div>`:`<p class="na" style="font-size:.7rem">No season stats yet</p>`;
    return `<div class="pc"><h4>${p.name||'TBD'}</h4><div class="pct">${lbl}</div>${rows}</div>`;
  }
  if(!PD||!PD.success){document.getElementById('pitP').innerHTML=`<p class="na">Pitcher stats unavailable</p>`;return;}
  document.getElementById('pitP').innerHTML=`<div class="pg">${pc(PD.awayPitcher,GD?GD.awayName:'AWAY')}${pc(PD.homePitcher,GD?GD.homeName:'HOME')}</div>`;
}
function renderPark(){
  const g=GD,pf=g.parkFactor||1.0,type=pf>=1.05?'Hitter-Friendly':pf<=0.95?'Pitcher-Friendly':'Neutral',col=pf>=1.05?'var(--o)':pf<=0.95?'var(--g)':'var(--c)';
  const desc=pf>=1.05?`&#128293; <strong style="color:var(--o)">${g.venue||'This park'}</strong> significantly boosts offense.`:pf<=0.95?`&#9968; <strong style="color:var(--g)">${g.venue||'This park'}</strong> suppresses offense. Favor unders.`:`&#9989; <strong style="color:var(--c)">${g.venue||'This park'}</strong> plays neutral.`;
  document.getElementById('pkP').innerHTML=`<div class="pkg"><div class="ps"><div class="pv" style="color:${col}">${pf}x</div><div class="plb">RUN FACTOR</div></div><div class="ps"><div class="pv" style="color:${col};font-size:.95rem">${type}</div><div class="plb">PARK TYPE</div></div></div><div style="margin-top:12px;padding:10px;background:rgba(0,0,0,.3);border-radius:8px;font-size:.72rem;color:var(--mu);line-height:1.6">${desc}</div>`;
}
function renderMatchup(){
  const g=GD;
  document.getElementById('maP').innerHTML=`
    <div class="sr"><span class="sk">AWAY</span><span class="sv" style="color:var(--t)">${g.awayName}</span></div>
    <div class="sr"><span class="sk">HOME</span><span class="sv" style="color:var(--t)">${g.homeName}</span></div>
    <div class="sr"><span class="sk">VENUE</span><span class="sv" style="font-family:Inter;font-weight:600;font-size:.72rem;color:var(--t)">${g.venue||'TBD'}</span></div>
    <div class="sr"><span class="sk">GAME TIME</span><span class="sv">${g.gameTime}</span></div>
    <div class="sr"><span class="sk">MODEL EDGE</span><span class="sv" style="color:var(--c)">${g.edge}%</span></div>
    <div class="sr"><span class="sk">PARK FACTOR</span><span class="sv" style="color:var(--o)">${g.parkFactor}x</span></div>
    <div class="sr"><span class="sk">STATUS</span><span class="sv" style="color:${g.status==='Live'?'var(--g)':'var(--c)'}">${g.status.toUpperCase()}</span></div>`;
}
function showTab(side){
  document.querySelectorAll('.tb2').forEach((b,i)=>b.classList.toggle('ac',(i===0&&side==='away')||(i===1&&side==='home')));
  const lu=side==='away'?AL:HL;
  if(!lu||!lu.length){document.getElementById('luP').innerHTML=`<p class="na">Lineup not yet posted. Check back ~3 hours before first pitch.</p>`;return;}
  const rows=lu.map((p,i)=>`<tr><td class="on">${i+1}</td><td>${p.name}</td><td><span class="pb3">${p.pos}</span></td><td class="${ac(p.avg)}">${p.avg}</td><td>${p.ab}</td><td>${p.hits}</td><td>${p.hr}</td><td>${p.rbi}</td></tr>`).join('');
  document.getElementById('luP').innerHTML=`<table><thead><tr><th>#</th><th>PLAYER</th><th>POS</th><th>AVG</th><th>AB</th><th>H</th><th>HR</th><th>RBI</th></tr></thead><tbody>${rows}</tbody></table>`;
}
loadGame();loadPitchers();loadLineups();
</script>
</body>
</html>"""

# ─────────────────────────────────────────
# ROUTES — serve HTML as strings (no template files needed!)
# ─────────────────────────────────────────
@app.route("/")
def index():
    return DASHBOARD_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}

@app.route("/deep-dive/<game_pk>")
def deep_dive(game_pk):
    # Accept any game_pk (int or string) — no type-casting that could 404
    return DEEP_DIVE_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}

@app.route("/api/games/today")
def api_games_today():
    try:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        raw      = fetch_schedule(date_str)
        games    = [x for x in (parse_game(g) for g in raw) if x is not None]
        return jsonify({"success": True, "date": date_str, "games": games, "count": len(games)})
    except Exception as e:
        print(f"[api_games_today] {e}\n{traceback.format_exc()}")
        return jsonify({"success": False, "error": str(e), "games": []}), 500

@app.route("/api/game/<game_pk>")
def api_game_detail(game_pk):
    try:
        r = requests.get(f"{MLB_API}/game/{game_pk}/boxscore", timeout=10)
        r.raise_for_status()
        data      = r.json()
        away_team = data.get("teams", {}).get("away", {})
        home_team = data.get("teams", {}).get("home", {})
        def get_batters(team_data):
            out = []
            for pid, p in team_data.get("players", {}).items():
                order = p.get("battingOrder")
                if not order:
                    continue
                try:
                    order_int = int(str(order).strip())
                    if order_int % 100 != 0:
                        continue
                    slot = order_int // 100
                except Exception:
                    continue
                pos = p.get("position", {}).get("abbreviation", "")
                s  = p.get("stats", {}).get("batting", {})
                ss = p.get("seasonStats", {}).get("batting", {})
                out.append({
                    "slot": slot,
                    "id": p.get("person", {}).get("id"),
                    "name": p.get("person", {}).get("fullName", ""),
                    "pos":  pos,
                    "avg":  ss.get("avg", ".---"),
                    "obp":  ss.get("obp", ".---"),
                    "slg":  ss.get("slg", ".---"),
                    "ops":  ss.get("ops", ".---"),
                    "seasonHr": ss.get("homeRuns", 0),
                    "seasonRbi": ss.get("rbi", 0),
                    "seasonSo": ss.get("strikeOuts", 0),
                    "ab":   s.get("atBats", 0),
                    "hits": s.get("hits", 0),
                    "hr":   s.get("homeRuns", 0),
                    "rbi":  s.get("rbi", 0),
                })
            out.sort(key=lambda x: x["slot"])
            return out[:9]
        return jsonify({"success": True, "awayBatters": get_batters(away_team), "homeBatters": get_batters(home_team)})
    except Exception as e:
        print(f"[api_game_detail] {e}
{traceback.format_exc()}")
        return jsonify({"success": False, "error": str(e), "awayBatters": [], "homeBatters": []})

@app.route("/api/pitchers/<game_pk>")
def api_pitchers(game_pk):
    try:
        raw = fetch_schedule(datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        for g in raw:
            if str(g.get("gamePk", "")) == str(game_pk):
                ap = g.get("teams", {}).get("away", {}).get("probablePitcher", {})
                hp = g.get("teams", {}).get("home", {}).get("probablePitcher", {})
                return jsonify({
                    "success": True,
                    "awayPitcher": {"id": ap.get("id"), "name": ap.get("fullName", "TBD"), "stats": pitcher_stats(ap["id"]) if ap.get("id") else {}},
                    "homePitcher": {"id": hp.get("id"), "name": hp.get("fullName", "TBD"), "stats": pitcher_stats(hp["id"]) if hp.get("id") else {}},
                })
        return jsonify({"success": False, "error": "Game not found",
                        "awayPitcher": {"name": "TBD", "stats": {}},
                        "homePitcher": {"name": "TBD", "stats": {}}})
    except Exception as e:
        print(f"[api_pitchers] {e}\n{traceback.format_exc()}")
        return jsonify({"success": False, "error": str(e),
                        "awayPitcher": {"name": "TBD", "stats": {}},
                        "homePitcher": {"name": "TBD", "stats": {}}})

@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not found", "path": str(e)}), 404

@app.errorhandler(500)
def server_error(e):
    print(f"[500] {e}\n{traceback.format_exc()}")
    return jsonify({"error": "Internal server error", "detail": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
