# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MLB Analytics Hub is a Flask application that serves an MLB prop-betting research and tracking platform. The backend exposes a large JSON API (~120 routes) consumed by ~15 standalone HTML pages (each a self-contained SPA loaded as a static string at startup). It deploys to **Fly.io** (primary region `EWR`) via `gunicorn_conf.py` + `fly.toml`. The repo also includes a CI workflow at `.github/workflows/deploy.yml` that runs `fly deploy --remote-only` on every push to `main`.

`app.py` is the monolith (~22.7k lines) but several concerns have been extracted into sibling modules — when working on them, edit the extracted file, not `app.py`.

## Running Locally

```bash
pip install -r requirements.txt   # Python 3.11.9 (see .python-version)
python app.py                     # dev server on $PORT or 10000
```

Production start command (mirrors Fly.io):
```bash
gunicorn app:app -c gunicorn_conf.py
```

Container build (used by Fly.io / `Dockerfile`): a multi-stage Python 3.11-slim image that bundles `redis-server` and starts it before the app, so `REDIS_URL=redis://localhost:6379` works out of the box. Override `REDIS_URL` to point at Upstash / Redis Cloud in production.

### Environment variables

**LLM / GCP (single provider — Anthropic was removed; see `app.py:116-148`):**
- `GOOGLE_CLOUD_API_KEY` (or `GEMINI_API_KEY`) — Gemini Developer API key used for every "AI" surface (game projections, AI boxscore, HR scouting, insights). Without it, all AI routes fall back to deterministic templates.
- `GOOGLE_CLOUD_PROJECT` — enables BigQuery (`bq_etl.py`, `/api/matchup`, `/api/cheatsheet`, `/api/insights`, `/api/projected-boxscore`). Requires ADC (`GOOGLE_APPLICATION_CREDENTIALS`); routes fall back to CSV when ADC is missing.
- `VERTEX_MODEL_FAST` / `VERTEX_MODEL_PRO` / `VERTEX_MODEL_NARRATIVE` / `VERTEX_MODEL_PREMIUM` — Gemini model overrides per tier (defaults: `gemini-2.5-flash-lite` for fast, `gemini-2.5-pro` for the rest).
- `BQ_DATASET` (default `mlb`), `BQ_BATTERS`, `BQ_PITCHERS`, `BQ_BVP_TABLE`, `BQ_SLATE_VIEW` — BigQuery table overrides.

**Odds API:**
- `ODDS_API_KEY` — The Odds API key. Routes degrade gracefully when absent.
- `ODDS_REGION` (default `us`).
- TTL tuning: `ODDS_EVENTS_TTL_SEC` (default 21600), `ODDS_GAME_TTL_SEC` (default 86400), `ODDS_NRFI_TTL_SEC` (default 300).

**Infra / operational:**
- `REDIS_URL` — Redis connection string. Absent → `redis_client.py` uses an in-memory dict with TTL semantics (same public API).
- `ADMIN_TOKEN` — when set, gates `/api/cache/warm`, `/api/pipeline/run`, brain-data routes, and other admin endpoints. Pass via `Authorization: Bearer <token>` or `X-Admin-Token: <token>`.
- `PYBASEBALL_CACHE=1` — set in `fly.toml`; prevents pybaseball from hammering FanGraphs on import.
- `XGB_MODEL_DIR` — override the `models/` path for the XGBoost artifacts.
- `DATA_DIR` — override the `data/` path (Fly.io mounts a persistent volume here at `/app/data`).
- `PORT` — bind port (Fly.io sets 8080; local default 10000).

**Tracker / capture knobs:** `TRACKER_AUTO_SYNC_ENABLED`, `TRACKER_AUTO_SYNC_MINUTES`, `TRACKER_CAPTURE_BUDGET_SEC`, `TRACKER_CAPTURE_INCLUDE_ODDS`, `TRACKER_CAPTURE_BACKGROUND`, `TRACKER_SIMS`.

**MLB memory store knobs:** `MLB_MEMORY_KEEP_SNAPSHOTS`, `MLB_MEMORY_MAX_BYTES`.

A `.env` file in the repo root is auto-loaded by `_load_local_env_file()` at boot (`app.py:14`).

## Architecture

### Module layout

