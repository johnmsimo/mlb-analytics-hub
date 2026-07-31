# ⚾ MLB Analytics Hub

A full-stack MLB analytics and sports betting prediction platform built with Flask, XGBoost, and Monte Carlo simulations. Aggregates data from MLB StatsAPI, FanGraphs, Baseball Savant (Statcast), and BallparkPal to power real-time prop predictions, player deep dives, and value bet identification.

---

## 🚀 Features

- **Props Dashboard** — XGBoost-powered player prop predictions with confidence tiers and calibration
- **Player Deep Dives** — Batter and pitcher breakdowns with Statcast metrics, spray charts, and pitch arsenal analysis
- **NRFI / YRFI Odds** — First inning run probability models using pitcher matchup and ballpark data
- **Value Bets Engine** — Identifies edges between model-implied probabilities and sportsbook lines
- **Breakout Detector** — Flags players showing early signs of breakout performance using rolling Statcast trends
- **HR Analytics** — Home run probability modeling with bat tracking data and launch angle metrics
- **Matchup Cards** — Side-by-side batter vs. pitcher matchup analysis with historical splits
- **Consistency Tracker** — Player consistency scoring and hit streak analysis for prop targeting
- **Bet Tracker** — Personal bet tracking with ROI and performance metrics
- **Cheat Sheet** — Daily condensed view of top plays and model outputs

---

## 🧠 ML & Modeling

| Module | Description |
|--------|-------------|
| `xgb_prop_pipeline.py` | XGBoost feature engineering and model training pipeline for player props |
| `xgb_prop_scorer.py` | Real-time scoring of player props against trained models |
| `xgb_training_pipeline.py` | Full training workflow with cross-validation and hyperparameter tuning |
| `stacked_calibrator.py` | Stacked ensemble calibration for probability outputs |
| `tier_calibrator.py` | Tiered confidence scoring system |
| `eval_models.py` | Model evaluation metrics, Brier scores, and calibration curves |
| `train_hr_tb_rbi.py` | Specialized training for HR, Total Bases, and RBI props |
| `nrfi_odds.py` | NRFI/YRFI probability model |

---

## 📡 Data Sources & Loaders

| Module | Source |
|--------|--------|
| `fangraphs_loader.py` | FanGraphs advanced stats (wRC+, xFIP, Stuff+, etc.) |
| `fg_stuff_loader.py` | FanGraphs Stuff+ / pitching quality metrics |
| `savant_arsenal.py` | Baseball Savant pitch arsenal data |
| `savant_bat_tracking.py` | Statcast bat tracking (swing speed, attack angle) |
| `framing_loader.py` | Catcher framing metrics |
| `ballparkpal_loader.py` | Park factor data from BallparkPal |
| `travel_features.py` | Team travel distance and fatigue features |
| `bq_etl.py` | BigQuery ETL pipeline for historical data |

---

## 🏗️ Architecture

```
app.py                     # Flask application entry point + all routes
pipeline_scheduler.py      # Automated data pipeline scheduling
pipeline_routes.py         # Pipeline trigger API endpoints
redis_client.py            # Redis caching layer
gunicorn_conf.py           # Gunicorn production config
Dockerfile                 # Docker container definition
fly.toml                   # Fly.io deployment config
```

**Frontend:** HTML/CSS/JavaScript dashboards served via Flask routes (`dashboard.html`, `props.html`, `deepdive.html`, `tracker.html`, etc.)

**Backend:** Python/Flask with Redis caching, Gunicorn as the WSGI server

