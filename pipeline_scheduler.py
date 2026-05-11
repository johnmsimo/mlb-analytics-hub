"""
pipeline_scheduler.py
Scheduled job that auto-runs the matchup pipeline each morning and caches
the result so Flask routes can serve it instantly.

Wired into your existing app.py collectors — no duplicate API calls.
Drop in repo root alongside app.py.
"""

import threading
import time
import logging
import os
import requests
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
log = logging.getLogger(__name__)

MLB_API = "https://statsapi.mlb.com/api/v1"

# ── Run time config ────────────────────────────────────────────────────────────
RUN_HOUR_ET   = 9
RUN_MINUTE_ET = 0

# ── In-memory cache ────────────────────────────────────────────────────────────
_cache = {
    "matchup_df": None,
    "games_df":   None,
    "last_run":   None,
    "status":     "idle",   # idle | running | done | error
    "error_msg":  None,
}

# ── Lazy imports from your existing modules ────────────────────────────────────
# Imported inside run_pipeline() to avoid circular import at module load time.

def _import_app_modules():
    """Import your existing collectors lazily so there's no circular dependency."""
    import importlib, sys
    app_mod = sys.modules.get("app") or importlib.import_module("app")

    fetch_schedule = getattr(app_mod, "fetch_schedule", None)

    _sv_batter  = getattr(app_mod, "sv_batter",  lambda name: {})
    _sv_pitcher = getattr(app_mod, "sv_pitcher", lambda name: {})

    return fetch_schedule, _sv_batter, _sv_pitcher


# ── 1. Today's Games (uses your fetch_schedule) ────────────────────────────────
def _build_games_df(fetch_schedule):
    today_str = datetime.now(ET).strftime("%m/%d/%Y")
    try:
        games_raw = fetch_schedule(today_str)
    except Exception as e:
        log.error(f"[pipeline] fetch_schedule failed: {e}")
        return pd.DataFrame()

    rows = []
    for g in games_raw:
        away = g.get("teams", {}).get("away", {})
        home = g.get("teams", {}).get("home", {})
        rows.append({
            "gamePk":            g.get("gamePk"),
            "game_date":         g.get("gameDate"),
            "away_team":         away.get("team", {}).get("abbreviation"),
            "away_team_id":      away.get("team", {}).get("id"),
            "home_team":         home.get("team", {}).get("abbreviation"),
            "home_team_id":      home.get("team", {}).get("id"),
            "away_pitcher_id":   away.get("probablePitcher", {}).get("id"),
            "away_pitcher_name": away.get("probablePitcher", {}).get("fullName"),
            "home_pitcher_id":   home.get("probablePitcher", {}).get("id"),
            "home_pitcher_name": home.get("probablePitcher", {}).get("fullName"),
            "venue":             g.get("venue", {}).get("name"),
        })
    return pd.DataFrame(rows)


# ── 2. Active Roster (uses your _get_active_roster cache) ─────────────────────
def _get_position_player_ids(team_id):
    """
    Uses your existing _get_active_roster() which already caches for 30 min.
    Imported from app context at runtime.
    """
    try:
        # Import from app module at runtime to avoid circular import
        import importlib, sys
        app_mod = sys.modules.get("app") or importlib.import_module("app")
        roster = app_mod._get_active_roster(team_id)
        return [
            p["person"]["id"]
            for p in roster
            if p.get("position", {}).get("code") not in ("P",)
        ]
    except Exception as e:
        log.warning(f"[pipeline] _get_active_roster({team_id}) failed: {e}")
        return []


# ── 3. BvP H2H ────────────────────────────────────────────────────────────────
def _get_bvp(batter_id, pitcher_id):
    season = datetime.now(ET).year
    url = f"{MLB_API}/people"
    params = {
        "personIds": batter_id,
        "hydrate": f"stats(group=[hitting],type=[vsPlayerTotal],opposingPlayerId={pitcher_id})"
    }
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        splits = r.json()["people"][0]["stats"][0]["splits"]
        if splits:
            s = splits[0]["stat"]
            return {
                "bvp_ab":  s.get("atBats", 0),
                "bvp_h":   s.get("hits", 0),
                "bvp_hr":  s.get("homeRuns", 0),
                "bvp_k":   s.get("strikeOuts", 0),
                "bvp_bb":  s.get("baseOnBalls", 0),
                "bvp_avg": s.get("avg", ".000"),
                "bvp_ops": s.get("ops", ".000"),
            }
    except Exception:
        pass
    return {"bvp_ab": 0, "bvp_h": 0, "bvp_hr": 0,
            "bvp_k": 0, "bvp_bb": 0, "bvp_avg": ".000", "bvp_ops": ".000"}


