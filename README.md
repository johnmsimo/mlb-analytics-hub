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

| Variable | Description |
|----------|-------------|
| `REDIS_URL` | Redis connection string |
| `FANGRAPHS_TOKEN` | FanGraphs API token |
| `SECRET_KEY` | Flask secret key |
| `BQ_CREDENTIALS` | BigQuery service account JSON (optional) |

---

## 📄 License

MIT License — feel free to fork and build on top of this.

---

*Built by [@johnmsimo](https://github.com/johnmsimo)*
