"""
Bet tracker — picks CRUD, grading, closing-line value (CLV).

Direct port of the MLB hub's tracker schema so the mental model carries over:
data/daily_tracker.json is {YYYY-MM-DD: {entries: [...], capturedAt,
gradedAt, closingCapturedAt}}; entries carry id/savedAt/gradedAt/marketKey/
line/side/price/grade/modelProb/clvEdge/...; dedup key when no id is
(date, gameId, player, marketKey, line).

Primary KPI = CLV: clvEdge = closingImplied - openingImplied (positive =
you beat the close). Closing capture force-refreshes each game's odds in a
window around kickoff (games carrying pending picks only) — without it the
daily snapshot is frozen and open == close by construction.

Grading reads final stats from nfl_data's boxscore-fed weekly rows (ESPN,
available minutes after games end) and final scores from the schedule.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta

import nfl_data
import odds_api
import projections
import value_engine as ve

_norm_name = odds_api.norm_player_name

_STORE_FILE = os.path.join(nfl_data.DATA_DIR, "daily_tracker.json")
_SETTINGS_FILE = os.path.join(nfl_data.DATA_DIR, "model_adjustments.json")
_lock = threading.RLock()
_store_cache: tuple | None = None      # (sig, store)
_closing_captured: set[str] = set()    # game_ids already captured this process

PROP_STAT = {
    "pass_yds": "passing_yards", "pass_tds": "passing_tds",
    "rush_yds": "rushing_yards", "receptions": "receptions",
    "rec_yds": "receiving_yards",
}
GAME_MARKETS = ("h2h", "spread", "total")

CLOSING_ENABLED = os.environ.get("TRACKER_CLOSING_CAPTURE_ENABLED", "1") == "1"
CLOSING_INTERVAL_MIN = int(os.environ.get("TRACKER_CLOSING_CAPTURE_MINUTES", "5"))
CLOSING_LEAD_MIN = int(os.environ.get("TRACKER_CLOSING_LEAD_MIN", "20"))
CLOSING_GRACE_MIN = int(os.environ.get("TRACKER_CLOSING_GRACE_MIN", "15"))
AUTO_SYNC_MIN = int(os.environ.get("TRACKER_AUTO_SYNC_MINUTES", "30"))


# ---------------------------------------------------------------------- store

def _sig() -> tuple:
    try:
        st = os.stat(_STORE_FILE)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return (0, 0)


def _load() -> dict:
    global _store_cache
    with _lock:
        sig = _sig()
        if _store_cache and _store_cache[0] == sig:
            return json.loads(json.dumps(_store_cache[1]))
        try:
            with open(_STORE_FILE, "r", encoding="utf-8") as f:
                store = json.load(f)
        except Exception:  # noqa: BLE001
            store = {}
        _store_cache = (sig, store)
        return json.loads(json.dumps(store))


def _save(store: dict) -> None:
    global _store_cache
    with _lock:
        tmp = _STORE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(store, f, separators=(",", ":"))
        os.replace(tmp, _STORE_FILE)
        _store_cache = (_sig(), store)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def get_settings() -> dict:
    defaults = {"bankroll": 1000.0, "kelly_fraction": 0.25,
                "max_bet_pct": 0.05, "unit_pct": 0.01}
    try:
        with open(_SETTINGS_FILE, "r", encoding="utf-8") as f:
            defaults.update(json.load(f))
    except Exception:  # noqa: BLE001
        pass
    return defaults


def save_settings(patch: dict) -> dict:
    cur = get_settings()
    for k in ("bankroll", "kelly_fraction", "max_bet_pct", "unit_pct"):
        if k in patch:
            try:
                cur[k] = float(patch[k])
            except (TypeError, ValueError):
                pass
    tmp = _SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cur, f)
    os.replace(tmp, _SETTINGS_FILE)
    return cur


# ---------------------------------------------------------------------- picks

_PICK_FIELDS = ("gameId", "season", "week", "gameday", "player", "playerId",
                "team", "opponent", "position", "marketKey", "marketLabel",
                "line", "side", "price", "book", "stakeDollars", "stakeUnits",
                "modelProb", "impliedProb", "fairProb", "edge", "evPct",
                "modelSource", "source")


def add_pick(payload: dict) -> dict:
    date = payload.get("gameday") or _today()
    entry = {k: payload.get(k) for k in _PICK_FIELDS}
    entry["id"] = payload.get("id") or uuid.uuid4().hex[:12]
    entry["savedAt"] = _now()
    entry["grade"] = "pending"
    entry["gradedAt"] = None
    if entry.get("price") is not None:
        entry["openingImplied"] = ve.american_to_implied(entry["price"])
    store = _load()
    day = store.setdefault(date, {"entries": []})
    dedup = (date, entry.get("gameId"), entry.get("player"),
             entry.get("marketKey"), entry.get("line"))
    for i, e in enumerate(day["entries"]):
        if (date, e.get("gameId"), e.get("player"), e.get("marketKey"),
                e.get("line")) == dedup:
            entry["id"] = e["id"]           # replace in place, keep identity
            day["entries"][i] = entry
            _save(store)
            return entry
    day["entries"].append(entry)
    _save(store)
    return entry


def update_pick(date: str, pick_id: str, patch: dict) -> dict | None:
    store = _load()
    for e in store.get(date, {}).get("entries", []):
        if e["id"] == pick_id:
            for k, v in patch.items():
                if k not in ("id", "savedAt"):
                    e[k] = v
            _save(store)
            return e
    return None


def delete_pick(date: str, pick_id: str) -> bool:
    store = _load()
    day = store.get(date)
    if not day:
        return False
    before = len(day["entries"])
    day["entries"] = [e for e in day["entries"] if e["id"] != pick_id]
    if len(day["entries"]) == before:
        return False
    _save(store)
    return True


def list_picks(date: str | None = None) -> dict:
    store = _load()
    if date:
        return {date: store.get(date, {"entries": []})}
    return store


# -------------------------------------------------------------------- grading

def _grade_game_market(e: dict, game: dict) -> tuple[str, float] | None:
    hs, as_ = game.get("home_score"), game.get("away_score")
    if hs is None or as_ is None:
        return None
    mk, side, line = e["marketKey"], e.get("side"), e.get("line")
    if mk == "h2h":
        if hs == as_:
            return ("push", 0.0)
        winner = "home" if hs > as_ else "away"
        return ("win" if side == winner else "loss", float(hs if side == "home" else as_))
    if mk == "spread":
        # line is the picked team's spread (e.g. home -3.5 stored as -3.5)
        margin = (hs - as_) if side == "home" else (as_ - hs)
        adj = margin + float(line or 0)
        if adj == 0:
            return ("push", margin)
        return ("win" if adj > 0 else "loss", margin)
    if mk == "total":
        total = hs + as_
        if total == line:
            return ("push", total)
        over = total > float(line or 0)
        return ("win" if (side == "over") == over else "loss", total)
    return None


def _grade_prop(e: dict, stat_rows: dict) -> tuple[str, float] | None:
    row = stat_rows.get((e.get("gameId"), str(e.get("playerId"))))
    if row is None:
        return None
    mk = e["marketKey"]
    if mk == "anytime_td":
        actual = row["rushing_tds"] + row["receiving_tds"]
    else:
        col = PROP_STAT.get(mk)
        if not col:
            return None
        actual = row[col]
    line = float(e.get("line") or 0)
    if actual == line:
        return ("push", actual)
    over = actual > line
    return ("win" if (e.get("side") == "over") == over else "loss", actual)


def grade_pending() -> dict:
    """Grade every pending pick whose game is final. Returns counts."""
    store = _load()
    seasons: set[int] = set()
    for date, day in store.items():
        for e in day.get("entries", []):
            if e.get("grade") == "pending" and e.get("season"):
                seasons.add(int(e["season"]))
    stat_rows: dict = {}
    games_by_id: dict = {}
    for season in seasons:
        for g in nfl_data.get_schedule(season):
            games_by_id[g["game_id"]] = g
        for r in nfl_data.get_player_week_stats(season):
            stat_rows[(r["game_id"], r["player_id"])] = r

    graded = 0
    for date, day in store.items():
        for e in day.get("entries", []):
            if e.get("grade") != "pending":
                continue
            game = games_by_id.get(e.get("gameId"))
            if not game or not game.get("completed"):
                continue
            res = (_grade_game_market(e, game) if e["marketKey"] in GAME_MARKETS
                   else _grade_prop(e, stat_rows))
            if not res:
                continue
            grade, actual = res
            e["grade"] = grade
            e["actual"] = actual
            e["gradedAt"] = _now()
            stake = float(e.get("stakeDollars") or 0)
            dec = ve.american_to_decimal(e.get("price")) or 1.0
            e["profitDollars"] = round(
                stake * (dec - 1) if grade == "win"
                else (-stake if grade == "loss" else 0.0), 2)
            graded += 1
        if any(x.get("gradedAt") for x in day.get("entries", [])):
            day["gradedAt"] = _now()
    if graded:
        _save(store)
    return {"graded": graded}


# ------------------------------------------------------------ closing capture

def _kickoff_window(game: dict) -> bool:
    try:
        ko = datetime.fromisoformat(game["date"].replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return False
    now = datetime.now(timezone.utc)
    return (ko - timedelta(minutes=CLOSING_LEAD_MIN)
            <= now <= ko + timedelta(minutes=CLOSING_GRACE_MIN))


def closing_capture_once() -> dict:
    """For games in their kickoff window carrying pending picks: force-refresh
    that game's odds once and record closing price + clvEdge on each pick."""
    if not odds_api.is_configured():
        return {"captured": 0, "reason": "odds not configured"}
    store = _load()
    captured = 0
    changed = False
    for date, day in store.items():
        pend = [e for e in day.get("entries", [])
                if e.get("grade") == "pending" and e.get("closingPrice") is None]
        if not pend:
            continue
        by_game: dict[str, list[dict]] = {}
        for e in pend:
            if e.get("gameId"):
                by_game.setdefault(e["gameId"], []).append(e)
        for gid, entries in by_game.items():
            season = int(entries[0].get("season") or nfl_data.default_season())
            game = next((g for g in nfl_data.get_schedule(season)
                         if g["game_id"] == gid), None)
            if not game or gid in _closing_captured or not _kickoff_window(game):
                continue
            ev = odds_api.find_event_for_game(game)
            if not ev:
                continue
            data = odds_api.fetch_event_odds_live(ev["id"])
            _closing_captured.add(gid)
            rows = odds_api.parse_prop_markets(data or {})
            gm = odds_api.parse_game_markets({**ev, **(data or {})})
            for e in entries:
                price = _closing_price_for(e, rows, gm)
                if price is None:
                    continue
                e["closingPrice"] = price
                closing_imp = ve.american_to_implied(price)
                opening_imp = e.get("openingImplied") or ve.american_to_implied(e.get("price"))
                if closing_imp is not None and opening_imp is not None:
                    e["closingImplied"] = round(closing_imp, 4)
                    e["clvEdge"] = round(closing_imp - opening_imp, 4)
                captured += 1
                changed = True
        if captured:
            day["closingCapturedAt"] = _now()
    if changed:
        _save(store)
    return {"captured": captured}