```
app.py                          ~22.7k LOC Flask app — routes, caches, projections
├── fangraphs_loader.py         CSV-backed FG stats (2021–2026 + Steamer projections)
├── pipeline_scheduler.py       Background matchup pipeline (runs at 9:00 ET daily)
├── pipeline_routes.py          /api/pipeline/* Blueprint (status/games/matchup/run)
├── brain_merge_patch.py        "Brain" overlay system — merges user-uploaded CSVs into FG/Savant caches
├── xgb_prop_scorer.py          Loads models/*.pkl; exposes xgb_hit_prob / xgb_k_prob / xgb_hr_prob / xgb_tb_prob / xgb_rbi_prob
├── stacked_calibrator.py       Meta-learner that blends XGB + BATX into one probability + 95% CI + verdict tier
├── xgb_prop_pipeline.py        Full XGB training pipeline (Statcast+FG features → calibrated isotonic)
├── xgb_training_pipeline.py    Legacy training script (uses pickle; do NOT use for production)
├── train_hr_tb_rbi.py          Trains HR/TB/RBI prop models
├── eval_models.py              Backtest + calibration evaluation harness
├── nrfi_odds.py                NRFI/YRFI devig math + odds parsing
├── nrfi_odds_routes.py         Registers /api/nrfi/odds, /api/nrfi/devig, /api/nrfi/odds-cache-status
├── savant_arsenal.py           Pitch arsenal helper
├── badge_patch.py              Performance badge tweaks
├── bq_etl.py                   Standalone ETL — populates BigQuery mlb.{batters,pitchers,bvp_situational,daily_slate_view}
├── redis_client.py             Thin Redis wrapper with in-memory fallback (JSON values + TTLs)
└── gunicorn_conf.py            1 worker, 8 gthreads, preload_app=False, post_fork cache preload
```

`app.py` imports these modules with `try / except ImportError` graceful-fallback shims, so a missing module does not crash boot — it just disables that feature (XGB scorer, stacked calibrator, pipeline blueprint, etc.). Preserve this pattern when adding new optional modules.

### `app.py` layout (top to bottom)

1. **Env load + LLM/BQ init** (lines ~1–304) — `_load_local_env_file`, Gemini client, BigQuery client, `_vertex_gemini_json` / `_vertex_gemini_text` / `_vertex_claude_json` (legacy alias).
2. **Module wiring** (~305–315) — `register_nrfi_odds_routes`, `app.register_blueprint(pipeline_bp)`.
3. **Player helper routes** (~315–760) — `/api/player/*/recent-games`, `/api/player/*/platoon-splits`, `/api/player/*/arsenal-matchup/*`, performance-badges.
4. **Global constants** (~1349–1460) — `MLB_API`, `WX_API`, `STADIUM_COORDS`, `DOME_VENUES`, `PARK_FACTORS`.
5. **FG cache** (~1462–1880) — `_load_fg_data()`, `_load_fg_data_from_mlb_api()` (bulk endpoint), `_maybe_refresh_fg()`, `fg_batter(name)` / `fg_pitcher(name)` with fuzzy lookup.
6. **Savant cache** (~2395–2810) — `_load_savant_data()`, `_maybe_refresh_savant()`, `sv_pitcher(name)` / `sv_batter(name)`, arsenal lookups.
7. **Secondary caches** — injury status, active rosters, MLB memory store, batter profiles, pitcher Statcast prewarm, arsenal priors.
8. **MLB Stats API collectors** (`fetch_schedule`, boxscores, transactions, team rosters) feeding the in-memory "memory store" (`data/mlb_memory_store.json`).
9. **Page routes** (~3832–3900) — return cached HTML strings (or re-read for dashboards/props/tracker/deepdive to pick up file updates without restart).
10. **Status / cache control** — `/api/status`, `/api/cache/status`, `/api/cache/warm` (POST, admin-token-gated), `/health` (Fly.io check, must always 200).
11. **Brain ingest routes** — `/api/brain/*` upload/list/delete/ingest. Calls `brain_merge_patch.load_brain_overlays()` to splice user-uploaded stats into the FG/Savant caches.
12. **Game + projection routes** — `/api/games/today`, `/api/game/<pk>`, `/api/game-projection/<pk>`, `/api/pitcher-matchup/<pk>`, `/api/player/<pk>`, `/api/bvp/*`, `/api/player-splits/*`, `/api/player/*/spray`, `/api/player/*/zonechart`.
13. **Odds API layer** (~11469–11935) — `ODDS_API_KEY`, `_find_odds_event`, `_load_event_odds`, `_parse_prop_markets`. In-memory + disk cache at `data/odds_cache.json`.
14. **Simulation + market routes** — `/api/simulate/<pk>`, `/api/market/<pk>`, `/api/nrfi/<pk>` (legacy in-app NRFI route; live-odds variants live in `nrfi_odds_routes.py`).
15. **Tracker system** (~13800–16400) — picks (`/api/tracker/pick`, `PATCH`, `DELETE`), parlays, performance, calibration, Brier, value, portfolio, bet slip, bankroll, attribution, daily capture (`/api/tracker/capture/<date>`), close-line capture, model record. Auto-sync runs in a background thread when `TRACKER_AUTO_SYNC_ENABLED=1`.
16. **Daily / cross-game** — `/api/teams/overview`, `/api/teams/pitching-rankings`, `/api/cheatsheets/today`, `/api/projections/monte-carlo`, `/api/lineup/<pk>`, `/api/capture-daily-slate/<date>`, `/api/parlay/build`.
17. **Prop projection model** (~17740–19090) — `/api/ai-boxscore/<pk>`, `/api/props/projections/<pk>`, `_project_batter`, `_project_batter_batx`, `_project_pitcher`, `_matchup_score`, `_platoon_blend`.
18. **Specialized sub-views** — `/api/umpire/<pk>`, `/api/props/trends/*`, `/api/props/quick/*`, `/api/bullpen/fatigue/<pk>`, `/api/f5/<pk>`, `/api/lineup-status/<pk>`, gameside deepdive, breakout detector, HR analytics, matchup card, cheatsheet (BigQuery-backed), insights, projected boxscore.
19. **Cache preload** (~21336–21415) — `_preload_caches()` kicks off FG/Savant/roster/arsenal loads in daemon threads. Called from `gunicorn_conf.py:post_fork()`, **not** at module import (see Gunicorn section).

