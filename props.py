"""Prop projections, slate scans, and props-related route handlers."""

import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import requests
from flask import jsonify, request
from mc_upgrades import BATX_WEIGHTS_V2


def configure_props_context(namespace):
    globals().update(namespace)


# Stat defaults used as fallback when both split and season are missing
_STAT_DEFAULTS = {
    'avg': 0.245,
    'obp': 0.315,
    'slg': 0.390,
    'ops': 0.705,
}

# ── BAT X component weights (calibration hook) ────────────────────────────────
# Sum of weights defines relative contribution to composite multiplier.
# Increase/decrease individual weights to calibrate vs historical results.
BATX_WEIGHTS = BATX_WEIGHTS_V2


def _batter_hand_note(batter, pitcher_hand):
    """
    Returns a short string describing the platoon matchup, e.g. 'LHB vs RHP (+)'
    Used for UI display on the projection card.
    """
    bat  = (batter.get('bats') or 'S').upper()
    pit  = (pitcher_hand or 'R').upper()
    if bat == 'S':
        return f"SB vs {'RHP' if pit == 'R' else 'LHP'} (switch)"
    favorable = (bat == 'L' and pit == 'R') or (bat == 'R' and pit == 'L')
    direction = '+' if favorable else '−'
    return f"{'LHB' if bat == 'L' else 'RHB'} vs {'RHP' if pit == 'R' else 'LHP'} ({direction})"


# ── Batter projection engine (v2 — platoon-aware) ────────────────────────────
def _project_batter(batter, opp_pitcher_name, opp_pitcher_fg, opp_pitcher_sv,
                    park_factor, weather, pitcher_hand='R'):
    """
    Returns per-prop projections for one batter.
    Uses platoon-blended avg/obp/slg/ops as base rates when split PA is available.
    """
    name = batter.get("name", "")
    fg   = fg_batter(name)
    sv   = sv_batter(name)

    # ── Platoon-blended base rates ────────────────────────────────────────────
    avg  = platoon_blend_v2(batter, pitcher_hand, 'avg')
    obp  = platoon_blend_v2(batter, pitcher_hand, 'obp')
    slg  = platoon_blend_v2(batter, pitcher_hand, 'slg')
    ops  = platoon_blend_v2(batter, pitcher_hand, 'ops')

    # Fallback chain for fg-only stats (not in splits)
    fg_pa  = _safe_f(fg.get("fg_pa"),  200)
    fg_hr  = _safe_f(fg.get("fg_hr"),  3)
    fg_rbi = _safe_f(fg.get("fg_rbi"), 12)
    fg_r   = _safe_f(fg.get("fg_r"),   12)
    hr_r   = fg_hr  / max(fg_pa, 1)
    rbi_r  = fg_rbi / max(fg_pa, 1)
    r_r    = fg_r   / max(fg_pa, 1)

    # Scale HR rate by platoon OPS ratio (strong platoon advantage boosts power)
    season_ops  = _safe_f(batter.get('ops'), _STAT_DEFAULTS['ops'])
    ops_ratio   = ops / max(season_ops, 0.400)            # ≈1.0 neutral, >1 platoon advantage
    hr_r_adj    = hr_r * ops_ratio                        # HR scales with ops split

    xwoba = _safe_f(sv.get("sv_xwoba") or fg.get("fg_woba"), 0.310)
    brl   = _safe_f(sv.get("sv_brl_pct"), 6.0) / 100

    # ── Pitcher resistance multiplier ─────────────────────────────────────────
    opp_era  = _safe_f(opp_pitcher_fg.get("fg_era")  or opp_pitcher_sv.get("sv_era_p"), 4.20)
    opp_xera = _safe_f(opp_pitcher_sv.get("sv_xera"), opp_era)
    opp_xfip = _safe_f(opp_pitcher_fg.get("fg_xfip"), (opp_era + opp_xera) / 2)
    opp_kpct = _safe_f(opp_pitcher_fg.get("fg_kpct"), 0.22)
    bat_kpct = _safe_f(fg.get("fg_kpct") or sv.get("sv_k_pct"), 0.22)

    # xFIP as 3rd anchor: stabilizes low-IP pitchers; weighted avg of ERA/xERA/xFIP
    pit_mult = min(1.25, max(0.72, (opp_era * 0.35 + opp_xera * 0.35 + opp_xfip * 0.30) / 4.20))
    k_adj    = 1.0 - max(0.0, (opp_kpct - bat_kpct) * 0.5)

    # ── Platoon K-rate modifier ───────────────────────────────────────────────
    # Platoon disadvantage → more K exposure
    bat  = (batter.get('bats') or 'S').upper()
    pit  = (pitcher_hand or 'R').upper()
    if bat != 'S':
        platoon_k_mod = 0.96 if ((bat == 'L' and pit == 'R') or (bat == 'R' and pit == 'L')) else 1.04
    else:
        platoon_k_mod = 1.0   # switch hitter, neutral
    k_adj *= platoon_k_mod

    # ── Weather multiplier ────────────────────────────────────────────────────
    dome      = weather.get("dome", False)
    wx_mult   = 1.0
    if not dome:
        temp_f   = _safe_f(weather.get("temp"), 72)
        wind_spd = _safe_f(weather.get("wind_speed"), 0)
        wx_mult  = 1.0 + (temp_f - 72) * 0.003 + min(0.06, float(wind_spd) * 0.003)

    # ── Expected PA by slot ───────────────────────────────────────────────────
    slot   = int(batter.get("slot") or 5)
    exp_pa = round(4.35 - (slot - 1) * 0.095, 2)

    # ── Projections ───────────────────────────────────────────────────────────
    # Hits: platoon avg replaces season avg as base
    hits_proj = round(max(0.05, avg * exp_pa * pit_mult * k_adj * park_factor * wx_mult), 3)

    # Total Bases: built from platoon slg
    tb_proj   = round(max(0.08, slg * exp_pa * pit_mult * k_adj * park_factor * wx_mult), 3)

    # HR: platoon-adjusted hr rate + barrel bonus + park + weather
    hr_pf     = min(1.30, park_factor * 1.08)
    hr_proj   = round(max(0.005,
        hr_r_adj * exp_pa * hr_pf * (1.0 + (brl - 0.06) * 0.8) * wx_mult
    ), 4)

    # RBI: correlated to hits+HR, slot bonus
    slot_rbi_bonus = max(0.8, 1.0 + (4 - abs(slot - 4)) * 0.03)
    rbi_proj  = round(max(0.05, rbi_r * exp_pa * pit_mult * park_factor * slot_rbi_bonus), 3)

    # Runs: lead-off slots score more
    slot_r_bonus = max(0.8, 1.1 - abs(slot - 1.5) * 0.025)
    r_proj    = round(max(0.04, r_r * exp_pa * pit_mult * slot_r_bonus), 3)

    # H+R+RBI combo
    hrr_proj  = round(hits_proj + r_proj + rbi_proj, 3)

    return {
        "hits":        hits_proj,
        "hr":          hr_proj,
        "tb":          tb_proj,
        "rbi":         rbi_proj,
        "r":           r_proj,
        "hrr":         hrr_proj,
        "expected_pa": exp_pa,
        # expose the blended rates so the frontend can show them
        "split_avg":   round(avg, 3),
        "split_ops":   round(ops, 3),
        "platoon_note": _batter_hand_note(batter, pitcher_hand),
    }


# ── BAT X projection engine (v3) ──────────────────────────────────────────────
_LEAGUE_WOBA    = 0.320
_LEAGUE_BB_PCT  = 0.083
_LEAGUE_K_PCT   = 0.220
_LEAGUE_EV      = 88.5
_LEAGUE_BRL_PCT = 0.063   # 6.3 %

def _empty_props_scan_payload(date_str):
    return {
        'success': True,
        'date': date_str,
        'games': [],
        'gameCount': 0,
        'props': [],
        'batters': [],
        'pitchers': [],
        'injury_summary': {'count': 0, 'players': []},
        'matchup': 'FULL SLATE · 0 GAMES',
        'scanMode': True,
        'generatedAt': datetime.now(timezone.utc).isoformat(),
        'cacheAgeSec': 0,
        'cached': False,
    }