def _closing_price_for(e: dict, prop_rows: list[dict], gm: dict):
    """Best closing price for the pick's exact (player, market, line, side)."""
    mk, side, line = e.get("marketKey"), e.get("side"), e.get("line")
    if mk in GAME_MARKETS:
        blk = {"h2h": gm.get("h2h", []), "spread": gm.get("spreads", []),
               "total": gm.get("totals", [])}[mk]
        best = None
        for r in blk:
            price = r.get(f"{side}_price")
            if mk == "spread" and r.get(f"{side}_point") != line:
                continue
            if mk == "total" and r.get("point") != line:
                continue
            if isinstance(price, (int, float)):
                dec = ve.american_to_decimal(price) or 0
                if best is None or dec > best[0]:
                    best = (dec, price)
        return best[1] if best else None
    inv = {v: k for k, v in projections.ODDS_KEY_TO_MARKET.items()}
    want = inv.get(mk)
    nkey = _norm_name(e.get("player"))
    best = None
    for r in prop_rows:
        if (r["base_key"] == want and r["side"] == side and r["line"] == line
                and _norm_name(r["player"]) == nkey
                and isinstance(r.get("price"), (int, float))):
            dec = ve.american_to_decimal(r["price"]) or 0
            if best is None or dec > best[0]:
                best = (dec, r["price"])
    return best[1] if best else None