### Pages and their routes

| HTML file | Route | Purpose |
|-----------|-------|---------|
| `dashboard.html` | `/` | Today's games, quick props, game cards |
| `deepdive.html` | `/deep-dive/<game_pk>` | Full game breakdown: lineups, weather, BvP, props |
| `props.html` | `/props` | Props research board with Monte Carlo ratings |
| `tracker.html` | `/tracker` | Bet slip, performance analytics, Kelly staking |
| `consistency.html` | `/consistency` | Player hit-rate over windows |
| `bvp.html` | `/batter-vs-pitcher` | BvP explorer |
| `value_bets.html` | `/value-bets` | CLV / value bets dashboard |
| `nrfi.html` | `/nrfi` | NRFI / YRFI dashboard |
| `tools.html` | `/tools` | Misc tools page |
| `pitcher_deepdive.html` | `/pitcher-deep-dive[/<pitcher_id>]` | Pitcher arsenal, zone chart, matchup vulnerability |
| `gameside_deepdive.html` | `/gameside-deepdive/<game_pk>` | F5, bullpen fatigue, lineup status |
| `breakout_detector.html` | `/breakout-detector` | Statcast breakout signal scoring |
| `cheatsheet.html` | `/cheatsheets` | Daily top prop targets (BQ-backed `/api/cheatsheet`) |
| `hr_analytics.html` | `/hr-analytics` | HR simulator, pitch mix, scouting writeups, daily scores |
| `edge_lab.html` | `/edge-lab` | Edge lab tools |
| `settings.html` | `/settings` | Admin: cache warm, brain ingest/upload, final daily summary, XGB training |

Each HTML file is loaded into a module-level string at boot via `_read_html_or_fallback()` (`app.py:784`). The dashboards that change most often (`dashboard.html`, `deepdive.html`, `props.html`, `tracker.html`) are re-read per-request with `Cache-Control: no-store` so a redeploy isn't needed to pick up frontend edits to those files. All data fetching is done client-side via `/api/*`.

### Key API route groups

