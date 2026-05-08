"""
nrfi_odds.py — Live NRFI/YRFI odds ingestion + devig engine

Usage in app.py:
    from nrfi_odds import get_nrfi_odds_for_game, NRFI_ODDS_CACHE

Requires env var:  ODDS_API_KEY  (free tier: the-odds-api.com)
Falls back gracefully when key is missing or API is unreachable.
"""

import os
import time
import logging
import threading
import requests

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
ODDS_API_KEY   = os.getenv("ODDS_API_KEY", "").strip()
ODDS_API_BASE  = "https://api.the-odds-api.com/v4"
NRFI_MARKET    = "h2h_1st_1_innings"          # The Odds API NRFI/YRFI market key
NRFI_CACHE_TTL = int(os.getenv("ODDS_NRFI_TTL_SEC", "300"))  # 5-min default

# Priority bookmakers for best-line scanning (sharp → recreational)
BOOK_PRIORITY = [
    "pinnacle", "draftkings", "fanduel", "betmgm",
    "caesars", "pointsbet_us", "betrivers", "unibet_us",
]

# ── In-memory cache ───────────────────────────────────────────────────────────
_cache_lock  = threading.Lock()
_odds_cache  = {"data": [], "ts": 0.0, "remaining": None}


# ── Conversion helpers ────────────────────────────────────────────────────────

def american_to_prob(odds: float) -> float:
    """Raw (vig-inclusive) implied probability from American odds."""
    if odds is None:
        return 0.0
    if odds >= 0:
        return 100.0 / (odds + 100.0)
    return abs(odds) / (abs(odds) + 100.0)


def prob_to_american(p: float) -> int:
    """Convert a true probability back to American odds (rounded)."""
    p = max(0.001, min(0.999, p))
    if p >= 0.5:
        return int(round(-(p / (1 - p)) * 100))
    return int(round(((1 - p) / p) * 100))


# ── Devig methods ─────────────────────────────────────────────────────────────

def devig_additive(probs: list[float]) -> list[float]:
    """
    Additive (proportional) devig — subtract overround evenly.
    Good baseline; underestimates sharp edges on large favourites.
    """
    total = sum(probs)
    if total <= 0:
        return probs
    return [p / total for p in probs]


def devig_multiplicative(probs: list[float]) -> list[float]:
    """
    Multiplicative devig — divide each implied prob by the overround.
    Standard industry method; slight favourite bias.
    """
    total = sum(probs)
    if total <= 0:
        return probs
    return [p / total for p in probs]


def devig_power(probs: list[float], tol: float = 1e-6, max_iter: int = 200) -> list[float]:
    """
    Power (Shin) devig — solves for k such that sum(p^k) == 1.
    Most accurate for NRFI markets; used as the primary method here.
    Matches the devig_power() already in mc_upgrades.py.
    """
    if not probs or all(p <= 0 for p in probs):
        return probs
    total = sum(probs)
    if abs(total - 1.0) < tol:
        return probs
    lo, hi = 0.5, 2.0
    for _ in range(max_iter):
        mid = (lo + hi) / 2.0
        val = sum(p ** mid for p in probs)
        if abs(val - 1.0) < tol:
            break
        if val > 1.0:
            lo = mid
        else:
            hi = mid
    k = (lo + hi) / 2.0
    raw = [p ** k for p in probs]
    total_raw = sum(raw)
    return [r / total_raw for r in raw] if total_raw > 0 else probs


def devig_wc(probs: list[float]) -> list[float]:
    """
    Worst-case (min-vig) devig — assumes all vig is on the underdog.
    Conservative; produces wider fair-value range.
    """
    if len(probs) != 2:
        return devig_additive(probs)
    a, b = sorted(probs, reverse=True)  # a = favourite implied
    fair_a = a - (sum(probs) - 1.0)
    fair_a = max(0.001, min(0.999, fair_a))
    return [fair_a, 1.0 - fair_a]


def devig_all(nrfi_american: float, yrfi_american: float) -> dict:
    """
    Run all four devig methods and return a dict with fair probs + American odds.
    Returns empty dict on bad inputs.
    """
    try:
        raw_nrfi = american_to_prob(nrfi_american)
        raw_yrfi = american_to_prob(yrfi_american)
        raw = [raw_nrfi, raw_yrfi]
        overround = round(sum(raw) - 1.0, 4)

        methods = {
            "additive":       devig_additive(raw),
            "multiplicative": devig_multiplicative(raw),
            "power":          devig_power(raw),
            "worst_case":     devig_wc(raw),
        }

        out = {"overround": overround, "methods": {}}
        for name, (fair_nrfi, fair_yrfi) in methods.items():
            out["methods"][name] = {
                "nrfi_fair_prob": round(fair_nrfi, 4),
                "yrfi_fair_prob": round(fair_yrfi, 4),
                "nrfi_fair_american": prob_to_american(fair_nrfi),
                "yrfi_fair_american": prob_to_american(fair_yrfi),
            }
        # Power devig is our recommended method
        out["recommended"] = out["methods"]["power"]
        return out
    except Exception as ex:
        logger.warning(f"[devig_all] {ex}")
        return {}