def _compute_props_scan_today_payload(date_str):
    now = time.time()
    _maybe_refresh_fg()
    _maybe_refresh_savant()
    _fetch_injury_status(force=False)

    adjustments = _get_adjustments()
    raw_games = fetch_schedule(date_str)
    parsed_games = [parse_game(g) for g in raw_games]
    batters = []
    pitchers = []
    flat_props = []
    injury_rows = []

    def _scan_game(game):
        game_pk = game.get('gamePk')
        local_batters = []
        local_pitchers = []
        local_props = []
        local_injuries = []
        if not game_pk:
            return local_batters, local_pitchers, local_props, local_injuries
        try:
            with app.test_request_context(
                f'/api/props/projections/{int(game_pk)}',
                query_string={'date': date_str},
            ):
                proj_resp = api_props_projections(int(game_pk))
            status_code = 200
            if isinstance(proj_resp, tuple):
                proj_resp, status_code = proj_resp
            proj_payload = proj_resp.get_json(silent=True) if hasattr(proj_resp, 'get_json') else None
            if status_code != 200 or not proj_payload or not proj_payload.get('success'):
                return local_batters, local_pitchers, local_props, local_injuries

            tracker_rows = _build_tracker_rows_for_game(int(game_pk), date_str, adjustments, _sched=raw_games, include_odds=True) or []
            best_by_player = {}
            for row in tracker_rows:
                player_key = (row.get('player') or '').lower()
                if not player_key:
                    continue
                current = best_by_player.get(player_key)
                if current is None or float(row.get('hubRating') or 0) > float(current.get('hubRating') or 0):
                    best_by_player[player_key] = row

            for batter in proj_payload.get('batters', []):
                player_key = (batter.get('name') or '').lower()
                best = best_by_player.get(player_key)
                item = dict(batter)
                item['gamePk'] = int(game_pk)
                item['matchup'] = proj_payload.get('matchup')
                item['awayAbbr'] = proj_payload.get('awayAbbr')
                item['homeAbbr'] = proj_payload.get('homeAbbr')
                item['scanHubRating'] = best.get('hubRating') if best else None
                item['scanEvPct'] = best.get('evPct') if best else None
                item['scanBestProp'] = best
                local_batters.append(item)

            for pitcher in proj_payload.get('pitchers', []):
                item = dict(pitcher)
                item['gamePk'] = int(game_pk)
                item['matchup'] = proj_payload.get('matchup')
                item['awayAbbr'] = proj_payload.get('awayAbbr')
                item['homeAbbr'] = proj_payload.get('homeAbbr')
                local_pitchers.append(item)

            for row in tracker_rows:
                item = dict(row)
                item['matchup'] = proj_payload.get('matchup')
                item['awayAbbr'] = proj_payload.get('awayAbbr')
                item['homeAbbr'] = proj_payload.get('homeAbbr')
                local_props.append(item)

            for row in (proj_payload.get('injury_summary') or {}).get('players', []):
                local_injuries.append(dict(row))
        except Exception:
            print(f'[api_props_scan_today game {game_pk}] {traceback.format_exc()}')
        return local_batters, local_pitchers, local_props, local_injuries

    scan_workers = min(6, max(1, len(raw_games)))
    with ThreadPoolExecutor(max_workers=scan_workers) as ex:
        futs = [ex.submit(_scan_game, g) for g in raw_games]
        for fut in as_completed(futs):
            b, p, props, injuries = fut.result()
            batters.extend(b)
            pitchers.extend(p)
            flat_props.extend(props)
            injury_rows.extend(injuries)

    flat_props.sort(key=lambda x: (-(float(x.get('hubRating') or 0)), -(float(x.get('edge') or 0)), -(float(x.get('adjProb') or 0))))
    batters.sort(key=lambda x: (-(float(x.get('scanHubRating') or 0)), -(float(((x.get('proj') or {}).get('hits') or 0)))))
    pitchers.sort(key=lambda x: (-(float((((x.get('proj') or {}).get('k')) or 0))), x.get('name') or ''))

    payload = {
        'success': True,
        'date': date_str,
        'games': parsed_games,
        'gameCount': len(parsed_games),
        'props': flat_props,
        'batters': batters,
        'pitchers': pitchers,
        'injury_summary': {
            'count': len(injury_rows),
            'players': injury_rows[:20],
        },
        'matchup': f'FULL SLATE · {len(parsed_games)} GAMES',
        'scanMode': True,
        'generatedAt': datetime.now(timezone.utc).isoformat(),
        'cacheAgeSec': 0,
        'cached': False,
    }
    with _props_scan_cache_lock:
        _PROPS_SCAN_CACHE[date_str] = {'ts': now, 'payload': payload}
    return payload

def _trigger_props_scan_refresh_async(date_str, reason='auto'):
    global _props_scan_refreshing
    with _props_scan_cache_lock:
        if _props_scan_refreshing:
            return False
        _props_scan_refreshing = True

    def _runner():
        global _props_scan_refreshing
        try:
            _compute_props_scan_today_payload(date_str)
            print(f'[props_scan] refreshed ({reason})')
        except Exception:
            print(f'[props_scan] refresh failed {traceback.format_exc()}')
        finally:
            with _props_scan_cache_lock:
                _props_scan_refreshing = False

    threading.Thread(target=_runner, daemon=True).start()
    return True

def _props_scan_today_payload(date_str, refresh=False):
    now = time.time()
    with _props_scan_cache_lock:
        cached = _PROPS_SCAN_CACHE.get(date_str)
        refreshing = _props_scan_refreshing
    ts = float((cached or {}).get('ts') or 0)
    age = int(now - ts) if ts else 0

    if cached and not refresh and (now - ts) < _PROPS_SCAN_TTL:
        payload = dict(cached.get('payload') or {})
        payload['cacheAgeSec'] = age
        payload['cached'] = True
        payload['computing'] = False
        return payload

    if refresh:
        return _compute_props_scan_today_payload(date_str)

    if cached:
        if not refreshing:
            _trigger_props_scan_refresh_async(date_str, reason='stale_cache')
        payload = dict(cached.get('payload') or {})
        payload['cacheAgeSec'] = age
        payload['cached'] = True
        payload['computing'] = True
        payload['message'] = 'Refreshing in background'
        return payload

    if not refreshing:
        _trigger_props_scan_refresh_async(date_str, reason='cold_start')
    payload = _empty_props_scan_payload(date_str)
    payload['computing'] = True
    payload['message'] = 'Computing... auto-refresh in 20s'
    return payload

def api_projections_monte_carlo():
    """Returns instantly from cache — background thread does all work."""
    force = request.args.get('refresh') == '1'
    _mc_maybe_refresh(force=force)
    with _mc_cache_lock:
        cached    = _mc_cache_data
        computing = _mc_computing
    if cached and not force:
        return jsonify(dict(cached, computing=computing))
    date_str = datetime.now(ET).strftime('%Y-%m-%d')
    return jsonify({'success': True, 'computing': True, 'date': date_str,
                    'hasOdds': bool(ODDS_API_KEY), 'games': [], 'topProps': [],
                    'message': 'Computing… auto-refreshing in 20s'})

def _props_fetch_game(game_pk, date_hint=None, gdata_override=None):
    """Fetch schedule entry + boxscore lineups, preferring the requested date.

    Pass gdata_override to skip the schedule fetch when the caller already has
    the raw schedule entry (e.g. cheatsheet bulk processing avoids N redundant
    fetch_schedule calls by passing the already-fetched entry directly).
    """
    if gdata_override and gdata_override.get("gamePk") == game_pk:
        gdata = gdata_override
    else:
        gdata = None
        candidate_dates = []
        if date_hint:
            date_hint = _normalize_date_str(date_hint, fallback='')
            if date_hint:
                candidate_dates.append(date_hint)
                try:
                    base_dt = datetime.strptime(date_hint, "%Y-%m-%d")
                    candidate_dates.extend([
                        (base_dt + timedelta(days=-1)).strftime("%Y-%m-%d"),
                        (base_dt + timedelta(days=1)).strftime("%Y-%m-%d"),
                    ])
                except Exception:
                    pass
        for delta in (0, -1, 1):
            candidate_dates.append((datetime.now(ET) + timedelta(days=delta)).strftime("%Y-%m-%d"))

        seen_dates = set()
        for date_str in candidate_dates:
            if not date_str or date_str in seen_dates:
                continue
            seen_dates.add(date_str)
            raw   = fetch_schedule(date_str)
            gdata = next((g for g in raw if g.get("gamePk") == game_pk), None)
            if gdata:
                break
    if not gdata:
        return None, [], [], {}, {}, {}

    away_t  = gdata.get("teams", {}).get("away", {})
    home_t  = gdata.get("teams", {}).get("home", {})
    ap_info = away_t.get("probablePitcher", {})
    hp_info = home_t.get("probablePitcher", {})

    # Skip boxscore fetch for postponed/cancelled games — there is no game to score.
    _gstatus = str((gdata.get("status") or {}).get("detailedState") or "").lower()
    _is_inactive = any(tok in _gstatus for tok in ("postponed", "cancelled", "canceled", "suspended"))

    away_bats, home_bats = [], []
    if not _is_inactive:
        try:
            r = requests.get(f"{MLB_API}/game/{game_pk}/boxscore", timeout=10)
            r.raise_for_status()
            box = r.json().get("teams", {})
            away_bats = get_batters_from_boxscore(box.get("away", {}), "away")
            home_bats = get_batters_from_boxscore(box.get("home", {}), "home")
        except Exception as ex:
            print(f"[props] boxscore error: {ex}")

    # Pre-game fallback: hydrate scheduled lineups from schedule payload.
    if not away_bats or not home_bats:
        lineups = (gdata.get("lineups") or {})

        def _parse_sched(hitters):
            out = []
            for i, p in enumerate(hitters or [], start=1):
                name = (p.get("fullName") or p.get("name") or "").strip()
                pid  = p.get("id") or p.get("playerId")
                pos  = (p.get("primaryPosition") or {}).get("abbreviation", "?")
                if not name:
                    continue
                fgb = fg_batter(name)
                svb = sv_batter(name)
                out.append({
                    "slot": i, "id": pid, "name": name, "pos": pos,
                    "lineup_status": "pending",
                    "avg": fgb.get("fg_avg", ".---"), "obp": fgb.get("fg_obp", ".---"),
                    "slg": fgb.get("fg_slg", ".---"), "ops": fgb.get("fg_ops", ".---"),
                    "ab": 0, "hits": 0, "hr": 0, "rbi": 0,
                    "fg_pa": fgb.get("fg_pa", "N/A"), "fg_r": fgb.get("fg_r", "N/A"),
                    "fg_sb": fgb.get("fg_sb", "N/A"), "fg_woba": fgb.get("fg_woba", "N/A"),
                    "fg_wrc": fgb.get("fg_wrc", "N/A"), "fg_war": fgb.get("fg_war", "N/A"),
                    "sv_xba": svb.get("sv_xba", "N/A"), "sv_xslg": svb.get("sv_xslg", "N/A"),
                    "sv_xwoba": svb.get("sv_xwoba", "N/A"), "sv_ev": svb.get("sv_ev", "N/A"),
                    "sv_hh_pct": svb.get("sv_hh_pct", "N/A"), "sv_brl_pct": svb.get("sv_brl_pct", "N/A"),
                    "sv_la": svb.get("sv_la", "N/A"),
                })
            return out

        if not away_bats:
            away_bats = _parse_sched(lineups.get("awayBatters") or [])
        if not home_bats:
            home_bats = _parse_sched(lineups.get("homeBatters") or [])

    # Last resort: build a provisional top-9 from active roster so projections
    # still render before official/projected lineups are published.
    if not away_bats or not home_bats:
        def _roster_fallback(team_id):
            out = []
            if not team_id:
                return out
            try:
                for p in _get_active_roster(team_id):
                    pos = ((p.get("position") or {}).get("abbreviation") or "?").upper()
                    if pos in ("P", "SP", "RP", "CP"):
                        continue
                    person = p.get("person") or {}
                    name = (person.get("fullName") or "").strip()
                    pid = person.get("id")
                    if not name:
                        continue
                    fgb = fg_batter(name)
                    svb = sv_batter(name)
                    out.append({
                        "slot": len(out) + 1,
                        "id": pid,
                        "name": name,
                        "pos": pos,
                        "lineup_status": "pending",
                        "avg": fgb.get("fg_avg", ".---"),
                        "obp": fgb.get("fg_obp", ".---"),
                        "slg": fgb.get("fg_slg", ".---"),
                        "ops": fgb.get("fg_ops", ".---"),
                        "ab": 0,
                        "hits": 0,
                        "hr": 0,
                        "rbi": 0,
                        "fg_pa": fgb.get("fg_pa", "N/A"),
                        "fg_r": fgb.get("fg_r", "N/A"),
                        "fg_sb": fgb.get("fg_sb", "N/A"),
                        "fg_woba": fgb.get("fg_woba", "N/A"),
                        "fg_wrc": fgb.get("fg_wrc", "N/A"),
                        "fg_war": fgb.get("fg_war", "N/A"),
                        "sv_xba": svb.get("sv_xba", "N/A"),
                        "sv_xslg": svb.get("sv_xslg", "N/A"),
                        "sv_xwoba": svb.get("sv_xwoba", "N/A"),
                        "sv_ev": svb.get("sv_ev", "N/A"),
                        "sv_hh_pct": svb.get("sv_hh_pct", "N/A"),
                        "sv_brl_pct": svb.get("sv_brl_pct", "N/A"),
                        "sv_la": svb.get("sv_la", "N/A"),
                    })
                    if len(out) >= 9:
                        break
            except Exception as ex:
                print(f"[props] roster fallback error team={team_id}: {ex}")
            return out

        away_team_id = (away_t.get("team") or {}).get("id")
        home_team_id = (home_t.get("team") or {}).get("id")
        if not away_bats:
            away_bats = _roster_fallback(away_team_id)
        if not home_bats:
            home_bats = _roster_fallback(home_team_id)

    return gdata, away_bats, home_bats, away_t, home_t, {"ap": ap_info, "hp": hp_info}

