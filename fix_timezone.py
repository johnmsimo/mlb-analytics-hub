#!/usr/bin/env python3
"""
Timezone fix patcher for mlb-analytics-hub/app.py
Fixes: server running in UTC causes Monday games to load on Sunday evenings EDT

Run from the repo root: python3 fix_timezone.py
"""
import shutil, os

src = "app.py"
backup = "app.py.bak"

if not os.path.exists(src):
    print("ERROR: app.py not found. Run this from the mlb-analytics-hub repo root.")
    exit(1)

shutil.copy(src, backup)
print(f"Backup saved -> {backup}")

with open(src, "r") as f:
    code = f.read()

original_utc_count = code.count("timezone.utc")

# FIX 1: Date string generation (the main culprit)
# These generate YYYY-MM-DD strings for MLB API schedule calls.
# At 8pm EDT the UTC clock is already midnight = next day.
code = code.replace(
    "datetime.now(timezone.utc).strftime('%Y-%m-%d')",
    "datetime.now(ET).strftime('%Y-%m-%d')"
)
code = code.replace(
    'datetime.now(timezone.utc).strftime("%Y-%m-%d")',
    'datetime.now(ET).strftime("%Y-%m-%d")'
)

# FIX 2: Cache freshness date comparisons
# These check if today's date matches the cache date.
# Must also use ET to stay consistent with schedule fetches.
code = code.replace(
    "datetime.now(timezone.utc).date()",
    "datetime.now(ET).date()"
)

fixed = original_utc_count - code.count("timezone.utc")
print(f"Fixed {fixed} timezone.utc -> ET occurrences")
print(f"Remaining timezone.utc (non-date logic, OK to keep): {code.count('timezone.utc')}")

with open(src, "w") as f:
    f.write(code)

print("\nSuccess! app.py is now timezone-aware (America/New_York).")
print("Redeploy to Render to apply the fix.")