- `/api/games/today` — today's schedule (hydrated from MLB Stats API)
- `/api/game/<game_pk>` — game detail + weather + park factor
- `/api/game-projection/<game_pk>` — Gemini-narrative + deterministic projection
- `/api/props/projections/<game_pk>` — per-batter prop projections with hub ratings
- `/api/props/scan/today` — cross-game MC grades for all props
- `/api/props/line-shopping/<game_pk>`, `/api/props/matchup-scores/<game_pk>`, `/api/props/trends/<game_pk>`, `/api/props/quick/<game_pk>`
- `/api/projections/monte-carlo` — full MC slate with per-game top props
- `/api/tracker/*` — full CRUD for picks, settings, performance, bankroll, calibration, Brier, attribution, value, portfolio, bet slip, closing-line capture
- `/api/cheatsheets/today` — daily cheatsheet (cached, async refresh)
- `/api/cheatsheet` — BQ-backed Vertex/Gemini cheatsheet
- `/api/breakout/candidates` — Statcast breakout scores (day-cached, 1h TTL)
- `/api/sharp-card/<game_pk>` — server-side Sharp Card rollup (side/total/environment/drivers/best-bet/grade); also locks + grades the verdict into `sharp_card_history.json`
- `/api/sharp-card/accuracy` — rolling hit-rate of recorded Sharp Card best bets
- `/api/umpire/<game_pk>` — HP umpire stats
- `/api/bullpen/fatigue/<game_pk>`, `/api/f5/<game_pk>`, `/api/lineup-status/<game_pk>`
- `/api/hr-analytics/*` — HR sim, pitch mix, scouting writeup, daily scores
- `/api/pipeline/{status,games,matchup,run}` — matchup pipeline blueprint
- `/api/nrfi/{odds,devig,odds-cache-status}` — live NRFI odds + devig (from `nrfi_odds_routes.py`)
- `/api/matchup`, `/api/insights/<game_pk>`, `/api/projected-boxscore/<game_pk>` — Vertex/Gemini + BigQuery-backed
- `/api/brain/*`, `/api/brain-data/*` — brain overlay upload/ingest
- `/api/memory/*` — MLB memory store (snapshot history of Stats API pulls)
- `/api/cache/{status,warm}` — operator dashboard
- `/api/status` — health metadata (cache states, AI/BQ availability)
- `/health` — Fly.io readiness probe (returns immediately even during cold boot)

### Prop projection model

`_project_batter()` (and the alternate `_project_batter_batx()` formulation) and `_project_pitcher()` combine FanGraphs season stats, Savant xStats, park factor, weather, and BvP history to produce `modelMean` and `adjProb` for each market. `_matchup_score()` computes a 0–100 score. Hub Rating is derived from edge vs. the book's implied probability. `_platoon_blend()` adjusts hitting stats by pitcher handedness.

When XGBoost artifacts are present, `xgb_prop_scorer.{xgb_hit_prob, xgb_k_prob, xgb_hr_prob, xgb_tb_prob, xgb_rbi_prob}` are called alongside the analytic models. `stacked_calibrator.calibrate(...)` fuses XGB + BATX into a single calibrated probability + 95% CI + verdict tier (STRONG_BET / LEAN_OVER / PASS / LEAN_UNDER / STRONG_FADE). The stacked calibrator uses a trained isotonic regression when `models/stacked_hit_calibrator.pkl` exists, and a principled logistic-blend fallback otherwise.

### XGBoost models

Four `.pkl` artifacts live in `models/`: `xgb_hits_over_0.5.pkl`, `xgb_k_over_3.5.pkl`, `xgb_k_over_4.5.pkl`, `xgb_k_over_5.5.pkl`. Each is a joblib-serialized `{"model": <CalibratedClassifierCV>, "features": [...], "meta": {...}}` dict. **Pin `xgboost==3.2.0` in both training and runtime** (matches the serialized artifacts — loading them under an older XGBoost emits a version warning) — see `docs/xgb_model_regeneration.md` for the full regeneration playbook (Colab notebook at `notebooks/xgb_production_export_colab.ipynb`). The scorer also accepts the legacy direct-model format and falls back to `models/xgb_feature_cols.json` for features.

**HR/TB/RBI models are deferred (not committed).** `xgb_prop_scorer` looks for `xgb_hr_over_0.5.pkl`, `xgb_tb_over_1.5.pkl`, and `xgb_rbi_over_0.5.pkl`, but those artifacts are not present in `models/`. The scorer skips any missing model file (`if not os.path.exists(path): continue`), so `xgb_hr_prob / xgb_tb_prob / xgb_rbi_prob` return `None` and those markets fall back to the analytic `_project_batter` model — this is expected, not a bug. To enable them, train via `train_hr_tb_rbi.py` (then backtest/calibrate per the regeneration playbook before committing under `xgboost==3.2.0`).

### Gemini / LLM integration