# ── Odds API fetcher ──────────────────────────────────────────────────────────

def _fetch_nrfi_odds_raw() -> list:
    """Fetch NRFI/YRFI odds from The Odds API and cache result."""
    if not ODDS_API_KEY:
        logger.info("[nrfi_odds] ODDS_API_KEY not set — skipping odds fetch")
        return []
    try:
        url = f"{ODDS_API_BASE}/sports/baseball_mlb/odds"
        params = {
            "apiKey":      ODDS_API_KEY,
            "regions":     "us",
            "markets":     NRFI_MARKET,
            "oddsFormat":  "american",
            "dateFormat":  "iso",
            "bookmakers":  ",".join(BOOK_PRIORITY),
        }
        resp = requests.get(url, params=params, timeout=12)
        remaining = resp.headers.get("x-requests-remaining")
        logger.info(f"[nrfi_odds] Odds API requests remaining: {remaining}")
        resp.raise_for_status()
        data = resp.json()
        with _cache_lock:
            _odds_cache["data"]      = data or []
            _odds_cache["ts"]        = time.time()
            _odds_cache["remaining"] = remaining
        return data or []
    except Exception as ex:
        logger.warning(f"[nrfi_odds] fetch failed: {ex}")
        return []


def _get_cached_odds() -> list:
    """Return cached odds, refreshing if stale."""
    with _cache_lock:
        age = time.time() - _odds_cache["ts"]
        data = _odds_cache["data"]
    if age > NRFI_CACHE_TTL or not data:
        return _fetch_nrfi_odds_raw()
    return data


# ── Team name fuzzy matcher ───────────────────────────────────────────────────

_MLB_TEAM_ALIASES: dict[str, list[str]] = {
    "arizona diamondbacks":  ["diamondbacks", "arizona", "d-backs"],
    "atlanta braves":        ["braves", "atlanta"],
    "baltimore orioles":     ["orioles", "baltimore"],
    "boston red sox":        ["red sox", "boston"],
    "chicago cubs":          ["cubs", "chicago cubs"],
    "chicago white sox":     ["white sox", "chicago white"],
    "cincinnati reds":       ["reds", "cincinnati"],
    "cleveland guardians":   ["guardians", "cleveland"],
    "colorado rockies":      ["rockies", "colorado"],
    "detroit tigers":        ["tigers", "detroit"],
    "houston astros":        ["astros", "houston"],
    "kansas city royals":    ["royals", "kansas city"],
    "los angeles angels":    ["angels", "la angels", "los angeles angels"],
    "los angeles dodgers":   ["dodgers", "la dodgers", "los angeles dodgers"],
    "miami marlins":         ["marlins", "miami"],
    "milwaukee brewers":     ["brewers", "milwaukee"],
    "minnesota twins":       ["twins", "minnesota"],
    "new york mets":         ["mets", "ny mets", "new york mets"],
    "new york yankees":      ["yankees", "ny yankees", "new york yankees"],
    "oakland athletics":     ["athletics", "a's", "oakland", "las vegas athletics"],
    "philadelphia phillies": ["phillies", "philadelphia"],
    "pittsburgh pirates":    ["pirates", "pittsburgh"],
    "san diego padres":      ["padres", "san diego"],
    "san francisco giants":  ["giants", "san francisco"],
    "seattle mariners":      ["mariners", "seattle"],
    "st. louis cardinals":   ["cardinals", "st. louis", "stl"],
    "tampa bay rays":        ["rays", "tampa bay"],
    "texas rangers":         ["rangers", "texas"],
    "toronto blue jays":     ["blue jays", "toronto"],
    "washington nationals":  ["nationals", "washington"],
}


def _team_matches(api_team_name: str, mlb_team_name: str) -> bool:
    """Fuzzy match between Odds API team name and MLB Stats API team name."""
    api_lower = api_team_name.lower().strip()
    mlb_lower = mlb_team_name.lower().strip()
    if api_lower == mlb_lower:
        return True
    if api_lower in mlb_lower or mlb_lower in api_lower:
        return True
    # Check aliases
    for canonical, aliases in _MLB_TEAM_ALIASES.items():
        names_to_check = [canonical] + aliases
        api_match  = any(a in api_lower or api_lower in a for a in names_to_check)
        mlb_match  = any(a in mlb_lower or mlb_lower in a for a in names_to_check)
        if api_match and mlb_match:
            return True
    return False


# ── Core lookup ───────────────────────────────────────────────────────────────