def _platoon_blend(batter, pitcher_hand, stat):
    """
    Returns a platoon-blended value for a given stat key ('avg', 'ops', 'slg', 'obp').
    Blends split stat with season stat weighted by PA sample size:
      >= 100 PA  → 80% split / 20% season
      >= 50  PA  → 65% split / 35% season
      >= 25  PA  → 45% split / 55% season
      <  25  PA  → season only
    Falls back gracefully when splits are missing.
    pitcher_hand: 'L' or 'R' (or 'S' treated as 'R')
    """
    hand = (pitcher_hand or 'R').upper()
    if hand not in ('L', 'R'):
        hand = 'R'

    split_key = f"vs_{'l' if hand == 'L' else 'r'}_{stat}"
    split_val = _safe_f(batter.get(split_key), 0.0)

    # Retrieve split PA — stored as vs_l_pa / vs_r_pa if present, else infer from ops
    pa_key    = f"vs_{'l' if hand == 'L' else 'r'}_pa"
    split_pa  = int(batter.get(pa_key, 0) or 0)

    # Season value
    season_map = {
        'avg': batter.get('avg') or batter.get('fg_avg'),
        'ops': batter.get('ops'),
        'slg': batter.get('slg') or batter.get('fg_slg'),
        'obp': batter.get('obp') or batter.get('fg_obp'),
    }
    season_val = _safe_f(season_map.get(stat), _STAT_DEFAULTS.get(stat, 0.0))

    # No split data — return season
    if split_val <= 0.0:
        return season_val

    # Blend weights by sample size
    if split_pa >= 100:
        w_split = 0.80
    elif split_pa >= 50:
        w_split = 0.65
    elif split_pa >= 25:
        w_split = 0.45
    else:
        w_split = 0.30   # small sample — lean season

    w_season  = 1.0 - w_split
    blended   = round(w_split * split_val + w_season * season_val, 4)
    return blended

