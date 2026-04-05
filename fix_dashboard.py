#!/usr/bin/env python3
"""
fix_dashboard.py  — run once in your project root
Fixes two bugs:
  1. Positions showing "?" (JS reads p.position but API sends p.pos)
  2. Live gameplay panel not rendering
Also adds /api/game/livedata route to app.py if missing.
"""
import os, sys, re, pathlib, shutil

HERE = pathlib.Path(__file__).parent
DASH = HERE / "dashboard.html"
APP  = HERE / "app.py"

for p in [DASH, APP]:
    if not p.exists():
        sys.exit(f"ERROR: {p.name} not found — run this from your project root")

dash = DASH.read_text(encoding="utf-8")
app  = APP.read_text(encoding="utf-8")
orig_dash_len = len(dash)
changes = []

# ── 1. Fix position bug: p.position → p.pos ──────────────────────────────────
POS_OLD  = "p.position.replace('Designated Hitter','DH').replace('Pitcher','P')"
POS_OLD2 = 'p.position.replace("Designated Hitter","DH").replace("Pitcher","P")'
POS_NEW  = "(p.pos||p.position||'').replace('Designated Hitter','DH').replace('Pitcher','P')"

if POS_OLD in dash:
    dash = dash.replace(POS_OLD, POS_NEW, 1)
    changes.append("FIX-1 OK  p.position -> p.pos (single-quote)")
elif POS_OLD2 in dash:
    dash = dash.replace(POS_OLD2, POS_NEW, 1)
    changes.append("FIX-1 OK  p.position -> p.pos (double-quote)")
else:
    m = re.search(r'var pos\s*=\s*p\.position\.replace', dash)
    if m:
        dash = dash[:m.start()] + "var pos = (p.pos||p.position||'').replace" + dash[m.end():]
        changes.append("FIX-1 OK  p.position -> p.pos (regex)")
    else:
        changes.append("FIX-1 WARN  position pattern not found — check manually")

# ── 2. Inject live slot into mkCard before .pr pitcher block ─────────────────
LIVE_SLOT = """${g.status==='Live'?'<div id="ls-'+g.gamePk+'" class="ls-placeholder"></div>':''}"""

if 'ls-placeholder' in dash:
    changes.append("FIX-2 SKIP  live slot already present")
else:
    pr_pats = ['<div class="pr">', "<div class='pr'>", '<div class=pr>']
    injected = False
    for pat in pr_pats:
        if pat in dash:
            idx = dash.find(pat)
            dash = dash[:idx] + LIVE_SLOT + dash[idx:]
            changes.append(f"FIX-2 OK  live slot injected before '{pat}'")
            injected = True
            break
    if not injected:
        m = re.search(r'<div\s+class=["\']?pr["\']?>', dash)
        if m:
            idx = m.start()
            dash = dash[:idx] + LIVE_SLOT + dash[idx:]
            changes.append("FIX-2 OK  live slot injected (regex)")
        else:
            changes.append("FIX-2 WARN  .pr block not found — live slot not injected")

# ── 3. Wire initLiveCards call ────────────────────────────────────────────────
WIRE_CALL = "if(window.initLiveCards)initLiveCards(gs);"
WIRE_MARKER = "initLiveCards(gs)"

if WIRE_MARKER in dash:
    changes.append("FIX-3 SKIP  initLiveCards already wired")
else:
    wire_pats = [
        "gs.forEach(function(g){fetch(`/api/lineup/",
        "gs.forEach(function(g){fetch('/api/lineup/",
        "gs.forEach(g=>fetch(`/api/lineup/",
        "gs.forEach(function(g) {fetch(`/api/lineup/",
        "gs.forEach(function(g) { fetch(`/api/lineup/",
    ]
    wired = False
    for pat in wire_pats:
        if pat in dash:
            dash = dash.replace(pat, WIRE_CALL + pat, 1)
            changes.append("FIX-3 OK  wired initLiveCards before lineup forEach")
            wired = True
            break
    if not wired:
        m = re.search(r'gs\.forEach\s*\(', dash)
        if m:
            dash = dash[:m.start()] + WIRE_CALL + dash[m.start():]
            changes.append("FIX-3 OK  wired initLiveCards (regex)")
        else:
            changes.append("FIX-3 WARN  gs.forEach not found — wire manually")

