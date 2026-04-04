#!/usr/bin/env python3
"""
MLB Analytics Hub — Live Gameplay Patch Installer
Run this ONCE in your project root:
    python apply_live_patches.py
"""
import os, sys, re, pathlib, shutil

HERE = pathlib.Path(__file__).parent
APP_PY     = HERE / "app.py"
DASH_HTML  = HERE / "dashboard.html"
APP_PATCH  = HERE / "app_live_patch.py"
DASH_PATCH = HERE / "dashboard_live_patch.html"

for p in [APP_PY, DASH_HTML, APP_PATCH, DASH_PATCH]:
    if not p.exists():
        sys.exit(f"ERROR: {p.name} not found in current directory")

APP_MARKER  = "# ── Live Game Data Route ──"
DASH_MARKER = "<!-- ═══════════════════════════════════════════════════════\n     LIVE GAMEPLAY PATCH"

# ── 1. Patch app.py ──────────────────────────────────────────────────────────
app_src   = APP_PY.read_text(encoding="utf-8")
app_patch = APP_PATCH.read_text(encoding="utf-8")

if APP_MARKER in app_src:
    idx = app_src.index(APP_MARKER)
    app_new = app_src[:idx] + app_patch.lstrip("\n")
    print("[app.py]  Replacing existing live-data route")
else:
    app_new = app_src + "\n" + app_patch
    print("[app.py]  Appending live-data route")

# ── 2. Patch mkCard to add live slot + wire initLiveCards call ───────────────
# We need to:
#   a) insert  <div id="ls-${g.gamePk}"></div>  inside Live cards in mkCard()
#   b) call    initLiveCards(gs)  after cards render

# a) Add live-score slot to mkCard — insert after the .mu team logos block
# We inject: `g.status==='Live' ? '<div id="ls-'+g.gamePk+'"></div>' : ''`
# Just before the `div class=pr` pitcher block.
if "ls-${g.gamePk}" not in app_new.replace("app_new", ""):
    pass  # handled in HTML side

# ── 3. Patch dashboard.html ──────────────────────────────────────────────────
dash_src   = DASH_HTML.read_text(encoding="utf-8")
dash_patch = DASH_PATCH.read_text(encoding="utf-8")

if DASH_MARKER in dash_src:
    idx = dash_src.index(DASH_MARKER)
    dash_new = dash_src[:idx] + dash_patch
    print("[dashboard.html]  Replacing existing live-gameplay block")
else:
    # Inject before </body>
    tag = "</body>"
    idx = dash_src.lower().rfind(tag)
    if idx >= 0:
        dash_new = dash_src[:idx] + dash_patch + "\n" + dash_src[idx:]
        print("[dashboard.html]  Injecting live-gameplay block before </body>")
    else:
        dash_new = dash_src + "\n" + dash_patch
        print("[dashboard.html]  Appending live-gameplay block")

# ── 4. Patch mkCard() to add the live-score slot ─────────────────────────────
# We look for the .pr pitcher block in mkCard and add the ls slot before it
LS_SLOT = "g.status==='Live'?`<div id=\"ls-${g.gamePk}\" class=\"ls-placeholder\"></div>`:``"
LS_SLOT_MARKER = "ls-placeholder"

if LS_SLOT_MARKER not in dash_new:
    # Find the .pr pitcher row in the mkCard template literal
    # The pattern we're looking for is the var pr = ... line just before the pitchers
    # We'll inject just before `<div class=\\"pr\\">`
    insert_after = 'var hW='
    idx2 = dash_new.find(insert_after)
    if idx2 < 0:
        insert_after = "var hW ="
        idx2 = dash_new.find(insert_after)

    if idx2 >= 0:
        # Go to end of that line
        eol = dash_new.find('\n', idx2)
        if eol < 0: eol = len(dash_new)
        # Find the next return div class=card in mkCard
        card_open = dash_new.find('return `<div class=\\"card\\"', idx2)
        if card_open < 0:
            card_open = dash_new.find("return `<div class='card'", idx2)
        if card_open < 0:
            card_open = dash_new.find('return `<div class="card"', idx2)
        # Look for the pitcher row .pr after the mu block
        pr_start = dash_new.find('<div class=\\"pr\\">', idx2)
        if pr_start < 0:
            pr_start = dash_new.find("<div class='pr'>", idx2)
        if pr_start < 0:
            pr_start = dash_new.find('<div class="pr">', idx2)

        if pr_start > 0:
            insert_txt = "\n${g.status==='Live'?`<div id=\"ls-${g.gamePk}\"></div>`:``}"
            dash_new = dash_new[:pr_start] + insert_txt + dash_new[pr_start:]
            print("[dashboard.html]  Injected live-score slot into mkCard()")
        else:
            print("[dashboard.html]  WARNING: Could not locate .pr block in mkCard — manual injection needed")
    else:
        print("[dashboard.html]  WARNING: Could not find mkCard insertion point — patch JS will still load")

# ── 5. Wire initLiveCards after cards render ─────────────────────────────────
WIRE_MARKER = "initLiveCards(gs)"
if WIRE_MARKER not in dash_new:
    # Find the place where lineup fetches are kicked off after gc.innerHTML is set
    target = "gs.forEach(function(g){fetch(`/api/lineup/"
    if target not in dash_new:
        target = "gs.forEach(g=>"
    if target not in dash_new:
        target = "gs.forEach(function(g)"

    idx3 = dash_new.rfind(target)
    if idx3 > 0:
        dash_new = dash_new[:idx3] + "if(window.initLiveCards)initLiveCards(gs);\n" + dash_new[idx3:]
        print("[dashboard.html]  Wired initLiveCards(gs) call")
    else:
        print("[dashboard.html]  WARNING: Could not wire initLiveCards — call it manually after gs.forEach")

# ── 6. Write output ──────────────────────────────────────────────────────────
shutil.copy(APP_PY,   APP_PY.with_suffix(".py.bak"))
APP_PY.write_text(app_new, encoding="utf-8")
print(f"[app.py]  Done — {len(app_new):,} chars  (backup: app.py.bak)")

shutil.copy(DASH_HTML, DASH_HTML.with_suffix(".html.bak"))
DASH_HTML.write_text(dash_new, encoding="utf-8")
print(f"[dashboard.html]  Done — {len(dash_new):,} chars  (backup: dashboard.html.bak)")

print()
print("✅  Live gameplay patch complete!")
print()
print("   New endpoint:  GET /api/game/livedata/<game_pk>")
print("   What it adds:  Live inning/score/R-H-E/bases/count/pitcher/batter panel")
print("   Refresh rate:  Every 30 seconds (auto-polls for all Live games)")
print()
print("   Restart your Render service to activate the new endpoint.")