def _project_batter_batx(batter, opp_pitcher_name, opp_pitcher_fg, opp_pitcher_sv,
                          park_factor, weather, pitcher_hand='R',
                          opp_pitcher_id=None,
                          form=None, bvp=None):
    """
    BAT X-style projection engine with named component weights (BATX_WEIGHTS).
    Signature is backwards-compatible with _project_batter; adds optional
    form / bvp kwargs (Phase 1 dicts) and an 'adjustments' key in the return dict.

    All per-component raw contributions are stored in `adjustments` for calibration.
    """
    W = BATX_WEIGHTS  # shorthand

    name = batter.get("name", "")
    fg   = fg_batter(name)
    sv   = sv_batter(name)

    # ── Slot / PA ─────────────────────────────────────────────────────────────
    slot   = int(batter.get("slot") or 5)
    exp_pa = round(4.35 - (slot - 1) * 0.095, 2)

    # ── Platoon-blended base rates ────────────────────────────────────────────
    avg  = platoon_blend_v2(batter, pitcher_hand, 'avg')
    obp  = platoon_blend_v2(batter, pitcher_hand, 'obp')
    slg  = platoon_blend_v2(batter, pitcher_hand, 'slg')
    ops  = platoon_blend_v2(batter, pitcher_hand, 'ops')

    season_avg = _safe_f(batter.get('avg') or fg.get('fg_avg'), _STAT_DEFAULTS['avg'])
    season_ops = _safe_f(batter.get('ops'), _STAT_DEFAULTS['ops'])

    # ── Raw stats ─────────────────────────────────────────────────────────────
    fg_pa   = _safe_f(fg.get("fg_pa"),  200)
    fg_hr   = _safe_f(fg.get("fg_hr"),  3)
    fg_rbi  = _safe_f(fg.get("fg_rbi"), 12)
    fg_r    = _safe_f(fg.get("fg_r"),   12)
    fg_iso  = _safe_f(fg.get("fg_iso"), 0.145)
    fg_bbp  = _safe_f(fg.get("fg_bbpct"), _LEAGUE_BB_PCT)
    fg_kp   = _safe_f(fg.get("fg_kpct"),  _LEAGUE_K_PCT)
    fg_woba = _safe_f(fg.get("fg_woba"), _LEAGUE_WOBA)
    fg_wrc  = _safe_f(fg.get("fg_wrc"),  100)

    sv_xba   = _safe_f(sv.get("sv_xba"),    season_avg)
    sv_xslg  = _safe_f(sv.get("sv_xslg"),   slg)
    sv_xwoba = _safe_f(sv.get("sv_xwoba") or fg.get("fg_woba"), _LEAGUE_WOBA)
    sv_ev    = _safe_f(sv.get("sv_ev"),      _LEAGUE_EV)
    sv_brl   = _safe_f(sv.get("sv_brl_pct"), _LEAGUE_BRL_PCT * 100) / 100   # → fraction
    sv_hh    = _safe_f(sv.get("sv_hh_pct"),  35.0) / 100                     # → fraction
    pull_air = _safe_f(sv.get("pull_pct_air"), 35.0) / 100

    hr_r  = fg_hr  / max(fg_pa, 1)
    rbi_r = fg_rbi / max(fg_pa, 1)
    r_r   = fg_r   / max(fg_pa, 1)

    # ── COMPONENT 1: Contact ──────────────────────────────────────────────────
    # Blend platoon AVG (80%) with xBA (20%); PA-confidence shrink vs league
    #   if <100 PA in platoon split, _platoon_blend already applies its own shrink.
    pa_conf    = min(1.0, fg_pa / 400)            # 0→1 as PA grows to 400
    avg_blend  = avg * 0.80 + sv_xba * 0.20
    contact_raw = avg_blend / _STAT_DEFAULTS['avg']   # ratio vs neutral baseline
    contact_raw = 1.0 + (contact_raw - 1.0) * (0.5 + 0.5 * pa_conf)  # shrink toward 1
    contact_contrib = (contact_raw - 1.0) * W["contact"]

    # ── COMPONENT 2: Power ────────────────────────────────────────────────────
    # ISO edge, barrel edge, EV edge, xSLG edge — combine with weights
    iso_edge  = (fg_iso - 0.145) / 0.145              # delta vs league-avg ISO
    brl_edge  = (sv_brl - _LEAGUE_BRL_PCT) / _LEAGUE_BRL_PCT
    ev_edge   = (sv_ev  - _LEAGUE_EV) / _LEAGUE_EV
    pull_edge = (pull_air - 0.35) / 0.35
    xslg_edge = (sv_xslg - _STAT_DEFAULTS['slg']) / _STAT_DEFAULTS['slg']
    power_raw = iso_edge * 0.30 + brl_edge * 0.30 + ev_edge * 0.15 + xslg_edge * 0.15 + pull_edge * 0.10
    power_contrib = power_raw * W["power"]

    # ── COMPONENT 3: Discipline ───────────────────────────────────────────────
    # Positive: high BB%, low K%, high xwOBA
    bb_edge   = (fg_bbp - _LEAGUE_BB_PCT) / _LEAGUE_BB_PCT
    k_edge    = (_LEAGUE_K_PCT - fg_kp)   / _LEAGUE_K_PCT   # lower K → positive
    woba_edge = (sv_xwoba - _LEAGUE_WOBA) / _LEAGUE_WOBA
    disc_raw  = bb_edge * 0.30 + k_edge * 0.40 + woba_edge * 0.30
    disc_contrib = disc_raw * W["discipline"]

    # ── COMPONENT 4: Platoon ──────────────────────────────────────────────────
    bat = (batter.get('bats') or 'S').upper()
    pit = (pitcher_hand or 'R').upper()
    if bat == 'S':
        platoon_edge = 0.0
    elif (bat == 'L' and pit == 'R') or (bat == 'R' and pit == 'L'):
        platoon_edge = +0.05   # favorable platoon advantage
    else:
        platoon_edge = -0.05   # unfavorable
    platoon_contrib = platoon_edge * W["platoon"]

    # ── COMPONENT 5: Park ─────────────────────────────────────────────────────
    park_edge    = (park_factor - 1.0)
    park_contrib = park_edge * W["park"]

    # ── COMPONENT 6: Weather ──────────────────────────────────────────────────
    dome     = weather.get("dome", False)
    temp_f   = _safe_f(weather.get("temp"), 72) if not dome else 72
    wind_spd = _safe_f(weather.get("wind_speed"), 0) if not dome else 0
    wx_raw   = (temp_f - 72) * 0.003 + min(0.06, float(wind_spd) * 0.003)
    wx_mult  = 1.0 + wx_raw                   # hard multiplier used in final stats
    wx_edge  = wx_raw
    wx_contrib = wx_edge * W["weather"]

    # ── COMPONENT 7: Pitcher resistance ──────────────────────────────────────
    opp_era  = _safe_f(opp_pitcher_fg.get("fg_era")  or opp_pitcher_sv.get("sv_era_p"), 4.20)
    opp_xera = _safe_f(opp_pitcher_sv.get("sv_xera"), opp_era)
    bat_kpct = fg_kp

    recent_form = _pitcher_recent_form(opp_pitcher_id) if opp_pitcher_id else {}
    pit_mult, kpct_blended = pitcher_component_v2(opp_pitcher_fg, opp_pitcher_sv, recent_form)
    k_adj     = 1.0 - _clamp((kpct_blended - bat_kpct) * 0.5, -0.15, 0.15)
    # Platoon K modifier: disadvantage means more Ks
    if bat != 'S':
        plat_k = 0.97 if ((bat == 'L' and pit == 'R') or (bat == 'R' and pit == 'L')) else 1.03
    else:
        plat_k = 1.0
    k_adj *= plat_k

    pitcher_edge = (pit_mult * k_adj) - 1.0
    pitcher_contrib = pitcher_edge * W["pitcher"]

    # ── COMPONENT 8: Recent form (Phase 1) ───────────────────────────────────
    form_edge   = 0.0
    form_label  = "no data"
    if form and isinstance(form, dict):
        l7 = form.get("l7") or {}
        rw = l7.get("raw_woba") if l7 else None
        if rw is not None:
            form_edge  = _clamp((rw - _LEAGUE_WOBA) / _LEAGUE_WOBA, -0.25, 0.25)
            form_label = f"L7 wOBA {rw:.3f}"
    form_contrib = form_edge * W["form"]

    # ── COMPONENT 9: BvP (Phase 1) ───────────────────────────────────────────
    bvp_edge    = 0.0
    bvp_label   = "no data"
    if bvp and isinstance(bvp, dict) and bvp.get("success"):
        reliability = _safe_f(bvp.get("reliability"), 0.0)
        we          = _safe_f(bvp.get("woba_edge"),    0.0)
        bvp_edge    = _clamp(we * reliability, -0.20, 0.20)
        bvp_label   = f"woba_edge {we:.3f} × rel {reliability:.2f}"
    bvp_contrib  = bvp_edge * W["bvp"]

    # ── Composite multiplier ─────────────────────────────────────────────────
    # Sum contributions; each is a %-point delta weighted by its component weight.
    delta = (contact_contrib + power_contrib + disc_contrib + platoon_contrib
             + park_contrib + wx_contrib + pitcher_contrib
             + form_contrib + bvp_contrib)
    composite = _clamp(1.0 + delta, 0.55, 1.55)

    # ── Projections ──────────────────────────────────────────────────────────
    # Use wx_mult separately to preserve existing weather logic; pit_mult/k_adj 
    # are already baked into pitcher_contrib → composite.
    # Keep legacy structure: hits, tb, hr, rbi, r, hrr

    ops_ratio = ops / max(season_ops, 0.400)
    hr_r_adj  = hr_r * ops_ratio * _clamp(brl_edge + 1.0, 0.70, 1.40)

    hits_proj = round(max(0.05, avg_blend * exp_pa * composite * wx_mult), 3)
    tb_proj   = round(max(0.08, slg       * exp_pa * composite * wx_mult), 3)

    hr_pf   = _clamp(park_factor * 1.08, 0.80, 1.35)
    pull_park_boost = 1.0 + (max(0.0, pull_edge) * max(0.0, park_factor - 1.0) * 1.25)
    pull_park_boost = _clamp(pull_park_boost, 0.95, 1.25)
    hr_proj = round(max(0.005,
        hr_r_adj * exp_pa * hr_pf * (1.0 + (sv_brl - _LEAGUE_BRL_PCT) * 0.8) * pull_park_boost * wx_mult
    ), 4)

    slot_rbi_bonus = max(0.8, 1.0 + (4 - abs(slot - 4)) * 0.03)
    rbi_proj = round(max(0.05, rbi_r * exp_pa * composite * slot_rbi_bonus), 3)

    slot_r_bonus = max(0.8, 1.1 - abs(slot - 1.5) * 0.025)
    r_proj   = round(max(0.04, r_r * exp_pa * composite * slot_r_bonus), 3)

    hrr_proj = round(hits_proj + r_proj + rbi_proj, 3)

    return {
        "hits":        hits_proj,
        "hr":          hr_proj,
        "tb":          tb_proj,
        "rbi":         rbi_proj,
        "r":           r_proj,
        "hrr":         hrr_proj,
        "expected_pa": exp_pa,
        "split_avg":   round(avg_blend, 3),
        "split_ops":   round(ops, 3),
        "platoon_note": _batter_hand_note(batter, pitcher_hand),
        # BAT X diagnostics
        "composite":   round(composite, 4),
        "adjustments": {
            "contact":    round(contact_contrib,  4),
            "power":      round(power_contrib,     4),
            "pull_air":   round(pull_edge * 0.10 * W["power"], 4),
            "discipline": round(disc_contrib,      4),
            "platoon":    round(platoon_contrib,   4),
            "park":       round(park_contrib,      4),
            "weather":    round(wx_contrib,        4),
            "pitcher":    round(pitcher_contrib,   4),
            "form":       round(form_contrib,      4),
            "bvp":        round(bvp_contrib,       4),
            "form_note":  form_label,
            "bvp_note":   bvp_label,
        },
    }

def _pitcher_recent_form(pitcher_id, n_starts=5):
    """
    Fetch last n_starts game logs for a pitcher and return blended weighted stats.
    Caches per pitcher per calendar day.
    Returns dict with era_recent, k9_recent, whip_recent, n_starts_found.
    """
    if not pitcher_id:
        return {}
    cache_key = f"{pitcher_id}_{datetime.now(ET).strftime('%Y-%m-%d')}"
    if cache_key in _pitcher_recent_cache:
        return _pitcher_recent_cache[cache_key]
    try:
        yr = datetime.now().year
        r  = requests.get(
            f"{MLB_API}/people/{pitcher_id}/stats",
            params={"stats": "gameLog", "group": "pitching", "season": yr},
            timeout=8,
        )
        r.raise_for_status()
        _stats_list = r.json().get("stats") or []
        all_splits = _stats_list[0].get("splits", []) if _stats_list else []
        # Only real starts (IP >= 3.0)
        starts = [
            sp for sp in all_splits
            if _safe_f(sp.get("stat", {}).get("inningsPitched", "0"), 0) >= 3.0
        ][-n_starts:]
        if not starts:
            _pitcher_recent_cache[cache_key] = {}
            return {}
        total_ip = total_er = total_k = total_bb = total_h = 0.0
        for sp in starts:
            st  = sp.get("stat", {})
            ip_s = str(st.get("inningsPitched", "0.0"))
            try:
                whole, thirds = ip_s.split(".")
                ip = int(whole) + int(thirds) / 3
            except Exception:
                ip = _safe_f(ip_s, 0)
            total_ip   += ip
            total_er   += _safe_f(st.get("earnedRuns"),   0)
            total_k    += _safe_f(st.get("strikeOuts"),   0)
            total_bb   += _safe_f(st.get("baseOnBalls"),  0)
            total_h    += _safe_f(st.get("hits"),         0)

        if total_ip < 1:
            _pitcher_recent_cache[cache_key] = {}
            return {}

        era_recent  = round((total_er  / total_ip) * 9, 2)
        k9_recent   = round((total_k   / total_ip) * 9, 2)
        bb9_recent  = round((total_bb  / total_ip) * 9, 2)
        whip_recent = round((total_h + total_bb) / total_ip, 3)

        out = {
            "era_recent":   _clamp(era_recent,  1.5, 12.0),
            "k9_recent":    _clamp(k9_recent,   2.0, 16.0),
            "bb9_recent":   _clamp(bb9_recent,  0.5,  9.0),
            "whip_recent":  _clamp(whip_recent, 0.6,  2.4),
            "n_starts":     len(starts),
            "total_ip":     round(total_ip, 1),
            "total_er":     int(total_er),
            "total_k":      int(total_k),
        }
        _pitcher_recent_cache[cache_key] = out
        return out
    except Exception as ex:
        print(f"[pitcher_recent_form] pid={pitcher_id}: {ex}")
        _pitcher_recent_cache[cache_key] = {}
        return {}