# ── 4. Remove old patch block if present ──────────────────────────────────────
if 'END LIVE GAMEPLAY PATCH' in dash:
    old_s = dash.find('<!-- =')
    old_e = dash.find('END LIVE GAMEPLAY PATCH')
    if old_s >= 0 and old_e >= 0:
        old_e = dash.find('-->', old_e) + 3
        dash = dash[:old_s] + dash[old_e:]
        changes.append("RM  removed old live patch block")

# ── 5. Inject live CSS + JS ───────────────────────────────────────────────────
LIVE_BLOCK = """
<!-- LIVE GAMEPLAY PATCH v2 -->
<style>
.ls-placeholder,.ls-wrap{display:block}
.ls-wrap{margin:10px 0 8px;background:rgba(0,229,255,.04);border:1px solid rgba(0,229,255,.14);border-radius:10px;overflow:hidden;font-size:.72rem}
.ls-inning{display:flex;align-items:center;justify-content:space-between;padding:6px 12px 4px;background:rgba(0,229,255,.07);border-bottom:1px solid rgba(0,229,255,.1)}
.ls-inn-lbl{font-family:Orbitron,monospace;font-size:.68rem;color:#00e5ff;letter-spacing:1.5px;font-weight:700}
.ls-outs{display:flex;gap:4px;align-items:center}
.ls-out-dot{width:9px;height:9px;border-radius:50%;border:1.5px solid rgba(0,229,255,.4);background:transparent;transition:background .2s}
.ls-out-dot.on{background:#ff9800;border-color:#ff9800;box-shadow:0 0 6px rgba(255,152,0,.5)}
.ls-count{font-family:Orbitron,monospace;font-size:.62rem;color:#8b949e;letter-spacing:1px}
.ls-count span{color:#00e5ff;font-weight:700}
.ls-score{display:grid;grid-template-columns:1fr auto;padding:8px 12px 6px}
.ls-rhe{width:100%}
.ls-rhe-hdr{display:grid;grid-template-columns:56px repeat(3,32px);font-family:Orbitron,monospace;font-size:.55rem;color:#4a5a6a;letter-spacing:1px;padding-bottom:2px;border-bottom:1px solid rgba(255,255,255,.06);margin-bottom:4px}
.ls-rhe-row{display:grid;grid-template-columns:56px repeat(3,32px);align-items:center;padding:2px 0}
.ls-rhe-row.batting .ls-rhe-abbr{color:#00e5ff}
.ls-rhe-abbr{font-family:Orbitron,monospace;font-size:.65rem;color:#8b9eb0;font-weight:700;letter-spacing:.5px}
.ls-rhe-val{font-family:Orbitron,monospace;font-size:.75rem;color:#e0eaff;font-weight:700;text-align:center}
.ls-rhe-val.runs{color:#00e5ff;font-size:.82rem}
.ls-right{display:flex;flex-direction:column;align-items:center;justify-content:center;padding-left:10px}
.ls-diamond{width:46px;height:46px;flex-shrink:0}
.ls-diamond svg{width:46px;height:46px}
.ls-players{display:grid;grid-template-columns:1fr 1fr;gap:6px;padding:5px 10px 8px;border-top:1px solid rgba(255,255,255,.05)}
.ls-player{background:rgba(0,0,0,.25);border-radius:7px;padding:6px 8px}
.ls-player-lbl{font-family:Orbitron,monospace;font-size:.5rem;color:#4a5a7a;letter-spacing:1.5px;text-transform:uppercase;margin-bottom:2px}
.ls-player-name{font-size:.68rem;color:#c9d8e8;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:120px}
.ls-player-stat{font-size:.6rem;color:#5a7a9a;margin-top:1px}
.ls-dueup{padding:4px 10px 7px;border-top:1px solid rgba(255,255,255,.04);font-size:.58rem;color:#4a6a8a;font-family:Orbitron,monospace;letter-spacing:.8px}
.ls-dueup span{color:#7090a8;margin-left:6px}
.ls-loading{padding:10px;text-align:center;font-family:Orbitron,monospace;font-size:.58rem;color:rgba(0,229,255,.4);letter-spacing:2px;animation:ls-pulse 1.4s ease-in-out infinite}
@keyframes ls-pulse{0%,100%{opacity:.4}50%{opacity:1}}
</style>
<script>
(function(){
  var _T={};
  function diamond(b){
    var on='rgba(0,229,255,1)',off='rgba(0,229,255,.15)';
    function sq(x,y,f){var c=f?on:off,s=f?'filter:drop-shadow(0 0 4px rgba(0,229,255,.9))':'';
      return '<rect x="'+(x-5)+'" y="'+(y-5)+'" width="10" height="10" rx="1.5" transform="rotate(45,'+x+','+y+')" fill="'+c+'" style="'+s+'"/>';}
    return '<svg viewBox="0 0 46 46" xmlns="http://www.w3.org/2000/svg">'+
      '<polyline points="23,4 41,23 23,42 5,23 23,4" fill="none" stroke="rgba(0,229,255,.12)" stroke-width="1"/>'+
      sq(23,4,b.second)+sq(5,23,b.third)+sq(41,23,b.first)+sq(23,42,false)+'</svg>';
  }
  function html(d,aAbbr,hAbbr){
    if(!d||!d.success)return '<div class="ls-loading">LIVE DATA UNAVAILABLE</div>';
    var dots=[0,1,2].map(function(i){return '<div class="ls-out-dot'+(i<d.outs?' on':'')+'"></div>';}).join('');
    var bat=d.inningLabel&&d.inningLabel.indexOf('BOT')===0?'home':'away';
    var pi=d.pitcher||{},ba=d.batter||{};
    var pSt=(pi.ip||'—')+' IP · '+(pi.er!==undefined&&pi.er!=='—'?pi.er+' ER':'—');
    var bSt=(ba.h||0)+'-'+(ba.ab||0)+' · '+(ba.ops||'—')+' OPS';
    var due=(d.dueUp||[]).filter(Boolean).map(function(n){return n.split(' ').pop();}).join(', ');
    return '<div class="ls-inning"><div class="ls-inn-lbl">'+(d.inningLabel||'—')+'</div><div class="ls-outs">'+dots+'</div>'+
      '<div class="ls-count">B<span>'+d.balls+'</span> S<span>'+d.strikes+'</span></div></div>'+
      '<div class="ls-score"><div><div class="ls-rhe">'+
      '<div class="ls-rhe-hdr"><span></span><span>R</span><span>H</span><span>E</span></div>'+
      '<div class="ls-rhe-row'+(bat==='away'?' batting':'')+'"><div class="ls-rhe-abbr">'+aAbbr+'</div>'+
      '<div class="ls-rhe-val runs">'+d.awayRuns+'</div><div class="ls-rhe-val">'+d.awayHits+'</div><div class="ls-rhe-val">'+d.awayErrors+'</div></div>'+
      '<div class="ls-rhe-row'+(bat==='home'?' batting':'')+'"><div class="ls-rhe-abbr">'+hAbbr+'</div>'+
      '<div class="ls-rhe-val runs">'+d.homeRuns+'</div><div class="ls-rhe-val">'+d.homeHits+'</div><div class="ls-rhe-val">'+d.homeErrors+'</div></div>'+
      '</div></div><div class="ls-right"><div class="ls-diamond">'+diamond(d.bases||{})+'</div></div></div>'+
      '<div class="ls-players">'+
      '<div class="ls-player"><div class="ls-player-lbl">PITCHING</div><div class="ls-player-name">'+(pi.name||'—')+'</div><div class="ls-player-stat">'+pSt+'</div></div>'+
      '<div class="ls-player"><div class="ls-player-lbl">AT BAT</div><div class="ls-player-name">'+(ba.name||'—')+'</div><div class="ls-player-stat">'+bSt+'</div></div></div>'+
      (due?'<div class="ls-dueup">DUE UP <span>'+due+'</span></div>':'');
  }
  function doFetch(pk,aA,hA){
    var el=document.getElementById('ls-'+pk);if(!el)return;
    fetch('/api/game/livedata/'+pk)
      .then(function(r){return r.json();})
      .then(function(d){var e2=document.getElementById('ls-'+pk);if(e2){e2.className='ls-wrap';e2.innerHTML=html(d,aA,hA);}})
      .catch(function(){var e2=document.getElementById('ls-'+pk);if(e2)e2.innerHTML='<div class="ls-loading">LIVE DATA UNAVAILABLE</div>';});
  }
  window.stopAllLivePolling=function(){Object.keys(_T).forEach(function(k){clearInterval(_T[k]);delete _T[k];});};
  window.initLiveCards=function(games){
    stopAllLivePolling();
    (games||[]).forEach(function(g){
      if(g.status!=='Live')return;
      var el=document.getElementById('ls-'+g.gamePk);
      if(!el)return;
      el.className='ls-wrap';
      el.innerHTML='<div class="ls-loading">\u25cf FETCHING LIVE DATA\u2026</div>';
      doFetch(g.gamePk,g.awayAbbr,g.homeAbbr);
      if(!_T[g.gamePk])_T[g.gamePk]=setInterval(function(){
        if(!document.getElementById('ls-'+g.gamePk)){clearInterval(_T[g.gamePk]);delete _T[g.gamePk];return;}
        doFetch(g.gamePk,g.awayAbbr,g.homeAbbr);
      },30000);
    });
  };
})();
</script>
<!-- END LIVE GAMEPLAY PATCH -->"""

