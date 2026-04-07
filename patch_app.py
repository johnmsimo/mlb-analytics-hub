#!/usr/bin/env python3
"""
patch_app.py — Run this in the same folder as app.py
Applies 4 weather fixes + removes any broken _deg_to_compass fragment.
Usage: python patch_app.py
"""
import re, sys, shutil
from datetime import datetime

SRC = "app.py"
BACKUP = f"app.py.bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}"

with open(SRC, "r") as f:
    src = f.read()

shutil.copy(SRC, BACKUP)
print(f"Backup saved: {BACKUP}")

# Step 1: Remove any broken _deg_to_compass that may have been injected
src = re.sub(
    r'\n[ \t]*def _deg_to_compass\(.*?\n(?:[ \t]+.*\n)*',
    '\n',
    src
)
print("Step 1: Cleaned any previous broken _deg_to_compass")

# Step 2: Inject clean _deg_to_compass at module level right before get_weather
COMPASS = (
    "\ndef _deg_to_compass(deg):\n"
    "    if deg is None:\n"
    "        return \"\"\n"
    "    dirs = [\"N\",\"NNE\",\"NE\",\"ENE\",\"E\",\"ESE\",\"SE\",\"SSE\",\n"
    "            \"S\",\"SSW\",\"SW\",\"WSW\",\"W\",\"WNW\",\"NW\",\"NNW\"]\n"
    "    return dirs[int((float(deg) + 11.25) / 22.5) % 16]\n"
    "\n"
)

if "def _deg_to_compass(" not in src:
    src = src.replace("def get_weather(", COMPASS + "def get_weather(", 1)
    print("Step 2: _deg_to_compass injected at module level")
else:
    print("Step 2: _deg_to_compass already present")

# Step 3: Add winddirection_10m to open-meteo params
OLD_HOURLY = '"hourly":"temperature_2m,precipitation_probability,windspeed_10m,weathercode"'
NEW_HOURLY = '"hourly":"temperature_2m,precipitation_probability,windspeed_10m,winddirection_10m,weathercode"'
if OLD_HOURLY in src:
    src = src.replace(OLD_HOURLY, NEW_HOURLY, 1)
    print("Step 3: winddirection_10m added to hourly params")
elif "winddirection_10m" in src:
    print("Step 3: winddirection_10m already in params")
else:
    print("WARNING Step 3: Could not find hourly param string - check manually")

# Step 4: Fix timezone from 'auto' to 'America/New_York'
OLD_TZ = '"forecast_days":2,"timezone":"auto"'
NEW_TZ  = '"forecast_days":2,"timezone":"America/New_York"'
if OLD_TZ in src:
    src = src.replace(OLD_TZ, NEW_TZ, 1)
    print("Step 4: timezone fixed to America/New_York")
elif '"timezone":"America/New_York"' in src:
    print("Step 4: timezone already correct")
else:
    print("WARNING Step 4: Could not find timezone param - check manually")

# Step 5: Extend the return dict with wind_dir and wind_str
if "wind_str" not in src:
    OLD_WIND_LINES = (
        '        temps = h.get("temperature_2m",[70]*48)\n'
        '        precip = h.get("precipitation_probability",[0]*48)\n'
        '        wind = h.get("windspeed_10m",[0]*48)\n'
        '        wcodes = h.get("weathercode",[0]*48)\n'
        '        wcode = wcodes[idx] if idx < len(wcodes) else 0\n'
        '        return {\n'
        '            "temp": round(temps[idx]) if idx < len(temps) else "N/A",\n'
        '            "rain_chance":precip[idx] if idx < len(precip) else "N/A",\n'
        '            "wind_speed": round(wind[idx]) if idx < len(wind) else "N/A",\n'
        '            "condition": wcode_map.get(wcode, "Clear"),\n'
        '        }'
    )
    NEW_WIND_LINES = (
        '        temps  = h.get("temperature_2m",[70]*48)\n'
        '        precip = h.get("precipitation_probability",[0]*48)\n'
        '        wind   = h.get("windspeed_10m",[0]*48)\n'
        '        wdir   = h.get("winddirection_10m",[None]*48)\n'
        '        wcodes = h.get("weathercode",[0]*48)\n'
        '        wcode  = wcodes[idx] if idx < len(wcodes) else 0\n'
        '        wind_spd = round(wind[idx]) if idx < len(wind) else "N/A"\n'
        '        wind_deg = wdir[idx] if idx < len(wdir) else None\n'
        '        compass  = _deg_to_compass(wind_deg) if wind_deg is not None else ""\n'
        '        wind_str = f"{wind_spd} mph {compass}".strip() if compass else f"{wind_spd} mph"\n'
        '        return {\n'
        '            "temp":        round(temps[idx]) if idx < len(temps) else "N/A",\n'
        '            "rain_chance": precip[idx] if idx < len(precip) else "N/A",\n'
        '            "wind_speed":  wind_spd,\n'
        '            "wind_dir":    compass,\n'
        '            "wind_str":    wind_str,\n'
        '            "condition":   wcode_map.get(wcode, "Clear"),\n'
        '        }'
    )
    if OLD_WIND_LINES in src:
        src = src.replace(OLD_WIND_LINES, NEW_WIND_LINES, 1)
        print("Step 5: wind_dir / wind_str added to return dict")
    else:
        print("WARNING Step 5: Could not match return block exactly - check manually")
else:
    print("Step 5: wind_str already in return dict")

# Write patched file
with open(SRC, "w") as f:
    f.write(src)

# Quick syntax check
import py_compile
try:
    py_compile.compile(SRC, doraise=True)
    print("\nSyntax OK - no IndentationError or SyntaxError found")
except py_compile.PyCompileError as e:
    print(f"\nSyntax error after patch: {e}")
    print("Restoring backup...")
    shutil.copy(BACKUP, SRC)
    print("Backup restored. Check your app.py manually.")
    sys.exit(1)

print("\nAll done. Commit app.py and deploy to Render.")
