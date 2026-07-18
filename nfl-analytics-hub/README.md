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
| `/` | dashboard.html | Weekly slate: scores, records, spread/total/ML pills |
| `/props` | props.html | Props board: projection, P(over), best price, edge/EV/Kelly, one-click Track |
| `/game/<id>` | game.html | Lines board (de-vig), defense-vs-position, top props |
| `/tracker` | tracker.html | CLV hero (Beat Close % / Avg CLV), record, picks, bankroll settings |

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
