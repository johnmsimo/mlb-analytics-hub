# pipeline_scheduler.py
"""
Background pipeline scheduler for MLB Analytics Hub.

Fires once at RUN_HOUR_ET:RUN_MINUTE_ET (morning data pull) and a second
time at _LINEUP_LOCK_HOUR_ET (11 AM ET) to re-score every K prop against
confirmed lineups and today's home-plate umpire assignments.
"""
from __future__ import annotations

import os
import threading
import time
import logging
import importlib
from datetime import datetime, date

import pandas as pd
import pytz

ET = pytz.timezone("America/New_York")
log = logging.getLogger(__name__)

# -- Morning pipeline fire time ------------------------------------------------
RUN_HOUR_ET   = 9
RUN_MINUTE_ET = 0

# -- Lineup-lock rescore time (after confirmed lineups post ~10-10:30 AM ET) ---
_LINEUP_LOCK_HOUR_ET   = 11
_LINEUP_LOCK_MINUTE_ET = 0

_cache: dict = {
    "matchup_df": pd.DataFrame(),
    "games_df":   pd.DataFrame(),
    "status":     "idle",
    "last_run":   None,
}
_cache_lock = threading.RLock()


def _import_app_modules():
    app_mod        = importlib.import_module("schedule_collector")
    fetch_schedule = getattr(app_mod, "fetch_schedule", None)
    sv_mod         = importlib.import_module("statcast_loader")
    _sv_batter     = getattr(sv_mod, "get_batter_statcast", None)
    _sv_pitcher    = getattr(sv_mod, "get_pitcher_statcast", None)
    return fetch_schedule, _sv_batter, _sv_pitcher


# -- 1. Today's Games ----------------------------------------------------------
def _build_games_df(fetch_schedule, target_date=None):
    date_str = target_date or datetime.now(ET).strftime("%Y-%m-%d")
    try:
        games_raw = fetch_schedule(date_str)
        return pd.DataFrame(games_raw) if games_raw else pd.DataFrame()
    except Exception as e:
        log.error(f"[pipeline] fetch_schedule failed: {e}")
        return pd.DataFrame()


# -- 2-7. Helper stubs (unchanged) ---------------------------------------------

def _get_position_player_ids(team_id):
    try:
        import requests
        url  = f"https://statsapi.mlb.com/api/v1/teams/{team_id}/roster?rosterType=active"
        resp = requests.get(url, timeout=10)
        return [
            p["person"]["id"]
            for p in resp.json().get("roster", [])
            if p.get("position", {}).get("type") not in ("Pitcher",)
        ]
    except Exception:
        return []


def _get_bvp(batter_id, pitcher_id):
    try:
        import requests
        url   = (
            f"https://statsapi.mlb.com/api/v1/people/{batter_id}/stats"
            f"?stats=vsPlayer&opposingPlayerId={pitcher_id}&group=hitting"
        )
        resp  = requests.get(url, timeout=10)
        stats = resp.json().get("stats", [])
        if not stats:
            return {}
        splits = stats[0].get("splits", [])
        if not splits:
            return {}
        s = splits[0].get("stat", {})
        return {
            "bvp_avg": float(s.get("avg",  0)),
            "bvp_ops": float(s.get("ops",  0)),
            "bvp_pa":  int(s.get("plateAppearances", 0)),
            "bvp_hr":  int(s.get("homeRuns", 0)),
            "bvp_so":  int(s.get("strikeOuts", 0)),
        }
    except Exception:
        return {}


def _get_platoon_splits(player_id, group="hitting"):
    try:
        import requests
        url    = (
            f"https://statsapi.mlb.com/api/v1/people/{player_id}/stats"
            f"?stats=statSplits&group={group}&sitCodes=vl,vr"
        )
        resp   = requests.get(url, timeout=10)
        result = {}
        for block in resp.json().get("stats", []):
            for split in block.get("splits", []):
                hand   = split.get("split", {}).get("code", "")
                s      = split.get("stat", {})
                suffix = f"_vs_{'L' if hand == 'vl' else 'R'}"
                result[f"avg{suffix}"]  = float(s.get("avg",  0))
                result[f"ops{suffix}"]  = float(s.get("ops",  0))
                result[f"slg{suffix}"]  = float(s.get("slg",  0))
                result[f"obp{suffix}"]  = float(s.get("obp",  0))
                result[f"woba{suffix}"] = float(s.get("woba", 0))
        return result
    except Exception:
        return {}


