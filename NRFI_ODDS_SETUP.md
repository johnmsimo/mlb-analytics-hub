# NRFI/YRFI Live Odds + Devig Setup

## Step 1 — Get a free Odds API key

1. Sign up at https://the-odds-api.com (free tier: 500 requests/month)
2. Copy your API key

## Step 2 — Add the env variable

**Render / Fly.io:**  
Add `ODDS_API_KEY=your_key_here` in your service environment variables.

**Local `.env` file:**  
```
ODDS_API_KEY=your_key_here
```

## Step 3 — Register routes in app.py

Add these two lines to `app.py` **after** `app = Flask(__name__)` and **before** `if __name__ == '__main__'`:

```python
from nrfi_odds_routes import register_nrfi_odds_routes
register_nrfi_odds_routes(app)
```

## Step 4 — Inject odds into your NRFI model route

Inside your existing NRFI game route (wherever you compute `nrfi_prob`), add:

```python
from nrfi_odds import get_nrfi_odds_for_game

# After computing nrfi_prob:
odds_info = get_nrfi_odds_for_game(away_team_name, home_team_name, nrfi_prob)

# Inject into your return payload:
return jsonify({
    ...,
    "nrfi_edge":           odds_info.get("nrfi_edge"),
    "yrfi_edge":           odds_info.get("yrfi_edge"),
    "book_price":          odds_info.get("book_price"),
    "bookmaker":           odds_info.get("bookmaker"),
    "market_nrfi_implied": odds_info.get("market_nrfi_implied"),
    "fair_nrfi_prob":      odds_info.get("fair_nrfi_prob"),
    "devig":               odds_info.get("devig"),
    "all_books":           odds_info.get("all_books"),
})
```

## API Endpoints Added

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/nrfi/odds?away=Yankees&home=Red+Sox&nrfi_prob=0.72` | Live odds + devig + edge |
| GET | `/api/nrfi/devig?nrfi_american=-130&yrfi_american=110` | Standalone devig any two odds |
| GET | `/api/nrfi/odds-cache-status` | Cache age + API quota remaining |

## Devig Methods Returned

| Method | Description | Best For |
|--------|-------------|----------|
| `power` (**recommended**) | Shin/power devig — solves for exponent k | NRFI two-way markets |
| `multiplicative` | Divide by overround | Standard quick check |
| `additive` | Subtract overround proportionally | Conservative baseline |
| `worst_case` | All vig on underdog | Wide fair-value range |

## Edge Interpretation

```
nrfi_edge = model_nrfi_prob - fair_nrfi_prob  (power devig)

+0.10 (+10%)  → STRONG PLAY  🔥
+0.05 (+5%)   → PLAY
+0.02 (+2%)   → LEAN
< +0.02       → No value
```

## Cache Behaviour

- Odds are fetched once from The Odds API and cached for **5 minutes** (configurable via `ODDS_NRFI_TTL_SEC` env var).
- Cache warms in the background on app startup.
- `x-requests-remaining` header is logged so you can monitor your free quota.
