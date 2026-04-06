# MLB Analytics Hub — Timezone Fix

## Problem
The Render server runs in UTC. At 8:00 PM EDT, the UTC clock is already 
12:00 AM the next day (Monday), so your app fetches Monday's schedule instead
of Sunday's.

## Root Cause
Multiple routes in `app.py` use:
```python
datetime.now(timezone.utc).strftime("%Y-%m-%d")  # ❌ UTC = wrong date after 8pm EDT
```

## Fix Applied
All date-string generation and cache-freshness checks now use Eastern Time:
```python
datetime.now(ET).strftime("%Y-%m-%d")  # ✅ Always returns correct game date
```
`ET = ZoneInfo("America/New_York")` is already defined at the top of `app.py`.

## How to Apply

### Option A — Run the patch script locally:
```bash
cd your-mlb-analytics-hub-folder
python3 fix_timezone.py
git add app.py
git commit -m "fix: use Eastern Time for date string generation"
git push
```

### Option B — Manual edits in app.py:
Find & replace ALL occurrences of:
- `datetime.now(timezone.utc).strftime('%Y-%m-%d')` → `datetime.now(ET).strftime('%Y-%m-%d')`
- `datetime.now(timezone.utc).strftime("%Y-%m-%d")` → `datetime.now(ET).strftime("%Y-%m-%d")`
- `datetime.now(timezone.utc).date()` → `datetime.now(ET).date()`

## Affected Routes
| Route | Fix |
|---|---|
| `/api/games/today` | Date fallback for schedule fetch |
| `/api/pitchers/<game_pk>` | Schedule lookup date |
| `/api/simulate/<game_pk>` | Schedule lookup date |
| `/api/game-projection/<game_pk>` | Schedule lookup date |
| `/api/market/<game_pk>` | Schedule lookup date |
| `_load_injury_data()` | Transaction date window |
| `_maybe_refresh_fg()` | Cache freshness check |
| `_maybe_refresh_savant()` | Cache freshness check |
| `_maybe_refresh_injuries()` | Cache freshness check |