def _project_pitcher(pitcher_name, pitcher_id, pitcher_fg, pitcher_sv, pitcher_stats,
                     opp_batters, park_factor, weather):
    fg   = pitcher_fg
    sv   = pitcher_sv

    # ── Season stats ──────────────────────────────────────────────────────────
    k9_season   = _safe_f(fg.get("fg_k9"),   8.5)
    bb9_season  = _safe_f(fg.get("fg_bb9"),  3.0)
    era_season  = _safe_f(fg.get("fg_era") or pitcher_stats.get("era"), 4.20)
    whip_season = _safe_f(fg.get("fg_whip") or pitcher_stats.get("whip"), 1.28)
    kpct        = _safe_f(fg.get("fg_kpct") or sv.get("sv_k_pct"), 0.22)

    # ── Recent form (last 3-5 starts) — 40% weight ────────────────────────────
    recent = _pitcher_recent_form(pitcher_id, n_starts=5)
    if recent:
        W_RECENT = 0.40;  W_SEASON = 0.60
        era  = W_SEASON * era_season  + W_RECENT * recent["era_recent"]
        k9   = W_SEASON * k9_season   + W_RECENT * recent["k9_recent"]
        bb9  = W_SEASON * bb9_season  + W_RECENT * recent["bb9_recent"]
        whip = W_SEASON * whip_season + W_RECENT * recent["whip_recent"]
    else:
        era  = era_season
        k9   = k9_season
        bb9  = bb9_season
        whip = whip_season

    # ── Form trend flag (for UI) ──────────────────────────────────────────────
    # "struggling" = recent ERA 1.5+ runs worse than season
    # "dealing"    = recent ERA 1.5+ runs better than season
    form_flag = "neutral"
    if recent:
        delta = recent["era_recent"] - era_season
        if delta >= 1.5:
            form_flag = "struggling"
        elif delta <= -1.5:
            form_flag = "dealing"

    # ── Expected IP (quality-start model) ─────────────────────────────────────
    base_ip = 5.3
    ip_adj  = 1.0 + (4.20 - era) * 0.10
    exp_ip  = round(min(8.0, max(3.5, base_ip + ip_adj)), 1)

    # ── Opponent quality adjustment ───────────────────────────────────────────
    opp_wobas, opp_kpcts = [], []
    for b in opp_batters[:9]:
        b_fg = fg_batter(b.get("name", ""))
        b_sv = sv_batter(b.get("name", ""))
        opp_wobas.append(_safe_f(b_fg.get("fg_woba") or b_sv.get("sv_xwoba"), 0.310))
        opp_kpcts.append(_safe_f(b_fg.get("fg_kpct") or b_sv.get("sv_k_pct"), 0.22))

    avg_opp_woba = sum(opp_wobas) / len(opp_wobas) if opp_wobas else 0.310
    avg_opp_kpct = sum(opp_kpcts) / len(opp_kpcts) if opp_kpcts else 0.22
    opp_quality  = _clamp(avg_opp_woba / 0.320, 0.85, 1.15)
    k_opp_adj    = 1.0 + (avg_opp_kpct - 0.22) * 0.4

    k_proj  = round(max(0.5, (k9 / 9) * exp_ip * k_opp_adj / opp_quality), 2)
    bb_proj = round(max(0.1, (bb9 / 9) * exp_ip * opp_quality), 2)

    if not weather.get("dome", False):
        temp_f = _safe_f(weather.get("temp"), 72)
        if temp_f > 88:
            k_proj = round(k_proj * 0.97, 2)

    return {
        "k":          k_proj,
        "bb":         bb_proj,
        "expected_ip": exp_ip,
        # Form metadata exposed to frontend
        "era_season":  round(era_season, 2),
        "era_recent":  round(recent.get("era_recent", era_season), 2) if recent else None,
        "era_blended": round(era, 2),
        "k9_recent":   round(recent.get("k9_recent",  k9_season),  2) if recent else None,
        "form_flag":   form_flag,
        "recent_starts": recent.get("n_starts", 0),
        "recent_ip":   recent.get("total_ip"),
        "recent_er":   recent.get("total_er"),
        "recent_k":    recent.get("total_k"),
    }

def _safe_f(val, default=0.0):
    try:
        v = float(val)
        return v if v == v else default   # NaN guard
    except (TypeError, ValueError):
        return default

def _matchup_score(batter, pitcher_fg, pitcher_sv, pitcher_hand='R'):
    """
    Returns a 0–100 matchup score broken into 4 sub-scores.
    Uses platoon-blended contact/power metrics when split data is available.
    """
    name = batter.get("name", "")
    fg   = fg_batter(name)
    sv   = sv_batter(name)

    # ── Contact (25 pts): platoon-blended avg + xBA ───────────────────────────
    avg  = platoon_blend_v2(batter, pitcher_hand, 'avg')
    xba  = _safe_f(sv.get("sv_xba"), avg)
    con  = round(min(25, max(0, ((avg + xba) / 2 - 0.180) / (0.340 - 0.180) * 25)), 1)

    # ── Power (25 pts): platoon-blended slg + xSLG + iso + barrel% ───────────
    slg  = platoon_blend_v2(batter, pitcher_hand, 'slg')
    xslg = _safe_f(sv.get("sv_xslg"), slg)
    iso  = _safe_f(fg.get("fg_iso"), 0.145)
    brl  = _safe_f(sv.get("sv_brl_pct"), 6.0) / 100
    pwr  = round(min(25, max(0,
        ((slg + xslg) / 2 - 0.290) / (0.600 - 0.290) * 22 + brl * 15 + iso * 10
    )), 1)

    # ── OBP (25 pts): platoon-blended obp + BB% ───────────────────────────────
    obp   = platoon_blend_v2(batter, pitcher_hand, 'obp')
    bbpct = _safe_f(fg.get("fg_bbpct"), 0.08)
    obp_s = round(min(25, max(0, (obp - 0.270) / (0.420 - 0.270) * 22 + bbpct * 10)), 1)

    # ── Statcast (25 pts): EV / HH% / xwOBA — pitcher penalty ────────────────
    ev   = _safe_f(sv.get("sv_ev"), 87.0)
    hh   = _safe_f(sv.get("sv_hh_pct") or sv.get("sv_hhpct"), 33.0) / 100
    xwob = _safe_f(sv.get("sv_xwoba") or fg.get("fg_woba"), 0.310)
    opp_kpct = _safe_f(pitcher_fg.get("fg_kpct"), 0.22)
    opp_xera = _safe_f(pitcher_sv.get("sv_xera"), 4.20)
    pit_pen  = max(0.0, (0.22 - opp_kpct) * 10 + (opp_xera - 4.20) * 1.5)
    stc  = round(min(25, max(0,
        (ev - 82) / (98 - 82) * 8 +
        hh * 12 +
        (xwob - 0.270) / (0.420 - 0.270) * 8 +
        pit_pen
    )), 1)

    # ── Platoon bonus / penalty (up to ±4 pts) ────────────────────────────────
    bat  = (batter.get('bats') or 'S').upper()
    pit  = (pitcher_hand or 'R').upper()
    if bat != 'S':
        platoon_bonus = 3.0 if ((bat == 'L' and pit == 'R') or (bat == 'R' and pit == 'L')) else -3.0
    else:
        platoon_bonus = 1.5  # switch hitter slight edge

    total = round(min(100, max(0, con + pwr + obp_s + stc + platoon_bonus)), 1)
    tier  = "A" if total >= 70 else "B" if total >= 55 else "C" if total >= 40 else "D"

    return {
        "score":        total,
        "tier":         tier,
        "contact":      con,
        "power":        round(pwr, 1),
        "obp":          obp_s,
        "statcast":     stc,
        "platoon_bonus": platoon_bonus,
        "platoon_note": _batter_hand_note(batter, pitcher_hand),
    }