# -------------------------------------------------------------------- summary

def performance_summary() -> dict:
    store = _load()
    entries = [e for day in store.values() for e in day.get("entries", [])]
    graded = [e for e in entries if e.get("grade") in ("win", "loss", "push")]
    wins = sum(1 for e in graded if e["grade"] == "win")
    losses = sum(1 for e in graded if e["grade"] == "loss")
    pushes = sum(1 for e in graded if e["grade"] == "push")
    profit = round(sum(e.get("profitDollars") or 0 for e in graded), 2)
    staked = sum(float(e.get("stakeDollars") or 0) for e in graded)
    with_clv = [e for e in entries if isinstance(e.get("clvEdge"), (int, float))]
    beat = sum(1 for e in with_clv if e["clvEdge"] > 0)
    per_market: dict[str, dict] = {}
    for e in graded:
        m = per_market.setdefault(e.get("marketKey") or "?",
                                  {"n": 0, "wins": 0, "losses": 0, "pushes": 0,
                                   "profit": 0.0})
        m["n"] += 1
        m[e["grade"] + ("s" if e["grade"] != "loss" else "es")] += 1
        m["profit"] = round(m["profit"] + (e.get("profitDollars") or 0), 2)
    decided = wins + losses
    return {
        "primaryKpi": {
            "metric": "clv",
            "value": round(beat / len(with_clv), 4) if with_clv else None,
            "avg_clv": round(sum(e["clvEdge"] for e in with_clv) / len(with_clv), 4)
                       if with_clv else None,
            "n": len(with_clv),
        },
        "picks": len(entries), "pending": len(entries) - len(graded),
        "wins": wins, "losses": losses, "pushes": pushes,
        "hit_rate": round(wins / decided, 4) if decided else None,
        "profitDollars": profit,
        "roi": round(profit / staked, 4) if staked else None,
        "per_market": per_market,
        "settings": get_settings(),
    }


# -------------------------------------------------------------------- workers

_workers_started = False


def start_background_workers() -> None:
    """Auto-grade + closing-capture loops. Called once from app preload."""
    global _workers_started
    if _workers_started:
        return
    _workers_started = True

    def _grade_loop():
        while True:
            time.sleep(AUTO_SYNC_MIN * 60)
            try:
                grade_pending()
            except Exception as e:  # noqa: BLE001
                print(f"[tracker] grade loop: {e}")

    def _closing_loop():
        while True:
            time.sleep(CLOSING_INTERVAL_MIN * 60)
            try:
                closing_capture_once()
            except Exception as e:  # noqa: BLE001
                print(f"[tracker] closing loop: {e}")

    threading.Thread(target=_grade_loop, daemon=True, name="tracker-grade").start()
    if CLOSING_ENABLED:
        threading.Thread(target=_closing_loop, daemon=True,
                         name="tracker-closing").start()