def _extract_batter_statcast(sv_batter_fn, player_name):
    try:
        if sv_batter_fn is None:
            return {}
        result = sv_batter_fn(player_name)
        return result if isinstance(result, dict) else {}
    except Exception:
        return {}


def _extract_pitcher_statcast(sv_pitcher_fn, pitcher_name):
    try:
        if sv_pitcher_fn is None:
            return {}
        result = sv_pitcher_fn(pitcher_name)
        return result if isinstance(result, dict) else {}
    except Exception:
        return {}


def _fetch_batter_row(batter_id, pitcher_id, pitcher_name, game_meta, sv_batter_fn):
    try:
        import requests
        url   = f"https://statsapi.mlb.com/api/v1/people/{batter_id}"
        resp  = requests.get(url, timeout=8)
        pdata = resp.json().get("people", [{}])[0]
        name  = pdata.get("fullName", str(batter_id))
        hand  = pdata.get("batSide", {}).get("code", "R")
    except Exception:
        name, hand = str(batter_id), "R"

    return {
        "batter_id":   batter_id,
        "batter_name": name,
        "bat_hand":    hand,
        **game_meta,
        **_get_bvp(batter_id, pitcher_id),
        **_get_platoon_splits(batter_id, "hitting"),
        **_extract_batter_statcast(sv_batter_fn, name),
    }


def _build_matchup_df(games_df, sv_batter_fn, sv_pitcher_fn):
    rows = []
    if games_df.empty:
        return pd.DataFrame()

    for _, game in games_df.iterrows():
        game_pk      = game.get("game_pk",    0)
        home_team_id = game.get("home_team_id", 0)
        away_team_id = game.get("away_team_id", 0)
        home_pitcher = game.get("home_pitcher_id")
        away_pitcher = game.get("away_pitcher_id")
        home_p_name  = game.get("home_pitcher_name", "")
        away_p_name  = game.get("away_pitcher_name", "")
        venue        = game.get("venue", "")
        game_time    = game.get("game_time", "")

        home_batters = _get_position_player_ids(home_team_id)
        away_batters = _get_position_player_ids(away_team_id)

        home_pitcher_stats = _extract_pitcher_statcast(sv_pitcher_fn, home_p_name)
        away_pitcher_stats = _extract_pitcher_statcast(sv_pitcher_fn, away_p_name)

        base = {"game_pk": game_pk, "venue": venue, "game_time": game_time}

        for bid in away_batters:
            rows.append(_fetch_batter_row(
                bid, home_pitcher, home_p_name,
                {**base,
                 "pitcher_id": home_pitcher, "pitcher_name": home_p_name,
                 "pitcher_hand": game.get("home_pitcher_hand", "R"),
                 "team": "away", "home_pitcher": False,
                 **{f"p_{k}": v for k, v in home_pitcher_stats.items()}},
                sv_batter_fn,
            ))

        for bid in home_batters:
            rows.append(_fetch_batter_row(
                bid, away_pitcher, away_p_name,
                {**base,
                 "pitcher_id": away_pitcher, "pitcher_name": away_p_name,
                 "pitcher_hand": game.get("away_pitcher_hand", "R"),
                 "team": "home", "home_pitcher": True,
                 **{f"p_{k}": v for k, v in away_pitcher_stats.items()}},
                sv_batter_fn,
            ))

    return pd.DataFrame(rows)


def _resolve_target_date(target_date=None):
    if target_date is None:
        return datetime.now(ET).date()
    if isinstance(target_date, str):
        return date.fromisoformat(target_date)
    if isinstance(target_date, datetime):
        return target_date.date()
    return target_date