def get_nrfi_odds_for_game(
    away_team: str,
    home_team: str,
    model_nrfi_prob: float | None = None,
) -> dict:
    """
    Find the best available NRFI price for a game and compute:
      - book_price (best American line across priority bookmakers)
      - bookmaker  (sportsbook offering that line)
      - market_nrfi_implied  (raw implied prob from best NRFI line)
      - market_yrfi_implied  (raw implied prob from matching YRFI line)
      - devig  (full devig breakdown via all four methods)
      - nrfi_edge  (model_nrfi_prob minus power-deviggged fair prob, if provided)
      - yrfi_edge  (1-model_nrfi_prob minus power-deviggged fair YRFI prob)
      - all_books  (list of every book's NRFI/YRFI American prices for comparison)

    Returns an empty dict if no match is found or API key is missing.
    """
    odds_data = _get_cached_odds()
    if not odds_data:
        return {}

    # ── Find matching game ────────────────────────────────────────────────
    matched_game = None
    for game in odds_data:
        api_home = game.get("home_team", "")
        api_away = game.get("away_team", "")
        home_ok  = _team_matches(api_home, home_team)
        away_ok  = _team_matches(api_away, away_team)
        # Also try reversed (Odds API sometimes swaps)
        if (home_ok and away_ok) or (_team_matches(api_home, away_team) and _team_matches(api_away, home_team)):
            matched_game = game
            break

    if not matched_game:
        logger.info(f"[nrfi_odds] No odds match for {away_team} @ {home_team}")
        return {}

    # ── Scan bookmakers — find best NRFI price and paired YRFI ────────────
    best_nrfi_american: float | None = None
    best_yrfi_american: float | None = None
    best_book: str = ""
    all_books: list[dict] = []

    book_map: dict[str, dict] = {}
    for book in matched_game.get("bookmakers", []):
        book_key   = book.get("key", "")
        book_title = book.get("title", book_key)
        nrfi_p = None
        yrfi_p = None
        for market in book.get("markets", []):
            if market.get("key") != NRFI_MARKET:
                continue
            for outcome in market.get("outcomes", []):
                name_lower = outcome.get("name", "").lower()
                price      = outcome.get("price")
                if price is None:
                    continue
                if "no run" in name_lower or "nrfi" in name_lower:
                    nrfi_p = float(price)
                elif "yes" in name_lower or "yrfi" in name_lower or "a run" in name_lower:
                    yrfi_p = float(price)
        if nrfi_p is not None:
            book_map[book_key] = {
                "book":            book_title,
                "book_key":        book_key,
                "nrfi_american":   nrfi_p,
                "yrfi_american":   yrfi_p,
                "nrfi_implied":    round(american_to_prob(nrfi_p), 4),
                "yrfi_implied":    round(american_to_prob(yrfi_p), 4) if yrfi_p else None,
            }
            all_books.append(book_map[book_key])

    if not book_map:
        return {}

    # Best NRFI = highest American price (most favourable to bettor)
    best_entry = max(all_books, key=lambda b: b["nrfi_american"])
    best_nrfi_american = best_entry["nrfi_american"]
    best_yrfi_american = best_entry["yrfi_american"]
    best_book          = best_entry["book"]

    # ── Devig ─────────────────────────────────────────────────────────────
    devig_result: dict = {}
    if best_yrfi_american is not None:
        devig_result = devig_all(best_nrfi_american, best_yrfi_american)

    fair_nrfi_prob = None
    if devig_result.get("recommended"):
        fair_nrfi_prob = devig_result["recommended"]["nrfi_fair_prob"]

    # ── Edge calculation ──────────────────────────────────────────────────
    nrfi_edge = None
    yrfi_edge = None
    if model_nrfi_prob is not None and fair_nrfi_prob is not None:
        nrfi_edge = round(float(model_nrfi_prob) - fair_nrfi_prob, 4)
        yrfi_edge = round((1.0 - float(model_nrfi_prob)) - (1.0 - fair_nrfi_prob), 4)

    return {
        "book_price":            best_nrfi_american,
        "bookmaker":             best_book,
        "market_nrfi_implied":   round(american_to_prob(best_nrfi_american), 4),
        "market_yrfi_implied":   round(american_to_prob(best_yrfi_american), 4) if best_yrfi_american else None,
        "nrfi_edge":             nrfi_edge,
        "yrfi_edge":             yrfi_edge,
        "fair_nrfi_prob":        fair_nrfi_prob,
        "devig":                 devig_result,
        "all_books":             all_books,
        "odds_api_remaining":    _odds_cache.get("remaining"),
    }


# ── Convenience: warm the cache in background at import time ─────────────────
def _warm_cache():
    try:
        _fetch_nrfi_odds_raw()
    except Exception:
        pass

if ODDS_API_KEY:
    threading.Thread(target=_warm_cache, daemon=True).start()
