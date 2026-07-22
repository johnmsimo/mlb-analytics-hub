# NFL Analytics Hub

NFL prop-betting research and tracking platform: weekly slate dashboard,
game lines/totals with de-vig fair prices, a player-props board backed by a
calibration-tested analytic projection model, and a bet tracker whose primary
KPI is Closing Line Value.

Companion to the MLB Analytics Hub — same architecture patterns, deliberately
modular from day one (no monolith).

## Quick start

```bash
pip install -r requirements.txt
python app.py            # dev server on $PORT or 10000
```

Production (mirrors Fly.io):

```bash
gunicorn app:app -c gunicorn_conf.py
```

Works with zero configuration: schedules, stats, and projections come from
ESPN's public API. Set `ODDS_API_KEY` to light up prices, edges, EV, Kelly
staking, and closing-line capture.

## Environment variables

| Var | Default | Purpose |
|-----|---------|---------|
| `ODDS_API_KEY` | — | The Odds API key (`americanfootball_nfl`). Absent → odds surfaces degrade gracefully |
| `ODDS_REGION` | `us` | Odds API region |
| `NFL_ODDS_GAME_TTL_SEC` | `21600` | Game-lines snapshot TTL |
| `NFL_ODDS_PROPS_TTL_SEC` | `21600` | Per-event props snapshot TTL |
| `NFL_ODDS_INCLUDE_ALT` | `0` | Add `*_alternate` prop markets (more credits) |
| `TRACKER_CLOSING_CAPTURE_ENABLED` | `1` | Closing-line capture worker |
| `TRACKER_CLOSING_CAPTURE_MINUTES` | `5` | Worker interval |
| `TRACKER_CLOSING_LEAD_MIN` / `TRACKER_CLOSING_GRACE_MIN` | `20` / `15` | Capture window around kickoff |
| `TRACKER_AUTO_SYNC_MINUTES` | `30` | Auto-grading interval |
| `DATA_DIR` / `NFL_DATA_DIR` | `./data` | Persistent state (Fly volume mounts here) |
| `REDIS_URL` | — | Optional; in-memory fallback otherwise |
| `PORT` | `10000` dev / `8080` Fly | Bind port |

A `.env` in the repo root is auto-loaded at boot.

## Pages

| Route | Page | Purpose |
|-------|------|---------|
| `/` | dashboard.html | Weekly slate: scores, records, line pills; auto-refreshes while games are live |
| `/props` | props.html | Props board: projection, P(over), best price, edge/EV/Kelly, add-to-slip |
| `/game/<id>` | game.html | Lines board (de-vig) with slip buttons, defense-vs-position, top props |
| `/player/<pid>` | player.html | Game-log chart vs an adjustable line, hit-rate splits, projection, matchup |
| `/tracker` | tracker.html | CLV hero, live pick pacing during games, picks, bankroll settings |

All pages share `static/theme.css` + `static/app.js` (mobile-first shell:
bottom nav on phones, persistent week state, and a sportsbook-style bet slip
that saves confirmed picks to the tracker).

## Data sources

- **ESPN public API** — schedules/scores (`scoreboard`), per-player game
  stats (`summary` boxscores, ingested incrementally into
  `data/player_week_{season}.csv`), positions (roster sweep). The weekly-stat
  schema intentionally mirrors nflverse's `player_stats` columns
  (`passing_yards`, `carries`, `receptions`, `targets`, ...) so a
  nflverse CSV import can drop in behind `get_player_week_stats()` without
  touching callers.
- **The Odds API** — h2h/spreads/totals + player props, one frozen snapshot
  per day (restored on boot to save credits) + a forced refresh per game at
  kickoff for closing-line capture.

## Model honesty

`projections.py` is a distribution-based analytic model (no ML in v1):
log-normal for rush/rec yards (right-skewed), normal for pass yards, Poisson
for receptions/TDs with an overdispersion damp on anytime-TD. Distribution
choices were driven by a leave-forward backtest on the full prior season
(`python projections.py`), which projects every player-week from only prior
weeks and compares predicted P(over) with the realized over-rate per market —
all six markets calibrate within ~3 points. Rows are tagged
`modelSource: 'analytic'` so a future ML layer can slot in cleanly.

## Deploy (Fly.io)

```bash
fly apps create nfl-analytics-hub
fly volumes create nfl_data --region ewr --size 1
fly secrets set ODDS_API_KEY=...
fly deploy
```

## Option D directory stage

Dedicated product pages are now available at `/games`, `/players`, `/teams`, and `/projections`.
Their supporting APIs are `/api/games/week`, `/api/players`, `/api/teams`, and `/api/projections`.
The directory endpoints support season selection, search, and relevant filters while reusing the existing schedule, player-log, defense-vs-position, and projection layers.

### Option D product sections

The dedicated product navigation now includes working pages and APIs for Games, Players, Teams, Projections, Analytics, Rankings, and Settings. Analytics and rankings are descriptive, transparent calculations built from the local season data; they are not represented as a trained machine-learning model or wagering advice.

## Authentication and production security

Phase 6 adds session authentication, CSRF protection for all state-changing requests, per-user/IP rate limits on sensitive endpoints, strict JSON validation, secure session cookies, a one-megabyte request-size ceiling, and common browser security headers.

Configure production with:

