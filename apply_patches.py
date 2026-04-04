#!/usr/bin/env python3
"""
MLB Analytics Hub — Player Modal Patch Installer
Run this ONCE in your project root (where app.py and dashboard.html live):
    python apply_patches.py
"""
import os, sys, pathlib, shutil

HERE = pathlib.Path(__file__).parent

APP_PY       = HERE / "app.py"
DASH_HTML    = HERE / "dashboard.html"
APP_PATCH    = HERE / "app_patch.py"
DASH_PATCH   = HERE / "dashboard_patch.html"

if not APP_PY.exists():
    sys.exit("ERROR: app.py not found in current directory")
if not DASH_HTML.exists():
    sys.exit("ERROR: dashboard.html not found in current directory")
if not APP_PATCH.exists():
    sys.exit("ERROR: app_patch.py not found — keep all patch files together")
if not DASH_PATCH.exists():
    sys.exit("ERROR: dashboard_patch.html not found — keep all patch files together")

APP_MARKER  = "# ── Player Profile Route (v2"
DASH_MARKER = "<!-- ═══════════════════════════════════════════════════════"

# ── Patch app.py ──────────────────────────────────────────────────
app_src   = APP_PY.read_text(encoding="utf-8")
app_patch = APP_PATCH.read_text(encoding="utf-8")

if APP_MARKER in app_src:
    idx = app_src.index(APP_MARKER)
    app_new = app_src[:idx] + app_patch.lstrip("\n")
    print("[app.py]  Replacing existing player-profile route block")
else:
    app_new = app_src + "\n" + app_patch
    print("[app.py]  Appending new player-profile route block")

shutil.copy(APP_PY, APP_PY.with_suffix(".py.bak"))
APP_PY.write_text(app_new, encoding="utf-8")
print(f"[app.py]  Done — {len(app_new):,} chars written  (backup: app.py.bak)")

# ── Patch dashboard.html ──────────────────────────────────────────
dash_src   = DASH_HTML.read_text(encoding="utf-8")
dash_patch = DASH_PATCH.read_text(encoding="utf-8")

if DASH_MARKER in dash_src:
    idx = dash_src.index(DASH_MARKER)
    dash_new = dash_src[:idx] + dash_patch
    print("[dashboard.html]  Replacing existing modal block")
else:
    tag = "</body>"
    if tag in dash_src.lower():
        idx = dash_src.lower().rfind(tag)
        dash_new = dash_src[:idx] + dash_patch + "\n" + dash_src[idx:]
        print("[dashboard.html]  Injecting modal before </body>")
    else:
        dash_new = dash_src + "\n" + dash_patch
        print("[dashboard.html]  Appending modal at end-of-file")

shutil.copy(DASH_HTML, DASH_HTML.with_suffix(".html.bak"))
DASH_HTML.write_text(dash_new, encoding="utf-8")
print(f"[dashboard.html]  Done — {len(dash_new):,} chars written  (backup: dashboard.html.bak)")

print()
print("✅  Patch complete.  Restart your Render service (or gunicorn) to pick up the changes.")
print()
print("   New endpoint added:  GET /api/player/<player_id>")
print("   Modal tabs:          OVERVIEW · GAME LOG · L/R SPLITS · ARSENAL · 🤖 AI SCOUT")
print("   Trigger:             Click any player card in the TEAMS tab")