def api_props_projections(game_pk):
    t0 = time.perf_counter()
    try:
        # Keep endpoint responsive during cold starts; refresh in background.
        _maybe_refresh_fg()
        _maybe_refresh_savant()
        _fetch_injury_status(force=False)

        date_hint = request.args.get('date')
        gdata, away_bats, home_bats, away_t, home_t, pitchers = _props_fetch_game(game_pk, date_hint=date_hint)
        if not gdata:
            return jsonify({"success": False, "error": "Game not found"}), 404

        away_abbr = away_t.get("team", {}).get("abbreviation", "AWAY")
        home_abbr = home_t.get("team", {}).get("abbreviation", "HOME")
        home_id   = home_t.get("team", {}).get("id")
        pf        = PARK_FACTORS.get(home_id, 1.0)

        away_full = away_t.get("team", {}).get("name", away_abbr)
        home_full = home_t.get("team", {}).get("name", home_abbr)

        event, _ = _find_odds_event(away_full, home_full)
        featured  = _load_event_odds(event.get("id") if event else None, featured_only=True) if event else []
        game_lines = {
            "awayFull":  away_full,
            "homeFull":  home_full,
            "moneyline": _best_moneyline(featured, away_full, home_full),
            "total":     _best_total(featured),
            "spread":    _best_spread(featured, away_full, home_full),
        }

        ap_info = pitchers["ap"]; hp_info = pitchers["hp"]
        ap_name = ap_info.get("fullName", "TBD")
        hp_name = hp_info.get("fullName", "TBD")
        ap_id   = ap_info.get("id");  hp_id  = hp_info.get("id")

        ap_fg = fg_pitcher(ap_name); hp_fg = fg_pitcher(hp_name)
        ap_sv = sv_pitcher(ap_name); hp_sv = sv_pitcher(hp_name)
        ap_st = pitcher_stats_mlb(ap_id) if ap_id else {}
        hp_st = pitcher_stats_mlb(hp_id) if hp_id else {}

        # ── Pitcher hands — the key new inputs ────────────────────────────────
        ap_hand = (ap_st.get("pitchHand") or "R").upper()
        hp_hand = (hp_st.get("pitchHand") or "R").upper()

        # Build league-wide Statcast distributions for percentile ranks.
        with _sv_lock:
            statcast_cache = dict(_sv_bat_statcast)
        ev_values = []
        hh_values = []
        brl_values = []
        pull_values = []
        for row in statcast_cache.values():
            try:
                ev_values.append(float(row.get('sv_ev')))
            except Exception:
                pass
            try:
                hh_values.append(float(row.get('sv_hh_pct')))
            except Exception:
                pass
            try:
                brl_values.append(float(row.get('sv_brl_pct')))
            except Exception:
                pass
            try:
                pull_values.append(float(row.get('pull_pct_air')))
            except Exception:
                pass

        # Weather
        ven   = gdata.get("venue", {})
        vid   = ven.get("id")
        vloc  = (ven.get("location") or {})
        coord = vloc.get("defaultCoordinates") or {}
        lat   = coord.get("latitude")
        lon   = coord.get("longitude")
        try:
            dt_utc = datetime.fromisoformat(gdata.get("gameDate", "").replace("Z", "+00:00"))
            ghour  = dt_utc.astimezone(ET).hour
        except Exception:
            ghour  = 13
        wx = get_weather(lat, lon, ghour, venue_id=vid)

        # ── Build batter projections (now passes pitcher_hand) ─────────────────
        away_confirmed = len(away_bats) >= 9 and all((b.get("lineup_status") or "confirmed") == "confirmed" for b in away_bats[:9])
        home_confirmed = len(home_bats) >= 9 and all((b.get("lineup_status") or "confirmed") == "confirmed" for b in home_bats[:9])

        def enrich_batters(batters, opp_pfg, opp_psv, opp_pst, opp_abbr, opp_pname, opp_pid, own_abbr='', lineup_confirmed=False):
            opp_hand = (opp_pst.get("pitchHand") or "R").upper()
            batters_top = list((batters or [])[:9])

            def _enrich_one(b):
                name = b.get("name", "")
                bfg  = fg_batter(name)
                bsv  = sv_batter(name)
                brl  = bsv.get("sv_brl_pct")
                hh   = bsv.get("sv_hh_pct")
                ev   = bsv.get("sv_ev")
                pull = bsv.get("pull_pct_air")
                bid  = b.get("id")
                form = _fetch_rolling_form(bid, False) if bid else None
                bvp  = _fetch_bvp(bid, opp_pid)      if (bid and opp_pid) else None
                pitch_adv = _pitch_type_advantage(bid, opp_pid, batter_name=name, pitcher_name=opp_pname) if (bid and opp_pid) else {"status": "neutral", "note": "Neutral matchup"}
                bvp_grade = _compute_bvp_grade(bvp) if bvp else 'D'
                injury = _get_player_injury(bid) if bid else None
                proj = _project_batter_batx(
                    b, opp_pname, opp_pfg, opp_psv, pf, wx,
                    pitcher_hand=opp_hand,
                    opp_pitcher_id=opp_pid,
                    form=form, bvp=bvp,
                )
                slot = int(b.get("slot") or 9)
                wx_adj = _safe_f((proj.get("adjustments") or {}).get("weather"), 0.0)
                wind_bucket = "out" if wx_adj > 0.01 else ("in" if wx_adj < -0.01 else "calm")
                park_bucket = "hitter" if pf >= 1.04 else ("pitcher" if pf <= 0.96 else "neutral")
                slot_bucket = "1-3" if slot <= 3 else ("4-6" if slot <= 6 else "7-9")
                return {
                    "name":         name,
                    "team":         own_abbr or b.get("team", ""),
                    "pos":          b.get("pos", ""),
                    "slot":         slot,
                    "id":           b.get("id"),
                    "bats":         b.get("bats", ""),
                    "opp_pitcher":  opp_pname,
                    "opp_hand":     opp_hand,
                    "opp_era":      opp_pfg.get("fg_era") or opp_psv.get("sv_era_p"),
                    "opp_k9":       opp_pfg.get("fg_k9"),
                    "avg":          b.get("avg") or bfg.get("fg_avg"),
                    "obp":          b.get("obp") or bfg.get("fg_obp"),
                    "slg":          b.get("slg") or bfg.get("fg_slg"),
                    "fg_woba":      bfg.get("fg_woba"),
                    "sv_xwoba":     bsv.get("sv_xwoba"),
                    "bvpGrade":     bvp_grade,
                    "bvpTooltip":   (bvp or {}).get("tooltip") if isinstance(bvp, dict) else "",
                    "bvpPA":        (bvp or {}).get("pa") if isinstance(bvp, dict) else 0,
                    "bvpOPS":       (bvp or {}).get("ops") if isinstance(bvp, dict) else None,
                    "pitchTypeAdvantage": (pitch_adv or {}).get("status", "neutral"),
                    "pitchTypeAdvantageNote": (pitch_adv or {}).get("note", "Neutral matchup"),
                    "pitchTypePrimary": (pitch_adv or {}).get("primary_pitch"),
                    "pitchTypePrimaryLabel": (pitch_adv or {}).get("primary_pitch_label"),
                    "pitchTypeUsagePct": (pitch_adv or {}).get("usage_pct"),
                    "sv_ev":        ev,
                    "sv_hh_pct":    hh,
                    "sv_brl_pct":   brl,
                    "pull_pct_air": pull,
                    "sv_ev_pct_rank": _pct_rank(ev_values, ev),
                    "sv_hh_pct_rank": _pct_rank(hh_values, hh),
                    "sv_brl_pct_rank": _pct_rank(brl_values, brl),
                    "pull_pct_air_rank": _pct_rank(pull_values, pull),
                    "lineupConfirmed": bool(lineup_confirmed),
                    "lineupStatus": "confirmed" if lineup_confirmed else "pending",
                    "injuryStatus": (injury or {}).get("status"),
                    "injuryType": (injury or {}).get("type"),
                    "injuryDescription": (injury or {}).get("description"),
                    "filterTags": {
                        "vsHand": opp_hand,
                        "homeAway": "home" if (own_abbr == home_abbr) else "away",
                        "windBucket": wind_bucket,
                        "parkBucket": park_bucket,
                        "slotBucket": slot_bucket,
                        "lineup": "confirmed" if lineup_confirmed else "pending",
                    },
                    # Expose platoon splits for UI
                    "vs_l_avg":     b.get("vs_l_avg"),
                    "vs_r_avg":     b.get("vs_r_avg"),
                    "vs_l_ops":     b.get("vs_l_ops"),
                    "vs_r_ops":     b.get("vs_r_ops"),
                    "proj":         proj,
                }

            workers = min(9, max(1, len(batters_top)))
            with ThreadPoolExecutor(max_workers=workers) as ex:
                return list(ex.map(_enrich_one, batters_top))

        away_proj = enrich_batters(away_bats, hp_fg, hp_sv, hp_st, home_abbr, hp_name, hp_id, own_abbr=away_abbr, lineup_confirmed=away_confirmed)
        home_proj = enrich_batters(home_bats, ap_fg, ap_sv, ap_st, away_abbr, ap_name, ap_id, own_abbr=home_abbr, lineup_confirmed=home_confirmed)
        all_batters = away_proj + home_proj

        injury_summary_rows = []
        for bb in all_batters:
            st = (bb.get("injuryStatus") or "").upper()
            if not st:
                continue
            injury_summary_rows.append({
                "name": bb.get("name"),
                "team": bb.get("team"),
                "status": st,
                "description": bb.get("injuryDescription") or bb.get("injuryType") or st,
            })

        # ── Pitcher projections ────────────────────────────────────────────────
        pitchers_out = []
        for pid, pname, pfg, psv, pst, opp_bats, pabbr, phand in [
            (ap_id, ap_name, ap_fg, ap_sv, ap_st, home_bats, away_abbr, ap_hand),
            (hp_id, hp_name, hp_fg, hp_sv, hp_st, away_bats, home_abbr, hp_hand),
        ]:
            if pname == "TBD":
                continue
            pinj = _get_player_injury(pid) if pid else None
            proj = _project_pitcher(pname, pid, pfg, psv, pst, opp_bats, pf, wx)
            pitchers_out.append({
                "name":      pname,
                "team":      pabbr,
                "id":        pid,
                "role":      "SP",
                "pitchHand": phand,
                "era":       pfg.get("fg_era") or psv.get("sv_era_p"),
                "whip":      pfg.get("fg_whip"),
                "fip":       pfg.get("fg_fip"),
                "xera":      psv.get("sv_xera"),
                "kpct":      pfg.get("fg_kpct") or psv.get("sv_k_pct"),
                "bbpct":     pfg.get("fg_bbpct") or psv.get("sv_bb_pct"),
                "opp_k_pct": sum(_safe_f(fg_batter(b.get("name","")).get("fg_kpct"), 0.22)
                                 for b in opp_bats[:9]) / max(len(opp_bats[:9]), 1),
                "opp_woba":  sum(_safe_f(fg_batter(b.get("name","")).get("fg_woba") or
                                         sv_batter(b.get("name","")).get("sv_xwoba"), 0.310)
                                 for b in opp_bats[:9]) / max(len(opp_bats[:9]), 1),
                "injuryStatus": (pinj or {}).get("status"),
                "injuryDescription": (pinj or {}).get("description"),
                "proj":      proj,
            })

        for pp in pitchers_out:
            st = (pp.get("injuryStatus") or "").upper()
            if not st:
                continue
            injury_summary_rows.append({
                "name": pp.get("name"),
                "team": pp.get("team"),
                "status": st,
                "description": pp.get("injuryDescription") or st,
            })


        # ── Attach Odds API prop lines/odds to each pitcher and batter ───────
        odds_props = {}
        if event:
            props_books = _load_event_odds(event.get('id'), featured_only=False) or []
            valid_names = set([x.get('name') for x in away_bats + home_bats if x.get('name')])
            ap = pitchers.get('ap', {}) if isinstance(pitchers, dict) else {}
            hp = pitchers.get('hp', {}) if isinstance(pitchers, dict) else {}
            if ap.get('fullName'): valid_names.add(ap.get('fullName'))
            if hp.get('fullName'): valid_names.add(hp.get('fullName'))
            market_props = _parse_prop_markets(props_books, valid_names)
            # Build lookup: (player, marketKey) -> list of lines/odds
            for prop in market_props:
                key = (prop.get('player'), prop.get('marketKey'))
                odds_props.setdefault(key, []).append({
                    'line': prop.get('line'),
                    'odds': prop.get('odds'),
                    'side': prop.get('side'),
                    'book': prop.get('book'),
                    'marketKey': prop.get('marketKey'),
                })

        # Attach to batters
        for b in all_batters:
            b['oddsMarkets'] = []
            for mk in ['batter_hits', 'batter_total_bases', 'batter_home_runs', 'batter_rbis']:
                odds = odds_props.get((b.get('name'), mk), [])
                if odds:
                    b['oddsMarkets'].extend(odds)

        # Attach to pitchers
        for p in pitchers_out:
            p['oddsMarkets'] = []
            for mk in ['pitcher_strikeouts', 'pitcher_outs_recorded', 'pitcher_earned_runs']:
                odds = odds_props.get((p.get('name'), mk), [])
                if odds:
                    p['oddsMarkets'].extend(odds)

        return jsonify({
            "success":     True,
            "gamePk":      game_pk,
            "matchup":     f"{away_abbr} @ {home_abbr}",
            "awayAbbr":    away_abbr,
            "homeAbbr":    home_abbr,
            "awayFull":    away_full,
            "homeFull":    home_full,
            "batters":     all_batters,
            "pitchers":    pitchers_out,
            "weather":     wx,
            "park_factor": pf,
            "lineup": {
                "awayConfirmed": away_confirmed,
                "homeConfirmed": home_confirmed,
                "overallConfirmed": bool(away_confirmed and home_confirmed),
            },
            "injury_summary": {
                "count": len(injury_summary_rows),
                "players": injury_summary_rows[:10],
            },
            "game_lines":  game_lines,
            "timestamp":   datetime.now(timezone.utc).isoformat(),
        })

    except Exception as ex:
        print(f"[api_props_projections] {traceback.format_exc()}")
        return jsonify({"success": False, "error": "Internal server error"}), 500
    finally:
        ms = int((time.perf_counter() - t0) * 1000)
        if ms >= 1000:
            print(f"[perf] /api/props/projections/{game_pk} {ms}ms")

