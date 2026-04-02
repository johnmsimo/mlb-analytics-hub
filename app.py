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

        try:
            temp_val  = int(str(weather.get("temp","70")).strip())
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
            "temp":        weather.get("temp", "N/A"),
            "wind":        weather.get("wind", "N/A"),
            "condition":   weather.get("condition", "N/A"),
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
  const hW=g.temp&&g.temp!='N/A'?`<div class="wr"><span>${wicon(g.condition)}</span><span class="wt"><span>${g.temp}&deg;F</span> &nbsp;&#124;&nbsp; ${g.wind}</span></div>`:'';
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
    <div class="pt">&#128203; LINEUP ANALYSIS</div>
    <div class="lt"><button class="tb2 ac" onclick="showTab('away')">AWAY LINEUP</button><button class="tb2" onclick="showTab('home')">HOME LINEUP</button></div>
    <div id="luP"><div class="sp"></div></div>
  </div>
</div>
<script>
const GPK=parseInt(window.location.pathname.split('/').pop());
let GD=null,PD=null,AL=[],HL=[];

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
  }catch(e){document.getElementById('pitP').innerHTML=`<p class="na">Pitcher data unavailable</p>`;}
}
async function loadLineups(){
  try{
    const r=await fetch('/api/game/'+GPK);
    const d=await r.json();
    if(d.success){AL=d.awayBatters||[];HL=d.homeBatters||[];}
    showTab('away');
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
  document.getElementById('wxP').innerHTML=`<div class="wg"><div class="ws"><div class="wi">${wic(g.condition)}</div><div class="wv">${g.temp}&deg;F</div><div class="wl">TEMPERATURE</div></div><div class="ws"><div class="wi">&#128168;</div><div class="wv" style="font-size:.82rem">${g.wind||'N/A'}</div><div class="wl">WIND</div></div></div><div class="wi2">${imp}</div>`;
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
                pos = p.get("position", {}).get("abbreviation", "")
                if pos in ["P", ""]:
                    continue
                s = p.get("stats", {}).get("batting", {})
                ss = p.get("seasonStats", {}).get("batting", {})
                out.append({
                    "name": p.get("person", {}).get("fullName", ""),
                    "pos":  pos,
                    "avg":  ss.get("avg", ".000"),
                    "ab":   s.get("atBats", 0),
                    "hits": s.get("hits", 0),
                    "hr":   s.get("homeRuns", 0),
                    "rbi":  s.get("rbi", 0),
                })
            return out[:9]
        return jsonify({"success": True, "awayBatters": get_batters(away_team), "homeBatters": get_batters(home_team)})
    except Exception as e:
        print(f"[api_game_detail] {e}")
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