if '</body>' in dash:
    dash = dash.replace('</body>', LIVE_BLOCK + '\n</body>', 1)
    changes.append("FIX-5 OK  injected live CSS+JS before </body>")
elif '</html>' in dash:
    dash = dash.replace('</html>', LIVE_BLOCK + '\n</html>', 1)
    changes.append("FIX-5 OK  injected live CSS+JS before </html>")
else:
    dash += '\n' + LIVE_BLOCK
    changes.append("FIX-5 OK  appended live CSS+JS")

# ── 6. Add /api/game/livedata route to app.py if missing ─────────────────────
if '/api/game/livedata/' in app:
    changes.append("APP SKIP  /api/game/livedata already in app.py")
else:
    ROUTE = '''

# ── Live Game Data Route (added by fix_dashboard.py) ───────────────────────
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
                    pip = st.get("inningsPitched", "x2014")
                    per = st.get("earnedRuns", "x2014")
            if bid:
                bs  = pl.get("ID{}".format(bid), {})
                bst = bs.get("stats", {}).get("batting", {})
                bss = bs.get("seasonStats", {}).get("batting", {})
                if bst: bab = bst.get("atBats", 0); bah = bst.get("hits", 0)
                if bss: baops = bss.get("ops", "x2014")

        return jsonify({
            "success": True, "gamePk": game_pk, "statusDetail": sd,
            "inningLabel": ilbl,
            "balls": ls.get("balls",0), "strikes": ls.get("strikes",0), "outs": ls.get("outs",0),
            "awayRuns": at.get("runs",0), "awayHits": at.get("hits",0), "awayErrors": at.get("errors",0),
            "homeRuns": ht.get("runs",0), "homeHits": ht.get("hits",0), "homeErrors": ht.get("errors",0),
            "bases": bases,
            "pitcher": {"name": pname, "ip": pip, "er": per},
            "batter":  {"name": bname, "ab": bab, "h": bah, "ops": baops},
            "dueUp": [od, ih],
        })
    except Exception as ex:
        print("[api_game_livedata]", ex)
        return jsonify({"success": False, "error": str(ex)}), 500
'''
    # Fix placeholder chars
    ROUTE = ROUTE.replace("x2014", "\u2014")
    app += ROUTE
    changes.append("APP OK  added /api/game/livedata route to app.py")

# ── 7. Write files ────────────────────────────────────────────────────────────
shutil.copy(DASH, DASH.with_suffix(".html.bak"))
DASH.write_text(dash, encoding="utf-8")

shutil.copy(APP, APP.with_suffix(".py.bak"))
APP.write_text(app, encoding="utf-8")

print()
print("=" * 52)
print("  MLB DASHBOARD FIX COMPLETE")
print("=" * 52)
for c in changes:
    print("  " + c)
print()
print("  dashboard.html  {:>7,} -> {:>7,} chars".format(orig_dash_len, len(dash)))
print("  Backups: dashboard.html.bak  app.py.bak")
print()
print("  Push to GitHub -> Render redeploys automatically")
print("=" * 52)