def api_props_scan_today():
    date_str = request.args.get('date') or datetime.now(ET).strftime('%Y-%m-%d')
    refresh = str(request.args.get('refresh') or '').strip().lower() in ('1', 'true', 'yes')
    try:
        return jsonify(_props_scan_today_payload(date_str, refresh=refresh))
    except Exception as ex:
        print(f"[api_props_scan_today] {traceback.format_exc()}")
        return jsonify({'success': False, 'error': 'Internal server error'}), 500

def api_props_line_shopping(game_pk):
    """Return line-shopping view grouped by player/market with all sportsbook lines."""
    try:
        _maybe_refresh_fg()
        _maybe_refresh_savant()

        gdata, away_bats, home_bats, away_t, home_t, pitchers = _props_fetch_game(game_pk, date_hint=request.args.get('date'))
        if not gdata:
            return jsonify({"success": False, "error": "Game not found"}), 404

        away_team_name = (away_t.get('team', {}) or {}).get('name', '')
        home_team_name = (home_t.get('team', {}) or {}).get('name', '')

        event, _ = _find_odds_event(away_team_name, home_team_name)
        if not event:
            return jsonify({
                "success": True,
                "gamePk": game_pk,
                "eventId": None,
                "groups": [],
                "players": {},
                "count": 0,
            })

        props_books = _load_event_odds(event.get('id'), featured_only=False) or []

        valid_names = set([x.get('name') for x in (away_bats + home_bats) if x.get('name')])
        ap = (pitchers or {}).get('ap', {}) or {}
        hp = (pitchers or {}).get('hp', {}) or {}
        if ap.get('fullName'):
            valid_names.add(ap.get('fullName'))
        if hp.get('fullName'):
            valid_names.add(hp.get('fullName'))

        market_props = _parse_prop_markets(props_books, valid_names)
        grouped = _group_line_shopping(market_props)

        return jsonify({
            "success": True,
            "gamePk": game_pk,
            "eventId": event.get('id'),
            "groups": grouped.get('groups', []),
            "players": grouped.get('by_player', {}),
            "count": len(grouped.get('groups', [])),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as ex:
        print(f"[api_props_line_shopping] {traceback.format_exc()}")
        return jsonify({"success": False, "error": "Internal server error"}), 500

def api_batting_order_matchups(game_pk):
    """Batting-order matchup rows for deep-dive tab, including pitch-type edge."""
    _maybe_refresh_fg()
    _maybe_refresh_savant()
    try:
        gdata, away_bats, home_bats, away_t, home_t, pitchers = _props_fetch_game(game_pk)
        if not gdata:
            return jsonify({"success": False, "error": "Game not found"}), 404

        ap_info = pitchers.get("ap", {})
        hp_info = pitchers.get("hp", {})
        ap_name = ap_info.get("fullName", "TBD")
        hp_name = hp_info.get("fullName", "TBD")
        ap_hand = (ap_info.get("pitchHand") or "R").upper()
        hp_hand = (hp_info.get("pitchHand") or "R").upper()
        ap_id = ap_info.get("id")
        hp_id = hp_info.get("id")

        away_abbr = away_t.get("team", {}).get("abbreviation", "AWAY")
        home_abbr = home_t.get("team", {}).get("abbreviation", "HOME")
        away_tid = away_t.get("team", {}).get("id")
        home_tid = home_t.get("team", {}).get("id")

        try:
            _, ranks_by_id = _get_team_pitching_rankings(force=False)
        except Exception:
            ranks_by_id = {}
        away_staff = ranks_by_id.get(away_tid) or {}
        home_staff = ranks_by_id.get(home_tid) or {}

        def _row(b, opp_pid, opp_name, opp_hand):
            nm = b.get("name", "")
            fgb = fg_batter(nm)
            svb = sv_batter(nm)
            split_avg = b.get("vs_l_avg") if opp_hand == "L" else b.get("vs_r_avg")
            pitch_adv = _pitch_type_advantage(b.get("id"), opp_pid, batter_name=nm, pitcher_name=opp_name) if (b.get("id") and opp_pid) else {"status": "neutral", "note": "Neutral matchup"}
            return {
                "slot": b.get("slot"),
                "name": nm,
                "pos": b.get("pos"),
                "avg": b.get("avg") or fgb.get("fg_avg") or "---",
                "slg": b.get("slg") or fgb.get("fg_slg") or "---",
                "iso": fgb.get("fg_iso") or "---",
                "ops": b.get("ops") or fgb.get("fg_ops") or "---",
                "xwoba": svb.get("sv_xwoba") or "---",
                "ev": svb.get("sv_ev") or "---",
                "brl_pct": svb.get("sv_brl_pct") or "---",
                "split_avg": split_avg if split_avg not in (None, "N/A", "") else 0,
                "pitch_type_advantage": (pitch_adv or {}).get("status", "neutral"),
                "pitch_type_note": (pitch_adv or {}).get("note", "Neutral matchup"),
            }

        away_rows = [_row(b, hp_id, hp_name, hp_hand) for b in away_bats[:9]]
        home_rows = [_row(b, ap_id, ap_name, ap_hand) for b in home_bats[:9]]

        return jsonify({
            "success": True,
            "gamePk": game_pk,
            "awayAbbr": away_abbr,
            "homeAbbr": home_abbr,
            "awayStaffBadge": _rank_staff_badge(away_staff.get('composite_rank')),
            "homeStaffBadge": _rank_staff_badge(home_staff.get('composite_rank')),
            "awayPitcherName": ap_name,
            "awayPitcherHand": ap_hand,
            "homePitcherName": hp_name,
            "homePitcherHand": hp_hand,
            "awayBatters": away_rows,
            "homeBatters": home_rows,
        })
    except Exception as ex:
        print(f"[api_batting_order_matchups] {traceback.format_exc()}")
        return jsonify({"success": False, "error": "Internal server error"}), 500

def api_props_matchup_scores(game_pk):
    try:
        _maybe_refresh_fg()
        _maybe_refresh_savant()

        gdata, away_bats, home_bats, away_t, home_t, pitchers = _props_fetch_game(game_pk, date_hint=request.args.get('date'))
        if not gdata:
            return jsonify({"success": False, "error": "Game not found"}), 404

        away_abbr = away_t.get("team", {}).get("abbreviation", "AWAY")
        home_abbr = home_t.get("team", {}).get("abbreviation", "HOME")

        ap_info = pitchers["ap"]; hp_info = pitchers["hp"]
        ap_name = ap_info.get("fullName", "TBD")
        hp_name = hp_info.get("fullName", "TBD")
        ap_id   = ap_info.get("id");  hp_id  = hp_info.get("id")

        ap_fg = fg_pitcher(ap_name); ap_sv = sv_pitcher(ap_name)
        hp_fg = fg_pitcher(hp_name); hp_sv = sv_pitcher(hp_name)
        ap_st = pitcher_stats_mlb(ap_id) if ap_id else {}
        hp_st = pitcher_stats_mlb(hp_id) if hp_id else {}

        ap_hand = (ap_st.get("pitchHand") or "R").upper()
        hp_hand = (hp_st.get("pitchHand") or "R").upper()

        def score_lineup(batters, opp_pfg, opp_psv, opp_hand):
            out = []
            for b in batters[:9]:
                sc = _matchup_score(b, opp_pfg, opp_psv, pitcher_hand=opp_hand)
                out.append({
                    "name":  b.get("name", ""),
                    "pos":   b.get("pos", ""),
                    "slot":  b.get("slot", 0),
                    "bats":  b.get("bats", ""),
                    "score": sc,
                })
            return sorted(out, key=lambda x: x["slot"])

        # home batters face away pitcher (ap_hand), away batters face home pitcher (hp_hand)
        away_scores = score_lineup(away_bats, hp_fg, hp_sv, hp_hand)
        home_scores = score_lineup(home_bats, ap_fg, ap_sv, ap_hand)

        return jsonify({
            "success": True,
            "gamePk":  game_pk,
            "away": {
                "abbr":         away_abbr,
                "pitcher_name": hp_name,
                "pitcher_hand": hp_hand,
                "pitcher_era":  hp_fg.get("fg_era") or hp_sv.get("sv_era_p"),
                "batters":      home_scores,
            },
            "home": {
                "abbr":         home_abbr,
                "pitcher_name": ap_name,
                "pitcher_hand": ap_hand,
                "pitcher_era":  ap_fg.get("fg_era") or ap_sv.get("sv_era_p"),
                "batters":      away_scores,
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    except Exception as ex:
        print(f"[api_props_matchup_scores] {traceback.format_exc()}")
        return jsonify({"success": False, "error": "Internal server error"}), 500

def _compute_dashboard_quick_props(game_pk, limit=3, date_hint=None):
    """Compute top quick-prop picks for a game card strip."""
    gdata, away_bats, home_bats, away_t, home_t, _pitchers = _props_fetch_game(game_pk, date_hint=date_hint)
    if not gdata:
        return None, []

    away_abbr = away_t.get("team", {}).get("abbreviation", "AWAY")
    home_abbr = home_t.get("team", {}).get("abbreviation", "HOME")

    tagged = [(b, away_abbr) for b in away_bats[:6]] + [(b, home_abbr) for b in home_bats[:6]]

    market_config = [
        ("hits", "hits", "Hits", [("0.5", "batter_hits"), ("1.5", "batter_hits")]),
        ("hr", "hr", "Home Runs", [("0.5", "batter_home_runs")]),
        ("tb", "tb", "Total Bases", [("1.5", "batter_total_bases"), ("2.5", "batter_total_bases")]),
        ("rbi", "rbi", "RBIs", [("0.5", "batter_rbis"), ("1.5", "batter_rbis")]),
    ]

    def _score_batter(batter, team):
        pid = batter.get("id")
        name = batter.get("name", "")
        if not pid:
            return []
        trends = _build_player_trends(int(pid), False)
        rates = trends.get("over_rates", {})
        best = {}

        for market, mkey, mlabel, line_pairs in market_config:
            for line_str, market_key in line_pairs:
                l10 = rates.get(mkey, {}).get(line_str, {}).get("l10", {})
                pct = l10.get("pct")
                tot = l10.get("total", 0)
                if pct is None or tot < 5 or pct < 0.60:
                    continue
                if market not in best or pct > best[market]["l10_pct"]:
                    edge = pct - 0.5
                    hub = _hub_rating(pct, edge, pct)
                    best[market] = {
                        "player": name,
                        "playerId": pid,
                        "team": team,
                        "market": mkey,
                        "marketKey": market_key,
                        "marketLabel": mlabel,
                        "line": float(line_str),
                        "l10_pct": round(pct, 3),
                        "l10_total": tot,
                        "hubRating": hub,
                        "evPct": round(edge * 100, 1),
                    }

        return list(best.values())

    all_picks = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(_score_batter, b, t): (b, t) for b, t in tagged}
        for fut in as_completed(futs):
            try:
                all_picks.extend(fut.result())
            except Exception:
                pass

    all_picks.sort(key=lambda x: x.get("l10_pct", 0), reverse=True)
    return gdata, all_picks[:limit]

def api_props_trends(game_pk):
    """
    Returns L5/L10 over rates for every batter and both starting pitchers in a game.
    Uses concurrent fetching to keep response time under ~4s.
    """
    try:
        date_hint = request.args.get('date')
        # Get lineups + pitchers
        gdata, away_bats, home_bats, away_t, home_t, pitchers = _props_fetch_game(game_pk, date_hint=date_hint)
        if not gdata:
            return jsonify({"success": False, "error": "Game not found"}), 404

        all_batters  = away_bats + home_bats
        ap_info      = pitchers["ap"]
        hp_info      = pitchers["hp"]

        # Build task list: (player_id, is_pitcher, name)
        tasks = []
        for b in all_batters:
            pid = b.get("id")
            if pid:
                tasks.append((int(pid), False, b.get("name", "")))
        for pi in [ap_info, hp_info]:
            pid = pi.get("id")
            if pid:
                tasks.append((int(pid), True, pi.get("fullName", "")))

        results = {}
        with ThreadPoolExecutor(max_workers=10) as ex:
            fut_map = {
                ex.submit(_build_player_trends, pid, is_pit): (pid, name)
                for pid, is_pit, name in tasks
            }
            for fut in as_completed(fut_map):
                pid, name = fut_map[fut]
                try:
                    data = fut.result()
                    results[str(pid)] = {"name": name, **data}
                except Exception as fe:
                    results[str(pid)] = {"name": name, "error": str(fe)}
        _gdata2, quick_props = _compute_dashboard_quick_props(game_pk, limit=3, date_hint=date_hint)

        return jsonify({
            "success":  True,
            "gamePk":   game_pk,
            "players":  results,
            "quickProps": quick_props,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    except Exception as ex:
        print(f"[api_props_trends] {traceback.format_exc()}")
        return jsonify({"success": False, "error": "Internal server error"}), 500

def api_props_quick(game_pk):
    """
    Returns top 3 batter prop edges for a game card inline strip.
    Uses L10 over rates — lightweight, no Odds API calls needed.
    """
    try:
        gdata, picks = _compute_dashboard_quick_props(game_pk, limit=3, date_hint=request.args.get('date'))
        if not gdata:
            return jsonify({"success": False, "error": "Game not found"}), 404
        return jsonify({
            "success": True,
            "gamePk":  game_pk,
            "picks":   picks,
        })

    except Exception as ex:
        print(f"[api_props_quick] {traceback.format_exc()}")
        return jsonify({"success": False, "error": "Internal server error"}), 500

def register_props_routes(app):
    app.add_url_rule('/api/projections/monte-carlo', view_func=api_projections_monte_carlo)
    app.add_url_rule('/api/props/projections/<int:game_pk>', view_func=api_props_projections)
    app.add_url_rule('/api/props/scan/today', view_func=api_props_scan_today)
    app.add_url_rule('/api/props/line-shopping/<int:game_pk>', view_func=api_props_line_shopping)
    app.add_url_rule('/api/batting-order-matchups/<int:game_pk>', view_func=api_batting_order_matchups)
    app.add_url_rule('/api/props/matchup-scores/<int:game_pk>', view_func=api_props_matchup_scores)
    app.add_url_rule('/api/props/trends/<int:game_pk>', view_func=api_props_trends)
    app.add_url_rule('/api/props/quick/<int:game_pk>', view_func=api_props_quick)


__all__ = ['configure_props_context', 'register_props_routes', '_STAT_DEFAULTS', 'BATX_WEIGHTS', '_LEAGUE_WOBA', '_LEAGUE_BB_PCT', '_LEAGUE_K_PCT', '_LEAGUE_EV', '_LEAGUE_BRL_PCT', '_empty_props_scan_payload', '_compute_props_scan_today_payload', '_trigger_props_scan_refresh_async', '_props_scan_today_payload', 'api_projections_monte_carlo', '_props_fetch_game', '_platoon_blend', '_project_batter_batx', '_pitcher_recent_form', '_project_pitcher', '_safe_f', '_matchup_score', 'api_props_projections', 'api_props_scan_today', 'api_props_line_shopping', 'api_batting_order_matchups', 'api_props_matchup_scores', '_compute_dashboard_quick_props', 'api_props_trends', 'api_props_quick', '_batter_hand_note', '_project_batter']
