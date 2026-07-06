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

**Closing-line capture knobs (true CLV):** `TRACKER_CLOSING_CAPTURE_ENABLED` (default 1), `TRACKER_CLOSING_CAPTURE_MINUTES` (worker interval, default 3), `TRACKER_CLOSING_LEAD_MIN` / `TRACKER_CLOSING_GRACE_MIN` (the window before/after first pitch in which the closing line is captured, default 12/8). The closing-capture worker (`_tracker_closing_capture_once`) force-refreshes each game's odds from the Odds API **around its own first pitch** (`_fetch_event_odds_live`, bypassing the frozen daily snapshot) for games carrying pending picks, then runs the standard close pass. Without this the daily odds snapshot is built once and frozen, so opening and "closing" read identical prices and `clvEdge` is ≈0 by construction (the Beat-Close% KPI is meaningless). Credit cost is capped at ~1 Odds API credit per game per day; in-memory `_TRACKER_CLOSING_CAPTURED_GAMES` prevents re-spending once a game's window passes.

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
├── stacked_calibrator.py       Meta-learner that blends XGB + BATX into one probability + 95% CI + market-aware verdict tier. Verdict tiers are ratio-based around each market's base rate (`_MARKET_BASE_RATE`: hits 0.66, TB 0.40, RBI 0.34, HR 0.13) so STRONG_BET/LEAN/FADE mean the same "above/below average" thing for every market — the ratios are tuned to reproduce the old absolute hits tiers exactly at base 0.66. Smart-Consensus: shifts the XGB/BATX blend by *measured* per-model Brier (`data/model_accuracies.json`, written by `update_model_accuracies`) only once ≥40 graded picks carry both per-model probs — otherwise falls back to the coverage heuristic (no fabricated skill)
├── value_engine.py             Single source of truth for betting math — american↔decimal↔implied, two-way de-vig (multiplicative + Buchdahl power), EV, fractional/Quarter-Kelly staking. Pure-Python, self-tested (`python value_engine.py`). Backs `/api/v1/edges`
├── prop_calibration.py         Per-market probability recalibration fit from graded tracker history (isotonic / log-odds recentre) — corrects displayed adjProb/edge before EV/hub
├── market_blend_learner.py     Learned model-vs-market blend weights — fits the Brier-optimal per-market `w` for `logit_blend_prob` from graded tracker history (pre-blend `rawMultProb` + de-vig'd opening prices → outcome), shrunk toward the hand-tuned `MARKET_MODEL_WEIGHTS` prior by sample size (prior unchanged below MIN_N=60 graded picks; PRIOR_N=150 pseudo-counts; clamped to [0.05, 0.95]). Pure-Python, self-tested (`python market_blend_learner.py`). Cached 30 min alongside prop_calibration; in-force weights surfaced as `blend_weights` in `/api/calibration/markets`
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
- `/api/edges/today` — slate-wide edge-ranked board (positive-value plays + letter grades), reuses the cached props-scan
- `/api/v1/edges` — quant JSON feed: the edge board filtered by **EV** (default `minEv=0.03`), each play enriched via `value_engine` with de-vig'd `fairProb`, true EV vs best price, and a Quarter-Kelly `stakePct`/`stakeUnits`/`stakeDollars` (sized from tracker bankroll/kelly_fraction/max_bet_pct). Backbone for a mobile/Telegram/Discord client. Degrades gracefully (no odds → empty list, still 200)
- `/api/props/line-shopping/<game_pk>`, `/api/props/matchup-scores/<game_pk>`, `/api/props/trends/<game_pk>`, `/api/props/quick/<game_pk>`
- `/api/props/hit-history/<player_id>` — per-game prop values vs a line for the props-page HIT HISTORY bar chart (`?market=<tracker key>&line=&n=&season=&player=`). Grades each game two ways: vs the **current** line applied retroactively, and vs each game's **recorded** line from that day's tracker capture (`_hit_history_recorded_rows`; the closing-capture worker refreshes its price at first pitch). `summary.closing.coverage` reports how many games actually carry a recorded line — the UI's LINE ⇄ CLOSING LINES toggle excludes uncovered games from the closing hit rate instead of silently falling back to the current line
- `/api/projections/monte-carlo` — full MC slate with per-game top props
- `/api/tracker/*` — full CRUD for picks, settings, performance, bankroll, calibration, Brier, attribution, value, portfolio, bet slip, closing-line capture
- `/api/cheatsheets/today` — daily cheatsheet (cached, async refresh)
- `/api/cheatsheet` — BQ-backed Vertex/Gemini cheatsheet
- `/api/breakout/candidates` — Statcast breakout scores (day-cached, 1h TTL). `sv_brl_pct`/`sv_hh_pct` are read in their native percent units (no `<=1 → ×100` rescale — that bug inflated genuine sub-1% barrel rates 100×) and clamped to physical ceilings; players still gated at `fg_pa < 30`.
- `/api/sharp-card/<game_pk>` — server-side Sharp Card rollup (side/total/environment/drivers/best-bet/grade); also locks + grades the verdict into `sharp_card_history.json`
- `/api/sharp-card/accuracy` — rolling hit-rate of recorded Sharp Card best bets
- `/api/umpire/<game_pk>` — HP umpire stats
- `/api/bullpen/fatigue/<game_pk>`, `/api/f5/<game_pk>`, `/api/lineup-status/<game_pk>`
- `/api/hr-analytics/*` — HR sim, pitch mix, scouting writeup, daily scores. `/daily-scores` regresses each batter's `iso`/`barrel_pct`/`hh_pct` toward league means via `_shrink(obs, fg_pa, mu, prior_n)` (prior_n 150/60/50) so a tiny-sample debut can't post a 1.000 ISO / 50% barrel and top the board; rows carry `pa` + `sampleReliable` (pa ≥ 80).
- `/api/pipeline/{status,games,matchup,run}` — matchup pipeline blueprint
- `/api/nrfi/{odds,devig,odds-cache-status}` — live NRFI odds + devig (from `nrfi_odds_routes.py`)
- `/api/matchup`, `/api/insights/<game_pk>`, `/api/projected-boxscore/<game_pk>` — Vertex/Gemini + BigQuery-backed
- `/api/brain/*`, `/api/brain-data/*` — brain overlay upload/ingest
- `/api/memory/*` — MLB memory store (snapshot history of Stats API pulls)
- `/api/cache/{status,warm}` — operator dashboard
- `/api/calibration/markets` — live model-vs-reality readout: per-market Brier + ECE from graded tracker picks alongside each model's held-out (2021-24/2025) benchmark from `models/model_metrics.json`; status flags (`warming_up` / `on_track` / `degraded` / `no_edge` / `drift`) tell you whether the XGB held-out skill is carrying into production. `?window=N` (default 60). The response also reports `calibration_applied` — the per-market correction `prop_calibration.py` is actively applying to displayed `adjProb` (see below) — `blend_weights` (the learned model-vs-market blend weight in force per market, from `market_blend_learner.py`), and `edge_display_cap`. **Drift guard:** each market carries a `drift` block that isolates the post-switchover cohort (`modelSource=='stacked'`, i.e. picks whose `adjProb` is XGB-stacked-driven) from the legacy cohort and flags `drifted` when the stacked cohort (≥`_DRIFT_MIN_N`=25 graded picks) is miscalibrated — `|mean_pred − mean_actual| ≥ 0.06`, `ECE ≥ 0.10`, or Brier regressed ≥0.02 vs legacy. Flagged markets get `status:'drift'` and appear in the top-level `drift_alerts`. This catches the transient where `prop_calibration` (fit on the old MC-based `preCalProb`) is briefly applied to the new stacked distribution; it self-resolves as the calibrator re-fits over its 120-day window.

**Live recalibration (`prop_calibration.py`).** Every prop/team-market row built by `_build_tracker_rows_for_game` / `_build_team_market_rows` runs its model probability through `_calibrate_prop_prob(market_key, prob)` before edge/EV/hub are computed, so displayed edge reflects realized accuracy rather than raw-model optimism. The per-market correction (isotonic when ≥80 graded picks and sklearn present, else a log-odds recentre to the realized base rate, else identity below 20) is fit from graded tracker history and cached for 30 min. **Backfill prior (`calibration_backfill.py`):** the isotonic layer no longer waits weeks for graded volume — the script scores every committed model over the current season to date (genuinely out-of-sample; the models train through 2024) against known outcomes and writes `data/calibration_backfill.json` (thousands of (prob, outcome) pairs per market). `_build_prop_calibrator` tops each market up to `_PROP_CAL_BACKFILL_TARGET` (600) with these pairs, and real tracker picks displace backfill at 4:1 (fully retired at ~150 graded picks/market) because backfill probs are the XGB output rather than the full fused `preCalProb`. Refreshed by the weekly regen workflow. Each row now persists `preCalProb` (the pre-calibration value — what the calibrator is *fit* on, so re-fitting never compounds a prior correction) and `calStatus`. Displayed edge is clamped to ±`EDGE_DISPLAY_CAP` (0.30) so a miscalibrated blowout can't advertise a 40-point edge.
- `/api/diag/serve-parity` — serve-parity self-test. Builds entity dicts the way the live scoring paths do and reports, per XGB model feature, how often it lands on its empty-input default across the slate; `default_rate ≥ 0.85` is flagged a `serve_gap` (the live caller isn't supplying that feature — the train/serve gap that silently kills held-out skill). Defaults are derived from `_build_*_features({})` so they never drift. `?games=N` (default 3, max 6). Backed by `xgb_prop_scorer.feature_default_report()`.
- `/api/status` — health metadata (cache states, AI/BQ availability)
- `/health` — Fly.io readiness probe (returns immediately even during cold boot)

### Prop projection model

`_project_batter()` (and the alternate `_project_batter_batx()` formulation) and `_project_pitcher()` combine FanGraphs season stats, Savant xStats, park factor, weather, and BvP history to produce `modelMean` and `adjProb` for each market. `_matchup_score()` computes a 0–100 score. Hub Rating is derived from edge vs. the book's implied probability. `_platoon_blend()` adjusts hitting stats by pitcher handedness.

`_pitch_type_advantage()` produces the `pitchTypeAdvantage` / `...Note` verdict. It first tries `_arsenal_matchup_from_stats(pitcher_id, batter_id)` — a **real** join of the batter's wOBA-by-pitch-type (`_sv_bat_arsenal_stats`) against the pitcher's actual per-pitch usage (`_sv_pit_arsenal_stats`), both keyed by MLBAM id (no name matching), yielding a usage-weighted `matchup_woba` compared to the batter's own cross-pitch baseline (favorable/neutral/unfavorable at ±0.020 wOBA). It falls back to `_pitch_type_advantage_proxy()` (the legacy single-most-used-pitch × AVG, or handedness split) only when arsenal coverage is thin (<35% of usage has batter data) — typically fringe/small-sample hitters. The arsenal path is tagged `source:'arsenal'` and adds `matchup_woba` / `baseline_woba` / `pitches[]`.

When XGBoost artifacts are present, `xgb_prop_scorer.{xgb_hit_prob, xgb_k_prob, xgb_hr_prob, xgb_tb_prob, xgb_rbi_prob}` are called alongside the analytic models. `stacked_calibrator.calibrate(...)` fuses XGB + BATX into a single calibrated probability + 95% CI + verdict tier (STRONG_BET / LEAN_OVER / PASS / LEAN_UNDER / STRONG_FADE). The stacked calibrator uses a trained isotonic regression when `models/stacked_hit_calibrator.pkl` exists, and a principled logistic-blend fallback otherwise.

### XGBoost models

Fourteen `.pkl` artifacts live in `models/` — the standard lines (`xgb_hits_over_0.5.pkl`, `xgb_k_over_{3.5,4.5,5.5}.pkl`, `xgb_hr_over_0.5.pkl`, `xgb_tb_over_1.5.pkl`, `xgb_rbi_over_0.5.pkl`) plus the alt-line variants (`xgb_hits_over_1.5.pkl`, `xgb_tb_over_{2.5,3.5}.pkl`, `xgb_rbi_over_1.5.pkl`, `xgb_k_over_{2.5,6.5,7.5}.pkl`). **Routing is line-exact:** `_BATTER_LINE_MODELS` maps (family, line) → model key; `xgb_batter_prob_full(family, line, …)` / `xgb_line_ready(family, line)` are the line-aware entry points the tracker fusion uses, and unmapped lines return `{}` so the analytic prob stands. The K nearest-line fallback is capped at 1.0 strikeout. Verdict tiers in `stacked_calibrator` anchor to line-aware base rates (`_MARKET_LINE_BASE_RATE`, derived from training pos-rates). Each artifact is a joblib-serialized `{"model": <CalibratedClassifierCV>, "features": [...], "meta": {...}}` dict. **Pin `xgboost==3.2.0` AND `scikit-learn==1.6.1` in both training and runtime** (the artifacts are `CalibratedClassifierCV` pickles — loading them under a different sklearn emits an `InconsistentVersionWarning` and may break) — see `docs/xgb_model_regeneration.md` for the full regeneration playbook. The scorer also accepts the legacy direct-model format and falls back to `models/xgb_feature_cols.json` for features.

**Calibration gate (`_xgb_calibrated` / `xgb_ready`).** The scorer only emits an XGB probability when the market's output is a *true* probability — either the model artifact is itself a fitted `CalibratedClassifierCV` (self-calibrated `predict_proba`, detected at load via `_self_cal`), **or** a post-hoc `models/iso_{market}.pkl` isotonic calibrator exists. A raw, uncalibrated `XGBClassifier` qualifies under neither (its `predict_proba` is bimodal/extreme), so it stays gated off and callers fall back to the analytic model — this is the A1 safety. The current seven committed models are self-calibrated, so `xgb_ready('hits'/'k'/'hr'/'tb'/'rbi')` is True.

**Current models were regenerated by `regenerate_models.py`** (not the older `train_prop_models.py` / Colab path, which silently zero-filled most features because it never merged FanGraphs K%/BB%/barrel%/whiff% or wired the opponent-pitcher join). `regenerate_models.py` sources season skills from the local `data/fg_{batting,pitching}_*.csv` by `xMLBAMID` (identical source + scale to the live scorer → no train/serve skew), captures each batter's real opposing starter from Statcast, **drops** features that genuinely can't be reconstructed historically (weather/umpire/bvp) rather than zero-filling them — park **is** reconstructable (the game's home team is known, and the script carries verbatim copies of app.py's park tables; keep them in sync) — trains on 2021–24, and validates on a 2025 held-out test. Held-out test AUC: hits **0.62**, k_3.5 0.70, k_4.5 0.71, k_5.5 0.72, hr **0.68**, tb **0.62**, rbi **0.61** — all beat base-rate Brier and are well-calibrated (predicted mean ≈ actual). The `meta` records `test_auc`/`train_auc`/`test_brier`/`baserate_brier`/`train_medians`/`xgboost_version`. Rerun with `python regenerate_models.py` (pybaseball disk-caches raw Statcast, so reruns skip the network); subset with `--markets hr tb rbi`.

The **hits** model also trains on `batting_order` + `expected_pa` (lineup role), reconstructed from Statcast at-bat order with `expected_pa` derived from the scorer's own slot→PA table — the live scorer already serves these via `lineup_loader`, so adding them lifted held-out AUC 0.591→0.622 (see commit history).

**"Heater" momentum (true-talent latency).** `regenerate_models.py` computes per-game contact quality from raw Statcast (`g_ev` = mean exit velo on batted balls, `g_hh` = ≥95mph share, `g_barrel` = `launch_speed_angle==6` share, `g_whiff` = swinging-strike/swing) and rolls them into recent-form levels (`l7_ev`/`l7_barrel`/…) plus recent-vs-30g-baseline ratios (`ev_momentum`/`barrel_momentum`, >1 = heating up). These are served live by `app._batter_momentum_features(batter_id)` — same Statcast source (`statcast_batter`), same aggregation, same scale → no train/serve skew. The serve defaults in `_build_batter_market_features` are the recorded batter-market `train_medians` so an un-pulled batter is the neutral case. **Momentum lives in the HR/TB/RBI models, not hits:** tested on the ≥1-hit market it was flat (held-out AUC 0.6221 vs 0.6223 — recent contact quality doesn't beat the season EV/HH the model already has for "gets a hit"), so it was not shipped there. In the power/run models it is a real but minor positive contributor (e.g. for HR it ranks 20–24 of 24; those markets are dominated by season ISO/HR-FB/xSLG + lineup volume + opp HR-allowed). Momentum is pulled only on the deep-dive / AI-boxscore path (where `xgb_hr/tb/rbi_prob` are called with a batter id), not the name-only slate scan, so it costs one daily-cached Statcast lookup per batter.

**Serve-time feature parity is mandatory.** A model only earns its held-out skill in production if the live scorer feeds the *same* features it trained on. Two fixes restored this: (1) hits — added the lineup-role features the scorer already served; (2) the **k-model's rolling form** (`l{3,5,10}Ks` / `l5KRate` / `l{3,5}IP` / `daysRest`, 7 of its 12 features) was being fed as constant defaults everywhere — `_pitcher_recent_form` never returned the `l3`/`l5`/`l10` keys the call sites read. `_pitcher_rolling_k_features(pitcher_id)` now computes them as per-start means matching `build_pitcher_matrix`, wired into the tracker-capture, `/api/market`, and pitcher-deep-dive paths. Whenever you add/rename a model feature, update BOTH `regenerate_models.py` and the matching `xgb_prop_scorer._build_*_features` + its live data source.

**HR, TB and RBI are all committed.** `xgb_hr_over_0.5.pkl` (AUC 0.68), `xgb_tb_over_1.5.pkl` (AUC 0.62), and `xgb_rbi_over_0.5.pkl` (AUC 0.61) are real, self-calibrated `regenerate_models.py` artifacts (`--markets hr tb rbi`). All three share one serve builder, `_build_batter_market_features` (thin `_build_hr/tb/rbi_features` aliases) — it emits the SUPERSET of their keys and each model's saved feature list selects its own columns; `_predict_batter_market_full` routes hr/tb/rbi to it (hits/k keep their own builders). **Park features:** HR trains on `park_hr` (hand-aware HR park multiplier via `_hr_park_factor_hand` — Yankee porch 1.34 L, Oracle 0.80 L, …) and TB on `park_factor`; callers supply `parkHr`/`parkFactor` from the game's home team id (deep-dive, tracker capture, lineup-props, serve-parity all do), neutral 1.0 when unknown. Measured effect: global held-out AUC/Brier flat-to-marginally-better (park re-levels probabilities across venues rather than re-ranking batters), venue-conditional calibration gap improves (HR −1.5%, TB −5% weighted mean |per-venue pred−actual|). **Park levels are learned, not static:** `regenerate_models.py` computes home-vs-road factors per team-season from its own Statcast pull (recency-weighted trailing 3 seasons, shrunk toward 1.0, leakage-safe — season S trains on factors ending S−1) and exports `data/park_factors_learned.json`; app.py's `_park_factor_for()` / `_hr_park_factor_hand()` serve the learned level (curated LHB/RHB asymmetry applied as a ratio on top) and fall back to the static tables when the file/team is absent. Measured honestly: learned levels did NOT move the venue gap ≥15% (HR −2.9%, TB −1.2%, TB2.5 +4.2%) — venue miscalibration is model bias, not table staleness — but they self-adapt to venue changes (Sacramento, Steinbrenner) the static table would need hand-edits for. **Bat-tracking (2024+):** `bt_bat_speed`/`bt_fast_swing`/`bt_squared_up`/`bt_blast` from the Savant leaderboards (`data/savant_bat_tracking_{2024,2025}.csv` for training, current-year for serve, looked up inside `_build_batter_market_features` so every caller has parity). Pre-2024 rows are NEVER imputed — `bt_*` stays NaN in training (XGB missing-branch) and a batter with no row serves NaN identically. Measured: tb alt lines +0.001 AUC, rest flat, importance ranks 19–30 — kept at zero serve cost; early-season stabilization (bat speed ~50 swings) is the expected live upside a full-season holdout can't see. **Weekly regen is automated:** `.github/workflows/model-regen.yml` retrains everything Monday 09:00 UTC (or on dispatch), refreshes leaderboards/park factors/benchmarks, and opens a PR — the ship gate runs inside the pipeline. Defaults are the batter-market train medians (identical across the three since they share training rows). The TB target uses the corrected total-bases formula (`1B+2·2B+3·3B+4·HR`; the old `is_hit+is_hr+is_double+is_triple` under-counted triples/HRs but was latent since TB was never trained). RBI is derived from Statcast score state (`post_bat_score − bat_score` per PA — a close proxy for official RBI). TB/RBI are scored on the deep-dive / AI-boxscore path (same place as HR), with momentum pulled once they're `xgb_ready`.

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
- `model_accuracies.json` — Smart-Consensus per-model Brier (XGB vs analytic) per market, written by `stacked_calibrator.update_model_accuracies` from graded tracker picks. Runtime-derived (gitignored); production learns its own
- `calibration_history.json` — historical calibration snapshots
- `value_history.json` — CLV/value series
- `odds_cache.json` — today's Odds API snapshot (restored on boot to avoid redundant API credits)
- `brain_upload_state.json` — registry of uploaded brain files
- `mlb_memory_store.json` — rolling snapshots of MLB Stats API pulls (capped by `MLB_MEMORY_KEEP_SNAPSHOTS` / `MLB_MEMORY_MAX_BYTES`)
- `fg_batting_{year}.csv`, `fg_pitching_{year}.csv`, `fg_steamer_bat_{year}.csv`, `fg_steamer_pit_{year}.csv` — 2021–2026 FanGraphs season + Steamer projection files used by `fangraphs_loader.py`
- `park_factors_learned.json` — learned per-team park factors (written by `regenerate_models.py`; served by `_park_factor_for` / `_hr_park_factor_hand` with static-table fallback)
- `savant_bat_tracking_{year}.csv`, `savant_framing_{year}.csv`, `savant_swing_take_{year}.csv` — Baseball Savant leaderboards via `savant_bat_tracking.py`. The 2024/2025 bat-tracking files are checked in as training data for the `bt_*` model features; the current-year file backs serve-time lookups. **Note:** the swing-take leaderboard CSV frequently comes back header-only from Savant (documented in `savant_bat_tracking.py`); `sv_swing_take()` returns `{}` and callers fall back, so an empty `savant_swing_take_{year}.csv` is expected, not a bug.
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

**XGB / stacked-calibrator fields (hits/hr/tb/rbi at their model line).** `_build_tracker_rows_for_game` fuses the XGB point probability with the analytic/MC probability via `stacked_calibrator.calibrate(market_key=mk)` **up-front**, and the fused `stackedProb` becomes the `base_prob` that flows through the existing market-blend + `prop_calibration` pipeline — so `adjProb`/`edge`/`evPct`/`hubRating` all reflect the fusion for these four markets (off-line markets, e.g. TB 2.5, keep the analytic prob). `modelSource` records `stacked` / `xgb_blend` / `mc`. The row persists `xgbProb`, `batxProb`, `stackedProb`, `stackCiLo`/`stackCiHi`, and `stackVerdict`/`stackVerdictLabel` (the verdict tiers are now market-aware — see `stacked_calibrator`). The persisted `xgbProb`/`batxProb` are exactly what `stacked_calibrator.update_model_accuracies` reads to drive Smart-Consensus; `_tracker_auto_sync_once` calls it after each grade pass, so once ≥40 graded picks per market carry both probs the blend shifts from the coverage heuristic to measured per-model Brier. (The Smart-Consensus updater reads only the raw per-model legs, so driving `adjProb` off the fusion creates no feedback loop.)

**Primary KPI = Closing Line Value.** `_tracker_live_summary()` emits a `primaryKpi` block (`metric:'clv'`, `value`=`clv_positive_rate` "beat-close %", `avg_clv`, `n`=graded-with-CLV) and the `tracker.html` hero leads with **Beat Close %** + **Avg CLV** (hit-rate / P&L demoted to supporting stats). CLV — does our taken price beat the closing line (`clvEdge = closingImplied − openingImplied`, positive = good) — is the lowest-variance, market-anchored measure of edge, unlike small-sample win/loss. The hero shows the CLV sample size (`n=`) so a tiny sample can't masquerade as skill; the picks board has a `Sort: CLV` option. Note the UI color convention: positive `clvEdge` is green (`clvColor()`), matching the backend which counts `clvEdge>0` as +CLV.

## Name matching

Player names are normalized to lowercase and matched with `difflib.get_close_matches(cutoff=0.78)` in `_fuzzy_lookup()`. Savant uses `"Last, First"` format; `_sv_key()` converts to `"First Last"` for consistent lookups. When adding new stat sources, follow the same convention. `_ascii_fold()` strips accents so names like "Acuña" match "Acuna".

## Working with this codebase

- **Editing `app.py`**: it's huge but well-grouped — use the section map above plus `grep -n '^@app.route'` to navigate. Most route handlers follow the same shape: pull from caches, call helpers, jsonify.
- **Editing an extracted module** (XGB, calibrator, FG loader, pipeline, Redis, NRFI odds, BQ ETL): edit the file directly. `app.py` imports them with `try/except ImportError` shims, so a missing module won't crash boot — but **don't rely on that for runtime correctness**; if a feature is supposed to work, make sure the import succeeds.
- **Adding a new HTML page**: drop the file at repo root, add a `_read_html_or_fallback()` call near `app.py:795`, add the route alongside the others around line 3832. If the page changes often, re-read it per-request (`html = _read_html_or_fallback(...)`) rather than caching the constant.
- **Adding a new env var**: add it to this file's "Environment variables" section *and* the relevant defaults block (`_admin_settings_default`, `_app_settings_default`, or the constant area near `app.py:137`).
- **Touching the model/calibration**: regenerate XGB artifacts via the Colab notebook (`docs/xgb_model_regeneration.md`), keep `xgboost==3.2.0` pinned, and commit refreshed `.pkl`s.
- **Tracker changes**: preserve the dedup key tuple and the `id` / `savedAt` / `gradedAt` invariants — downstream calibration/performance views depend on them.