# -- Main pipeline -------------------------------------------------------------
def run_pipeline(target_date=None):
    with _cache_lock:
        if _cache.get("status") == "running":
            log.warning("[pipeline] Already running - skipping duplicate trigger.")
            return
        _cache["status"] = "running"

    try:
        fetch_date_str = (
            target_date if isinstance(target_date, str)
            else _resolve_target_date(target_date).strftime("%Y-%m-%d")
        )
        resolved_date = _resolve_target_date(target_date)

        fetch_schedule, sv_batter_fn, sv_pitcher_fn = _import_app_modules()
        if fetch_schedule is None:
            raise ImportError("fetch_schedule not found in schedule_collector")

        games_df = _build_games_df(fetch_schedule, target_date=fetch_date_str)

        if games_df.empty:
            log.warning("[pipeline] No games found - matchup_df will be empty.")
            with _cache_lock:
                _cache["games_df"]   = games_df
                _cache["matchup_df"] = pd.DataFrame()
                _cache["status"]     = "done"
                _cache["last_run"]   = datetime.now(ET).isoformat()
            return

        matchup_df = _build_matchup_df(games_df, sv_batter_fn, sv_pitcher_fn)

        with _cache_lock:
            _cache["games_df"]   = games_df
            _cache["matchup_df"] = matchup_df
            _cache["status"]     = "done"
            _cache["last_run"]   = datetime.now(ET).isoformat()

        log.info(f"[pipeline] Done - {len(matchup_df)} matchup rows for {fetch_date_str}")

        try:
            _refresh_best_bets_signals(resolved_date)
        except Exception as e:
            log.warning(f"[pipeline] Best Bets refresh failed: {e}")

        with _cache_lock:
            _cache["status"] = "done"

    except Exception as e:
        log.error(f"[pipeline] run_pipeline failed: {e}", exc_info=True)
        with _cache_lock:
            _cache["status"] = "error"


def _refresh_best_bets_signals(target_date):
    """Refresh FG Stuff+, catcher framing, bat tracking, Ballpark Pal, umpire,
    and the tier calibrator.  Each step is independently try/except'd.
    """
    year     = target_date.year if hasattr(target_date, "year") else datetime.now(ET).year
    date_str = target_date.strftime("%Y-%m-%d") if hasattr(target_date, "strftime") else None

    for label, work in (
        ("fg_stuff",        lambda: __import__("fg_stuff_loader").fetch_and_save(year)),
        ("framing",         lambda: __import__("framing_loader").fetch_and_save(year)),
        ("bat_tracking",    lambda: __import__("savant_bat_tracking").fetch_and_save(year)),
        ("ballparkpal",     lambda: __import__("ballparkpal_loader").fetch_and_save(date_str)),
        ("umpire",          lambda: __import__("umpire_loader").fetch_and_save(date_str)),
        ("tier_calibrator", lambda: __import__("tier_calibrator").calibrate()),
    ):
        try:
            n = work()
            log.info(f"[pipeline.best_bets] {label}: refreshed (rows/result={n!r})")
        except Exception as e:
            log.warning(f"[pipeline.best_bets] {label} failed: {e}")


