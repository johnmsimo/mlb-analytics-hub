# ⚡ MLB Analytics Hub

**Template 4 — Analytics Command Center**
Real-time MLB game dashboard with pitcher stats, weather, park factors, and lineup analysis.

## 🚀 Deploy to Render

1. Push these files to your GitHub repo
2. Go to [render.com](https://render.com) → New → Web Service
3. Connect your GitHub repo
4. Render auto-detects `render.yaml` — just click **Deploy**
5. Your live URL is ready in ~2 minutes

## 📁 File Structure

```
mlb-analytics-hub/
├── app.py                          ← Flask backend + MLB API
├── requirements.txt                ← Flask, gunicorn, requests
├── render.yaml                     ← Render auto-config
├── .gitignore
├── README.md
└── templates/
    ├── mlb_analytics_hub.html      ← Main dashboard
    └── deep_dive_analytics.html    ← Game analysis page
```

## 🔌 Data Sources

- **MLB Stats API** (free, no key needed) — games, pitchers, lineups, boxscores
- **Park Factors** — built-in constants for all 30 ballparks
- **Weather** — via MLB schedule hydration

## ⚙️ Run Locally

```bash
pip install -r requirements.txt
python app.py
# Visit http://localhost:5000
```

## 📡 API Endpoints

| Endpoint | Description |
|---|---|
| `GET /` | Main dashboard |
| `GET /deep-dive/<game_pk>` | Game analysis |
| `GET /api/games/today` | Today's games JSON |
| `GET /api/game/<game_pk>` | Boxscore / lineups |
| `GET /api/pitchers/<game_pk>` | Pitcher season stats |