**Deployment:** Dockerized, deployed on [Fly.io](https://fly.io)

---

## 🛠️ Setup & Installation

### Prerequisites

- Python 3.11+
- Redis
- Docker (for containerized deployment)

### Local Development

```bash
# Clone the repository
git clone https://github.com/johnmsimo/mlb-analytics-hub.git
cd mlb-analytics-hub

# Install dependencies
pip install -r requirements.txt

# Set environment variables
cp .env.example .env
# Fill in API keys: FANGRAPHS_TOKEN, etc.

# Start Redis
redis-server

# Run the Flask app
python app.py
```

### Docker

```bash
docker build -t mlb-analytics-hub .
docker run -p 8080:8080 mlb-analytics-hub
```

### Deploy to Fly.io

```bash
fly deploy
```

---

## 📁 Project Structure

```
mlb-analytics-hub/
├── app.py                        # Main Flask app
├── data/                         # Cached data files
├── models/                       # Saved XGBoost model artifacts
├── notebooks/                    # Exploratory analysis notebooks
├── static/                       # Static assets (CSS, JS, images)
├── docs/                         # Documentation
├── .github/                      # GitHub Actions workflows
├── requirements.txt
├── Dockerfile
└── fly.toml
```

---

## 📊 Key Stats & Props Modeled

- **Hitting:** Hits, Total Bases, Home Runs, RBIs, Runs Scored, Strikeouts
- **Pitching:** Strikeouts (K), Earned Runs, Outs Recorded
- **Game Props:** NRFI/YRFI, First 5 Innings totals
- **Parlays:** Correlated prop stacking analysis

---

## ⚙️ Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `PORT` | Gunicorn/Fly.io HTTP port | `8080` |
| `DATA_DIR` | Persistent application data directory | `./data` |
| `REDIS_URL` | Redis connection string; in-memory fallback when unset | unset |
| `REDIS_HEALTH_INTERVAL` | Seconds between Redis health probes | `30` |
| `REDIS_FAILURE_THRESHOLD` | Consecutive failures before opening the circuit | `5` |
| `REDIS_CIRCUIT_TIMEOUT` | Seconds before a half-open recovery probe | `60` |
| `CACHE_STALE_TTL` | Extra seconds to retain stale-if-error cache shadows | `300` |
| `CACHE_ALLOW_STALE` | Serve stale cached data when recomputation fails | `true` |
| `MLB_SCHEDULE_CACHE_TTL` | Freshness window for shared hydrated date/game schedules | `120` |
| `MLB_STATS_API_BASE_URL` | MLB Stats API root used by the shared client | `https://statsapi.mlb.com/api` |
| `MLB_HTTP_TIMEOUT` / `MLB_BULK_HTTP_TIMEOUT` | Standard and bulk MLB request timeouts in seconds | `10` / `60` |
| `MLB_SLOW_REQUEST_MS` | Slow MLB upstream structured-log threshold in milliseconds | `1000` |
| `PERFORMANCE_MONITOR_ENABLED` | Collect bounded in-process request latency metrics | `true` |
| `PERFORMANCE_SLOW_MS` | Slow-request structured-log threshold in milliseconds | `1000` |
| `PERFORMANCE_SAMPLE_SIZE` | Recent request durations retained for aggregate percentiles | `2048` |
| `PERFORMANCE_ROUTE_LIMIT` | Maximum normalized route groups retained per process | `256` |
| `XGB_SCORE_CACHE_TTL` | Process-local XGBoost scoring-result freshness window | `300` |
| `XGB_SCORE_CACHE_MAX_ENTRIES` | Maximum memoized XGBoost scoring results per worker | `2048` |
| `ADMIN_TOKEN` | Protects pipeline and training mutations | unset |
| `CACHE_ADMIN_TOKEN` | Protects cache invalidation and metric-reset endpoints | unset |
| `CACHE_TTL_LIVE` / `SCHEDULE` / `STATS` / `ANALYTICS` / `STATIC` | Shared cache TTL policies in seconds | `30` / `300` / `3600` / `900` / `21600` |
| `HTTP_RETRY_TOTAL` / `HTTP_RETRY_BACKOFF` | Outbound HTTP retry count and backoff factor | `3` / `0.5` |
| `HTTP_POOL_CONNECTIONS` / `HTTP_POOL_MAXSIZE` | HTTPS connection pool sizing | `16` / `32` |
| `GOOGLE_CLOUD_PROJECT` | Enables BigQuery ETL when configured | unset |
| `BQ_DATASET` / `BQ_LOCATION` | BigQuery dataset and region | `mlb` / `US` |
| `BQ_ETL_HOUR_ET` / `BQ_ETL_MINUTE_ET` | Daily ETL schedule in Eastern time | `9` / `30` |
| `ODDS_API_KEY` | The Odds API credential | unset |
| `ODDS_NRFI_TTL_SEC` / `DK_ODDS_TTL_SEC` | Sportsbook cache TTLs in seconds | `300` / `300` |
| `DK_GEO` / `DK_MLB_EVENT_GROUP` | DraftKings routing and MLB event group | `dkusnj` / `84240` |
| `MLB_BASE_URL` / `MLB_ADMIN_TOKEN` | Backfill script target and optional token override | production URL / `ADMIN_TOKEN` |

Runtime modules read these values through `config.settings`, which applies type conversion, safe numeric fallbacks, and range validation. Redis writes are mirrored to process memory, the circuit breaker automatically fails over during outages, and health probes restore Redis after recovery. `/api/cache/status` exposes the secret-safe backend, circuit, latency, failure, and stale-cache state.

Request performance monitoring adds `X-Response-Time-Ms` and `Server-Timing` headers, emits structured logs for requests over `PERFORMANCE_SLOW_MS`, and exposes bounded, normalized route aggregates at `GET /api/performance/status`. Raw URLs, query strings, headers, and bodies are never retained. `POST /api/performance/metrics/reset` requires `X-Admin-Token` matching `ADMIN_TOKEN`.

MLB schedule, team-venue, boxscore, v1.1 live-feed, player/stat, roster, standings, and transaction reads use the pooled shared MLB client and resilient cache. Repeated consumers share endpoint-aware freshness policies (`LIVE`, `SCHEDULE`, `STATS`, or `STATIC`), schedule reads retain the dedicated `MLB_SCHEDULE_CACHE_TTL`, concurrent misses are deduplicated, query parameters are isolated, and stale data remains available during short MLB or Redis outages. DraftKings MLB and The Odds API MLB reads use the same safeguards with their existing `DK_ODDS_TTL_SEC` and `ODDS_NRFI_TTL_SEC` freshness windows; unrelated external APIs remain uncached.

MLB Stats API traffic from shared schedule caching, game-day loaders, the pipeline scheduler, and BigQuery ETL uses one pooled retrying client. Slow/error logs contain normalized endpoint patterns without query parameters.

Umpire historical features and daily home-plate assignments use thread-safe, mtime-aware parsed snapshots. Exact, last-name, and first-name fallback indexes preserve the existing matching order without reparsing or rescanning the source files for every K-prop feature build.

Game-day weather features use thread-safe, mtime-aware parsed snapshots. Repeated per-player lookups reuse the same three-hour date snapshot, while concurrent cold reads collapse to one schedule and Open-Meteo refresh.

Tracker analytics endpoints reuse one caller-private tracker snapshot and one
calibration-history snapshot for an entire response. Market/day series no
longer clone the full season store per market or per day, and bulk pick
recalculation loads model adjustments once while preserving identical results.
A representative 22,500-entry, 14-market dashboard build improved from about
190 ms to 42 ms.

The tracker snapshot is also indexed by date. Read-only day and rolling-window
routes deserialize only the requested dates instead of cloning the full
season-long `daily_tracker.json`; writers and full exports keep private mutable
full-store reads. Concurrent cold reads share one parse, and device/inode/mtime/
size invalidation detects atomic file replacement. A representative 12.3 MB,
180-day store made repeated 14-day reads about 17× faster with identical data.
Single-day tracker mutations also use copy-on-write: unchanged days reuse their
cached compact JSON, only the changed day is encoded again, and the atomic
replace advances the in-memory day snapshot immediately. Concurrent updates to
different dates remain serialized and full-history readers rebuild a private
store lazily only when they actually request one. On a representative 12.3 MB,
180-day tracker, 12 sequential single-day writes were about 5.1× faster with
identical final data. The date, today, and entries APIs also reuse immutable
JSON and gzip representations per tracker/adjustment file version. Strong ETags
are computed from the selected identity or gzip bytes, so only a validator for
the same encoding can make a refresh bodyless. Day commits and atomic file
replacements still invalidate cached bytes without changing decoded contracts.

Model adjustments and calibration history use thread-safe, file-version-aware
parsed snapshots across requests. Concurrent cold reads collapse to one JSON
parse, external and atomic file replacements invalidate automatically, and each
caller receives a private mutable copy.

The growing MLB memory store also uses a thread-safe, file-version-aware
snapshot with immutable per-snapshot blobs. Status and summary-only routes read
precomputed metadata without materializing the full seasonal store; full-store
callers build one immutable payload lazily under singleflight and still receive
private mutable values. Concurrent cold reads collapse to one parse and atomic
file replacements invalidate by device/inode/mtime/size. Three-hour collection
appends reuse canonical JSON for every unchanged retained snapshot, transforming
only the new snapshot and the one aging into compact history before the atomic
replacement. On identical copies of the checked-in 11.9 MB store, repeated warm
appends improved from about 173 ms to 90 ms (1.9×) with byte-equivalent retained
data and the same retention, compaction, and failure-recovery contracts.
Full and summary `/api/memory/latest` responses also cache their immutable JSON
and gzip representations per file version. Strong ETags are computed from the
selected identity or gzip bytes, so cross-encoding validators receive a full
200 response. Successful appends and atomic replacements still invalidate the
cached bytes automatically, and the decoded response schema is unchanged. On the
checked-in 594 KB latest snapshot, 100 repeated gzip-enabled route requests
improved from about 1.54 seconds to 0.036 seconds (15.36 ms to 0.36 ms each,
roughly 42.7×) with byte-identical decoded JSON.

XGBoost probability and full interval/Monte Carlo scoring results are memoized
per worker using the exact model, market, line, and final feature vector.
Concurrent identical scores collapse to one computation, returned objects are
copy-isolated, and TTL/LRU bounds prevent stale or unbounded process memory.

Savant bat-tracking, FanGraphs Stuff+, and catcher-framing feature lookups build
immutable ID/name indexes when their process-local dataframe snapshots refresh.
Hot scoring paths avoid repeated pandas column scans while preserving ID-first,
normalized-name, partial-name, and previous-season fallback behavior.
Bat-speed percentiles are also ranked once per Savant snapshot and reused by
BATX/XGBoost enrichment instead of rescanning the leaderboard for every batter.

The primary FanGraphs batting, pitching, and projection loader also indexes each
season dataframe once. Unique-player lookups no longer pay pandas boolean scans
across the six-season fallback chain, while sample-size merging and caller-copy
semantics remain unchanged.

Live-lineup feature lookups reuse an mtime-aware, per-worker parsed snapshot
indexed by MLB ID and exact player name. Hourly lineup-file refreshes invalidate
the snapshot automatically, concurrent stale requests collapse to one refresh,
and partial-name queries preserve their previous first-match behavior.

---

## 📄 License

MIT License — feel free to fork and build on top of this.

---

*Built by [@johnmsimo](https://github.com/johnmsimo)*

