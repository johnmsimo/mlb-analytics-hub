"""
draftkings_odds.py — Direct DraftKings unofficial sportsbook API scraper.

Fetches MLB game lines (moneylines, run lines, totals) and player props
(batter and pitcher) directly from DraftKings' internal sportsbook API.
No API key required — mirrors the same caching/devig patterns as nrfi_odds.py.

Public API:
  get_game_lines()                    -> list[dict]
  get_player_props(category="batter") -> list[dict]
  get_props_for_player(name, cat)     -> list[dict]
  get_all_mlb_odds()                  -> dict
  discover_offer_categories()         -> list[dict]
  normalize_player_name(name)         -> str
  cache_status()                      -> dict
"""

import re
import time
import logging
import threading

import requests

from config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DK_ODDS_TTL = settings.draftkings_odds_ttl_seconds

# DraftKings geo-routing prefix — NJ default; change via env if blocked.
# Options: dkusnj, dkusco, dkusil, dkuspa, dkusva, dkusmi
DK_GEO = settings.draftkings_geo

# MLB event group ID (stable)
MLB_EVENT_GROUP = settings.draftkings_mlb_event_group

# Known stable MLB offer category IDs — auto-validated at startup
_DEFAULT_CATEGORIES: dict = {
    "game_lines":    487,
    "batter_props":  1000,
    "pitcher_props": 1001,
    "game_props":    1002,
}

# Geo fallback rotation order
_GEO_FALLBACKS = ["dkusnj", "dkusco", "dkusil", "dkuspa"]

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://sportsbook.draftkings.com/",
    "Origin":          "https://sportsbook.draftkings.com",
}

# ---------------------------------------------------------------------------
# Module-level caches (mirrors nrfi_odds.py structure)
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()

_game_lines_cache:    dict = {"data": [], "ts": 0.0}
_batter_props_cache:  dict = {"data": [], "ts": 0.0}
_pitcher_props_cache: dict = {"data": [], "ts": 0.0}

# Live category IDs — updated by discover_offer_categories() at startup
_live_categories: dict = dict(_DEFAULT_CATEGORIES)

# ---------------------------------------------------------------------------
# Odds math helpers (same interface as nrfi_odds.py)
# ---------------------------------------------------------------------------

def american_to_prob(odds: float) -> float:
    if odds is None:
        return 0.0
    if odds >= 0:
        return 100.0 / (odds + 100.0)
    return abs(odds) / (abs(odds) + 100.0)


def prob_to_american(p: float) -> int:
    p = max(0.001, min(0.999, p))
    if p >= 0.5:
        return int(round(-(p / (1 - p)) * 100))
    return int(round(((1 - p) / p) * 100))


def devig_multiplicative(probs: list) -> list:
    total = sum(probs)
    if total <= 0:
        return probs
    return [p / total for p in probs]


def devig_power(probs: list, tol: float = 1e-6, max_iter: int = 200) -> list:
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

# ---------------------------------------------------------------------------
# Player name normalisation (for joining against FanGraphs / Statcast)
# ---------------------------------------------------------------------------

_NAME_SUFFIXES = re.compile(r"\b(jr|sr|ii|iii|iv)\b\.?", re.I)


def normalize_player_name(name: str) -> str:
    """Lowercase, strip suffixes and punctuation for fuzzy matching."""
    if not name:
        return ""
    n = name.lower().strip()
    n = _NAME_SUFFIXES.sub("", n)
    n = re.sub(r"[^a-z\s]", "", n)
    return " ".join(n.split())

# ---------------------------------------------------------------------------
# Low-level HTTP with geo rotation
# ---------------------------------------------------------------------------

def _dk_get(path: str, params: dict = None, retries: int = 2) -> dict:
    """GET a DraftKings endpoint, rotating geo prefixes on 403/451."""
    geos_to_try = [DK_GEO] + [g for g in _GEO_FALLBACKS if g != DK_GEO]
    for geo in geos_to_try:
        base = f"https://sportsbook-nash.draftkings.com/api/sportscontent/{geo}/v1"
        url  = f"{base}{path}"
        _params = {"format": "json", **(params or {})}
        for attempt in range(retries + 1):
            try:
                resp = requests.get(url, headers=_HEADERS, params=_params, timeout=12)
                if resp.status_code == 429:
                    wait = 10 * (attempt + 1)
                    logger.warning(f"[DK] 429 on geo={geo}, waiting {wait}s")
                    time.sleep(wait)
                    continue
                if resp.status_code in (403, 451):
                    logger.info(f"[DK] {resp.status_code} on geo={geo}, trying fallback")
                    break  # try next geo
                resp.raise_for_status()
                return resp.json()
            except requests.RequestException as exc:
                logger.warning(f"[DK] error (geo={geo}, attempt={attempt}): {exc}")
                if attempt < retries:
                    time.sleep(2 ** attempt)
    logger.error(f"[DK] all geos exhausted for {path}")
    return {}

