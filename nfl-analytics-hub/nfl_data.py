"""
NFL data layer — ESPN public API backed, nflverse-compatible schema.

Primary source is ESPN's unauthenticated site API (scoreboard / summary /
rosters). The internal weekly player-stat schema mirrors nflverse's
`player_stats` columns (passing_yards, carries, receptions, targets, ...) so a
future nflverse CSV import can drop in behind `get_player_week_stats()`
without touching any caller.

All fetches disk-cache under DATA_DIR:
  schedule_{season}.json      full-season schedule (REG weeks 1-18 + POST 1-5)
  player_week_{season}.csv    per-player per-game stat lines (built from
                              final-game boxscores, incrementally by game_id)
  positions.json              athlete id -> position (roster sweep, 7d TTL)

Everything is thread-safe via a module lock; heavy builds (a full season of
boxscores is ~285 HTTP calls) run through a small thread pool and only ever
fetch games not already in the disk cache.
"""
from __future__ import annotations

import csv
import io
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"

DATA_DIR = (
    os.environ.get("NFL_DATA_DIR")
    or os.environ.get("DATA_DIR")
    or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
)
os.makedirs(DATA_DIR, exist_ok=True)

_HTTP = requests.Session()
_HTTP.headers.update({"User-Agent": "nfl-analytics-hub/1.0"})

_lock = threading.RLock()
_mem: dict = {}          # key -> (expires_epoch, value)

REG_WEEKS = list(range(1, 19))
POST_WEEKS = list(range(1, 6))   # WC, DIV, CONF, (pro bowl slot unused), SB

STAT_COLS = [
    "completions", "attempts", "passing_yards", "passing_tds", "interceptions",
    "sacks", "carries", "rushing_yards", "rushing_tds",
    "receptions", "targets", "receiving_yards", "receiving_tds", "fumbles_lost",
]
ROW_COLS = [
    "season", "season_type", "week", "game_id", "gameday", "team", "opponent",
    "home", "player_id", "player_name", "position",
] + STAT_COLS

POS_GROUPS = ("QB", "RB", "WR", "TE")


# ---------------------------------------------------------------- HTTP / cache

