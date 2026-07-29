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

MLB schedule, team-venue, boxscore, v1.1 live-feed, player/stat, roster, standings, and transaction reads use the pooled shared MLB client and resilient cache. Repeated consumers share endpoint-aware freshness policies (`LIVE`, `SCHEDULE`, `STATS`, or `STATIC`), schedule reads retain the dedicated `MLB_SCHEDULE_CACHE_TTL`, concurrent misses are deduplicated, query parameters are isolated, and stale data remains available during short MLB or Redis outages.

MLB Stats API traffic from shared schedule caching, game-day loaders, the pipeline scheduler, and BigQuery ETL uses one pooled retrying client. Slow/error logs contain normalized endpoint patterns without query parameters.

XGBoost probability and full interval/Monte Carlo scoring results are memoized
per worker using the exact model, market, line, and final feature vector.
Concurrent identical scores collapse to one computation, returned objects are
copy-isolated, and TTL/LRU bounds prevent stale or unbounded process memory.

Savant bat-tracking, FanGraphs Stuff+, and catcher-framing feature lookups build
immutable ID/name indexes when their process-local dataframe snapshots refresh.
Hot scoring paths avoid repeated pandas column scans while preserving ID-first,
normalized-name, partial-name, and previous-season fallback behavior.

---

## 📄 License

MIT License — feel free to fork and build on top of this.

---

*Built by [@johnmsimo](https://github.com/johnmsimo)*