# ── 4. Platoon Splits ──────────────────────────────────────────────────────────
def _get_platoon_splits(player_id, group="hitting"):
    season = datetime.now(ET).year
    url = f"{MLB_API}/people/{player_id}/stats"
    params = {
        "stats": "statSplits",
        "group": group,
        "sitCodes": "vl,vr",
        "season": season,
    }
    result = {}
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        for stat_group in r.json().get("stats", []):
            for split in stat_group.get("splits", []):
                code = (split.get("split", {}) or {}).get("code", "")
                s    = split.get("stat", {})
                pfx  = "vsR" if code == "vr" else "vsL"
                pa   = max(int(s.get("plateAppearances", 0) or 0), 1)
                result.update({
                    f"{pfx}_avg":    s.get("avg", ".000"),
                    f"{pfx}_ops":    s.get("ops", ".000"),
                    f"{pfx}_slg":    s.get("slg", ".000"),
                    f"{pfx}_k_pct":  round(int(s.get("strikeOuts", 0) or 0) / pa, 3),
                    f"{pfx}_bb_pct": round(int(s.get("baseOnBalls", 0) or 0) / pa, 3),
                    f"{pfx}_ab":     int(s.get("atBats", 0) or 0),
                })
    except Exception as e:
        log.debug(f"[pipeline] platoon splits {player_id}: {e}")
    return result


# ── 5. Statcast via your sv_batter / sv_pitcher ────────────────────────────────
def _extract_batter_statcast(sv_batter_fn, player_name):
    """
    Pulls from your existing sv_batter() cache — same data source your
    performance badges and prop scorer already use. Zero extra API calls.
    """
    d = sv_batter_fn(player_name) or {}
    return {
        "batter_ev_avg":       d.get("svevavg"),
        "batter_adj_ev":       d.get("svhyperspd"),        # hyper_speed / adj EV
        "batter_barrel_pct":   d.get("svbrlpct"),
        "batter_hard_hit_pct": d.get("svhhpct"),
        "batter_xba":          d.get("svxba"),
        "batter_xwoba":        d.get("svxwoba"),
        "batter_xslg":         d.get("svxslg"),
        "batter_la_avg":       d.get("svlaavg"),
    }

def _extract_pitcher_statcast(sv_pitcher_fn, pitcher_name):
    """
    Pulls from your existing sv_pitcher() cache.
    """
    d = sv_pitcher_fn(pitcher_name) or {}
    return {
        "pitcher_ev_allowed":         d.get("svevavg"),
        "pitcher_barrel_pct_allowed": d.get("svbrlpct"),
        "pitcher_hard_hit_allowed":   d.get("svhhpct"),
        "pitcher_xwoba_allowed":      d.get("svxwoba"),
        "pitcher_xba_allowed":        d.get("svxba"),
        "pitcher_k9":                 d.get("svk9"),
        "pitcher_bb9":                d.get("svbb9"),
        "pitcher_whip":               d.get("svwhip"),
    }


# ── 6. Build Full Matchup DataFrame ───────────────────────────────────────────
def _fetch_batter_row(batter_id, pitcher_id, pitcher_name, game_meta,
                      pitcher_splits, pitcher_statcast, sv_batter_fn):
    """Fetch all per-batter data in one call (runs inside a thread pool)."""
    try:
        pr = requests.get(f"{MLB_API}/people/{batter_id}", timeout=6)
        person = pr.json()["people"][0]
        batter_name = person.get("fullName", "")
        batter_hand = (person.get("batSide") or {}).get("code", "")
    except Exception:
        batter_name = ""
        batter_hand = ""

    bvp            = _get_bvp(batter_id, pitcher_id)
    batter_splits  = _get_platoon_splits(batter_id, group="hitting")
    batter_statcast = _extract_batter_statcast(sv_batter_fn, batter_name)

    return {
        **game_meta,
        "batter_id":   batter_id,
        "batter_name": batter_name,
        "batter_hand": batter_hand,
        "pitcher_id":  pitcher_id,
        "pitcher_name": pitcher_name,
        **batter_statcast,
        **bvp,
        **batter_splits,
        **{f"pitcher_{k}": v for k, v in pitcher_splits.items()},
        **pitcher_statcast,
    }


