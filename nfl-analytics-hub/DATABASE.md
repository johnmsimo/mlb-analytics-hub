# NFL Analytics Database

## Storage strategy

- Development: SQLite (`data/nfl_analytics.db`)
- Production: PostgreSQL via `DATABASE_URL`
- ORM: SQLAlchemy 2.x / Flask-SQLAlchemy
- Imports are idempotent and tracked in `data_sync_runs`

## Core entities

- `teams`: canonical franchises and abbreviations
- `seasons`: season dimension
- `games`: schedule, status, venue, participants, score
- `team_game_stats`: team-level game facts and advanced-stat fields
- `players`: canonical player identity and bio fields
- `player_team_seasons`: roster history by team and season
- `player_game_stats`: per-player, per-game box-score facts
- `coaches`: canonical coach identity
- `coaching_assignments`: team, season, role, and tenure history
- `analytics_snapshots`: versioned model outputs and derived metrics
- `data_sync_runs`: ingestion audit trail

## Data quality rules

- External IDs are unique when available.
- Game and player-game facts use natural uniqueness constraints.
- Home and away teams must differ.
- Foreign keys are enforced in SQLite and PostgreSQL.
- Sync jobs are safe to rerun and update existing rows.

## Current import coverage

The included importer loads all cached schedule and player-week files. Coach and richer team-stat ingestion are schema-ready but require a selected source and mapping contract. Use `data/coaches_template.csv` as the initial manual/licensed feed format.

## Phase 8: aggregate warehouse and coach ingestion

The normalized game facts are now rolled up into query-optimized season tables:

- `team_season_stats`
- `player_season_stats`
- `coach_season_stats`

Completed games also generate two `team_game_stats` rows (one per team). These are derived only from recorded scores; richer fields such as EPA and success rate remain null until an authorized play-by-play source is connected.

Import coach assignments using the documented CSV contract:

```bash
cp data/coaches_template.csv data/coaches.csv
# Replace the example row with licensed/verified records.
python import_coaches.py data/coaches.csv
```

Rebuild all aggregate tables:

```bash
python rebuild_analytics.py
python rebuild_analytics.py --season 2025
```

Administrative endpoints:

```text
GET  /api/data/coaches?season=2025
POST /api/data/rebuild-analytics?season=2025
```

All rebuild operations are idempotent. They update existing aggregate rows rather than duplicating them.

## Phase 9: provenance, quality, and entity profiles

The warehouse now keeps a source registry and immutable raw record versions before normalization. This makes data changes auditable and allows future reprocessing when parsers or analytics models change.

### Full pipeline

```bash
python sync_pipeline.py
python sync_pipeline.py --season 2025 --skip-ingest
```

The pipeline performs these idempotent stages:

1. Register the source and capture versioned raw payloads.
2. Upsert normalized teams, games, players, memberships, and facts.
3. Rebuild team, player, and coach season aggregates.
4. Run referential-integrity and sanity checks.

### Data-management endpoints

```text
GET  /api/data/status
GET  /api/data/sources
GET  /api/data/quality?severity=error&resolved=false
POST /api/data/quality/run
GET  /api/data/teams/{abbr}/profile
GET  /api/data/players/{id}/profile
GET  /api/data/coaches/{id}/profile
```

All write endpoints remain protected by authentication and CSRF controls.

### Quality checks

Current checks detect invalid completed games, impossible scores, duplicate team identities, negative player counting stats, player facts attached to a non-participating team, missing player-team-season memberships, and coaches without assignments.

## Phase 10 production platform

### Migrations and PostgreSQL

Use migrations for every production schema change:

```bash
flask --app app db migrate -m "describe schema change"
flask --app app db upgrade
```

PostgreSQL connection pooling is controlled by `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, and `DB_POOL_TIMEOUT`.

### Play-by-play ingestion

A normalized template is provided at `data/play_by_play_template.csv`.

```bash
python import_play_by_play.py data/play_by_play.csv --season 2025
```

This populates play facts and rebuilds team EPA, success rate, third-down, red-zone, and explosive-play metrics.

### Scheduler

For local or small deployments:

```bash
ENABLE_SCHEDULER=true python scheduler_service.py
```

Production should run exactly one scheduler service, separate from Gunicorn web workers.

### Operations

- `/ready` checks database readiness.
- `/metrics` exposes Prometheus metrics.
- `/admin/data` provides the authenticated data control center.
- `/api/admin/overview` reports freshness, inventory, jobs, and source status.

### Local stack

```bash
docker compose up --build
```

This starts PostgreSQL, Redis, the web app, and a separate scheduler service.

## Phase 11: External provider synchronization

The default external connector uses nflverse release assets for play-by-play and
`nflreadpy` for weekly rosters, injuries, depth charts, and snap counts.

```bash
flask --app app db upgrade
python sync_external.py --season 2025 --datasets pbp
python sync_external.py --season 2025 --datasets rosters,injuries,depth_charts,snap_counts
```

To enable scheduled imports in the dedicated scheduler process:

```env
ENABLE_EXTERNAL_SYNC=true
EXTERNAL_DATA_SEASON=2025
EXTERNAL_DATASETS=pbp,rosters,injuries,depth_charts,snap_counts
```

The public nflverse data is appropriate for development, research, and
historical analytics subject to its dataset-specific attribution and upstream
terms. A commercial live-data feed can be implemented behind
`external_providers.py` for production latency and service-level guarantees.