```bash
APP_ENV=production
SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
ADMIN_USERNAME=admin
ADMIN_PASSWORD='use-a-password-manager-generated-secret'
```

Development defaults to username `admin` and password `nfl-dev` when `ADMIN_PASSWORD` is unset. Never use that default publicly. For local-only work, `AUTH_DISABLED=1` bypasses sign-in; it is not intended for production.

## Database warehouse

Phase 7 adds a normalized SQL data store. SQLite is used automatically for local development at `data/nfl_analytics.db`; production should set `DATABASE_URL` to PostgreSQL.

Initialize and import existing cached data:

```bash
pip install -r requirements.txt
python init_db.py --sync
```

Inventory and sync endpoints:

```text
GET  /api/data/status
POST /api/data/sync
```

Core tables cover teams, seasons, games, team-game stats, players, player-team seasons, player-game stats, coaches, coaching assignments, versioned analytics snapshots, and data-sync audit runs. The coach schema is ready; a licensed/reliable coaching source still needs to be selected before automated coach ingestion is enabled.

### Warehouse aggregation

```bash
python rebuild_analytics.py --season 2025
python import_coaches.py data/coaches.csv
```

The database now includes season-level team, player, and coach aggregates. Coach records are imported only from a verified CSV feed; no staff names are fabricated.

### Data platform pipeline

Run a complete ingestion, aggregate rebuild, and data-quality pass:

```bash
python sync_pipeline.py
```

Phase 9 adds raw source provenance, source licensing metadata, repeatable quality checks, and consolidated profile APIs for teams, players, and coaches. See `DATABASE.md` for the schema and endpoint contract.

## Phase 12: credentialed integrations

The warehouse now includes opt-in connectors for current stadium weather, live NFL odds, verified coaching assignments, and league transactions. Credentials are never required at startup; each connector fails closed when its key is absent.

```bash
flask --app app db upgrade
python sync_commercial.py --season 2026 --week 1 --datasets weather,odds
python sync_commercial.py --season 2026 --datasets coaches,transactions
```

Required environment variables are documented in `.env.example`. The OpenWeather connector uses stadium latitude/longitude, The Odds API stores timestamped bookmaker/market/outcome snapshots, and SportsDataIO endpoint templates are configurable to match the feeds enabled on your subscription.

Admin trigger:

```text
POST /api/admin/commercial-sync?season=2026&week=1&datasets=weather,odds,coaches,transactions
```

Because live feeds, official tracking data, Next Gen Stats, contracts, and redistribution rights depend on paid agreements, those datasets cannot be populated without the corresponding provider credentials and entitlements. The provider boundary and raw-provenance layer are ready for additional licensed adapters.

## Version 2.0 — Football Intelligence Platform

Version 2.0 adds a warehouse-backed intelligence layer rather than reading directly from provider payloads.

### New capabilities

- Multi-season backfill orchestration (`historical_backfill.py`)
- Team intelligence profiles with record, EPA, success rates and injury context
- Matchup intelligence combining team ratings, current injuries, weather and odds freshness
- Live Intelligence Center at `/live`
- Data-source freshness and recent ingestion-run monitoring
- Versioned `/api/v2` contracts for future web, mobile and assistant clients

### Historical backfill

```bash
python historical_backfill.py --start 2016 --end 2025 \
  --datasets pbp,rosters,injuries,depth_charts,snap_counts
```

Credentialed datasets can be included when configured:

```bash
python historical_backfill.py --start 2024 --end 2025 \
  --datasets pbp,rosters,injuries,depth_charts,snap_counts \
  --commercial weather,odds,coaches,transactions
```

### Version 2 APIs

```text
GET /api/v2/platform/health
GET /api/v2/live?season=2025&week=1
GET /api/v2/teams/PHI/intelligence?season=2025
GET /api/v2/games/{game_id}/intelligence
```

Public nflverse datasets are appropriate for historical analysis and delayed updates. Low-latency production use still requires configured commercial provider credentials and appropriate licensing.

## Version 2.1 — Integration Control Plane

The unified integration endpoint exposes configuration status without returning
secret values:

```bash
curl http://localhost:10000/api/admin/integrations
```

Run a combined sync (partial failures are returned with HTTP 207):

```bash
curl -X POST http://localhost:10000/api/admin/integrations/sync \
  -H 'Content-Type: application/json' \
  -d '{"season": 2025, "week": 1, "datasets": ["live_games", "weather", "odds", "injuries"]}'
```

Weather defaults to the keyless National Weather Service adapter. Set an
identifying `NWS_USER_AGENT`. Set `WEATHER_PROVIDER=openweather` and configure
`OPENWEATHER_API_KEY` to use the OpenWeather fallback.

Credentialed feeds fail closed and are reported per dataset, allowing public
nflverse and NWS imports to complete even when commercial credentials are not
present.

## Version 2.2 deployment release

This repository is ready for a private GitHub repository and Fly.io deployment.

- Production image: `Dockerfile`
- Fly process configuration: `fly.toml`
- Database migration release command: `flask --app app db upgrade`
- CI workflow: `.github/workflows/ci.yml`
- Continuous deployment: `.github/workflows/fly.yml`
- Deployment guide: `DEPLOY_FLY.md`

The deployed architecture uses separate `web` and `worker` process groups. PostgreSQL and Redis connection strings must be configured as Fly secrets.