All "AI" surfaces route through `_vertex_gemini_json()` / `_vertex_gemini_text()` (`app.py:204-289`). The legacy `_vertex_claude_json()` is now a thin alias so existing call sites work without edits — but the model is Gemini, not Claude. Anthropic was removed in production because most Claude-backed surfaces were already falling through to deterministic templates; the comment block at `app.py:116-131` explains the decision. The four model tiers (`fast` / `reasoning` / `narrative` / `premium`) all resolve to `gemini-2.5-pro` by default except `fast` (`gemini-2.5-flash-lite`).

Thinking is explicitly disabled (`thinking_budget: 0`) because none of the JSON surfaces benefit from chain-of-thought and thinking tokens count against `max_output_tokens` (causing empty/truncated JSON).

### BigQuery integration

`bq_etl.py` is a standalone CLI that populates `mlb.batters`, `mlb.pitchers`, `mlb.bvp_situational`, and the `mlb.daily_slate_view`:

```bash
python bq_etl.py --refresh all      # or batters | pitchers | bvp | view
```

It does **not** import `app.py` (which would trigger a 150MB cache preload). The Vertex-backed routes (`/api/matchup`, `/api/cheatsheet`, `/api/insights`, `/api/projected-boxscore`) query these tables via `_bq_query_rows()` and fall back to CSV / in-memory caches when BQ is unavailable.

### Brain overlay system

`brain_merge_patch.py` lets the user upload custom CSVs (e.g. internal projections, scout reports) under `/api/brain-data/upload`. `load_brain_overlays()` reads them at startup and merges their values into the FG/Savant caches via alias tables (e.g. `wrc_plus → fg_wrc`, `barrel_pct → sv_brl_pct`). The patched `fg_batter` / `fg_pitcher` / `sv_batter` / `sv_pitcher` functions imported from `brain_merge_patch` are what `app.py` actually uses — the in-file definitions are the underlying primitives.

### Matchup pipeline