# -- Lineup-lock rescore -------------------------------------------------------
def _rescore_with_confirmed_lineups(target_date=None):
    """
    Re-run _refresh_best_bets_signals then patch the live matchup_df with
    real confirmed-lineup stats and umpire zone features.
    Called automatically at 11:00 AM ET after MLB lineups post.
    """
    resolved = _resolve_target_date(target_date)
    log.info(f"[pipeline] Lineup-lock rescore triggered for {resolved}")
    try:
        _refresh_best_bets_signals(resolved)

        try:
            import lineup_loader
            import umpire_loader
            lineups = lineup_loader.get_today_lineups()          # game_pk -> lineup stats dict
            ump_map = umpire_loader.fetch_and_save(             # game_pk -> ump name
                resolved.strftime("%Y-%m-%d")
            )

            with _cache_lock:
                df = _cache.get("matchup_df")
                if df is not None and not df.empty and "game_pk" in df.columns:
                    df = df.copy()
                    for idx, row in df.iterrows():
                        gpk = int(row.get("game_pk", 0))

                        # Umpire zone features
                        ump_name = ump_map.get(gpk, "")
                        uf = umpire_loader.get_umpire_features(ump_name)
                        df.at[idx, "ump_zone_size"] = uf["ump_zone_size"]
                        df.at[idx, "ump_k_boost"]   = uf["ump_k_boost"]

                        # Confirmed-lineup k_pct / xwoba
                        lineup_data = lineups.get(gpk)
                        if lineup_data:
                            is_home_pitcher = bool(row.get("home_pitcher", False))
                            opp_kpct  = (
                                lineup_data.get("away_k_pct")
                                if is_home_pitcher
                                else lineup_data.get("home_k_pct")
                            )
                            opp_xwoba = (
                                lineup_data.get("away_xwoba")
                                if is_home_pitcher
                                else lineup_data.get("home_xwoba")
                            )
                            if opp_kpct  is not None:
                                df.at[idx, "opp_lineup_k_pct_proxy"] = float(opp_kpct)
                            if opp_xwoba is not None:
                                df.at[idx, "opp_lineup_xwoba_proxy"] = float(opp_xwoba)

                    _cache["matchup_df"] = df
                    log.info(
                        f"[pipeline] Lineup-lock patch applied to matchup_df "
                        f"({len(df)} rows) for {resolved}"
                    )
        except Exception as e:
            log.warning(f"[pipeline] Lineup/umpire injection failed: {e}")

    except Exception as e:
        log.error(f"[pipeline] Lineup-lock rescore failed: {e}")


# -- Scheduler loop ------------------------------------------------------------
def _scheduler_loop():
    log.info(
        f"[pipeline] Scheduler armed - "
        f"main run {RUN_HOUR_ET:02d}:{RUN_MINUTE_ET:02d} ET, "
        f"lineup-lock rescore {_LINEUP_LOCK_HOUR_ET:02d}:{_LINEUP_LOCK_MINUTE_ET:02d} ET."
    )
    last_run_date     = None
    last_rescore_date = None
    while True:
        now   = datetime.now(ET)
        today = now.date()

        # Morning pipeline (9 AM ET)
        if (now.hour == RUN_HOUR_ET and now.minute == RUN_MINUTE_ET
                and today != last_run_date):
            last_run_date = today
            threading.Thread(target=run_pipeline, daemon=True).start()

        # Lineup-lock rescore (11 AM ET)
        if (now.hour == _LINEUP_LOCK_HOUR_ET and now.minute == _LINEUP_LOCK_MINUTE_ET
                and today != last_rescore_date):
            last_rescore_date = today
            threading.Thread(
                target=_rescore_with_confirmed_lineups, daemon=True
            ).start()

        time.sleep(30)


def start_scheduler():
    """Call once from app.py after app is created.
    Loads today's data from disk on cold-start, then arms the daily loops.
    """
    out_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "data", f"mlb_matchups_{datetime.now(ET).date().strftime('%Y%m%d')}.csv"
    )
    if os.path.exists(out_path):
        log.info(f"[pipeline] Cold-start: loading {out_path} from disk.")
        try:
            with _cache_lock:
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


# -- Public accessors (used by pipeline_routes.py) -----------------------------
def get_matchup_df() -> pd.DataFrame:
    with _cache_lock:
        return _cache.get("matchup_df", pd.DataFrame()).copy()

def get_games_df() -> pd.DataFrame:
    with _cache_lock:
        return _cache.get("games_df", pd.DataFrame()).copy()

def get_pipeline_status() -> dict:
    with _cache_lock:
        return {
            "status":   _cache.get("status", "idle"),
            "last_run": _cache.get("last_run"),
        }
