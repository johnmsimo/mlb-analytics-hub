#!/usr/bin/env python3
"""
fix_timezone.py — run by Render build command after pip install

Fixes the UTC rollover bug: at 8 PM ET the server thinks it's
already tomorrow (UTC), so Today's Games shows tomorrow's schedule.

PATCHES app.py in-place:
  datetime.now(timezone.utc)  →  datetime.now(ET)
  datetime.now().strftime     →  datetime.now(ET).strftime
  datetime.utcnow()           →  datetime.now(ET)
  date.today()                →  datetime.now(ET).date()

Also ensures `from zoneinfo import ZoneInfo` and ET = ZoneInfo("America/New_York")
are present at the top of app.py.
"""
import pathlib, re, sys, shutil

APP = pathlib.Path("app.py")
if not APP.exists():
    print("[fix_timezone] app.py not found — skipping")
    sys.exit(0)

src = APP.read_text(encoding="utf-8")

# Skip if already tiny placeholder
if len(src.strip()) < 200:
    print("[fix_timezone] app.py appears to be a placeholder — skipping")
    sys.exit(0)

orig = src
changes = []

# ── 1. Ensure zoneinfo import ──────────────────────────────────────────────
if "from zoneinfo import ZoneInfo" not in src:
    # Insert after the last `from datetime` or `import datetime` line
    lines = src.splitlines(keepends=True)
    insert_at = 0
    for i, line in enumerate(lines):
        if re.match(r'(from datetime|import datetime)', line.strip()):
            insert_at = i + 1
    if insert_at == 0:
        # Fall back to after first import block
        for i, line in enumerate(lines):
            if line.strip().startswith("import ") or line.strip().startswith("from "):
                insert_at = i + 1
    lines.insert(insert_at, "from zoneinfo import ZoneInfo\n")
    src = "".join(lines)
    changes.append("Added: from zoneinfo import ZoneInfo")

# ── 2. Ensure ET variable is defined ─────────────────────────────────────
if 'ET = ZoneInfo("America/New_York")' not in src and "ET=ZoneInfo" not in src:
    # Insert after the ZoneInfo import line
    src = src.replace(
        "from zoneinfo import ZoneInfo\n",
        'from zoneinfo import ZoneInfo\nET = ZoneInfo("America/New_York")\n',
        1
    )
    changes.append('Added: ET = ZoneInfo("America/New_York")')

# ── 3. Replace UTC date calls ──────────────────────────────────────────────
# Pattern: datetime.now(timezone.utc).strftime  →  datetime.now(ET).strftime
before = src
src = re.sub(
    r'datetime\.now\(timezone\.utc\)',
    'datetime.now(ET)',
    src
)
if src != before:
    n = len(re.findall(r'datetime\.now\(timezone\.utc\)', before))
    changes.append(f"Replaced {n}x datetime.now(timezone.utc) → datetime.now(ET)")

# Pattern: datetime.now().strftime  →  datetime.now(ET).strftime
before = src
src = re.sub(
    r'datetime\.now\(\)\.strftime',
    'datetime.now(ET).strftime',
    src
)
if src != before:
    n = len(re.findall(r'datetime\.now\(\)\.strftime', before))
    changes.append(f"Replaced {n}x datetime.now().strftime → datetime.now(ET).strftime")

# Pattern: datetime.now().date()  →  datetime.now(ET).date()
before = src
src = re.sub(
    r'datetime\.now\(\)\.date\(\)',
    'datetime.now(ET).date()',
    src
)
if src != before:
    n = len(re.findall(r'datetime\.now\(\)\.date\(\)', before))
    changes.append(f"Replaced {n}x datetime.now().date() → datetime.now(ET).date()")

# Pattern: datetime.utcnow()  →  datetime.now(ET)
before = src
src = re.sub(
    r'datetime\.utcnow\(\)',
    'datetime.now(ET)',
    src
)
if src != before:
    n = len(re.findall(r'datetime\.utcnow\(\)', before))
    changes.append(f"Replaced {n}x datetime.utcnow() → datetime.now(ET)")

# Pattern: date.today()  →  datetime.now(ET).date()
before = src
src = re.sub(
    r'(?<![\w.])date\.today\(\)',
    'datetime.now(ET).date()',
    src
)
if src != before:
    n = len(re.findall(r'(?<![\w.])date\.today\(\)', before))
    changes.append(f"Replaced {n}x date.today() → datetime.now(ET).date()")

# ── 4. Write if changed ───────────────────────────────────────────────────
if src != orig:
    shutil.copy(APP, APP.with_suffix(".py.tz_bak"))
    APP.write_text(src, encoding="utf-8")
    print("[fix_timezone] PATCHED app.py:")
    for c in changes:
        print(f"  ✓ {c}")
else:
    print("[fix_timezone] No UTC date calls found — app.py already uses ET or no changes needed")

print("[fix_timezone] Done.")