# ---------------------------------------------------------------------------
# Category discovery
# ---------------------------------------------------------------------------

def discover_offer_categories(event_group_id: int = MLB_EVENT_GROUP) -> list:
    """
    Fetch root event-group payload, return all offer categories,
    and refresh _live_categories with today's actual IDs from DK.
    Call once at startup to avoid hardcoded ID drift.
    """
    data = _dk_get(f"/leagues/{event_group_id}")
    if not data:
        logger.warning("[DK] discover_offer_categories: empty response")
        return []

    categories = []
    for cat in data.get("eventGroup", {}).get("offerCategories", []):
        cat_id = cat.get("offerCategoryId")
        name   = cat.get("name", "")
        categories.append({"id": cat_id, "name": name})
        name_lower = name.lower()
        if "game line" in name_lower:
            _live_categories["game_lines"] = cat_id
        elif "batter" in name_lower:
            _live_categories["batter_props"] = cat_id
        elif "pitcher" in name_lower:
            _live_categories["pitcher_props"] = cat_id
        elif "game prop" in name_lower or "team total" in name_lower:
            _live_categories["game_props"] = cat_id

    logger.info(f"[DK] discovered {len(categories)} categories; live map: {_live_categories}")
    return categories

# ---------------------------------------------------------------------------
# Game lines
# ---------------------------------------------------------------------------

def _fetch_game_lines_raw(event_group_id: int = MLB_EVENT_GROUP) -> list:
    cat_id = _live_categories.get("game_lines", _DEFAULT_CATEGORIES["game_lines"])
    data   = _dk_get(f"/leagues/{event_group_id}/categories/{cat_id}")
    if not data:
        return []

    games = []
    for event in data.get("eventGroup", {}).get("events", []):
        game = {
            "game_id":    event.get("eventId"),
            "name":       event.get("name"),
            "start_time": event.get("startDate"),
            "home_team":  event.get("homeTeam", {}).get("name", ""),
            "away_team":  event.get("awayTeam", {}).get("name", ""),
            "markets":    [],
        }
        for offer_group in event.get("displayGroupOffers", []):
            for offer in offer_group:
                market = {"label": offer.get("label"), "outcomes": []}
                for outcome in offer.get("outcomes", []):
                    try:
                        odds_val = float(outcome.get("oddsAmerican") or 0) or None
                    except (TypeError, ValueError):
                        odds_val = None
                    market["outcomes"].append({
                        "label":        outcome.get("label"),
                        "line":         outcome.get("line"),
                        "odds":         odds_val,
                        "implied_prob": round(american_to_prob(odds_val), 4) if odds_val else None,
                    })
                game["markets"].append(market)
        games.append(game)

    logger.info(f"[DK] fetched {len(games)} game line records")
    return games


def _get_game_lines_cached(event_group_id: int = MLB_EVENT_GROUP) -> list:
    with _cache_lock:
        age    = time.time() - _game_lines_cache["ts"]
        cached = _game_lines_cache["data"]
    if age > DK_ODDS_TTL or not cached:
        fresh = _fetch_game_lines_raw(event_group_id)
        with _cache_lock:
            _game_lines_cache["data"] = fresh
            _game_lines_cache["ts"]   = time.time()
        return fresh
    return cached

# ---------------------------------------------------------------------------
# Player props
# ---------------------------------------------------------------------------

def _fetch_props_raw(category_key: str, event_group_id: int = MLB_EVENT_GROUP) -> list:
    cat_id = _live_categories.get(category_key, _DEFAULT_CATEGORIES.get(category_key))
    if not cat_id:
        logger.warning(f"[DK] unknown category key: {category_key}")
        return []

    data = _dk_get(f"/leagues/{event_group_id}/categories/{cat_id}")
    if not data:
        return []

    props = []
    for cat in data.get("eventGroup", {}).get("offerCategories", []):
        for sub in cat.get("offerSubcategoryDescriptors", []):
            market_name = sub.get("name", "")
            for offer_group in sub.get("offerSubcategory", {}).get("offers", []):
                for offer in offer_group:
                    player_name = offer.get("label", "")
                    prop = {
                        "market":      market_name,
                        "player":      player_name,
                        "player_norm": normalize_player_name(player_name),
                        "category":    category_key,
                        "outcomes":    [],
                    }
                    for outcome in offer.get("outcomes", []):
                        try:
                            odds_val = float(outcome.get("oddsAmerican") or 0) or None
                        except (TypeError, ValueError):
                            odds_val = None
                        prop["outcomes"].append({
                            "side":         outcome.get("label"),
                            "line":         outcome.get("line"),
                            "odds":         odds_val,
                            "implied_prob": round(american_to_prob(odds_val), 4) if odds_val else None,
                        })

                    # Power-devig fair probs (Over/Under pairs only)
                    overs  = [o for o in prop["outcomes"] if str(o.get("side", "")).lower() == "over"]
                    unders = [o for o in prop["outcomes"] if str(o.get("side", "")).lower() == "under"]
                    if overs and unders and overs[0]["odds"] and unders[0]["odds"]:
                        raw  = [american_to_prob(overs[0]["odds"]), american_to_prob(unders[0]["odds"])]
                        fair = devig_power(raw)
                        prop["fair_over_prob"]  = round(fair[0], 4)
                        prop["fair_under_prob"] = round(fair[1], 4)
                        prop["overround"]       = round(sum(raw) - 1.0, 4)

                    props.append(prop)

    logger.info(f"[DK] fetched {len(props)} {category_key} records")
    return props