def _build_matchup_df(games_df, sv_batter_fn, sv_pitcher_fn):
    rows = []

    for _, game in games_df.iterrows():
        for batting_side, pitching_side in [("away", "home"), ("home", "away")]:
            pitcher_id   = game.get(f"{pitching_side}_pitcher_id")
            pitcher_name = game.get(f"{pitching_side}_pitcher_name")
            batting_team_id = game.get(f"{batting_side}_team_id")

            if pd.isna(pitcher_id) or not pitcher_id:
                continue

            pitcher_id = int(pitcher_id)

            pitcher_splits   = _get_platoon_splits(pitcher_id, group="pitching")
            pitcher_statcast = _extract_pitcher_statcast(sv_pitcher_fn, pitcher_name or "")

            batter_ids = _get_position_player_ids(batting_team_id)

            game_meta = {
                "game_pk":      game["gamePk"],
                "game_date":    game["game_date"],
                "venue":        game.get("venue"),
                "batting_team": game[f"{batting_side}_team"],
                "pitching_team": game[f"{pitching_side}_team"],
            }

            with ThreadPoolExecutor(max_workers=min(20, len(batter_ids) or 1)) as ex:
                futures = [
                    ex.submit(
                        _fetch_batter_row,
                        bid, pitcher_id, pitcher_name, game_meta,
                        pitcher_splits, pitcher_statcast, sv_batter_fn,
                    )
                    for bid in batter_ids
                ]
                for fut in as_completed(futures):
                    try:
                        rows.append(fut.result())
                    except Exception as e:
                        log.warning(f"[pipeline] batter row failed: {e}")

    return pd.DataFrame(rows)


# ── 7. Main Pipeline Run ───────────────────────────────────────────────────────
def run_pipeline():
    global _cache
    _cache["status"]    = "running"
    _cache["error_msg"] = None
    log.info("[pipeline] Run started.")

    try:
        fetch_schedule, sv_batter_fn, sv_pitcher_fn = _import_app_modules()

        if fetch_schedule is None:
            raise ImportError("fetch_schedule not found in schedule_collector")

        games_df = _build_games_df(fetch_schedule)

        if games_df.empty:
            log.info("[pipeline] No games today.")
            _cache.update({"games_df": games_df, "matchup_df": pd.DataFrame(),
                           "last_run": datetime.now().isoformat(), "status": "done"})
            return

        matchup_df = _build_matchup_df(games_df, sv_batter_fn, sv_pitcher_fn)

        _cache.update({
            "games_df":   games_df,
            "matchup_df": matchup_df,
            "last_run":   datetime.now().isoformat(),
            "status":     "done",
        })

        # Persist to disk for Fly.io restart recovery
        out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, f"mlb_matchups_{date.today().strftime('%Y%m%d')}.csv")
        matchup_df.to_csv(out_path, index=False)
        log.info(f"[pipeline] Done — {len(matchup_df)} rows → {out_path}")

    except Exception as e:
        _cache["status"]    = "error"
        _cache["error_msg"] = str(e)
        log.error(f"[pipeline] Error: {e}", exc_info=True)


# ── 8. Daily Scheduler Loop ────────────────────────────────────────────────────
def _scheduler_loop():
    log.info(f"[pipeline] Scheduler armed — fires {RUN_HOUR_ET:02d}:{RUN_MINUTE_ET:02d} ET daily.")
    last_run_date = None
    while True:
        now = datetime.now(ET)
        if (now.hour == RUN_HOUR_ET and now.minute == RUN_MINUTE_ET
                and now.date() != last_run_date):
            last_run_date = now.date()
            threading.Thread(target=run_pipeline, daemon=True).start()
        time.sleep(30)


def start_scheduler():
    """Call once from app.py after app is created. Runs pipeline on boot + daily at 9 AM ET."""
    # Boot run — load from disk if today's CSV exists, else fetch fresh
    out_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "data", f"mlb_matchups_{date.today().strftime('%Y%m%d')}.csv"
    )
    if os.path.exists(out_path):
        log.info(f"[pipeline] Cold-start: loading {out_path} from disk.")
        try:
            _cache["matchup_df"] = pd.read_csv(out_path)
            _cache["status"]     = "done"
            _cache["last_run"]   = datetime.fromtimestamp(
                os.path.getmtime(out_path)).isoformat()
        except Exception as e:
            log.warning(f"[pipeline] Disk load failed: {e}. Fetching fresh.")
            threading.Thread(target=run_pipeline, daemon=True).start()
    else:
        threading.Thread(target=run_pipeline, daemon=True).start()

    threading.Thread(target=_scheduler_loop, daemon=True).start()


# ── Public accessors (used by pipeline_routes.py) ─────────────────────────────
def get_matchup_df() -> pd.DataFrame:
    return _cache["matchup_df"] if _cache["matchup_df"] is not None else pd.DataFrame()

def get_games_df() -> pd.DataFrame:
    return _cache["games_df"] if _cache["games_df"] is not None else pd.DataFrame()

def get_pipeline_status() -> dict:
    df = _cache["matchup_df"]
    return {
        "status":   _cache["status"],
        "last_run": _cache["last_run"],
        "rows":     len(df) if df is not None else 0,
        "games":    len(_cache["games_df"]) if _cache["games_df"] is not None else 0,
        "error":    _cache["error_msg"],
    }
