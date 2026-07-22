# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project overview

NFL Analytics Hub — Flask app serving an NFL prop-betting research + tracking
platform. Modular by design (lesson learned from the 22k-line MLB monolith):
`app.py` is a thin factory; every feature lives in its own module. Keep it
that way — new features get a new module/blueprint, not lines in app.py.

```
app.py                  thin factory: page routes, /health, /api/status, blueprint registration, preload hook
nfl_data.py             ESPN-backed data layer (schedules, boxscore-fed weekly player stats, positions,
                        defense-vs-position, team summaries) with disk caches under data/
odds_api.py             The Odds API layer: daily frozen snapshot (data/odds_cache.json, restored on boot),
                        per-event props, fetch_event_odds_live() bypass for closing capture,
                        norm_player_name() (shared cross-source name matcher)
projections.py          analytic prop model: per-market distributions + leave-forward backtest (self-tested)
tracker.py              picks store (data/daily_tracker.json), grading, CLV closing capture, workers
value_engine.py         betting math (devig/EV/Kelly) — verbatim copy from the MLB hub, self-tested
redis_client.py         Redis wrapper with in-memory fallback — verbatim copy from the MLB hub
routes/games.py         /api/games/*, /api/game/<id>, /api/odds/status — lines + de-vig + best-price EV
routes/props.py         /api/props/board, /api/props/game/<id>, /api/edges/week
routes/players.py       /api/player/<pid> — game logs + per-market projections vs next opponent + book lines
routes/tracker_routes.py /api/tracker/* CRUD + performance + grade + closing-capture + live pace + settings
static/theme.css        design system (volt-accent sister to the MLB hub; validated chart colors)
static/app.js           shared shell: topbar/bottom-nav, persistent week state, bet-slip store
dashboard/props/game/player/tracker.html   pages on the shared shell, fetch /api/* client-side
```

## Key invariants

- **Week-based navigation, date-based tracker.** Slates are (season, week,
  season_type REG|POST); tracker days are the game's `gameday` (YYYY-MM-DD).
  `nfl_data.current_week()` derives the current week from the schedule;
  `nfl_data.stats_season()` returns the season whose stats feed projections
  (falls back to the prior season during offseason/early weeks).
- **ESPN is the source; schema is nflverse-compatible.** Weekly stat rows use
  nflverse column names (`passing_yards`, `carries`, `targets`, ...). A future
  nflverse import must only replace the internals of
  `get_player_week_stats()`. Stats build incrementally by game_id into
  `data/player_week_{season}.csv` — never re-fetch games already cached.
- **Model honesty.** `projections.py` distributions were chosen against the
  leave-forward backtest (`python projections.py`): log-normal rush/rec yds,
  normal pass yds, Poisson receptions/TDs (+ `_TD_DAMP` overdispersion damp).
  Any model change must re-run the backtest and keep predicted-vs-actual
  over-rates within a few points. `MIN_MEAN` volume floors gate fringe rows.
  Displayed edge is capped ±0.30 (`EDGE_DISPLAY_CAP`).
- **Odds credit hygiene.** One frozen snapshot per day (persisted, restored on
  boot). `fetch_event_odds_live()` is ONLY for the closing-capture window;
  `_closing_captured` prevents re-spending per game. Alt markets behind
  `NFL_ODDS_INCLUDE_ALT` (default off).
- **Tracker schema is the MLB hub's.** Entries carry id/savedAt/gradedAt/
  marketKey/line/side/price/grade/modelProb/clvEdge...; dedup key is
  (date, gameId, player, marketKey, line); replacing a dup keeps the original
  id. CLV convention: `clvEdge = closingImplied - openingImplied`, positive =
  beat the close, and it is the PRIMARY KPI (hero on tracker.html).
  Market keys: props `pass_yds|pass_tds|rush_yds|receptions|rec_yds|
  anytime_td`; game markets `h2h|spread|total` (spread line = picked team's
  number; total side = over|under).
- **Gunicorn:** 1 worker, gthread, `preload_app=False` (daemon cache loaders
  don't survive fork), preload runs in `post_fork` after the port binds so
  `/health` is 200 from second 0. Same rationale as the MLB hub — don't
  change without re-reading gunicorn_conf.py's comments.
- **Graceful degradation.** No ODDS_API_KEY → every odds surface returns
  empty and routes still 200. Missing stats → projections return None and the
  row is skipped, never fabricated.
- **Frontend shell.** Every page loads `static/theme.css` + `static/app.js`
  and calls `NFLHub.shell(active)` — that injects topbar, mobile bottom nav,
  and the bet-slip drawer. The slip lives in localStorage (`nfl.slip`) until
  "Confirm picks" POSTs each row to `/api/tracker/pick`; week selection
  persists in `nfl.week`. New pages must use the shell, not hand-rolled nav.
  The player-page chart's over/under bar colors (`--bar-over`/`--bar-under`)
  are CVD/contrast-validated on the card surface — don't swap them casually.
- **Live gameday.** dashboard polls the week endpoint every 30s only while a
  game is in progress; tracker polls `/api/tracker/live` (60s) which reads
  `nfl_data.live_game_stats()` (60s-TTL boxscore cache) for pick pacing.

## Verification

- `python value_engine.py` and `python projections.py` (selftest + backtest).
- `python app.py`, then: `/api/games/week?season=<last>&week=18`,
  `/api/props/board?...`, tracker pick → `/api/tracker/grade` cycle against
  known final scores (grading reads real ESPN boxscore stats).
- Frontend: load all four pages; they must render with odds offline.