def _get_props_cached(category_key: str, event_group_id: int = MLB_EVENT_GROUP) -> list:
    cache = _batter_props_cache if category_key == "batter_props" else _pitcher_props_cache
    with _cache_lock:
        age    = time.time() - cache["ts"]
        cached = cache["data"]
    if age > DK_ODDS_TTL or not cached:
        fresh = _fetch_props_raw(category_key, event_group_id)
        with _cache_lock:
            cache["data"] = fresh
            cache["ts"]   = time.time()
        return fresh
    return cached

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_game_lines(event_group_id: int = MLB_EVENT_GROUP) -> list:
    """Today's MLB game lines (moneyline, run line, totals) from DraftKings."""
    return _get_game_lines_cached(event_group_id)


def get_player_props(category: str = "batter", event_group_id: int = MLB_EVENT_GROUP) -> list:
    """Today's MLB player props. category: 'batter' or 'pitcher'."""
    key = "batter_props" if "batter" in category.lower() else "pitcher_props"
    return _get_props_cached(key, event_group_id)


def get_props_for_player(player_name: str, category: str = "batter") -> list:
    """
    Filter props to a single player by normalized name.
    Plug return value directly into your Monte Carlo edge calc.
    """
    needle = normalize_player_name(player_name)
    return [
        p for p in get_player_props(category)
        if needle in p.get("player_norm", "") or p.get("player_norm", "") in needle
    ]


def get_all_mlb_odds(event_group_id: int = MLB_EVENT_GROUP) -> dict:
    """Fetch game lines + batter props + pitcher props with polite 1.2s delays."""
    game_lines    = get_game_lines(event_group_id)
    time.sleep(1.2)
    batter_props  = get_player_props("batter", event_group_id)
    time.sleep(1.2)
    pitcher_props = get_player_props("pitcher", event_group_id)
    return {
        "game_lines":    game_lines,
        "batter_props":  batter_props,
        "pitcher_props": pitcher_props,
        "fetched_at":    time.time(),
    }


def cache_status() -> dict:
    """Cache age, record count, TTL, and geo for all three buckets."""
    now = time.time()
    with _cache_lock:
        return {
            "geo":             DK_GEO,
            "live_categories": _live_categories,
            "game_lines": {
                "age_sec":  round(now - _game_lines_cache["ts"], 1),
                "records":  len(_game_lines_cache["data"]),
                "ttl_sec":  DK_ODDS_TTL,
                "is_fresh": (now - _game_lines_cache["ts"]) < DK_ODDS_TTL,
            },
            "batter_props": {
                "age_sec":  round(now - _batter_props_cache["ts"], 1),
                "records":  len(_batter_props_cache["data"]),
                "ttl_sec":  DK_ODDS_TTL,
                "is_fresh": (now - _batter_props_cache["ts"]) < DK_ODDS_TTL,
            },
            "pitcher_props": {
                "age_sec":  round(now - _pitcher_props_cache["ts"], 1),
                "records":  len(_pitcher_props_cache["data"]),
                "ttl_sec":  DK_ODDS_TTL,
                "is_fresh": (now - _pitcher_props_cache["ts"]) < DK_ODDS_TTL,
            },
        }

# ---------------------------------------------------------------------------
# Warm caches at import time (daemon threads — won't block startup)
# ---------------------------------------------------------------------------

def _warm_all():
    try:
        discover_offer_categories()
        time.sleep(0.5)
        _fetch_game_lines_raw()
        time.sleep(1.2)
        _fetch_props_raw("batter_props")
        time.sleep(1.2)
        _fetch_props_raw("pitcher_props")
    except Exception as exc:
        logger.warning(f"[DK] warm_all failed: {exc}")


threading.Thread(target=_warm_all, daemon=True).start()
