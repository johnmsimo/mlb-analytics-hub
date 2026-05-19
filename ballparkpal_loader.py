"""
ballparkpal_loader.py — Ballpark Pal microclimate park-factor loader.

Ballpark Pal publicly publishes daily, per-game park factors that account for
park × handedness × wind × temp × humidity × pressure. Their HR multiplier
captures the actual conditions on game day rather than a static park constant.

This loader fetches their daily JSON and exposes bp_park_factor(game_pk, hand,
stat) returning a multiplier. Falls back to None when unavailable so callers
can use the existing PARK_FACTORS constant.

Cached daily at data/ballparkpal_<YYYY-MM-DD>.json.

NOTE: Ballpark Pal does not publish an official API. We attempt a few known
public URL shapes; when none responds, we return empty silently and the caller
falls back to the existing static factors.
"""

import json
import os
import threading
from datetime import datetime, timedelta
from urllib.request import Request, urlopen

DATA_DIR = os.environ.get("DATA_DIR") or os.path.join(os.path.dirname(__file__), "data")

_lock = threading.Lock()
_cache = {}   # date_str → {parsed_at, by_team: {team_abbr: {hand: {stat: mult}}}}

REFRESH_HOURS = 4
USER_AGENT = "Mozilla/5.0 (compatible; mlb-analytics-hub/1.0)"

# Candidate JSON endpoints (Ballpark Pal exposes a few; first-success wins).
CANDIDATE_URLS = [
    "https://www.ballparkpal.com/api/games?date={date}",
    "https://ballparkpal.com/api/games?date={date}",
]


def _path(date_str):
    return os.path.join(DATA_DIR, f"ballparkpal_{date_str}.json")


def _fetch(date_str, timeout=10):
    for tmpl in CANDIDATE_URLS:
        url = tmpl.format(date=date_str)
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            if raw and raw.lstrip().startswith(("{", "[")):
                return json.loads(raw)
        except Exception:
            continue
    return None


def _parse(raw):
    """Normalize whatever shape we get into:
      { team_abbr: { 'R'|'L': { 'HR': float, 'H': float, 'TB': float, 'R': float } } }
    """
    if not raw:
        return {}
    out = {}
    games = raw.get("games") if isinstance(raw, dict) else raw
    if not isinstance(games, list):
        return {}
    for g in games:
        if not isinstance(g, dict):
            continue
        home = (g.get("home_team") or g.get("home") or g.get("homeAbbr") or "").upper()
        pf = g.get("park_factors") or g.get("factors") or {}
        if not home or not isinstance(pf, dict):
            continue
        per_hand = {}
        for hand_key in ("R", "L", "rhb", "lhb", "right", "left"):
            sub = pf.get(hand_key)
            if not isinstance(sub, dict):
                continue
            h = "R" if hand_key.lower().startswith("r") else "L"
            entry = per_hand.setdefault(h, {})
            for stat in ("HR", "H", "TB", "R", "1B", "2B", "3B", "BB", "K"):
                v = sub.get(stat) or sub.get(stat.lower())
                try:
                    entry[stat] = float(v)
                except (TypeError, ValueError):
                    pass
        # Some endpoints flatten to a single HR multiplier
        if not per_hand:
            for stat in ("HR", "H", "TB", "R"):
                v = pf.get(stat) or pf.get(stat.lower())
                try:
                    f = float(v)
                    per_hand.setdefault("R", {})[stat] = f
                    per_hand.setdefault("L", {})[stat] = f
                except (TypeError, ValueError):
                    pass
        if per_hand:
            out[home] = per_hand
    return out


def fetch_and_save(date_str=None):
    date_str = date_str or datetime.utcnow().strftime("%Y-%m-%d")
    raw = _fetch(date_str)
    if not raw:
        return 0
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(_path(date_str), "w", encoding="utf-8") as f:
        json.dump(raw, f)
    return len(raw.get("games", [])) if isinstance(raw, dict) else len(raw)


def _load(date_str):
    now = datetime.utcnow()
    slot = _cache.get(date_str)
    if slot and (now - slot["parsed_at"]).total_seconds() < REFRESH_HOURS * 3600:
        return slot["by_team"]
    with _lock:
        # Re-check
        slot = _cache.get(date_str)
        if slot and (now - slot["parsed_at"]).total_seconds() < REFRESH_HOURS * 3600:
            return slot["by_team"]
        # Try local file first
        raw = None
        p = _path(date_str)
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    raw = json.load(f)
            except Exception:
                raw = None
        if raw is None:
            raw = _fetch(date_str)
            if raw is not None:
                try:
                    os.makedirs(DATA_DIR, exist_ok=True)
                    with open(p, "w", encoding="utf-8") as f:
                        json.dump(raw, f)
                except Exception:
                    pass
        by_team = _parse(raw) if raw else {}
        _cache[date_str] = {"parsed_at": now, "by_team": by_team}
        return by_team


def bp_park_factor(home_team_abbr, hand="R", stat="HR", date_str=None):
    """Return Ballpark Pal multiplier for (team, hand, stat) on date_str.

    Returns None when no data is available — caller should fall back to the
    static PARK_FACTORS constant.
    """
    if not home_team_abbr:
        return None
    date_str = date_str or datetime.utcnow().strftime("%Y-%m-%d")
    by_team = _load(date_str)
    entry = by_team.get(str(home_team_abbr).upper())
    if not entry:
        return None
    hand = "R" if str(hand or "R").upper().startswith("R") else "L"
    sub = entry.get(hand) or entry.get("R") or entry.get("L")
    if not sub:
        return None
    v = sub.get(stat) or sub.get(stat.upper())
    try:
        f = float(v)
        if 0.4 < f < 2.0:
            return round(f, 4)
    except (TypeError, ValueError):
        return None
    return None


if __name__ == "__main__":
    import sys
    date_str = sys.argv[1] if len(sys.argv) > 1 else None
    n = fetch_and_save(date_str)
    print(f"[ballparkpal_loader] cached {n} games for {date_str or 'today'}")