`pipeline_scheduler.py` runs at **09:00 ET every day**, builds `games_df` (today's schedule) and `matchup_df` (all batter×pitcher cells with FG/Savant features), and caches them in memory. The `pipeline_bp` blueprint surfaces them under `/api/pipeline/{status,games,matchup,run}`. `run_pipeline()` is also callable from `/api/cache/warm` and from `/api/pipeline/run` (admin-token-gated, rate-limited to 3/min, 10/hr).

### Redis layer

`redis_client.get_redis()` returns a singleton that speaks JSON: callers store/retrieve plain Python objects with TTLs, and the wrapper hides whether the backend is real Redis or the thread-safe in-memory dict fallback. `flask-limiter` is also wired through Redis when `REDIS_URL` is set.

### Data persistence

All persistent state lives under `data/` (Fly.io mounts `mlb_data` volume to `/app/data`):

- `daily_tracker.json` — picks keyed by `YYYY-MM-DD` → day record with `entries[]`, `capturedAt`, `gradedAt`, `closingCapturedAt`. Sharp Card "Save Bet" writes game-level picks under the existing `h2h` / `totals` markets (the auto-grader settles those from the linescore), tagged `source:'sharp_card'`.
- `sharp_card_history.json` — keyed by `YYYY-MM-DD` → `{<game_pk>: {bestBet, grade, recordedAt, gradedAt}}`. Locks each game's pre-game Sharp Card best bet once, then grades it vs the final via `_grade_game_bet`; backs `/api/sharp-card/accuracy`.
- `model_adjustments.json` — Kelly settings, market multipliers, bankroll, calibration nudges
- `calibration_history.json` — historical calibration snapshots
- `value_history.json` — CLV/value series
- `odds_cache.json` — today's Odds API snapshot (restored on boot to avoid redundant API credits)
- `brain_upload_state.json` — registry of uploaded brain files
- `mlb_memory_store.json` — rolling snapshots of MLB Stats API pulls (capped by `MLB_MEMORY_KEEP_SNAPSHOTS` / `MLB_MEMORY_MAX_BYTES`)
- `fg_batting_{year}.csv`, `fg_pitching_{year}.csv`, `fg_steamer_bat_{year}.csv`, `fg_steamer_pit_{year}.csv` — 2021–2026 FanGraphs season + Steamer projection files used by `fangraphs_loader.py`
- `savant_bat_tracking_{year}.csv`, `savant_framing_{year}.csv`, `savant_swing_take_{year}.csv` — Baseball Savant leaderboards via `savant_bat_tracking.py`. **Note:** the swing-take leaderboard CSV frequently comes back header-only from Savant (documented in `savant_bat_tracking.py`); `sv_swing_take()` returns `{}` and callers fall back, so an empty `savant_swing_take_{year}.csv` is expected, not a bug.
- `mlb_matchups_YYYYMMDD.csv` — daily BvP snapshot files, **generated on demand by the pipeline** (date-stamped, ephemeral). Not checked in; absence is normal.

The static reference CSVs (FanGraphs/Steamer/Savant) are checked into the repo so Fly.io deploys have them available. The date-stamped/JSON runtime artifacts (`daily_tracker.json`, `mlb_matchups_*.csv`, `lineups_*.json`, `umpires_*.json`, etc.) are regenerated at runtime.

### Gunicorn / Fly.io constraints

`fly.toml`: 4GB memory, 2 shared CPUs, `auto_stop_machines='off'`, `min_machines_running=1`, 10m health-check grace period, `/health` probe every 30s.

`gunicorn_conf.py`:
- **`workers=1`** — Savant + FG caches are ~150 MB; two workers double that and trigger OOM.
- **`worker_class='gthread'`, `threads=8`** — non-trivial concurrency for I/O-bound MLB API calls; 8 threads guarantees `/health` always has a free thread even when Monte Carlo / Savant fetches are in flight.
- **`preload_app=False`** — preload imports `app.py` in the master, kicks off daemon cache-loader threads in the master, then forks; daemon threads don't survive fork, so workers inherit `_fg_loading=True` with no thread actually running. Caches never populate. Do not change.
- **`timeout=120`** — Fly.io watchdog marks unhealthy long before the old 600s setting fired.
- **`max_requests=0`** — recycling triggers a full FG/Savant reload, which blows the memory budget.
- **`post_fork` hook calls `_preload_caches()` AFTER the worker binds to port** — `/health` returns 200 from second 0 while caches build in the background.

If you touch any of these settings, re-read the rationale comments first.

## Tracker schema

Tracker picks (`data/daily_tracker.json[date].entries[]`) carry: `id`, `savedAt`, `gradedAt`, `source`, `gamePk`, `player`, `marketKey`, `line`, `side`, `price`, `book`, `stakeDollars`, `stakeUnits`, `profitDollars`, `profitUnits`, `grade` (`pending` / `win` / `loss` / `push`), `hubRating`, `edge`, `modelProb`, `impliedProb`, `clvEdge`, plus optional metadata. Deduplication when `id` is absent uses the composite key `(date, gamePk, player, marketKey, line)`. `_tracker_pick_payload()` adds a derived `sideLabel` for the UI.

## Name matching

Player names are normalized to lowercase and matched with `difflib.get_close_matches(cutoff=0.78)` in `_fuzzy_lookup()`. Savant uses `"Last, First"` format; `_sv_key()` converts to `"First Last"` for consistent lookups. When adding new stat sources, follow the same convention. `_ascii_fold()` strips accents so names like "Acuña" match "Acuna".

## Working with this codebase

- **Editing `app.py`**: it's huge but well-grouped — use the section map above plus `grep -n '^@app.route'` to navigate. Most route handlers follow the same shape: pull from caches, call helpers, jsonify.
- **Editing an extracted module** (XGB, calibrator, FG loader, pipeline, Redis, NRFI odds, BQ ETL): edit the file directly. `app.py` imports them with `try/except ImportError` shims, so a missing module won't crash boot — but **don't rely on that for runtime correctness**; if a feature is supposed to work, make sure the import succeeds.
- **Adding a new HTML page**: drop the file at repo root, add a `_read_html_or_fallback()` call near `app.py:795`, add the route alongside the others around line 3832. If the page changes often, re-read it per-request (`html = _read_html_or_fallback(...)`) rather than caching the constant.
- **Adding a new env var**: add it to this file's "Environment variables" section *and* the relevant defaults block (`_admin_settings_default`, `_app_settings_default`, or the constant area near `app.py:137`).
- **Touching the model/calibration**: regenerate XGB artifacts via the Colab notebook (`docs/xgb_model_regeneration.md`), keep `xgboost==3.2.0` pinned, and commit refreshed `.pkl`s.
- **Tracker changes**: preserve the dedup key tuple and the `id` / `savedAt` / `gradedAt` invariants — downstream calibration/performance views depend on them.
