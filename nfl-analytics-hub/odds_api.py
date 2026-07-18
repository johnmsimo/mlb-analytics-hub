"""
The Odds API layer for NFL (americanfootball_nfl).

Ported pattern from the MLB hub: one frozen daily snapshot per day persisted
to data/odds_cache.json (restored on boot so redeploys don't re-spend
credits), per-event prop fetches cached inside the snapshot, and an explicit
`fetch_event_odds_live()` bypass used only by the tracker's closing-line
capture around kickoff.

Degrades gracefully: without ODDS_API_KEY every getter returns empty and the
routes still 200.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone

import requests

import nfl_data

API_BASE = "https://api.the-odds-api.com/v4"
SPORT = "americanfootball_nfl"

GAME_MARKETS = "h2h,spreads,totals"
PROP_MARKETS = [
    "player_pass_yds", "player_pass_tds", "player_rush_yds",
    "player_receptions", "player_reception_yds", "player_anytime_td",
]
_ALT_MARKETS = [
    "player_pass_yds_alternate", "player_rush_yds_alternate",
    "player_receptions_alternate", "player_reception_yds_alternate",
]

_REGION = os.environ.get("ODDS_REGION", "us")
_GAME_TTL = int(os.environ.get("NFL_ODDS_GAME_TTL_SEC", "21600"))
_PROPS_TTL = int(os.environ.get("NFL_ODDS_PROPS_TTL_SEC", "21600"))
_INCLUDE_ALT = os.environ.get("NFL_ODDS_INCLUDE_ALT", "0") == "1"

_CACHE_FILE = os.path.join(nfl_data.DATA_DIR, "odds_cache.json")
_lock = threading.RLock()
_snapshot: dict | None = None


def _api_key() -> str | None:
    return os.environ.get("ODDS_API_KEY") or None


def is_configured() -> bool:
    return _api_key() is not None


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _load_snapshot() -> dict:
    global _snapshot
    with _lock:
        if _snapshot is not None:
            return _snapshot
        try:
            with open(_CACHE_FILE, "r", encoding="utf-8") as f:
                _snapshot = json.load(f)
        except Exception:  # noqa: BLE001
            _snapshot = {}
        if not isinstance(_snapshot, dict):
            _snapshot = {}
        return _snapshot


def _save_snapshot() -> None:
    with _lock:
        tmp = _CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_snapshot, f, separators=(",", ":"))
        os.replace(tmp, _CACHE_FILE)


def _get(path: str, **params):
    key = _api_key()
    if not key:
        return None
    params = {"apiKey": key, **params}
    r = requests.get(f"{API_BASE}{path}", params=params, timeout=25)
    if r.status_code != 200:
        raise RuntimeError(f"Odds API {r.status_code}: {r.text[:200]}")
    return r.json()


# ----------------------------------------------------------------- game odds

def get_game_odds(force: bool = False) -> list[dict]:
    """Featured markets (h2h/spreads/totals) for all upcoming NFL events.
    Snapshot-cached per day with a TTL; costs ~3 credits per refresh."""
    if not is_configured():
        return []
    snap = _load_snapshot()
    with _lock:
        blk = snap.get("game_odds") or {}
        fresh = (blk.get("date") == _today()
                 and time.time() - blk.get("fetched_at", 0) < _GAME_TTL)
        if fresh and not force:
            return blk.get("events", [])
    events = _get(f"/sports/{SPORT}/odds",
                  regions=_REGION, markets=GAME_MARKETS, oddsFormat="american") or []
    with _lock:
        snap["game_odds"] = {"date": _today(), "fetched_at": time.time(),
                             "events": events}
        _save_snapshot()
    return events


def _norm(name: str | None) -> str:
    return (name or "").lower().strip()


def norm_player_name(name: str | None) -> str:
    """Normalize a player name for cross-source matching (ESPN <-> books):
    lowercase, ascii-fold, strip punctuation."""
    import unicodedata
    s = unicodedata.normalize("NFKD", (name or "")).encode("ascii", "ignore").decode()
    return s.lower().replace(".", "").replace("'", "").replace("-", " ").strip()


def find_event_for_game(game: dict) -> dict | None:
    """Match an ESPN schedule game to an Odds API event by full team names."""
    home, away = _norm(game.get("home_name")), _norm(game.get("away_name"))
    for ev in get_game_odds():
        if _norm(ev.get("home_team")) == home and _norm(ev.get("away_team")) == away:
            return ev
    return None


# ---------------------------------------------------------------- event props

def _prop_market_keys() -> list[str]:
    return PROP_MARKETS + (_ALT_MARKETS if _INCLUDE_ALT else [])


def get_event_props(odds_event_id: str, force: bool = False) -> dict | None:
    """Player-prop odds for one event, snapshot-cached per day."""
    if not is_configured():
        return None
    snap = _load_snapshot()
    with _lock:
        cell = (snap.get("event_props") or {}).get(odds_event_id)
        if (cell and not force and cell.get("date") == _today()
                and time.time() - cell.get("fetched_at", 0) < _PROPS_TTL):
            return cell.get("data")
    try:
        data = _get(f"/sports/{SPORT}/events/{odds_event_id}/odds",
                    regions=_REGION, markets=",".join(_prop_market_keys()),
                    oddsFormat="american")
    except RuntimeError as e:
        # 422 = markets not yet posted for this event; cache the miss briefly.
        data = None
        if "422" not in str(e):
            raise
    with _lock:
        snap.setdefault("event_props", {})[odds_event_id] = {
            "date": _today(), "fetched_at": time.time(), "data": data}
        _save_snapshot()
    return data


def fetch_event_odds_live(odds_event_id: str, markets: list[str] | None = None) -> dict | None:
    """Force-refresh one event's odds RIGHT NOW, bypassing the daily snapshot.
    Used only by the closing-line capture around kickoff — ~1 credit/market."""
    if not is_configured():
        return None
    data = _get(f"/sports/{SPORT}/events/{odds_event_id}/odds",
                regions=_REGION,
                markets=",".join(markets or (_prop_market_keys() + GAME_MARKETS.split(","))),
                oddsFormat="american")
    with _lock:
        snap = _load_snapshot()
        snap.setdefault("event_props", {})[odds_event_id] = {
            "date": _today(), "fetched_at": time.time(), "data": data,
            "closing": True}
        _save_snapshot()
    return data


# ------------------------------------------------------------------- parsing

def parse_game_markets(ev: dict) -> dict:
    """One Odds API event -> {h2h: {book: {home,away}}, spreads: ..., totals: ...}."""
    out: dict = {"h2h": [], "spreads": [], "totals": []}
    home, away = ev.get("home_team"), ev.get("away_team")
    for bk in ev.get("bookmakers", []):
        book = bk.get("title") or bk.get("key")
        for m in bk.get("markets", []):
            mkey = m.get("key")
            oc = {o.get("name"): o for o in m.get("outcomes", [])}
            if mkey == "h2h" and home in oc and away in oc:
                out["h2h"].append({"book": book,
                                   "home_price": oc[home].get("price"),
                                   "away_price": oc[away].get("price")})
            elif mkey == "spreads" and home in oc and away in oc:
                out["spreads"].append({"book": book,
                                       "home_point": oc[home].get("point"),
                                       "home_price": oc[home].get("price"),
                                       "away_point": oc[away].get("point"),
                                       "away_price": oc[away].get("price")})
            elif mkey == "totals":
                over, under = oc.get("Over"), oc.get("Under")
                if over and under:
                    out["totals"].append({"book": book,
                                          "point": over.get("point"),
                                          "over_price": over.get("price"),
                                          "under_price": under.get("price")})
    return out


def parse_prop_markets(event_odds: dict | None) -> list[dict]:
    """Event props payload -> flat rows:
    {market_key, base_key, is_alt, player, line, side, price, book}.
    player_anytime_td has no Over/Under — it's Yes-shaped; side='over', line=0.5."""
    rows: list[dict] = []
    if not event_odds:
        return rows
    for bk in event_odds.get("bookmakers", []):
        book = bk.get("title") or bk.get("key")
        for m in bk.get("markets", []):
            mkey = m.get("key") or ""
            base = mkey.replace("_alternate", "")
            is_alt = mkey.endswith("_alternate")
            for o in m.get("outcomes", []):
                player = o.get("description") or o.get("name")
                side = _norm(o.get("name"))
                if base == "player_anytime_td":
                    if side not in ("yes", "no"):
                        continue
                    rows.append({"market_key": mkey, "base_key": base,
                                 "is_alt": is_alt, "player": player,
                                 "line": 0.5,
                                 "side": "over" if side == "yes" else "under",
                                 "price": o.get("price"), "book": book})
                elif side in ("over", "under"):
                    rows.append({"market_key": mkey, "base_key": base,
                                 "is_alt": is_alt, "player": player,
                                 "line": o.get("point"), "side": side,
                                 "price": o.get("price"), "book": book})
    return rows


def snapshot_status() -> dict:
    snap = _load_snapshot()
    blk = snap.get("game_odds") or {}
    return {
        "configured": is_configured(),
        "snapshot_date": blk.get("date"),
        "game_events": len(blk.get("events", [])),
        "event_props_cached": len(snap.get("event_props") or {}),
    }