def _get_json(url: str, timeout: int = 20, retries: int = 2):
    last = None
    for i in range(retries + 1):
        try:
            r = _HTTP.get(url, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            last = f"HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001
            last = str(e)
        time.sleep(0.5 * (i + 1))
    raise RuntimeError(f"ESPN fetch failed: {url} ({last})")


def _mem_get(key: str):
    with _lock:
        hit = _mem.get(key)
        if hit and hit[0] > time.time():
            return hit[1]
    return None


def _mem_set(key: str, value, ttl: float):
    with _lock:
        _mem[key] = (time.time() + ttl, value)


def _disk_path(name: str) -> str:
    return os.path.join(DATA_DIR, name)


def _read_json(name: str):
    try:
        with open(_disk_path(name), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def _write_json(name: str, obj) -> None:
    tmp = _disk_path(name) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"))
    os.replace(tmp, _disk_path(name))


# ------------------------------------------------------------------- schedule

def _parse_event(ev: dict, season: int, week: int, season_type: str) -> dict:
    comp = ev["competitions"][0]
    home = away = {}
    for c in comp.get("competitors", []):
        side = {"team": c.get("team", {}).get("abbreviation"),
                "name": c.get("team", {}).get("displayName"),
                "id": c.get("team", {}).get("id"),
                "score": int(c["score"]) if str(c.get("score", "")).isdigit() else None,
                "record": next((r.get("summary") for r in c.get("records", [])
                                if r.get("type") == "total"), None)}
        if c.get("homeAway") == "home":
            home = side
        else:
            away = side
    status = comp.get("status", {}).get("type", {})
    return {
        "game_id": ev["id"],
        "season": season,
        "season_type": season_type,
        "week": week,
        "date": ev.get("date"),
        "name": ev.get("name"),
        "short_name": ev.get("shortName"),
        "venue": comp.get("venue", {}).get("fullName"),
        "state": status.get("state"),            # pre / in / post
        "completed": bool(status.get("completed")),
        "status_detail": status.get("shortDetail"),
        "home_team": home.get("team"), "home_name": home.get("name"),
        "home_id": home.get("id"), "home_score": home.get("score"),
        "home_record": home.get("record"),
        "away_team": away.get("team"), "away_name": away.get("name"),
        "away_id": away.get("id"), "away_score": away.get("score"),
        "away_record": away.get("record"),
    }


def fetch_week_scoreboard(season: int, week: int, seasontype: int = 2,
                          ttl: float = 60.0) -> list[dict]:
    """Live scoreboard for one week (short memory TTL — used on game day)."""
    key = f"sb:{season}:{seasontype}:{week}"
    hit = _mem_get(key)
    if hit is not None:
        return hit
    url = f"{ESPN_BASE}/scoreboard?dates={season}&seasontype={seasontype}&week={week}"
    data = _get_json(url)
    stype = "REG" if seasontype == 2 else "POST"
    games = [_parse_event(ev, season, week, stype) for ev in data.get("events", [])]
    _mem_set(key, games, ttl)
    return games


def get_schedule(season: int, refresh: bool = False) -> list[dict]:
    """Full-season schedule (REG 1-18 + POST). Disk-cached; past seasons are
    permanent, the current/future season refreshes every 6h."""
    key = f"sched:{season}"
    if not refresh:
        hit = _mem_get(key)
        if hit is not None:
            return hit
        cached = _read_json(f"schedule_{season}.json")
        if cached:
            age = time.time() - cached.get("fetched_at", 0)
            season_over = all(g["completed"] for g in cached["games"]) and cached["games"]
            if season_over or age < 6 * 3600:
                _mem_set(key, cached["games"], 300)
                return cached["games"]

    games: list[dict] = []
    for wk in REG_WEEKS:
        games.extend(fetch_week_scoreboard(season, wk, 2, ttl=1))
    for wk in POST_WEEKS:
        try:
            games.extend(fetch_week_scoreboard(season, wk, 3, ttl=1))
        except RuntimeError:
            break
    _write_json(f"schedule_{season}.json", {"fetched_at": time.time(), "games": games})
    _mem_set(key, games, 300)
    return games


def default_season(today: datetime | None = None) -> int:
    """NFL season year: Mar+ -> current calendar year (upcoming/ongoing),
    Jan/Feb -> previous year (that season's playoffs)."""
    now = today or datetime.now(timezone.utc)
    return now.year if now.month >= 3 else now.year - 1


def current_week(season: int | None = None) -> dict:
    """First week containing a non-final game (else the last played week)."""
    season = season or default_season()
    games = get_schedule(season)
    if not games:
        return {"season": season, "week": 1, "season_type": "REG"}
    for g in games:
        if not g["completed"]:
            return {"season": season, "week": g["week"],
                    "season_type": g["season_type"]}
    last = games[-1]
    return {"season": season, "week": last["week"],
            "season_type": last["season_type"]}


def get_week_games(season: int, week: int, season_type: str = "REG",
                   live: bool = False) -> list[dict]:
    if live:
        return fetch_week_scoreboard(season, week, 2 if season_type == "REG" else 3)
    return [g for g in get_schedule(season)
            if g["week"] == week and g["season_type"] == season_type]


# ------------------------------------------------------------------ positions

def _team_ids() -> list[str]:
    hit = _mem_get("teamids")
    if hit:
        return hit
    data = _get_json(f"{ESPN_BASE}/teams?limit=40")
    ids = [t["team"]["id"]
           for lg in data.get("sports", [{}])[0].get("leagues", [{}])
           for t in lg.get("teams", [])]
    _mem_set("teamids", ids, 86400)
    return ids


def get_positions(refresh: bool = False) -> dict[str, str]:
    """athlete id -> position abbreviation, from a 32-team roster sweep."""
    key = "positions"
    hit = _mem_get(key)
    if hit is not None and not refresh:
        return hit
    cached = _read_json("positions.json")
    if cached and not refresh and time.time() - cached.get("fetched_at", 0) < 7 * 86400:
        _mem_set(key, cached["positions"], 3600)
        return cached["positions"]

    positions: dict[str, str] = (cached or {}).get("positions", {}).copy()
    try:
        def _one(tid):
            out = {}
            try:
                d = _get_json(f"{ESPN_BASE}/teams/{tid}/roster")
                for grp in d.get("athletes", []):
                    for a in grp.get("items", []):
                        pos = (a.get("position") or {}).get("abbreviation")
                        if pos:
                            out[str(a["id"])] = pos
            except Exception:  # noqa: BLE001
                pass
            return out
        with ThreadPoolExecutor(max_workers=6) as ex:
            for res in ex.map(_one, _team_ids()):
                positions.update(res)
        _write_json("positions.json", {"fetched_at": time.time(),
                                       "positions": positions})
    except Exception:  # noqa: BLE001
        pass
    _mem_set(key, positions, 3600)
    return positions


def _heuristic_position(row: dict) -> str:
    if row["attempts"] >= 3:
        return "QB"
    if row["carries"] > row["receptions"]:
        return "RB"
    return "WR"


# ------------------------------------------------------- boxscore -> stat rows

def _stat_idx(grp: dict) -> dict[str, int]:
    return {k: i for i, k in enumerate(grp.get("keys", []))}


def _num(stats: list, idx: dict, key: str, part: int | None = None) -> int:
    i = idx.get(key)
    if i is None or i >= len(stats):
        return 0
    v = stats[i]
    if part is not None:
        bits = str(v).replace("-", "/").split("/")
        v = bits[part] if part < len(bits) else 0
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _parse_boxscore(summary: dict, game: dict) -> list[dict]:
    rows: dict[str, dict] = {}
    gameday = (game.get("date") or "")[:10]

    def base_row(team: str, opp: str, home: int, ath: dict) -> dict:
        pid = str(ath.get("id"))
        if pid not in rows:
            rows[pid] = {c: 0 for c in STAT_COLS}
            rows[pid].update({
                "season": game["season"], "season_type": game["season_type"],
                "week": game["week"], "game_id": game["game_id"],
                "gameday": gameday, "team": team, "opponent": opp,
                "home": home, "player_id": pid,
                "player_name": ath.get("displayName"), "position": "",
            })
        return rows[pid]

    for side in summary.get("boxscore", {}).get("players", []):
        team = side.get("team", {}).get("abbreviation")
        if team == game["home_team"]:
            opp, home = game["away_team"], 1
        else:
            opp, home = game["home_team"], 0
        for grp in side.get("statistics", []):
            name, idx = grp.get("name"), _stat_idx(grp)
            if name not in ("passing", "rushing", "receiving", "fumbles"):
                continue
            for a in grp.get("athletes", []):
                ath, stats = a.get("athlete", {}), a.get("stats", [])
                r = base_row(team, opp, home, ath)
                if name == "passing":
                    r["completions"] += _num(stats, idx, "completions/passingAttempts", 0)
                    r["attempts"] += _num(stats, idx, "completions/passingAttempts", 1)
                    r["passing_yards"] += _num(stats, idx, "passingYards")
                    r["passing_tds"] += _num(stats, idx, "passingTouchdowns")
                    r["interceptions"] += _num(stats, idx, "interceptions")
                    r["sacks"] += _num(stats, idx, "sacks-sackYardsLost", 0)
                elif name == "rushing":
                    r["carries"] += _num(stats, idx, "rushingAttempts")
                    r["rushing_yards"] += _num(stats, idx, "rushingYards")
                    r["rushing_tds"] += _num(stats, idx, "rushingTouchdowns")
                elif name == "receiving":
                    r["receptions"] += _num(stats, idx, "receptions")
                    r["receiving_yards"] += _num(stats, idx, "receivingYards")
                    r["receiving_tds"] += _num(stats, idx, "receivingTouchdowns")
                    r["targets"] += _num(stats, idx, "receivingTargets")
                elif name == "fumbles":
                    r["fumbles_lost"] += _num(stats, idx, "fumblesLost")
    return list(rows.values())


def fetch_game_boxscore(game_id: str) -> dict:
    return _get_json(f"{ESPN_BASE}/summary?event={game_id}")


def live_game_stats(game: dict, ttl: float = 60.0) -> list[dict]:
    """In-progress (or just-final) player stat rows for one game, parsed from
    the live boxscore. Short memory TTL — polled on game days."""
    key = f"livebox:{game['game_id']}"
    hit = _mem_get(key)
    if hit is not None:
        return hit
    try:
        rows = _parse_boxscore(fetch_game_boxscore(game["game_id"]), game)
    except Exception:  # noqa: BLE001
        rows = []
    _mem_set(key, rows, ttl)
    return rows


# -------------------------------------------------------- player week stats

def _csv_name(season: int) -> str:
    return f"player_week_{season}.csv"


def _read_stat_csv(season: int) -> list[dict]:
    path = _disk_path(_csv_name(season))
    if not os.path.exists(path):
        return []
    out = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            for c in STAT_COLS + ["season", "week", "home"]:
                row[c] = int(row.get(c) or 0)
            out.append(row)
    return out


def _write_stat_csv(season: int, rows: list[dict]) -> None:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=ROW_COLS)
    w.writeheader()
    for r in rows:
        w.writerow({c: r.get(c, "") for c in ROW_COLS})
    tmp = _disk_path(_csv_name(season)) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(buf.getvalue())
    os.replace(tmp, _disk_path(_csv_name(season)))


def get_player_week_stats(season: int, refresh: bool = False,
                          max_new_games: int | None = None) -> list[dict]:
    """All per-player game lines for a season. Incremental: fetches boxscores
    only for completed games missing from the disk cache."""
    key = f"pws:{season}"
    if not refresh:
        hit = _mem_get(key)
        if hit is not None:
            return hit

    with _lock:
        rows = _read_stat_csv(season)
        have = {r["game_id"] for r in rows}

    finals = [g for g in get_schedule(season) if g["completed"]]
    missing = [g for g in finals if g["game_id"] not in have]
    if max_new_games is not None:
        missing = missing[:max_new_games]

    if missing:
        positions = get_positions()

        def _one(g):
            try:
                return _parse_boxscore(fetch_game_boxscore(g["game_id"]), g)
            except Exception:  # noqa: BLE001
                return []
        new_rows: list[dict] = []
        with ThreadPoolExecutor(max_workers=6) as ex:
            for res in ex.map(_one, missing):
                new_rows.extend(res)
        for r in new_rows:
            r["position"] = positions.get(r["player_id"]) or _heuristic_position(r)
        with _lock:
            rows = _read_stat_csv(season)  # re-read under lock (parallel callers)
            have = {r["game_id"] for r in rows}
            rows.extend([r for r in new_rows if r["game_id"] not in have])
            rows.sort(key=lambda r: (r["season_type"] != "REG", r["week"]))
            _write_stat_csv(season, rows)

    # Backfill positions for rows that predate a roster sweep.
    positions = get_positions()
    for r in rows:
        if not r.get("position"):
            r["position"] = positions.get(r["player_id"]) or _heuristic_position(r)

    _mem_set(key, rows, 600)
    return rows


def player_game_logs(season: int) -> dict[str, list[dict]]:
    """player_id -> game rows sorted chronologically (REG then POST)."""
    key = f"logs:{season}"
    hit = _mem_get(key)
    if hit is not None:
        return hit
    logs: dict[str, list[dict]] = {}
    for r in get_player_week_stats(season):
        logs.setdefault(r["player_id"], []).append(r)
    for rows in logs.values():
        rows.sort(key=lambda r: (r["season_type"] != "REG", r["week"]))
    _mem_set(key, logs, 600)
    return logs


def player_index(season: int) -> dict[str, dict]:
    """player_id -> {name, team (latest), position}."""
    key = f"pidx:{season}"
    hit = _mem_get(key)
    if hit is not None:
        return hit
    idx: dict[str, dict] = {}
    for pid, rows in player_game_logs(season).items():
        last = rows[-1]
        idx[pid] = {"player_id": pid, "name": last["player_name"],
                    "team": last["team"], "position": last["position"],
                    "games": len(rows)}
    _mem_set(key, idx, 600)
    return idx


# ------------------------------------------------------- defense vs position

def _pos_group(pos: str) -> str | None:
    if pos in POS_GROUPS:
        return pos
    if pos in ("FB",):
        return "RB"
    return None


def defense_vs_position(season: int) -> dict:
    """team -> pos_group -> per-game stats allowed + ratio vs league average.

    The NFL analogue of pitcher-vs-batter: how many receiving yards does this
    defense concede to WRs per game, relative to league average?
    """
    key = f"dvp:{season}"
    hit = _mem_get(key)
    if hit is not None:
        return hit

    stats = ("passing_yards", "rushing_yards", "receiving_yards",
             "receptions", "targets", "carries",
             "passing_tds", "rushing_tds", "receiving_tds")
    acc: dict[str, dict[str, dict[str, float]]] = {}
    games_per_team: dict[str, set] = {}
    for r in get_player_week_stats(season):
        grp = _pos_group(r["position"])
        if not grp:
            continue
        d = r["opponent"]
        if not d:
            continue
        games_per_team.setdefault(d, set()).add(r["game_id"])
        cell = acc.setdefault(d, {}).setdefault(grp, {s: 0.0 for s in stats})
        for s in stats:
            cell[s] += r[s]

    league: dict[str, dict[str, list[float]]] = {}
    out: dict[str, dict] = {}
    for team, groups in acc.items():
        g = max(len(games_per_team.get(team, ())), 1)
        out[team] = {"games": g}
        for grp, cell in groups.items():
            pg = {s: round(cell[s] / g, 2) for s in stats}
            out[team][grp] = pg
            for s, v in pg.items():
                league.setdefault(grp, {}).setdefault(s, []).append(v)

    for team, groups in out.items():
        for grp, pg in list(groups.items()):
            if grp == "games":
                continue
            ratios = {}
            for s, v in pg.items():
                avg = league.get(grp, {}).get(s)
                mean = sum(avg) / len(avg) if avg else 0
                ratios[f"{s}_ratio"] = round(v / mean, 3) if mean else 1.0
            pg.update(ratios)

    _mem_set(key, out, 600)
    return out


# --------------------------------------------------------------- team stats

def team_summaries(season: int) -> dict[str, dict]:
    """team abbrev -> record, points for/against per game (from schedule)."""
    key = f"teams:{season}"
    hit = _mem_get(key)
    if hit is not None:
        return hit
    out: dict[str, dict] = {}
    for g in get_schedule(season):
        if not g["completed"] or g["season_type"] != "REG":
            continue
        for side, opp in (("home", "away"), ("away", "home")):
            t = g[f"{side}_team"]
            if not t:
                continue
            d = out.setdefault(t, {"team": t, "games": 0, "pf": 0, "pa": 0,
                                   "wins": 0, "losses": 0, "ties": 0})
            ps, pa = g[f"{side}_score"], g[f"{opp}_score"]
            if ps is None or pa is None:
                continue
            d["games"] += 1
            d["pf"] += ps
            d["pa"] += pa
            if ps > pa:
                d["wins"] += 1
            elif ps < pa:
                d["losses"] += 1
            else:
                d["ties"] += 1
    for d in out.values():
        g = max(d["games"], 1)
        d["ppg"] = round(d["pf"] / g, 1)
        d["papg"] = round(d["pa"] / g, 1)
        d["record"] = f"{d['wins']}-{d['losses']}" + (f"-{d['ties']}" if d["ties"] else "")
    _mem_set(key, out, 600)
    return out


# ------------------------------------------------------------------ preload

def preload(season: int | None = None) -> None:
    """Background warm: schedule + stats + aggregates for the working season
    (falls back to the prior season when the current one hasn't started)."""
    season = season or default_season()
    try:
        get_schedule(season)
        rows = get_player_week_stats(season)
        if not rows:                      # offseason: warm last season instead
            get_player_week_stats(season - 1)
            defense_vs_position(season - 1)
        else:
            defense_vs_position(season)
        team_summaries(season)
    except Exception as e:  # noqa: BLE001
        print(f"[nfl_data] preload error: {e}")


def stats_season(season: int | None = None) -> int:
    """The season whose stats should feed projections: the requested/current
    season once it has data, else the one before (offseason + early weeks)."""
    season = season or default_season()
    if get_player_week_stats(season):
        return season
    return season - 1


if __name__ == "__main__":
    s = default_season()
    print("default season:", s, "| current week:", current_week(s))
    sched = get_schedule(s)
    print("schedule games:", len(sched))
