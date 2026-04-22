# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MLB Analytics Hub is a single-file Flask application (`app.py`, ~12,000 lines) that serves an MLB prop-betting research and tracking platform. The backend exposes a large JSON API consumed by several standalone HTML pages (each a self-contained SPA loaded as a static string at startup). It deploys to Render via `gunicorn_conf.py`.

## Running Locally

```bash
pip install -r requirements.txt
python app.py          # dev server on port 10000 (or $PORT)
```

Production start command (mirrors Render):
```bash
gunicorn app:app -c gunicorn_conf.py
```

**Required env vars:**
- `ODDS_API_KEY` — The Odds API key (https://the-odds-api.com). Many `/api/props/*` routes degrade gracefully when absent.
- `ANTHROPIC_API_KEY` — Used by `_claude_matchup_insights()` (game projections) and `/api/ai-boxscore/<game_pk>`. Falls back to `_local_boxscore_projections()` when absent.
- `PYBASEBALL_CACHE=1` — Prevents pybaseball from hammering FanGraphs on every import (set in render.yaml, set locally too).

Optional TTL tuning (Odds API credit conservation):
- `ODDS_EVENTS_TTL_SEC` (default 21600), `ODDS_GAME_TTL_SEC` (default 86400), `ODDS_NRFI_TTL_SEC` (default 300)

## Architecture

### Single-file backend (`app.py`)

There are no modules — everything lives in `app.py`. The file is organized in roughly this order:

1. **Global constants** — `MLB_API`, `WX_API`, `STADIUM_COORDS`, `DOME_VENUES`, `PARK_FACTORS`
2. **In-memory stat caches** (loaded once at boot via background threads):
   - `_fg_bat` / `_fg_pit` — FanGraphs-compatible stats derived from MLB Stats API bulk endpoint (`/stats?playerPool=all`). Loaded by `_load_fg_data_from_mlb_api()`. Access via `fg_batter(name)` / `fg_pitcher(name)`, which use fuzzy name matching.
   - `_sv_*` caches — Baseball Savant CSVs (xERA, xwOBA, Statcast EV, pitch arsenal %). Loaded by `_load_savant_data()`. Access via `sv_pitcher(name)` / `sv_batter(name)`.
   - Both caches refresh daily; `_maybe_refresh_fg()` / `_maybe_refresh_savant()` are called at the top of most stat-heavy routes.
3. **Secondary caches** — injury status, team pitching rankings, cheatsheet data, Monte Carlo results, umpire data — each with its own lock and TTL.
4. **Odds API layer** — `_find_odds_event()`, `_load_event_odds()`, `_parse_prop_markets()`. In-memory + file-persisted cache in `data/odds_cache.json`. Markets fetched: H2H, spreads, totals, NRFI, all batter/pitcher props.
5. **Flask routes** — grouped by feature area (see below).
6. **Tracker system** — picks stored in `data/daily_tracker.json` keyed by date → list of pick dicts. Adjustments/settings in `data/model_adjustments.json`. Calibration history in `data/calibration_history.json`.

### Pages and their routes

| HTML file | Route | Purpose |
|-----------|-------|---------|
| `dashboard.html` | `/` | Today's games, quick props, game cards |
| `deepdive.html` | `/deep-dive/<game_pk>` | Full game breakdown: lineups, weather, BvP, props |
| `props.html` | `/props` | Props research board with Monte Carlo ratings |
| `tracker.html` | `/tracker` | Bet slip, performance analytics, Kelly staking |
| `consistency.html` | `/consistency` | Player hit-rate over windows |
| `pitcher_deepdive.html` | `/pitcher-deep-dive[/<pitcher_id>]` | Pitcher arsenal, zone chart, matchup vulnerability |
| `gameside_deepdive.html` | `/gameside-deepdive/<game_pk>` | F5, bullpen fatigue, lineup status |
| `breakout_detector.html` | `/breakout-detector` | Statcast breakout signal scoring |
| `cheatsheet.html` | `/cheatsheets` | Daily top prop targets |

Each HTML file is read into a module-level string at startup (`_read_html_or_fallback()`). Routes simply return these strings. All data fetching is done client-side via `/api/*` fetch calls.

### Key API route groups

- `/api/games/today` — today's schedule (hydrated from MLB Stats API)
- `/api/game/<game_pk>` — game detail + weather + park factor
- `/api/game-projection/<game_pk>` — Claude or local projection
- `/api/props/projections/<game_pk>` — per-batter prop projections with hub ratings
- `/api/props/scan/today` — cross-game MC grades for all props
- `/api/projections/monte-carlo` — full MC slate with per-game top props
- `/api/tracker/*` — full CRUD for picks, settings, performance, bankroll, attribution
- `/api/cheatsheets/today` — daily cheatsheet (cached, async refresh)
- `/api/breakout/candidates` — Statcast breakout scores
- `/api/umpire/<game_pk>` — HP umpire stats
- `/api/bullpen/fatigue/<game_pk>` — bullpen workload
- `/api/f5/<game_pk>` — first-5-innings model
- `/api/status` — health check (Render uses this; responds immediately even during cold boot)

### Prop projection model

`_project_batter()` and `_project_pitcher()` combine FanGraphs season stats, Savant xStats, park factor, weather, and BvP history to produce `modelMean` and `adjProb` for each market. `_matchup_score()` computes a 0–100 score. Hub Rating is derived from edge vs. the book's implied probability. `_platoon_blend()` adjusts hitting stats by pitcher handedness.

### Claude AI integration

Two call sites, both with graceful fallback:
1. `_claude_matchup_insights()` — 3–5 storyline bullets for `/api/game-projection/<game_pk>`
2. `/api/ai-boxscore/<game_pk>` — full projected boxscore; falls back to `_local_boxscore_projections()` when `ANTHROPIC_API_KEY` is absent

Both use `claude-sonnet-4-20250514` and request raw JSON output (no markdown).

### Data persistence

All persistent state lives in `data/` (git-ignored):
- `daily_tracker.json` — picks keyed by date string
- `model_adjustments.json` — Kelly settings, market multipliers, bankroll
- `calibration_history.json` — historical calibration snapshots
- `value_history.json` — CLV/value series
- `odds_cache.json` — today's Odds API snapshot (restored on boot to avoid redundant API calls)

### Gunicorn / memory constraints

Single worker (`workers=1`) is intentional — the Savant + FG caches are ~150 MB and two workers would OOM the Render instance. `preload_app=False` is critical: preloading causes background daemon threads to not survive the fork, leaving caches permanently empty. Do not change either setting.

## Tracker Schema

The tracker entry schema is partially implemented (see `TRACKER_SCHEMA_REVIEW.md`). Phase 1 critical fields (`id`, `savedAt`, `source`, `stakeDollars`, `gradedAt`) were the highest-priority gaps as of the last review. The deduplication key is `(date, gamePk, player, marketKey, line)` — used when `id` is absent.

## Name Matching

Player names are normalized to lowercase and matched with `difflib.get_close_matches(cutoff=0.78)` in `_fuzzy_lookup()`. Savant uses `"Last, First"` format; `_sv_key()` converts to `"First Last"` for consistent lookups. When adding new stat sources, follow the same convention.
