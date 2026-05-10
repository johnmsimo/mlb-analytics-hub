"""Tracker persistence, grading, and analytics route handlers."""

import csv as csvmod
import io
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import requests
from flask import Response, jsonify, request


def configure_tracker_context(namespace):
    globals().update(namespace)


def _append_calibration_history(event_type, adjustments, meta=None):
    meta = meta or {}
    hist = _load_json(CAL_HISTORY_STORE, [])
    hist.append({
        'timestamp': datetime.now().isoformat(),
        'eventType': event_type,
        'date': meta.get('date'),
        'window': meta.get('window'),
        'applied': meta.get('applied', []),
        'note': meta.get('note'),
        'adjustments': adjustments,
    })
    _save_json(CAL_HISTORY_STORE, hist[-800:])

def _history_in_window(end_date_str, window_days):
    dates = set(_dates_in_window(end_date_str, window_days))
    hist = _load_json(CAL_HISTORY_STORE, [])
    out = []
    for row in hist:
        if not isinstance(row, dict):
            continue
        ts = (row.get('timestamp') or '')[:10]
        if ts in dates:
            out.append(row)
    return out

def _daily_series(end_date_str, window_days, market_key=None):
    store = _tracker_store()
    dates = list(reversed(_dates_in_window(end_date_str, window_days)))
    series = []
    for ds in dates:
        day = _normalize_tracker_day(store.get(ds))
        rows = list(day.get('entries', []) or [])
        if market_key:
            rows = [r for r in rows if r.get('marketKey') == market_key]
        active = [r for r in rows if r.get('grade') in ('win', 'loss')]
        wins = sum(1 for r in active if r.get('grade') == 'win')
        losses = sum(1 for r in active if r.get('grade') == 'loss')
        hit_rate = round(wins / max(1, wins + losses), 4) if active else None
        avg_edge = round(sum(float(r.get('edge') or 0) for r in active) / max(1, len(active)), 4) if active else None
        series.append({
            'date': ds, 'graded': len(active), 'wins': wins, 'losses': losses,
            'hit_rate': hit_rate, 'avg_edge': avg_edge
        })
    return series

def _multiplier_history(end_date_str, window_days, market_key):
    hist = _history_in_window(end_date_str, window_days)
    points = []
    for row in hist:
        adj = row.get('adjustments', {}) or {}
        mult = ((adj.get('market_multipliers') or {}).get(market_key))
        if mult is None:
            continue
        points.append({
            'timestamp': row.get('timestamp'),
            'multiplier': float(mult),
            'eventType': row.get('eventType'),
            'note': row.get('note'),
            'applied': row.get('applied', []),
        })
    return points

def _default_adjustments():
    return {
        'captured_per_game': 14,
        'best_edge_threshold': 0.03,
        'best_prob_threshold': 0.58,
        'bankroll': 1000.0,
        'kelly_fraction': 0.50,
        'unit_size_pct': 0.01,
        'max_bet_pct': 0.03,
        'max_daily_risk_pct': 0.12,
        'max_team_exposure_pct': 0.05,
        'max_market_exposure_pct': 0.05,
        'max_game_exposure_pct': 0.08,
        'market_multipliers': {
            'batter_hits': 1.00,
            'batter_total_bases': 1.00,
            'batter_home_runs': 1.00,
            'batter_rbis': 1.00,
            'batter_runs_scored': 1.00,
            'batter_hits_runs_rbis': 1.00,
            'batter_stolen_bases': 1.00,
            'pitcher_strikeouts': 1.00,
            'nrfi': 1.00,
            'yrfi': 1.00,
        }
    }

def _get_adjustments():
    obj = _load_json(ADJUST_STORE, _default_adjustments())
    d = _default_adjustments()
    d.update({k: v for k, v in obj.items() if k != 'market_multipliers'})
    d['market_multipliers'].update(obj.get('market_multipliers', {}))
    return d

def _market_mult(market_key, adjustments):
    return float((adjustments or {}).get('market_multipliers', {}).get(market_key, 1.0) or 1.0)

def _clamp01(v):
    return _clamp(v, 0.01, 0.99)

def _tracker_stat_from_boxscore(player_obj, market_key):
    if not player_obj:
        return None
    if market_key == 'pitcher_strikeouts':
        p = player_obj.get('stats', {}).get('pitching', {})
        return int(p.get('strikeOuts', 0) or 0)
    b = player_obj.get('stats', {}).get('batting', {})
    hits = int(b.get('hits', 0) or 0)
    doubles = int(b.get('doubles', 0) or 0)
    triples = int(b.get('triples', 0) or 0)
    hr = int(b.get('homeRuns', 0) or 0)
    singles = max(0, hits - doubles - triples - hr)
    mapping = {
        'batter_hits': hits,
        'batter_total_bases': singles + 2 * doubles + 3 * triples + 4 * hr,
        'batter_home_runs': hr,
        'batter_rbis': int(b.get('rbi', 0) or 0),
        'batter_runs_scored': int(b.get('runs', 0) or 0),
        'batter_hits_runs_rbis': hits + int(b.get('runs', 0) or 0) + int(b.get('rbi', 0) or 0),
        'batter_stolen_bases': int(b.get('stolenBases', 0) or 0),
    }
    return mapping.get(market_key)

def _grade_over(actual, line):
    if actual is None:
        return 'pending'
    if float(actual) > float(line):
        return 'win'
    if float(actual) < float(line):
        return 'loss'
    return 'push'

def _grade_side(actual, line, side='Over'):
    if actual is None:
        return 'pending'
    side_l = str(side or 'Over').lower()
    if side_l == 'under':
        if float(actual) < float(line):
            return 'win'
        if float(actual) > float(line):
            return 'loss'
        return 'push'
    return _grade_over(actual, line)

def _hub_rating(adj_prob, edge, l10_over_rate=0.5):
    trend_bonus = (l10_over_rate - 0.5) * 20
    edge_bonus  = min((edge or 0) * 100, 20)
    prob_base   = (adj_prob or 0) * 60
    raw = prob_base + edge_bonus + trend_bonus
    return max(0, min(100, round(raw)))

def _projection_reason_short(player, market_key, adj_prob, edge, opp_name=''):
    lbl = market_key.replace('batter_', '').replace('pitcher_', '').replace('_', ' ')
    if edge is not None:
        return f"{player} rates well for {lbl}; model {adj_prob:.1%} with edge {edge:.1%} versus market."
    if opp_name:
        return f"{player} rates well for {lbl}; model {adj_prob:.1%} against {opp_name}."
    return f"{player} rates well for {lbl}; model probability {adj_prob:.1%}."

def _build_tracker_rows_for_game(game_pk, capture_date, adjustments=None, _sched=None, include_odds=False):
    adjustments = adjustments or _get_adjustments()
    raw = _sched if _sched is not None else fetch_schedule(capture_date)
    g = next((x for x in raw if x.get('gamePk') == game_pk), None)
    if not g:
        return []

    away_team = g.get('teams', {}).get('away', {}).get('team', {})
    home_team = g.get('teams', {}).get('home', {}).get('team', {})
    away_team_id = away_team.get('id')
    home_team_id = home_team.get('id')
    away_abbr = away_team.get('abbreviation', 'AWAY')
    home_abbr = home_team.get('abbreviation', 'HOME')

    # ── Try boxscore lineups first (works once game has started) ─────────────
    away_lineup, home_lineup = [], []
    try:
        box = requests.get(f"{MLB_API}/game/{game_pk}/boxscore", timeout=10).json().get('teams', {})
        away_lineup = get_batters_from_boxscore(box.get('away', {}), 'away')
        home_lineup = get_batters_from_boxscore(box.get('home', {}), 'home')
    except Exception as ex:
        print(f'[tracker_rows] boxscore fetch failed for {game_pk}: {ex}')

    # ── Pre-game fallback: scheduled lineups from already hydrated schedule ──
    if not away_lineup or not home_lineup:
        try:
            lineups = (g.get('lineups') or {})

            def _parse_scheduled_lineup(hitters):
                out = []
                for i, p in enumerate(hitters or [], start=1):
                    name = (p.get('fullName') or p.get('name') or '').strip()
                    pid  = p.get('id') or p.get('playerId')
                    pos  = (p.get('primaryPosition') or {}).get('abbreviation', '?')
                    if not name:
                        continue
                    fgb = fg_batter(name); svb = sv_batter(name)
                    out.append({'slot': i, 'id': pid, 'name': name, 'pos': pos, 'bats': 'S',
                                'fg_pa': fgb.get('fg_pa','N/A'), 'fg_r': fgb.get('fg_r','N/A'),
                                'fg_sb': fgb.get('fg_sb','N/A'), 'fg_woba': fgb.get('fg_woba','N/A'),
                                'fg_wrc': fgb.get('fg_wrc','N/A'), 'fg_war': fgb.get('fg_war','N/A'),
                                'sv_xba': svb.get('sv_xba','N/A'), 'sv_xslg': svb.get('sv_xslg','N/A'),
                                'sv_xwoba': svb.get('sv_xwoba','N/A'), 'sv_ev': svb.get('sv_ev','N/A'),
                                'sv_hh_pct': svb.get('sv_hh_pct','N/A'), 'sv_brl_pct': svb.get('sv_brl_pct','N/A'),
                                'sv_la': svb.get('sv_la','N/A'),
                                'avg': fgb.get('fg_avg','.---'), 'obp': fgb.get('fg_obp','.---'),
                                'slg': fgb.get('fg_slg','.---'), 'ops': fgb.get('fg_ops','.---'),
                                'ab': 0, 'hits': 0, 'hr': 0, 'rbi': 0})
                return out

            if not away_lineup:
                away_lineup = _parse_scheduled_lineup(lineups.get('awayBatters', []))
            if not home_lineup:
                home_lineup = _parse_scheduled_lineup(lineups.get('homeBatters', []))
        except Exception as ex:
            print(f'[tracker_rows] scheduled lineup parse failed for {game_pk}: {ex}')

    # ── Last resort: active roster (top 9 position players) ─────────────────
    def _roster_lineup(team_id):
        try:
            out = []
            for entry in _get_active_roster(team_id):
                pos = ((entry.get('position') or {}).get('abbreviation') or '?')
                if pos in ('P', 'SP', 'RP', 'CP'):
                    continue
                name = ((entry.get('person') or {}).get('fullName') or '').strip()
                pid  = ((entry.get('person') or {}).get('id'))
                if not name:
                    continue
                fgb = fg_batter(name); svb = sv_batter(name)
                out.append({'slot': len(out)+1, 'id': pid, 'name': name, 'pos': pos, 'bats': 'S',
                            'fg_pa': fgb.get('fg_pa','N/A'), 'fg_r': fgb.get('fg_r','N/A'),
                            'fg_sb': fgb.get('fg_sb','N/A'), 'fg_woba': fgb.get('fg_woba','N/A'),
                            'fg_wrc': fgb.get('fg_wrc','N/A'), 'fg_war': fgb.get('fg_war','N/A'),
                            'sv_xba': svb.get('sv_xba','N/A'), 'sv_xslg': svb.get('sv_xslg','N/A'),
                            'sv_xwoba': svb.get('sv_xwoba','N/A'), 'sv_ev': svb.get('sv_ev','N/A'),
                            'sv_hh_pct': svb.get('sv_hh_pct','N/A'), 'sv_brl_pct': svb.get('sv_brl_pct','N/A'),
                            'sv_la': svb.get('sv_la','N/A'),
                            'avg': fgb.get('fg_avg','.---'), 'obp': fgb.get('fg_obp','.---'),
                            'slg': fgb.get('fg_slg','.---'), 'ops': fgb.get('fg_ops','.---'),
                            'ab': 0, 'hits': 0, 'hr': 0, 'rbi': 0})
                if len(out) >= 9:
                    break
            return out
        except Exception:
            return []

    if not away_lineup:
        away_lineup = _roster_lineup(away_team_id)
    if not home_lineup:
        home_lineup = _roster_lineup(home_team_id)

    if not away_lineup or not home_lineup:
        return []

    park = PARK_FACTORS.get(home_team_id, 1.0)

    away_p = g.get('teams', {}).get('away', {}).get('probablePitcher', {})
    home_p = g.get('teams', {}).get('home', {}).get('probablePitcher', {})
    away_pitcher = _pitcher_model(away_p.get('fullName', 'Away SP'), away_p.get('id'), away_team_id)
    home_pitcher = _pitcher_model(home_p.get('fullName', 'Home SP'), home_p.get('id'), home_team_id)

    sims = int(os.getenv('TRACKER_SIMS', '700') or 700)
    sims = max(300, min(5000, sims))
    rng = random.Random(game_pk + int(capture_date.replace('-', '')) + 10)
    away_store = {i: [] for i in range(len(away_lineup))}
    home_store = {i: [] for i in range(len(home_lineup))}
    away_starter_lines, home_starter_lines = [], []

    for _ in range(sims):
        away_off = _simulate_offense(away_lineup, home_pitcher, home_team_id, park, rng)
        home_off = _simulate_offense(home_lineup, away_pitcher, away_team_id, park, rng)
        for i, line in enumerate(away_off['batters']): away_store[i].append(line)
        for i, line in enumerate(home_off['batters']): home_store[i].append(line)
        home_starter_lines.append(away_off['starter'])
        away_starter_lines.append(home_off['starter'])

    away_props, home_props = [], []
    for i, b in enumerate(away_lineup):
        s = _summarize_player(away_store[i]); s.update({'id': b.get('id'), 'name': b.get('name'), 'slot': b.get('slot'), 'pos': b.get('pos'), 'bats': b.get('bats', 'S')}); away_props.append(s)
    for i, b in enumerate(home_lineup):
        s = _summarize_player(home_store[i]); s.update({'id': b.get('id'), 'name': b.get('name'), 'slot': b.get('slot'), 'pos': b.get('pos'), 'bats': b.get('bats', 'S')}); home_props.append(s)

    away_sp = _summarize_pitcher(away_starter_lines); away_sp.update({'name': away_pitcher['name'], 'id': away_pitcher.get('id'), 'pitchHand': away_pitcher['pitchHand']})
    home_sp = _summarize_pitcher(home_starter_lines); home_sp.update({'name': home_pitcher['name'], 'id': home_pitcher.get('id'), 'pitchHand': home_pitcher['pitchHand']})

    market_props = []
    if include_odds:
        event, _ = _find_odds_event(away_team.get('name', ''), home_team.get('name', ''))
        props_books = _load_event_odds(event.get('id') if event else None, featured_only=False) if event else []
        valid_names = set([x.get('name') for x in away_lineup + home_lineup if x.get('name')])
        if away_pitcher.get('name'): valid_names.add(away_pitcher.get('name'))
        if home_pitcher.get('name'): valid_names.add(home_pitcher.get('name'))
        market_props = _parse_prop_markets(props_books, valid_names)

    def find_market(player, mk, line):
        for item in market_props:
            if item.get('player') == player and item.get('market_key') == mk and float(item.get('line')) == float(line):
                return item
        return None

    rows = []

    def process_hitters(arr, team_abbr, opp_name):
        for p in arr:
            player_name = p.get('name')
            for mk, mean_field in _BATTER_MEAN_FIELD_FOR_MK.items():
                # Use actual market lines from Odds API; fall back to single default
                mkt_lines = _market_lines_for_player(market_props, player_name, mk)
                lines_to_use = mkt_lines if mkt_lines else [_BATTER_FALLBACK_LINE[mk]]
                for line in lines_to_use:
                    prob_field = _BATTER_PROB_FIELD_FOR.get((mk, line))
                    if prob_field:
                        raw_prob = float(p.get(prob_field, 0) or 0)
                    else:
                        raw_prob = _poisson_over_prob(float(p.get(mean_field, 0) or 0), line)
                    if raw_prob < 0.10:
                        continue
                    raw_mult_prob = _clamp01(raw_prob * _market_mult(mk, adjustments))
                    market = find_market(player_name, mk, line)
                    msum = _market_price_summary(market_props, player_name, mk, line)
                    market_implied = msum.get('market_implied')
                    if market_implied and market_implied > 0:
                        over_imp = market_implied
                        under_imp = _american_to_implied(msum.get('best_under_price')) or (1 - over_imp)
                        adj_prob = logit_blend_prob(raw_mult_prob, over_imp, mk, over_imp, under_imp)
                    else:
                        adj_prob = raw_mult_prob
                    edge = (adj_prob - market_implied) if market_implied is not None else None
                    score = (edge * 100.0 if edge is not None else 0) + adj_prob
                    hub = _hub_rating(adj_prob, edge or 0)
                    mi = market_implied
                    ev_pct = round(adj_prob / mi - 1, 4) if mi and mi > 0 else None
                    temp_row = {
                        'date': capture_date, 'gamePk': game_pk, 'team': team_abbr, 'player': p.get('name'), 'playerId': p.get('id'), 'slot': p.get('slot'), 'marketKey': mk, 'line': line, 'recommendedSide': 'Over',
                        'rawProb': round(raw_prob, 4), 'rawMultProb': round(raw_mult_prob, 4), 'adjProb': round(adj_prob, 4), 'modelMean': round(float(p.get(mean_field, 0) or 0), 3), 'edge': round(edge, 4) if edge is not None else None,
                        'bookmaker': msum.get('market_bookmaker'), 'marketPrice': msum.get('best_over_price'), 'marketImplied': market_implied,
                        'bestAvailablePrice': msum.get('best_over_price'), 'bestAvailableBook': msum.get('best_over_book'),
                        'bestOverPrice': msum.get('best_over_price'), 'bestOverBook': msum.get('best_over_book'),
                        'bestUnderPrice': msum.get('best_under_price'), 'bestUnderBook': msum.get('best_under_book'),
                        'lineRange': msum.get('line_range'), 'bookCount': msum.get('book_count'), 'lineVaries': msum.get('line_varies'),
                        'best_over_price': msum.get('best_over_price'), 'best_over_book': msum.get('best_over_book'),
                        'best_under_price': msum.get('best_under_price'), 'best_under_book': msum.get('best_under_book'),
                        'line_range': msum.get('line_range'), 'book_count': msum.get('book_count'), 'line_varies': msum.get('line_varies'),
                        'score': round(score, 4), 'hubRating': hub, 'evPct': ev_pct, 'opp': opp_name, 'reason': _projection_reason_short(p.get('name'), mk, adj_prob, edge, opp_name), 'status': 'pending', 'actual': None, 'grade': 'pending', 'openingPrice': msum.get('best_over_price'), 'openingImplied': market_implied, 'closingPrice': None, 'closingImplied': None, 'closingBookmaker': None, 'closingCapturedAt': None, 'clvEdge': None, 'profitUnits': None, 'profitDollars': None,
                        'parlayId': None, 'parlayLeg': None
                    }
                    # Phase 1: Add schema fields
                    temp_row['id'] = str(uuid4())
                    temp_row['savedAt'] = datetime.now(ET).isoformat()
                    temp_row['source'] = 'props_board'
                    stake_profile = _stake_profile(temp_row, adjustments)
                    temp_row['stakeDollars'] = stake_profile.get('stake_dollars')
                    # Phase 2: Add tier and BvP grade
                    temp_row['confidenceTier'] = _confidence_tier(temp_row)
                    if home_pitcher and home_pitcher.get('id'):
                        bvp_data = _fetch_bvp(p.get('id'), home_pitcher.get('id'))
                        temp_row['bvpGrade'] = _compute_bvp_grade(bvp_data)
                    else:
                        temp_row['bvpGrade'] = None
                    rows.append(temp_row)

    process_hitters(away_props, away_abbr, home_pitcher.get('name'))
    process_hitters(home_props, home_abbr, away_pitcher.get('name'))

    k_xgb_ready = xgb_ready('k')
    for sp, team_abbr in [(away_sp, away_abbr), (home_sp, home_abbr)]:
        sp_mkt_lines = _market_lines_for_player(market_props, sp.get('name'), 'pitcher_strikeouts')
        k_lines = sp_mkt_lines if sp_mkt_lines else [3.5, 4.5, 5.5]
        mean_k = float(sp.get('mean_k', 0) or 0)
        # ── FanGraphs enrichment for pitcher K props (done once per starter) ──
        if k_xgb_ready:
            sp = {**sp, **enrich_pitcher(sp)}   # merges real FG stats into sp dict
        # ─────────────────────────────────────────────────────────────────────
        for line in k_lines:
            prob_field = _K_PROB_FIELD_FOR.get(line)
            if prob_field:
                raw_prob = float(sp.get(prob_field, 0) or 0)
            else:
                raw_prob = _poisson_over_prob(mean_k, line)
            # XGBoost blend for K props (60% XGB / 40% Monte Carlo when model loaded)
            _xgb_k = xgb_k_prob(sp, line=line) if k_xgb_ready else None
            if _xgb_k is not None:
                raw_prob = max(0.0, min(1.0, 0.40 * raw_prob + 0.60 * _xgb_k))
            if raw_prob < 0.12:
                continue
            raw_mult_prob = _clamp01(raw_prob * _market_mult('pitcher_strikeouts', adjustments))
            market = find_market(sp.get('name'), 'pitcher_strikeouts', line)
            msum = _market_price_summary(market_props, sp.get('name'), 'pitcher_strikeouts', line)
            market_implied = msum.get('market_implied')
            if market_implied and market_implied > 0:
                over_imp = market_implied
                under_imp = _american_to_implied(msum.get('best_under_price')) or (1 - over_imp)
                adj_prob = logit_blend_prob(raw_mult_prob, over_imp, 'pitcher_strikeouts', over_imp, under_imp)
            else:
                adj_prob = raw_mult_prob
            edge = (adj_prob - market_implied) if market_implied is not None else None
            score = (edge * 100.0 if edge is not None else 0) + adj_prob
            hub = _hub_rating(adj_prob, edge or 0)
            mi = market_implied
            ev_pct = round(adj_prob / mi - 1, 4) if mi and mi > 0 else None
            temp_row = {
                'date': capture_date, 'gamePk': game_pk, 'team': team_abbr, 'player': sp.get('name'), 'playerId': sp.get('id'), 'marketKey': 'pitcher_strikeouts', 'line': line, 'recommendedSide': 'Over',
                'rawProb': round(raw_prob, 4), 'rawMultProb': round(raw_mult_prob, 4), 'adjProb': round(adj_prob, 4), 'modelMean': round(float(sp.get('mean_k', 0) or 0), 3), 'edge': round(edge, 4) if edge is not None else None, 'xgbKProb': round(_xgb_k, 4) if _xgb_k is not None else None,
                'bookmaker': msum.get('market_bookmaker'), 'marketPrice': msum.get('best_over_price'), 'marketImplied': market_implied,
                'bestAvailablePrice': msum.get('best_over_price'), 'bestAvailableBook': msum.get('best_over_book'),
                'bestOverPrice': msum.get('best_over_price'), 'bestOverBook': msum.get('best_over_book'),
                'bestUnderPrice': msum.get('best_under_price'), 'bestUnderBook': msum.get('best_under_book'),
                'lineRange': msum.get('line_range'), 'bookCount': msum.get('book_count'), 'lineVaries': msum.get('line_varies'),
                'best_over_price': msum.get('best_over_price'), 'best_over_book': msum.get('best_over_book'),
                'best_under_price': msum.get('best_under_price'), 'best_under_book': msum.get('best_under_book'),
                'line_range': msum.get('line_range'), 'book_count': msum.get('book_count'), 'line_varies': msum.get('line_varies'),
                'score': round(score, 4), 'hubRating': hub, 'evPct': ev_pct, 'opp': '', 'reason': _projection_reason_short(sp.get('name'), 'pitcher_strikeouts', adj_prob, edge), 'status': 'pending', 'actual': None, 'grade': 'pending', 'openingPrice': msum.get('best_over_price'), 'openingImplied': market_implied, 'closingPrice': None, 'closingImplied': None, 'closingBookmaker': None, 'closingCapturedAt': None, 'clvEdge': None, 'profitUnits': None, 'profitDollars': None,
                'parlayId': None, 'parlayLeg': None
            }
            # Phase 1: Add schema fields
            temp_row['id'] = str(uuid4())
            temp_row['savedAt'] = datetime.now(ET).isoformat()
            temp_row['source'] = 'props_board'
            stake_profile = _stake_profile(temp_row, adjustments)
            temp_row['stakeDollars'] = stake_profile.get('stake_dollars')
            # Phase 2: Add tier and BvP grade
            temp_row['confidenceTier'] = _confidence_tier(temp_row)
            if away_pitcher and away_pitcher.get('id'):
                bvp_data = _fetch_bvp(sp.get('id'), away_pitcher.get('id'))
                temp_row['bvpGrade'] = _compute_bvp_grade(bvp_data)
            else:
                temp_row['bvpGrade'] = None
            rows.append(temp_row)

    rows.sort(key=lambda x: x.get('score', 0), reverse=True)
    keep = int((adjustments or {}).get('captured_per_game', 14) or 14)
    return rows[:keep]

def _build_tracker_rows_quick(game_obj, capture_date, adjustments=None):
    """Fast fallback when full simulation fails. Uses confirmed lineups only."""
    adjustments = adjustments or _get_adjustments()
    g = game_obj or {}
    away_team = g.get('teams', {}).get('away', {}).get('team', {})
    home_team = g.get('teams', {}).get('home', {}).get('team', {})
    away_abbr = away_team.get('abbreviation', 'AWAY')
    home_abbr = home_team.get('abbreviation', 'HOME')
    game_pk = g.get('gamePk')

    lineups = (g.get('lineups') or {})
    away_hitters = lineups.get('awayBatters') or []
    home_hitters = lineups.get('homeBatters') or []
    rows = []

    def _emit(hitters, team_abbr):
        for i, p in enumerate(hitters[:9], start=1):
            name = (p.get('fullName') or p.get('name') or '').strip()
            pid = p.get('id') or p.get('playerId')
            if not name:
                continue
            # Conservative heuristic probabilities by lineup slot.
            base_hit = max(0.45, min(0.63, 0.58 - (i - 1) * 0.015))
            base_tb2 = max(0.23, min(0.42, 0.36 - (i - 1) * 0.012))
            for mk, line, raw_prob in [
                ('batter_hits', 0.5, base_hit),
                ('batter_total_bases', 1.5, base_tb2),
            ]:
                adj_prob = _clamp01(raw_prob * _market_mult(mk, adjustments))
                row_data = {
                    'date': capture_date,
                    'gamePk': game_pk,
                    'team': team_abbr,
                    'player': name,
                    'playerId': pid,
                    'marketKey': mk,
                    'line': line,
                    'recommendedSide': 'Over',
                    'rawProb': round(raw_prob, 4),
                    'adjProb': round(adj_prob, 4),
                    'modelMean': None,
                    'edge': None,
                    'bookmaker': None,
                    'marketPrice': None,
                    'marketImplied': None,
                    'bestAvailablePrice': None,
                    'bestAvailableBook': None,
                    'bestOverPrice': None,
                    'bestOverBook': None,
                    'bestUnderPrice': None,
                    'bestUnderBook': None,
                    'lineRange': None,
                    'bookCount': 0,
                    'lineVaries': False,
                    'best_over_price': None,
                    'best_over_book': None,
                    'best_under_price': None,
                    'best_under_book': None,
                    'line_range': None,
                    'book_count': 0,
                    'line_varies': False,
                    'score': round(adj_prob, 4),
                    'hubRating': _hub_rating(adj_prob, 0),
                    'evPct': None,
                    'opp': '',
                    'reason': f'Fallback capture from confirmed lineup slot {i}.',
                    'status': 'pending',
                    'actual': None,
                    'grade': 'pending',
                    'openingPrice': None,
                    'openingImplied': None,
                    'closingPrice': None,
                    'closingImplied': None,
                    'closingBookmaker': None,
                    'closingCapturedAt': None,
                    'clvEdge': None,
                    'profitUnits': None,
                    'profitDollars': None,
                    'parlayId': None,
                    'parlayLeg': None,
                }
                # Phase 1: Add schema fields
                row_data['id'] = str(uuid4())
                row_data['savedAt'] = datetime.now(ET).isoformat()
                row_data['source'] = 'props_board'
                stake_profile = _stake_profile(row_data, adjustments)
                row_data['stakeDollars'] = stake_profile.get('stake_dollars')
                # Phase 2: Add tier and BvP grade (fallback capture)
                row_data['confidenceTier'] = _confidence_tier(row_data)
                row_data['bvpGrade'] = None  # No pitcher data in quick capture
                rows.append(row_data)

    _emit(away_hitters, away_abbr)
    _emit(home_hitters, home_abbr)
    rows.sort(key=lambda x: x.get('score', 0), reverse=True)
    keep = int((adjustments or {}).get('captured_per_game', 14) or 14)
    return rows[:keep]

def _tracker_row_key(row):
    return (
        row.get('date'),
        row.get('gamePk'),
        row.get('player'),
        row.get('marketKey'),
        float(row.get('line') or 0),
    )

def _merge_tracker_entries(existing_rows, new_rows):
    out = {}
    for r in (existing_rows or []):
        if isinstance(r, dict):
            out[_tracker_row_key(r)] = r
    for r in (new_rows or []):
        if isinstance(r, dict):
            out[_tracker_row_key(r)] = r
    rows = list(out.values())
    rows.sort(key=lambda x: x.get('score', 0), reverse=True)
    return rows

def _tracker_capture_continue_bg(date_str, remaining_games, sched, adjustments, include_odds):
    try:
        bg_rows = []
        for g in remaining_games:
            gpk = g.get('gamePk')
            try:
                bg_rows.extend(_build_tracker_rows_for_game(gpk, date_str, adjustments, _sched=sched, include_odds=include_odds))
            except Exception:
                print(f'[tracker_capture_bg_game {gpk}]', traceback.format_exc())
        if not bg_rows:
            return
        store = _load_json(TRACKER_STORE, {})
        day = _normalize_tracker_day(store.get(date_str))
        merged = _merge_tracker_entries(day.get('entries', []), _recalc_tracker_entries(bg_rows))
        day['entries'] = merged
        day['capturedAt'] = datetime.now().isoformat()
        store[date_str] = day
        _save_json(TRACKER_STORE, store)
    except Exception:
        print('[tracker_capture_bg]', traceback.format_exc())
    finally:
        with _TRACKER_CAPTURE_LOCK:
            _TRACKER_CAPTURE_JOBS.pop(date_str, None)

def _tracker_auto_sync_once():
    store = _tracker_store()
    if not store:
        return
    today = datetime.now(ET).strftime('%Y-%m-%d')
    for ds in sorted(store.keys(), reverse=True):
        if ds > today:
            continue
        day = _normalize_tracker_day(store.get(ds))
        pending = [r for r in day.get('entries', []) if r.get('grade') == 'pending']
        if not pending:
            continue
        try:
            with app.test_request_context(f'/api/tracker/close/{ds}', method='POST'):
                api_tracker_close(ds)
            with app.test_request_context(f'/api/tracker/grade/{ds}', method='POST'):
                api_tracker_grade(ds)
        except Exception:
            print(f'[tracker_auto_sync_once {ds}] {traceback.format_exc()}')

def _start_tracker_auto_sync_worker():
    global _TRACKER_AUTO_SYNC_STARTED
    if str(os.getenv('TRACKER_AUTO_SYNC_ENABLED', '1')).strip().lower() not in ('1', 'true', 'yes'):
        return
    with _TRACKER_AUTO_SYNC_LOCK:
        if _TRACKER_AUTO_SYNC_STARTED:
            return
        _TRACKER_AUTO_SYNC_STARTED = True

    interval_min = max(5, int(os.getenv('TRACKER_AUTO_SYNC_MINUTES', '15') or 15))

    def _runner():
        while True:
            try:
                _tracker_auto_sync_once()
            except Exception:
                print(f'[tracker_auto_sync] {traceback.format_exc()}')
            time.sleep(interval_min * 60)

    threading.Thread(target=_runner, daemon=True).start()

def api_tracker_adjustments():
    if request.method == 'POST':
        denied = _check_admin_auth()
        if denied:
            return denied
        payload = request.get_json(silent=True) or {}
        base = _get_adjustments()
        for key in ['captured_per_game', 'best_edge_threshold', 'best_prob_threshold', 'bankroll', 'kelly_fraction', 'unit_size_pct', 'max_bet_pct', 'max_daily_risk_pct', 'max_team_exposure_pct', 'max_market_exposure_pct', 'max_game_exposure_pct']:
            if key in payload:
                base[key] = payload[key]
        if 'market_multipliers' in payload and isinstance(payload['market_multipliers'], dict):
            base['market_multipliers'].update(payload['market_multipliers'])
        _save_json(ADJUST_STORE, base)
        _append_calibration_history('manual_save', base, {'note': 'Manual adjustment save'})
        return jsonify({'success': True, 'adjustments': base})
    return jsonify({'success': True, 'adjustments': _get_adjustments()})

def api_tracker_date(date_str):
    store = _load_json(TRACKER_STORE, {})
    day = _normalize_tracker_day(store.get(date_str))
    day['entries'] = _recalc_tracker_entries(day.get('entries', []))
    return jsonify({'success': True, 'date': date_str, 'adjustments': _get_adjustments(), 'capturedAt': day.get('capturedAt'), 'gradedAt': day.get('gradedAt'), 'closingCapturedAt': day.get('closingCapturedAt'), 'entries': day.get('entries', []), 'summary': _tracker_summary(day.get('entries', []))})

def api_tracker_capture(date_str):
    denied = _check_admin_auth()
    if denied:
        return denied
    try:
        adjustments = _get_adjustments()
        sched = fetch_schedule(date_str)
        if not sched:
            return jsonify({'success': False, 'error': f'No games found for {date_str}'}), 404

        # Keep request under edge/proxy timeout by enforcing a hard budget.
        # Capture Closing can fetch lines after this returns.
        budget_sec = float(os.getenv('TRACKER_CAPTURE_BUDGET_SEC', '240') or 240)
        deadline = time.time() + max(30.0, min(300.0, budget_sec))
        all_entries = []
        captured_games = 0
        recovered_games = 0
        failed_games = []
        include_odds = str(os.getenv('TRACKER_CAPTURE_INCLUDE_ODDS', '0')).strip().lower() in ('1', 'true', 'yes')

        # Parallelise I/O-bound boxscore/roster fetches across games.
        # cap workers at 4 to stay within the single-Gunicorn-worker memory budget.
        max_workers = min(4, len(sched))
        time_limit = max(1.0, deadline - time.time() - 1.0)

        def _capture_one(g):
            gpk = g.get('gamePk')
            try:
                rows = _build_tracker_rows_for_game(gpk, date_str, adjustments, _sched=sched, include_odds=include_odds)
                return gpk, rows, None, False
            except Exception as ex:
                print(f'[tracker_capture_game {gpk}]', traceback.format_exc())
                try:
                    rows = _build_tracker_rows_quick(g, date_str, adjustments)
                    return gpk, rows, None, True
                except Exception as ex2:
                    return gpk, [], str(ex)[:140], False

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_capture_one, g): g for g in sched}
            games_within_budget = []
            games_timed_out = []
            for fut in as_completed(futures, timeout=time_limit):
                games_within_budget.append(fut)
            timed_out_futures = [f for f in futures if f not in set(games_within_budget)]
            games_timed_out = [futures[f] for f in timed_out_futures]

        for fut in games_within_budget:
            try:
                gpk, rows, err, recovered = fut.result()
                if err:
                    failed_games.append({'gamePk': gpk, 'error': err})
                else:
                    all_entries.extend(rows)
                    captured_games += 1
                    if recovered:
                        recovered_games += 1
            except Exception:
                pass

        store = _load_json(TRACKER_STORE, {})
        day = _normalize_tracker_day(store.get(date_str))
        entries = _merge_tracker_entries(day.get('entries', []), _recalc_tracker_entries(all_entries))
        entries.sort(key=lambda x: x.get('score', 0), reverse=True)
        store[date_str] = {'capturedAt': datetime.now().isoformat(), 'gradedAt': None, 'closingCapturedAt': None, 'entries': entries}
        _save_json(TRACKER_STORE, store)
        timed_out = bool(games_timed_out)
        remaining_games = games_timed_out if timed_out else []
        background_started = False
        if remaining_games and str(os.getenv('TRACKER_CAPTURE_BACKGROUND', '1')).strip().lower() in ('1', 'true', 'yes'):
            with _TRACKER_CAPTURE_LOCK:
                if date_str not in _TRACKER_CAPTURE_JOBS:
                    t = threading.Thread(
                        target=_tracker_capture_continue_bg,
                        args=(date_str, remaining_games, sched, adjustments, include_odds),
                        daemon=True,
                    )
                    _TRACKER_CAPTURE_JOBS[date_str] = {'startedAt': datetime.now().isoformat(), 'remaining': len(remaining_games)}
                    t.start()
                    background_started = True
        msg = None
        if timed_out:
            if background_started:
                msg = f'Partial capture: {captured_games}/{len(sched)} games processed. Continuing {len(remaining_games)} game(s) in background.'
            else:
                msg = f'Partial capture: {captured_games}/{len(sched)} games processed in time budget.'
        elif not entries:
            msg = f'Capture completed for {captured_games}/{len(sched)} games but produced 0 entries.'
        if recovered_games:
            extra = f' Recovered {recovered_games} game(s) via fallback capture.'
            msg = (msg or 'Captured.') + extra
        return jsonify({
            'success': True,
            'date': date_str,
            'entries': entries,
            'summary': _tracker_summary(entries),
            'capturedAt': store[date_str]['capturedAt'],
            'capturedGames': captured_games,
            'totalGames': len(sched),
            'timedOut': timed_out,
            'backgroundStarted': background_started,
            'remainingGames': len(remaining_games),
            'recoveredGames': recovered_games,
            'failedGames': failed_games,
            'message': msg,
        })
    except Exception:
        print('[tracker_capture]', traceback.format_exc())
        return jsonify({'success': False, 'error': 'Capture failed — check server logs'}), 500

def api_tracker_grade(date_str):
    denied = _check_admin_auth()
    if denied:
        return denied
    store = _load_json(TRACKER_STORE, {})
    raw_day = store.get(date_str)
    if raw_day is None:
        return jsonify({'success': False, 'error': 'No captured tracker data for this date'}), 404
    day = _normalize_tracker_day(raw_day)
    sched = fetch_schedule(date_str)
    games = {g.get('gamePk'): g for g in sched}
    for row in day.get('entries', []):
        gpk = row.get('gamePk')
        g = games.get(gpk)
        if not g:
            continue
        status = ((g.get('status') or {}).get('detailedState') or '').lower()
        if 'final' not in status:
            continue
        try:
            mk = row.get('marketKey')
            if mk in ('nrfi', 'yrfi'):
                inns = (g.get('linescore') or {}).get('innings') or []
                first = inns[0] if inns else {}
                a1 = int((((first.get('away') or {}).get('runs')) or 0))
                h1 = int((((first.get('home') or {}).get('runs')) or 0))
                actual = a1 + h1
                row['actual'] = actual
                row['grade'] = _grade_side(actual, row.get('line', 0.5), 'Under' if mk == 'nrfi' else 'Over')
                row['status'] = 'graded'
                continue
            box = requests.get(f"{MLB_API}/game/{gpk}/boxscore", timeout=10).json().get('teams', {})
            players = {}
            for side in ['away', 'home']:
                players.update((box.get(side) or {}).get('players', {}))
            pobj = None
            pid = row.get('playerId')
            if pid:
                pobj = players.get(f'ID{pid}')
            if not pobj:
                for v in players.values():
                    if (v.get('person', {}).get('fullName') or '').lower() == (row.get('player') or '').lower():
                        pobj = v; break
            actual = _tracker_stat_from_boxscore(pobj, row.get('marketKey'))
            row['actual'] = actual
            row['grade'] = _grade_side(actual, row.get('line'), row.get('recommendedSide') or 'Over')
            row['status'] = 'graded'
        except Exception:
            print('[tracker_grade_row]', traceback.format_exc())
    day['entries'] = _recalc_tracker_entries(day.get('entries', []))
    day['gradedAt'] = datetime.now().isoformat()
    store[date_str] = day
    _save_json(TRACKER_STORE, store)
    return jsonify({'success': True, 'date': date_str, 'entries': day.get('entries', []), 'summary': _tracker_summary(day.get('entries', [])), 'gradedAt': day.get('gradedAt')})

def _tracker_store():
    return _load_json(TRACKER_STORE, {})

def _coerce_tracker_entries(entries):
    if not isinstance(entries, list):
        return []
    return [row for row in entries if isinstance(row, dict)]

def _normalize_tracker_day(day_payload):
    if isinstance(day_payload, dict):
        return {
            'capturedAt': day_payload.get('capturedAt'),
            'gradedAt': day_payload.get('gradedAt'),
            'closingCapturedAt': day_payload.get('closingCapturedAt'),
            'entries': _coerce_tracker_entries(day_payload.get('entries', [])),
        }
    if isinstance(day_payload, list):
        return {
            'capturedAt': None,
            'gradedAt': None,
            'closingCapturedAt': None,
            'entries': _coerce_tracker_entries(day_payload),
        }
    return {
        'capturedAt': None,
        'gradedAt': None,
        'closingCapturedAt': None,
        'entries': [],
    }

def _dates_in_window(end_date_str, window_days):
    try:
        end_dt = datetime.strptime(end_date_str, '%Y-%m-%d').date()
    except Exception:
        end_dt = datetime.now().date()
    return [(end_dt - timedelta(days=i)).isoformat() for i in range(max(1, int(window_days)))]

def _collect_window_entries(end_date_str, window_days):
    store = _tracker_store()
    dates = set(_dates_in_window(end_date_str, window_days))
    rows = []
    for ds, payload in store.items():
        if ds in dates:
            rows.extend(_normalize_tracker_day(payload).get('entries', []))
    return rows

def _market_calibration(entries, current_adj):
    by_market = {}
    for row in entries:
        mk = row.get('marketKey')
        if not mk:
            continue
        by_market.setdefault(mk, [])
        if row.get('grade') in ('win', 'loss'):
            by_market[mk].append(row)

    out = []
    for mk, rows in by_market.items():
        graded = len(rows)
        wins = sum(1 for r in rows if r.get('grade') == 'win')
        losses = sum(1 for r in rows if r.get('grade') == 'loss')
        hit_rate = wins / max(1, graded)
        avg_edge = sum(float(r.get('edge') or 0) for r in rows) / max(1, graded)
        avg_prob = sum(float(r.get('adjProb') or r.get('rawProb') or 0) for r in rows) / max(1, graded)
        target = CALIBRATION_TARGETS.get(mk, 0.54)
        current_mult = float(current_adj.get('market_multipliers', {}).get(mk, 1.0) or 1.0)
        delta = hit_rate - target
        if graded < 8:
            confidence = 'LOW SAMPLE'
            suggested = current_mult
            action = 'hold'
        else:
            shift = _clamp(delta * 0.60, -0.08, 0.08)
            suggested = round(_clamp(current_mult + shift, 0.80, 1.20), 3)
            if suggested > current_mult + 0.004:
                action = 'increase'
            elif suggested < current_mult - 0.004:
                action = 'decrease'
            else:
                action = 'hold'
            confidence = 'HIGH' if graded >= 20 else 'MEDIUM'
        rationale = f"{mk}: {wins}-{losses} over last sample, hit rate {hit_rate:.1%} vs target {target:.1%}, avg edge {avg_edge:.1%}."
        out.append({
            'marketKey': mk,
            'graded': graded,
            'wins': wins,
            'losses': losses,
            'hit_rate': round(hit_rate, 4),
            'target_rate': round(target, 4),
            'avg_edge': round(avg_edge, 4),
            'avg_prob': round(avg_prob, 4),
            'current_multiplier': round(current_mult, 3),
            'suggested_multiplier': round(suggested, 3),
            'action': action,
            'confidence': confidence,
            'rationale': rationale,
        })
    out.sort(key=lambda x: (x['action'] == 'hold', -x['graded'], x['marketKey']))
    return out

def _overall_window_summary(entries):
    graded = [x for x in entries if x.get('grade') in ('win', 'loss', 'push')]
    wins = sum(1 for x in graded if x.get('grade') == 'win')
    losses = sum(1 for x in graded if x.get('grade') == 'loss')
    pushes = sum(1 for x in graded if x.get('grade') == 'push')
    active = [x for x in graded if x.get('grade') in ('win', 'loss')]
    hit_rate = round(wins / max(1, wins + losses), 4) if active else 0.0
    avg_edge = round(sum(float(x.get('edge') or 0) for x in active) / max(1, len(active)), 4) if active else 0.0
    return {
        'tracked': len(entries), 'graded': len(graded), 'wins': wins, 'losses': losses,
        'pushes': pushes, 'hit_rate': hit_rate, 'avg_edge': avg_edge
    }

def _tracker_side_label(row):
    side = row.get('recommendedSide') or row.get('side')
    if side:
        return side
    market = str(row.get('marketKey') or '').lower()
    if market in ('nrfi', 'yrfi'):
        return market.upper()
    return 'Over'

def _tracker_live_summary(entries, adjustments=None):
    adjustments = adjustments or _get_adjustments()
    summary = _tracker_summary(entries)
    graded = [x for x in entries if x.get('grade') in ('win', 'loss', 'push')]
    active = [x for x in graded if x.get('grade') in ('win', 'loss')]
    pending = [x for x in entries if (x.get('grade') or 'pending') == 'pending']
    planned_stake = round(sum(float(x.get('stakeDollars') or 0) for x in entries), 2)
    pending_risk = round(sum(float(x.get('stakeDollars') or 0) for x in pending), 2)
    profit_dollars = round(sum(float(x.get('profitDollars') or 0) for x in graded), 2)
    profit_units = round(sum(float(x.get('profitUnits') or 0) for x in graded), 3)
    clv_rows = [x for x in graded if x.get('clvEdge') is not None]
    positive_clv = [x for x in clv_rows if float(x.get('clvEdge') or 0) > 0]
    avg_clv = round(sum(float(x.get('clvEdge') or 0) for x in clv_rows) / max(1, len(clv_rows)), 4) if clv_rows else None
    live_bankroll = round(float(adjustments.get('bankroll') or 0) + profit_dollars, 2)
    summary['pending'] = len(pending)
    summary['units'] = profit_units
    summary['at_risk'] = pending_risk
    summary['avg_clv'] = avg_clv
    summary['clv_positive_rate'] = round(len(positive_clv) / max(1, len(clv_rows)), 4) if clv_rows else None
    summary['bankroll'] = {
        'starting_bankroll': float(adjustments.get('bankroll') or 0),
        'live_bankroll': live_bankroll,
        'planned_stake': planned_stake,
        'pending_risk': pending_risk,
    }
    summary['value'] = {
        'dollars': profit_dollars,
        'units': profit_units,
        'avg_clv': avg_clv,
        'clv_positive_rate': summary['clv_positive_rate'],
        'graded_with_clv': len(clv_rows),
    }
    summary['snapshot'] = {
        'picks': summary.get('picks', 0),
        'graded': summary.get('graded', 0),
        'pending': len(pending),
        'hit_rate': summary.get('hit_rate', 0.0),
        'units': profit_units,
        'profit_dollars': profit_dollars,
        'at_risk': pending_risk,
        'bankroll': live_bankroll,
        'clv_rate': summary['clv_positive_rate'],
        'avg_clv': avg_clv,
    }
    if active:
        summary['avg_edge'] = round(sum(float(x.get('edge') or 0) for x in active) / len(active), 4)
    return summary

def _tracker_find_pick(store, pick_id, date_hint=None):
    dates = []
    if date_hint:
        dates.append(date_hint)
    dates.extend([ds for ds in store.keys() if ds not in dates])
    for ds in dates:
        payload = _normalize_tracker_day(store.get(ds))
        entries = payload.get('entries', [])
        for idx, row in enumerate(entries):
            if row.get('id') == pick_id:
                return ds, payload, entries, idx, row
    return None, None, None, None, None

def _tracker_pick_payload(row):
    out = dict(row)
    out['sideLabel'] = _tracker_side_label(row)
    return out

def _tracker_today_payload(date_str=None):
    date_str = date_str or datetime.now(ET).strftime('%Y-%m-%d')
    store = _tracker_store()
    day = _normalize_tracker_day(store.get(date_str))
    entries = [_tracker_pick_payload(x) for x in day.get('entries', [])]
    entries.sort(key=lambda x: ((x.get('grade') or 'pending') != 'pending', -(float(x.get('hubRating') or 0)), -(float(x.get('edge') or 0))))
    adjustments = _get_adjustments()
    return {
        'success': True,
        'date': date_str,
        'capturedAt': day.get('capturedAt'),
        'gradedAt': day.get('gradedAt'),
        'closingCapturedAt': day.get('closingCapturedAt'),
        'entries': entries,
        'summary': _tracker_live_summary(entries, adjustments),
        'settings': adjustments,
    }

def _tracker_performance_payload(date_str=None, window_days=30):
    date_str = date_str or datetime.now(ET).strftime('%Y-%m-%d')
    window_days = max(1, int(window_days or 30))
    entries = _collect_window_entries(date_str, window_days)
    adjustments = _get_adjustments()
    calibration = _market_calibration(entries, adjustments)
    overall_series = _daily_series(date_str, window_days, None)
    available_markets = sorted({row.get('marketKey') for row in entries if row.get('marketKey')})
    value_rows = [row for row in entries if row.get('grade') in ('win', 'loss', 'push')]
    clv_rows = [row for row in value_rows if row.get('clvEdge') is not None]
    top_clv = sorted(clv_rows, key=lambda x: float(x.get('clvEdge') or 0), reverse=True)[:10]
    daily = []
    for ds in reversed(_dates_in_window(date_str, window_days)):
        rows = _normalize_tracker_day(_tracker_store().get(ds)).get('entries', [])
        graded = [r for r in rows if r.get('grade') in ('win', 'loss', 'push')]
        active = [r for r in graded if r.get('grade') in ('win', 'loss')]
        wins = sum(1 for r in active if r.get('grade') == 'win')
        losses = sum(1 for r in active if r.get('grade') == 'loss')
        risk = round(sum(float(r.get('stakeDollars') or 0) for r in graded), 2)
        profit = round(sum(float(r.get('profitDollars') or 0) for r in graded), 2)
        units = round(sum(float(r.get('profitUnits') or 0) for r in graded), 3)
        clv_day = [r for r in graded if r.get('clvEdge') is not None]
        daily.append({
            'date': ds,
            'bets': len(rows),
            'graded': len(graded),
            'wins': wins,
            'losses': losses,
            'hit_rate': round(wins / max(1, wins + losses), 4) if active else None,
            'risk': risk,
            'profit': profit,
            'units': units,
            'roi': round(profit / risk, 4) if risk > 0 else None,
            'avg_clv': round(sum(float(r.get('clvEdge') or 0) for r in clv_day) / len(clv_day), 4) if clv_day else None,
        })
    return {
        'success': True,
        'date': date_str,
        'window': window_days,
        'summary': _tracker_live_summary(entries, adjustments),
        'overallSeries': overall_series,
        'availableMarkets': available_markets,
        'multiplierHistory': {mk: _multiplier_history(date_str, window_days, mk) for mk in available_markets},
        'calibration': calibration,
        'topCLV': top_clv,
        'daily': daily,
    }

def _tracker_backtest_payload(start_date, end_date, sims=2000):
    start_dt = datetime.strptime(start_date, '%Y-%m-%d').date()
    end_dt = datetime.strptime(end_date, '%Y-%m-%d').date()
    if end_dt < start_dt:
        start_dt, end_dt = end_dt, start_dt

    sims = max(250, min(10000, int(sims or 2000)))
    store = _tracker_store()
    daily = []
    cur = start_dt
    total_bets = 0.0
    total_wins = 0.0
    total_profit = 0.0
    total_risk = 0.0
    total_units = 0.0

    def _row_prob(row):
        try:
            p = float(row.get('adjProb') or row.get('rawProb') or 0)
            return _clamp(p, 0.01, 0.99)
        except Exception:
            return None

    def _row_stake(row):
        try:
            s = float(row.get('stakeDollars') or 0)
            return max(0.0, s)
        except Exception:
            return 0.0

    def _row_win_units(row):
        pu = _profit_units_from_american(row.get('openingPrice'))
        if pu is None:
            pu = _profit_units_from_american(row.get('marketPrice'))
        if pu is None:
            pu = _profit_units_from_american(row.get('bestAvailablePrice'))
        if pu is None:
            try:
                mi = float(row.get('marketImplied') or row.get('openingImplied') or 0)
                if mi > 0:
                    pu = max(0.0, (1.0 / mi) - 1.0)
            except Exception:
                pu = None
        return float(pu if pu is not None else 1.0)

    # Seed by selected window so repeated runs are stable.
    seed = int(start_dt.strftime('%Y%m%d')) * 100000000 + int(end_dt.strftime('%Y%m%d'))
    rng = random.Random(seed)

    while cur <= end_dt:
        ds = cur.strftime('%Y-%m-%d')
        rows = _normalize_tracker_day(store.get(ds)).get('entries', [])
        modeled = []
        for row in rows:
            if row.get('marketKey') == 'parlay':
                continue
            p = _row_prob(row)
            if p is None:
                continue
            stake = _row_stake(row)
            if stake <= 0:
                continue
            modeled.append({
                'p': p,
                'stake': stake,
                'win_units': _row_win_units(row),
            })

        if not modeled:
            daily.append({
                'date': ds,
                'bets': 0,
                'graded': 0,
                'wins': 0,
                'losses': 0,
                'hit_rate': None,
                'units': 0.0,
                'profit': 0.0,
                'risk': 0.0,
                'roi': None,
            })
            cur += timedelta(days=1)
            continue

        risk = sum(m['stake'] for m in modeled)
        sim_wins = 0.0
        sim_losses = 0.0
        sim_units = 0.0
        sim_profit = 0.0
        for _ in range(sims):
            wins_i = 0
            losses_i = 0
            units_i = 0.0
            profit_i = 0.0
            for m in modeled:
                if rng.random() <= m['p']:
                    wins_i += 1
                    units_i += m['win_units']
                    profit_i += m['stake'] * m['win_units']
                else:
                    losses_i += 1
                    units_i -= 1.0
                    profit_i -= m['stake']
            sim_wins += wins_i
            sim_losses += losses_i
            sim_units += units_i
            sim_profit += profit_i

        wins = sim_wins / sims
        losses = sim_losses / sims
        units = sim_units / sims
        profit = sim_profit / sims
        hit_rate = wins / max(1.0, (wins + losses))
        roi = (profit / risk) if risk > 0 else None

        total_bets += len(modeled)
        total_wins += wins
        total_profit += profit
        total_risk += risk
        total_units += units

        daily.append({
            'date': ds,
            'bets': len(modeled),
            'graded': len(modeled),
            'wins': round(wins, 2),
            'losses': round(losses, 2),
            'hit_rate': round(hit_rate, 4) if hit_rate is not None else None,
            'units': round(units, 3),
            'profit': round(profit, 2),
            'risk': round(risk, 2),
            'roi': round(roi, 4) if roi is not None else None,
        })
        cur += timedelta(days=1)

    summary = {
        'bets': int(round(total_bets)),
        'wins': round(total_wins, 2),
        'hit_rate': round(total_wins / max(1.0, total_bets), 4) if total_bets else None,
        'profit': round(total_profit, 2),
        'risk': round(total_risk, 2),
        'units': round(total_units, 3),
        'roi': round(total_profit / total_risk, 4) if total_risk > 0 else None,
    }
    return {
        'success': True,
        'start': start_dt.strftime('%Y-%m-%d'),
        'end': end_dt.strftime('%Y-%m-%d'),
        'days': (end_dt - start_dt).days + 1,
        'mode': 'historical_resimulation',
        'sims': sims,
        'summary': summary,
        'daily': daily,
    }

def _tracker_export_rows(date_str):
    store = _tracker_store()
    day = _normalize_tracker_day(store.get(date_str))
    rows = [dict(row) for row in day.get('entries', [])]
    rows.sort(key=lambda x: ((x.get('savedAt') or ''), (x.get('player') or ''), (x.get('marketKey') or '')))
    return day, rows

def _tracker_export_csv_text(date_str):
    day, rows = _tracker_export_rows(date_str)
    fields = [
        'id', 'date', 'savedAt', 'source', 'player', 'playerId', 'gamePk', 'team', 'opp',
        'marketKey', 'line', 'recommendedSide', 'rawProb', 'adjProb', 'modelMean', 'edge',
        'evPct', 'hubRating', 'bvpGrade', 'pitchTypeAdvantage', 'nrfiProb', 'bookmaker',
        'marketPrice', 'marketImplied', 'bestAvailablePrice', 'bestAvailableBook', 'openingPrice',
        'openingImplied', 'parlayId', 'parlayLeg', 'stakeDollars', 'kellyFraction',
        'confidenceTier', 'status', 'actual', 'grade', 'gradedAt', 'closingPrice',
        'closingImplied', 'closingBookmaker', 'closingCapturedAt', 'clvEdge', 'profitDollars',
        'profitUnits', 'reason'
    ]
    output = io.StringIO()
    writer = csvmod.DictWriter(output, fieldnames=fields, extrasaction='ignore')
    writer.writeheader()
    for row in rows:
        cleaned = dict(row)
        if isinstance(cleaned.get('matchupStorylines'), list):
            cleaned['matchupStorylines'] = ' | '.join(str(x) for x in cleaned.get('matchupStorylines') or [])
        if isinstance(cleaned.get('legs'), list):
            cleaned['legs'] = json.dumps(cleaned.get('legs'))
        writer.writerow(cleaned)
    return day, output.getvalue()

def _pdf_escape(value):
    return str(value).replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')

def _pdf_wrap_lines(lines, width=96):
    wrapped = []
    for raw in lines:
        text = str(raw or '')
        if not text:
            wrapped.append('')
            continue
        while len(text) > width:
            cut = text.rfind(' ', 0, width + 1)
            if cut < width * 0.55:
                cut = width
            wrapped.append(text[:cut].rstrip())
            text = text[cut:].lstrip()
        wrapped.append(text)
    return wrapped

def _simple_pdf_bytes(lines):
    wrapped = _pdf_wrap_lines(lines)
    lines_per_page = 44
    page_chunks = [wrapped[i:i + lines_per_page] for i in range(0, len(wrapped), lines_per_page)] or [[]]

    objects = []
    page_object_ids = []
    content_object_ids = []
    next_obj_id = 1

    catalog_id = next_obj_id; next_obj_id += 1
    pages_id = next_obj_id; next_obj_id += 1

    for _chunk in page_chunks:
        page_object_ids.append(next_obj_id)
        next_obj_id += 1
        content_object_ids.append(next_obj_id)
        next_obj_id += 1

    font_id = next_obj_id

    objects.append((catalog_id, f'<< /Type /Catalog /Pages {pages_id} 0 R >>'.encode('latin-1')))
    kids = ' '.join(f'{pid} 0 R' for pid in page_object_ids)
    objects.append((pages_id, f'<< /Type /Pages /Kids [{kids}] /Count {len(page_object_ids)} >>'.encode('latin-1')))

    for page_id, content_id, chunk_index, chunk in zip(page_object_ids, content_object_ids, range(len(page_chunks)), page_chunks):
        content_lines = ['BT', '/F1 14 Tf', '50 760 Td']
        if not chunk:
            content_lines.append('(No data) Tj')
        else:
            first = True
            for line in chunk:
                safe_line = _pdf_escape(line)[:140]
                if first:
                    content_lines.append(f'({safe_line}) Tj')
                    first = False
                else:
                    content_lines.append('0 -15 Td')
                    content_lines.append(f'({safe_line}) Tj')
        content_lines.extend(['0 -24 Td', f'(Page {chunk_index + 1} of {len(page_chunks)}) Tj', 'ET'])
        stream = ('\n'.join(content_lines)).encode('latin-1', 'replace')
        page_body = f'<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {content_id} 0 R >>'.encode('latin-1')
        content_body = b'<< /Length ' + str(len(stream)).encode('ascii') + b' >>\nstream\n' + stream + b'\nendstream'
        objects.append((page_id, page_body))
        objects.append((content_id, content_body))

    objects.append((font_id, b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>'))
    objects.sort(key=lambda x: x[0])

    pdf = bytearray(b'%PDF-1.4\n')
    offsets = {0: 0}
    for obj_id, body in objects:
        offsets[obj_id] = len(pdf)
        pdf.extend(f'{obj_id} 0 obj\n'.encode('ascii'))
        pdf.extend(body)
        pdf.extend(b'\nendobj\n')
    xref_start = len(pdf)
    pdf.extend(f'xref\n0 {font_id + 1}\n'.encode('ascii'))
    pdf.extend(b'0000000000 65535 f \n')
    for obj_id in range(1, font_id + 1):
        off = offsets.get(obj_id, 0)
        pdf.extend(f'{off:010d} 00000 n \n'.encode('ascii'))
    pdf.extend(f'trailer\n<< /Size {font_id + 1} /Root {catalog_id} 0 R >>\nstartxref\n{xref_start}\n%%EOF'.encode('ascii'))
    return bytes(pdf)

def _tracker_export_pdf_bytes(date_str):
    day, rows = _tracker_export_rows(date_str)
    summary = _tracker_live_summary(rows, _get_adjustments())
    active = [row for row in rows if row.get('grade') in ('win', 'loss')]
    lines = [
        'MLB Analytics Hub - Tracker Summary Card',
        f'Date: {date_str}',
        f'Saved picks: {summary.get("picks", 0)} | Graded: {summary.get("graded", 0)} | Pending: {summary.get("pending", 0)}',
        f'Hit rate: {round((summary.get("hit_rate") or 0) * 100, 1)}% | Profit: ${summary.get("value", {}).get("dollars", 0):.2f} | Units: {summary.get("value", {}).get("units", 0):.2f}',
        f'At risk: ${summary.get("snapshot", {}).get("at_risk", 0):.2f} | Bankroll: ${summary.get("snapshot", {}).get("bankroll", 0):.2f}',
        f'Captured at: {day.get("capturedAt") or "-"}',
        ' ',
        'Top tracked picks:'
    ]
    top_rows = sorted(rows, key=lambda x: (-(float(x.get('hubRating') or 0)), -(float(x.get('edge') or 0))))[:28]
    if not top_rows:
        lines.append('No tracked picks for this day.')
    else:
        for row in top_rows:
            grade = (row.get('grade') or 'pending').upper()
            side = row.get('recommendedSide') or row.get('side') or 'Over'
            profit = row.get('profitDollars')
            profit_text = '-' if profit is None else f'${float(profit):.2f}'
            lines.append(f'{row.get("player") or "-"} | {row.get("marketKey") or "-"} {side} {row.get("line") if row.get("line") is not None else "-"} | HUB {row.get("hubRating") or 0} | {grade} | {profit_text}')
    if active:
        lines.extend([' ', 'Best graded edges:'])
        for row in sorted(active, key=lambda x: -(float(x.get('edge') or 0)))[:6]:
            lines.append(f'{row.get("player") or "-"} | {row.get("marketKey") or "-"} | edge {round((float(row.get("edge") or 0) * 100), 1)}% | grade {(row.get("grade") or "pending").upper()}')
    subtitle = f'Tracker summary card for {date_str} · {summary.get("picks", 0)} tracked picks'
    return _simple_pdf_bytes(lines, title='MLB Analytics Hub - Tracker Summary Card', subtitle=subtitle)

def api_tracker_calibration_dashboard(date_str):
    window = int(request.args.get('window', 14) or 14)
    adjustments = _get_adjustments()
    markets = list((adjustments.get('market_multipliers') or {}).keys())
    market_series = {mk: _daily_series(date_str, window, mk) for mk in markets}
    multiplier_history = {mk: _multiplier_history(date_str, window, mk) for mk in markets}
    events = _history_in_window(date_str, window)
    return jsonify({
        'success': True,
        'date': date_str,
        'window': window,
        'overallSeries': _daily_series(date_str, window, None),
        'marketSeries': market_series,
        'multiplierHistory': multiplier_history,
        'events': events[-120:],
        'availableMarkets': markets,
        'adjustments': adjustments,
    })

def api_tracker_calibration(date_str):
    window = int(request.args.get('window', 7) or 7)
    entries = _collect_window_entries(date_str, window)
    adjustments = _get_adjustments()
    return jsonify({
        'success': True,
        'date': date_str,
        'window': window,
        'summary': _overall_window_summary(entries),
        'markets': _market_calibration(entries, adjustments),
        'adjustments': adjustments,
    })

def api_tracker_model_record():
    today = datetime.now(ET).strftime('%Y-%m-%d')
    entries = _collect_window_entries(today, 30)
    graded  = [e for e in entries if e.get('grade') in ('win', 'loss')]
    _MK_MAP = {
        'batter_hits': 'hits',
        'batter_home_runs': 'hr',
        'batter_total_bases': 'tb',
        'batter_rbis': 'rbi',
        'batter_runs_scored': 'runs',
        'batter_hits_runs_rbis': 'hrr',
        'pitcher_strikeouts': 'pitcher_strikeouts',
        'nrfi': 'nrfi',
        'yrfi': 'yrfi',
        'parlay': 'parlay',
    }
    by_mkt = {}
    for e in graded:
        mk  = e.get('marketKey', '')
        key = _MK_MAP.get(mk, mk)
        by_mkt.setdefault(key, {'wins': 0, 'losses': 0})
        if e.get('grade') == 'win':  by_mkt[key]['wins']   += 1
        else:                         by_mkt[key]['losses'] += 1
    record = {}
    for key, v in by_mkt.items():
        total = v['wins'] + v['losses']
        record[key] = {'wins': v['wins'], 'losses': v['losses'],
                       'graded': total,
                       'hit_rate': round(v['wins'] / total, 3) if total else 0.0}

    # Flat summary fields for UI consumers that only need hit rates.
    summary = {
        'hits': record.get('hits', {}).get('hit_rate', 0.0),
        'hr': record.get('hr', {}).get('hit_rate', 0.0),
        'pitcher_strikeouts': record.get('pitcher_strikeouts', {}).get('hit_rate', 0.0),
        'batter_total_bases': record.get('tb', {}).get('hit_rate', 0.0),
        'batter_rbis': record.get('rbi', {}).get('hit_rate', 0.0),
        'batter_runs_scored': record.get('runs', {}).get('hit_rate', 0.0),
        'batter_hits_runs_rbis': record.get('hrr', {}).get('hit_rate', 0.0),
        'parlay': record.get('parlay', {}).get('hit_rate', 0.0),
        'nrfi': record.get('nrfi', {}).get('hit_rate', 0.0),
        'yrfi': record.get('yrfi', {}).get('hit_rate', 0.0),
    }

    # Back-compat alias for prior frontend key.
    if 'pitcher_strikeouts' in record and 'k' not in record:
        record['k'] = record['pitcher_strikeouts']

    return jsonify({
        'success': True,
        'record': record,
        'summary': summary,
        'window': 30,
        'total_graded': len(graded),
    })

def api_tracker_export(date_str):
    fmt = (request.args.get('format') or 'csv').strip().lower()
    if fmt not in ('csv', 'pdf'):
        return jsonify({'success': False, 'error': 'format must be csv or pdf'}), 400

    if fmt == 'csv':
        _, csv_text = _tracker_export_csv_text(date_str)
        return Response(
            csv_text,
            mimetype='text/csv',
            headers={'Content-Disposition': f'attachment; filename=tracker-{date_str}.csv'}
        )

    pdf_bytes = _tracker_export_pdf_bytes(date_str)
    return Response(
        pdf_bytes,
        mimetype='application/pdf',
        headers={'Content-Disposition': f'attachment; filename=tracker-summary-{date_str}.pdf'}
    )

def api_tracker_today():
    date_str = request.args.get('date') or datetime.now(ET).strftime('%Y-%m-%d')
    return jsonify(_tracker_today_payload(date_str))

def api_tracker_performance():
    date_str = request.args.get('date') or datetime.now(ET).strftime('%Y-%m-%d')
    window = int(request.args.get('window', 30) or 30)
    return jsonify(_tracker_performance_payload(date_str, window))

def api_tracker_backtest():
    start = (request.args.get('start') or '').strip()
    end = (request.args.get('end') or '').strip()
    try:
        sims = max(200, min(5000, int(request.args.get('sims', 5000) or 5000)))
    except (TypeError, ValueError):
        sims = 5000
    if not start or not end:
        return jsonify({'success': False, 'error': 'start and end are required (YYYY-MM-DD)'}), 400
    try:
        return jsonify(_tracker_backtest_payload(start, end, sims=sims))
    except ValueError:
        return jsonify({'success': False, 'error': 'Invalid date format. Use YYYY-MM-DD.'}), 400
    except Exception as ex:
        print(f'[api_tracker_backtest] {traceback.format_exc()}')
        return jsonify({'success': False, 'error': str(ex)}), 500

def api_tracker_settings():
    if request.method == 'GET':
        return jsonify({'success': True, 'settings': _get_adjustments()})

    payload = request.get_json(silent=True) or {}
    current = _get_adjustments()
    for key in [
        'bankroll', 'kelly_fraction', 'unit_size_pct', 'max_bet_pct', 'max_daily_risk_pct',
        'max_team_exposure_pct', 'max_market_exposure_pct', 'max_game_exposure_pct',
        'captured_per_game', 'best_edge_threshold', 'best_prob_threshold'
    ]:
        if key in payload:
            current[key] = payload.get(key)
    if isinstance(payload.get('market_multipliers'), dict):
        current['market_multipliers'].update(payload.get('market_multipliers') or {})
    _save_json(ADJUST_STORE, current)
    return jsonify({'success': True, 'settings': current})

def api_tracker_pick():
    denied = _check_admin_auth()
    if denied:
        return denied
    entry = request.get_json(silent=True) or {}
    if not entry.get('player') or not entry.get('marketKey'):
        return jsonify({'success': False, 'error': 'Missing player or marketKey'}), 400

    today = entry.get('date') or datetime.now(ET).strftime('%Y-%m-%d')
    entry['date']   = today
    entry['source'] = entry.get('source') or 'props_board'
    entry.setdefault('savedAt', datetime.now(ET).isoformat())
    entry.setdefault('recommendedSide', entry.get('recommendedSide') or _tracker_side_label(entry))

    if not entry.get('id'):
        entry['id'] = str(uuid.uuid4())

    # Recompute badge fields in case frontend omitted them
    adj_prob = float(entry.get('adjProb') or 0)
    edge_val = float(entry.get('edge') or 0)
    if entry.get('hubRating') is None:
        entry['hubRating'] = _hub_rating(adj_prob, edge_val)
    mi = float(entry.get('marketImplied') or 0)
    if entry.get('evPct') is None and mi > 0:
        entry['evPct'] = round(adj_prob / mi - 1, 4)

    if entry.get('confidenceTier') is None:
        entry['confidenceTier'] = _confidence_tier(entry)
    if entry.get('stakeDollars') is None:
        entry['stakeDollars'] = (_stake_profile(entry, _get_adjustments()) or {}).get('stake_dollars')

    # Ensure grading fields exist
    for k, v in [('status','pending'),('grade','pending'),('actual',None),
                 ('gradedAt',None),('closingPrice',None),('closingBookmaker', None), ('closingCapturedAt', None),
                 ('clvEdge',None),('profitUnits',None), ('profitDollars', None), ('parlayId', None), ('parlayLeg', None)]:
        entry.setdefault(k, v)

    store = _load_json(TRACKER_STORE, {})
    day   = store.setdefault(today, {'capturedAt': None, 'gradedAt': None,
                                      'closingCapturedAt': None, 'entries': []})

    # Deduplicate on player + marketKey + line
    for ex in day.get('entries', []):
        if (ex.get('player') == entry.get('player') and
                ex.get('marketKey') == entry.get('marketKey') and
                str(ex.get('line', '')) == str(entry.get('line', ''))):
            return jsonify({'success': True, 'id': ex.get('id'), 'duplicate': True})

    day['entries'].append(entry)
    if not day.get('capturedAt'):
        day['capturedAt'] = datetime.now().isoformat()
    _save_json(TRACKER_STORE, store)
    return jsonify({'success': True, 'id': entry['id'], 'duplicate': False, 'entry': _tracker_pick_payload(entry), 'today': _tracker_today_payload(today)})

def api_tracker_pick_patch(pick_id):
    denied = _check_admin_auth()
    if denied:
        return denied
    payload = request.get_json(silent=True) or {}
    date_hint = payload.get('date') or request.args.get('date')
    store = _load_json(TRACKER_STORE, {})
    date_str, day, entries, idx, row = _tracker_find_pick(store, pick_id, date_hint)
    if row is None:
        return jsonify({'success': False, 'error': 'Pick not found'}), 404

    editable = {
        'stakeDollars', 'actual', 'grade', 'status', 'closingPrice', 'closingBookmaker',
        'closingCapturedAt', 'clvEdge', 'profitUnits', 'profitDollars', 'reason',
        'bookmaker', 'bestAvailablePrice', 'bestAvailableBook', 'recommendedSide', 'confidenceTier'
    }
    for key in editable:
        if key in payload:
            row[key] = payload.get(key)

    grade = row.get('grade') or 'pending'
    row['status'] = 'pending' if grade == 'pending' else row.get('status') or 'graded'
    if grade in ('win', 'loss', 'push') and not row.get('gradedAt'):
        row['gradedAt'] = datetime.now(ET).isoformat()

    entries[idx] = row
    day['entries'] = entries
    store[date_str] = day
    _save_json(TRACKER_STORE, store)
    return jsonify({'success': True, 'entry': _tracker_pick_payload(row), 'today': _tracker_today_payload(date_str)})

def api_tracker_pick_delete(pick_id):
    denied = _check_admin_auth()
    if denied:
        return denied
    today = request.args.get('date') or datetime.now(ET).strftime('%Y-%m-%d')
    store = _load_json(TRACKER_STORE, {})
    date_str, day, entries, idx, row = _tracker_find_pick(store, pick_id, today)
    if row is not None:
        day['entries'] = [e for e in entries if e.get('id') != pick_id]
        store[date_str] = day
        _save_json(TRACKER_STORE, store)
        return jsonify({'success': True, 'today': _tracker_today_payload(date_str)})
    return jsonify({'success': False, 'error': 'Pick not found'}), 404

def api_tracker_parlay():
    payload = request.get_json(silent=True) or {}
    legs = payload.get('legs') or []
    if not isinstance(legs, list) or len(legs) < 2:
        return jsonify({'success': False, 'error': 'At least two legs are required'}), 400

    today = payload.get('date') or datetime.now(ET).strftime('%Y-%m-%d')
    parlay_id = payload.get('parlayId') or str(uuid.uuid4())
    combined_prob = 1.0
    combined_ev = 0.0
    for leg in legs:
        try:
            combined_prob *= float(leg.get('adjProb') or 0)
            combined_ev += float(leg.get('evPct') or 0)
        except Exception:
            pass

    entry = {
        'id': parlay_id,
        'date': today,
        'savedAt': datetime.now(ET).isoformat(),
        'source': payload.get('source') or 'tracker',
        'player': payload.get('name') or f"{len(legs)}-Leg Parlay",
        'playerId': None,
        'gamePk': None,
        'team': payload.get('team') or 'MULTI',
        'opp': payload.get('opp') or 'MULTI',
        'marketKey': 'parlay',
        'line': len(legs),
        'recommendedSide': 'Parlay',
        'rawProb': combined_prob,
        'adjProb': combined_prob,
        'modelMean': None,
        'edge': payload.get('edge') or combined_ev,
        'evPct': payload.get('evPct') or combined_ev,
        'hubRating': payload.get('hubRating') or _hub_rating(combined_prob, payload.get('edge') or combined_ev),
        'bookmaker': payload.get('bookmaker'),
        'marketPrice': payload.get('marketPrice'),
        'marketImplied': payload.get('marketImplied'),
        'bestAvailablePrice': payload.get('bestAvailablePrice'),
        'bestAvailableBook': payload.get('bestAvailableBook'),
        'openingPrice': payload.get('openingPrice'),
        'openingImplied': payload.get('openingImplied'),
        'stakeDollars': payload.get('stakeDollars') or 0,
        'kellyFraction': payload.get('kellyFraction') or _get_adjustments().get('kelly_fraction'),
        'confidenceTier': payload.get('confidenceTier') or 'B',
        'status': 'pending',
        'actual': None,
        'grade': 'pending',
        'gradedAt': None,
        'closingPrice': None,
        'closingImplied': None,
        'closingBookmaker': None,
        'closingCapturedAt': None,
        'clvEdge': None,
        'profitDollars': None,
        'profitUnits': None,
        'reason': payload.get('reason') or 'Saved from parlay builder',
        'matchupStorylines': payload.get('matchupStorylines') or [],
        'legs': legs,
        'parlayId': parlay_id,
        'parlayLeg': None,
    }
    store = _load_json(TRACKER_STORE, {})
    day = store.setdefault(today, {'capturedAt': None, 'gradedAt': None, 'closingCapturedAt': None, 'entries': []})
    day['entries'].append(entry)
    if not day.get('capturedAt'):
        day['capturedAt'] = datetime.now(ET).isoformat()
    store[today] = day
    _save_json(TRACKER_STORE, store)
    return jsonify({'success': True, 'id': parlay_id, 'entry': entry, 'today': _tracker_today_payload(today)})

def api_tracker_calibration_apply():
    denied = _check_admin_auth()
    if denied:
        return denied
    payload = request.get_json(silent=True) or {}
    date_str = payload.get('date') or datetime.now().strftime('%Y-%m-%d')
    window = int(payload.get('window', 7) or 7)
    selected = payload.get('markets') or []
    current = _get_adjustments()
    suggestions = _market_calibration(_collect_window_entries(date_str, window), current)
    chosen = [s for s in suggestions if (not selected or s['marketKey'] in selected) and s['action'] != 'hold']
    for row in chosen:
        current['market_multipliers'][row['marketKey']] = row['suggested_multiplier']
    _save_json(ADJUST_STORE, current)
    _append_calibration_history('auto_apply', current, {'date': date_str, 'window': window, 'applied': chosen, 'note': 'Auto-calibration apply'})
    return jsonify({'success': True, 'applied': chosen, 'adjustments': current, 'window': window, 'date': date_str})

def _profit_units_from_american(price):
    try:
        p = float(price)
        if p > 0:
            return round(p / 100.0, 4)
        if p < 0:
            return round(100.0 / abs(p), 4)
    except Exception:
        pass
    return None

def _recalc_tracker_entries(entries):
    for row in entries or []:
        _recalc_tracker_entry(row)
    return entries or []

def api_tracker_close(date_str):
    store = _tracker_store()
    raw_day = store.get(date_str)
    if raw_day is None:
        return jsonify({'success': False, 'error': 'No captured tracker data for this date'}), 404
    day = _normalize_tracker_day(raw_day)
    entries = day.get('entries', [])
    by_game = {}
    for row in entries:
        by_game.setdefault(row.get('gamePk'), []).append(row)
    sched = fetch_schedule(date_str)
    games = {g.get('gamePk'): g for g in sched}
    lock = __import__('threading').Lock()
    updated_count = [0]

    def _close_one(gpk_rows):
        gpk, rows = gpk_rows
        g = games.get(gpk)
        if not g:
            return
        game_status = ((g.get('status') or {}).get('detailedState') or '').lower()
        is_final = 'final' in game_status
        away_name = g.get('teams', {}).get('away', {}).get('team', {}).get('name', '')
        home_name = g.get('teams', {}).get('home', {}).get('team', {}).get('name', '')
        event, _ = _find_odds_event(away_name, home_name)
        if not event:
            return
        try:
            books = _load_event_odds(event.get('id'), featured_only=False)
        except Exception:
            return
        valid_names = set(r.get('player') for r in rows if r.get('player'))
        props = _parse_prop_markets(books, valid_names)
        now_ts = datetime.now().isoformat()
        local_updated = 0
        players = {}
        if is_final:
            try:
                box = requests.get(f"{MLB_API}/game/{gpk}/boxscore", timeout=10).json().get('teams', {})
                for side in ['away', 'home']:
                    players.update((box.get(side) or {}).get('players', {}))
            except Exception:
                players = {}
        for row in rows:
            m = next((x for x in props if x.get('player') == row.get('player') and x.get('market_key') == row.get('marketKey') and float(x.get('line', 0)) == float(row.get('line', 0))), None)
            if not m:
                continue
            row['closingPrice'] = m.get('over_price')
            row['closingBookmaker'] = m.get('bookmaker')
            row['closingCapturedAt'] = now_ts
            # If capture ran in fast mode without odds, backfill opening/market now.
            row['marketPrice'] = m.get('over_price')
            row['bookmaker'] = m.get('bookmaker')
            row['marketImplied'] = m.get('over_implied')
            if row.get('openingPrice') is None:
                row['openingPrice'] = m.get('over_price')
            if row.get('openingImplied') is None:
                row['openingImplied'] = m.get('over_implied')

            if is_final and row.get('grade') == 'pending':
                pobj = None
                pid = row.get('playerId')
                if pid:
                    pobj = players.get(f'ID{pid}')
                if not pobj:
                    for v in players.values():
                        if (v.get('person', {}).get('fullName') or '').lower() == (row.get('player') or '').lower():
                            pobj = v
                            break
                actual = _tracker_stat_from_boxscore(pobj, row.get('marketKey'))
                row['actual'] = actual
                row['grade'] = _grade_over(actual, row.get('line'))
                row['status'] = 'graded'
                row['gradedAt'] = datetime.now(ET).isoformat()

            _recalc_tracker_entry(row)
            local_updated += 1
        with lock:
            updated_count[0] += local_updated

    with ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(_close_one, by_game.items()))

    day['entries'] = _recalc_tracker_entries(entries)
    day['closingCapturedAt'] = datetime.now().isoformat()
    store[date_str] = day
    _save_json(TRACKER_STORE, store)
    return jsonify({'success': True, 'date': date_str, 'updated': updated_count[0], 'entries': day.get('entries', []), 'summary': _tracker_summary(day.get('entries', [])), 'closingCapturedAt': day.get('closingCapturedAt')})

def api_tracker_value_dashboard(date_str):
    window = int(request.args.get('window', 14) or 14)
    adjustments = _get_adjustments()
    markets = list((adjustments.get('market_multipliers') or {}).keys())
    overall = _daily_value_series(date_str, window, None)
    market_series = {mk: _daily_value_series(date_str, window, mk) for mk in markets}
    entries = _collect_window_entries(date_str, window)
    graded = [r for r in entries if r.get('grade') in ('win', 'loss', 'push')]
    top_clv = sorted([r for r in graded if r.get('clvEdge') is not None], key=lambda x: x.get('clvEdge', 0), reverse=True)[:12]
    worst_clv = sorted([r for r in graded if r.get('clvEdge') is not None], key=lambda x: x.get('clvEdge', 0))[:12]
    top_profit = sorted([r for r in graded if r.get('profitUnits') is not None], key=lambda x: x.get('profitUnits', 0), reverse=True)[:12]
    return jsonify({'success': True, 'date': date_str, 'window': window, 'overallSeries': overall, 'marketSeries': market_series, 'availableMarkets': markets, 'windowSummary': _value_summary(entries), 'topCLV': top_clv, 'worstCLV': worst_clv, 'topProfit': top_profit})

def _kelly_fraction(prob, price):
    try:
        p = float(prob)
        odds = float(price)
        if odds > 0:
            b = odds / 100.0
        elif odds < 0:
            b = 100.0 / abs(odds)
        else:
            return 0.0
        q = 1.0 - p
        frac = ((b * p) - q) / b
        return round(max(0.0, frac), 6)
    except Exception:
        return 0.0

def _stake_profile(row, adjustments):
    bankroll = float((adjustments or {}).get('bankroll', 1000.0) or 1000.0)
    unit_size_pct = float((adjustments or {}).get('unit_size_pct', 0.01) or 0.01)
    kelly_fraction = float((adjustments or {}).get('kelly_fraction', 0.50) or 0.50)
    max_bet_pct = float((adjustments or {}).get('max_bet_pct', 0.03) or 0.03)
    price = row.get('openingPrice') if row.get('openingPrice') is not None else row.get('marketPrice')
    prob = row.get('adjProb') if row.get('adjProb') is not None else row.get('rawProb')
    full_kelly = _kelly_fraction(prob, price) if price is not None and prob is not None else 0.0
    sized_pct = min(max_bet_pct, max(0.0, full_kelly * kelly_fraction))
    unit_dollars = bankroll * unit_size_pct
    stake_dollars = round(bankroll * sized_pct, 2)
    stake_units = round(stake_dollars / max(0.01, unit_dollars), 3) if stake_dollars > 0 else 0.0
    return {
        'bankroll': bankroll,
        'unit_dollars': round(unit_dollars, 2),
        'full_kelly_pct': round(full_kelly, 4),
        'stake_pct': round(sized_pct, 4),
        'stake_dollars': stake_dollars,
        'stake_units': round(stake_units, 3),
    }

def _recalc_tracker_entry(row):
    if row.get('openingPrice') is None and row.get('marketPrice') is not None:
        row['openingPrice'] = row.get('marketPrice')
    row['openingImplied'] = _american_to_implied(row.get('openingPrice'))
    if row.get('marketImplied') is None and row.get('marketPrice') is not None:
        row['marketImplied'] = _american_to_implied(row.get('marketPrice'))
    implied_for_edge = row.get('marketImplied') or row.get('openingImplied')
    if row.get('closingPrice') is not None:
        row['closingImplied'] = _american_to_implied(row.get('closingPrice'))
        if implied_for_edge is None:
            implied_for_edge = row.get('closingImplied')
    if row.get('openingImplied') is not None and row.get('closingImplied') is not None:
        row['clvEdge'] = round(float(row['closingImplied']) - float(row['openingImplied']), 4)
    else:
        row.setdefault('clvEdge', None)
    if row.get('edge') is None and row.get('adjProb') is not None and implied_for_edge is not None:
        try:
            row['edge'] = round(float(row['adjProb']) - float(implied_for_edge), 4)
        except Exception:
            row['edge'] = None
    if row.get('evPct') is None and row.get('adjProb') is not None and implied_for_edge not in (None, 0):
        try:
            mi = float(implied_for_edge)
            if mi > 0:
                row['evPct'] = round(float(row['adjProb']) / mi - 1, 4)
        except Exception:
            row['evPct'] = None

    adj = _get_adjustments()
    stake = _stake_profile(row, adj)
    row['fullKellyPct'] = stake['full_kelly_pct']
    row['stakePct'] = stake['stake_pct']
    row['stakeUnits'] = stake['stake_units']
    if row.get('stakeDollars') is None:
        row['stakeDollars'] = stake['stake_dollars']

    if row.get('grade') in ('win', 'loss', 'push'):
        if row.get('grade') == 'win':
            row['profitUnits'] = _profit_units_from_american(row.get('openingPrice'))
        elif row.get('grade') == 'loss':
            row['profitUnits'] = -1.0
        else:
            row['profitUnits'] = 0.0
        row['profitDollars'] = round(float(row.get('profitUnits') or 0) * float(row.get('stakeDollars') or 0), 2)
    else:
        row['profitUnits'] = None
        row['profitDollars'] = None
    return row

def _bankroll_summary(entries, adjustments):
    bankroll = float((adjustments or {}).get('bankroll', 1000.0) or 1000.0)
    unit_size_pct = float((adjustments or {}).get('unit_size_pct', 0.01) or 0.01)
    max_daily_risk_pct = float((adjustments or {}).get('max_daily_risk_pct', 0.12) or 0.12)
    graded = [r for r in entries if r.get('grade') in ('win', 'loss', 'push')]
    planned = [r for r in entries if float(r.get('stakeDollars') or 0) > 0]
    total_staked = round(sum(float(r.get('stakeDollars') or 0) for r in planned), 2)
    total_profit = round(sum(float(r.get('profitDollars') or 0) for r in graded if r.get('profitDollars') is not None), 2)
    live_bankroll = round(bankroll + total_profit, 2)
    daily_cap = round(bankroll * max_daily_risk_pct, 2)
    return {
        'starting_bankroll': round(bankroll, 2),
        'unit_dollars': round(bankroll * unit_size_pct, 2),
        'planned_stake': total_staked,
        'daily_risk_cap': daily_cap,
        'risk_used_pct': round(total_staked / max(0.01, bankroll), 4),
        'over_cap': total_staked > daily_cap,
        'realized_profit': total_profit,
        'live_bankroll': live_bankroll,
    }

def _value_summary(entries):
    graded = [r for r in entries if r.get('grade') in ('win', 'loss', 'push')]
    units = round(sum(float(r.get('profitUnits') or 0) for r in graded if r.get('profitUnits') is not None), 4)
    dollars = round(sum(float(r.get('profitDollars') or 0) for r in graded if r.get('profitDollars') is not None), 2)
    total_staked = round(sum(float(r.get('stakeDollars') or 0) for r in graded if r.get('stakeDollars') is not None), 2)
    roi = round(dollars / max(0.01, total_staked), 4) if graded else 0.0
    clv = [float(r.get('clvEdge')) for r in graded if r.get('clvEdge') is not None]
    avg_clv = round(sum(clv) / max(1, len(clv)), 4) if clv else 0.0
    clv_pos = round(sum(1 for x in clv if x > 0) / max(1, len(clv)), 4) if clv else 0.0
    return {'units': units, 'dollars': dollars, 'staked': total_staked, 'roi': roi, 'avg_clv': avg_clv, 'clv_positive_rate': clv_pos, 'graded_with_clv': len(clv)}

def _daily_value_series(end_date_str, window_days, market_key=None):
    store = _tracker_store()
    dates = list(reversed(_dates_in_window(end_date_str, window_days)))
    series = []
    for ds in dates:
        rows = list((store.get(ds) or {}).get('entries', []) or [])
        if market_key:
            rows = [r for r in rows if r.get('marketKey') == market_key]
        graded = [r for r in rows if r.get('grade') in ('win', 'loss', 'push')]
        staked = round(sum(float(r.get('stakeDollars') or 0) for r in graded if r.get('stakeDollars') is not None), 2)
        units = round(sum(float(r.get('profitUnits') or 0) for r in graded if r.get('profitUnits') is not None), 4)
        dollars = round(sum(float(r.get('profitDollars') or 0) for r in graded if r.get('profitDollars') is not None), 2)
        roi = round(dollars / max(0.01, staked), 4) if graded and staked > 0 else None
        clv = [float(r.get('clvEdge')) for r in graded if r.get('clvEdge') is not None]
        avg_clv = round(sum(clv) / max(1, len(clv)), 4) if clv else None
        clv_pos = round(sum(1 for x in clv if x > 0) / max(1, len(clv)), 4) if clv else None
        series.append({'date': ds, 'staked': staked, 'units': units, 'dollars': dollars, 'roi': roi, 'avg_clv': avg_clv, 'clv_pos_rate': clv_pos})
    return series

def _tracker_summary(entries):
    total = len(entries)
    graded = [x for x in entries if x.get('grade') in ('win', 'loss', 'push')]
    wins = sum(1 for x in graded if x.get('grade') == 'win')
    losses = sum(1 for x in graded if x.get('grade') == 'loss')
    pushes = sum(1 for x in graded if x.get('grade') == 'push')
    hit_rate = round(wins / max(1, wins + losses), 3) if graded else 0.0
    by_market = {}
    for x in entries:
        mk = x.get('marketKey')
        by_market.setdefault(mk, {'picks': 0, 'wins': 0, 'losses': 0, 'pushes': 0, 'units': 0.0, 'dollars': 0.0})
        by_market[mk]['picks'] += 1
        if x.get('grade') == 'win':
            by_market[mk]['wins'] += 1
        elif x.get('grade') == 'loss':
            by_market[mk]['losses'] += 1
        elif x.get('grade') == 'push':
            by_market[mk]['pushes'] += 1
        if x.get('profitUnits') is not None:
            by_market[mk]['units'] = round(by_market[mk]['units'] + float(x.get('profitUnits') or 0), 4)
        if x.get('profitDollars') is not None:
            by_market[mk]['dollars'] = round(by_market[mk]['dollars'] + float(x.get('profitDollars') or 0), 2)
    adjustments = _get_adjustments()
    return {
        'picks': total, 'graded': len(graded), 'wins': wins, 'losses': losses, 'pushes': pushes,
        'hit_rate': hit_rate, 'by_market': by_market, 'value': _value_summary(entries), 'bankroll': _bankroll_summary(entries, adjustments)
    }

def _portfolio_plan(entries, adjustments):
    bankroll = float((adjustments or {}).get('bankroll', 1000.0) or 1000.0)
    daily_cap = bankroll * float((adjustments or {}).get('max_daily_risk_pct', 0.12) or 0.12)
    team_cap = bankroll * float((adjustments or {}).get('max_team_exposure_pct', 0.05) or 0.05)
    market_cap = bankroll * float((adjustments or {}).get('max_market_exposure_pct', 0.05) or 0.05)
    game_cap = bankroll * float((adjustments or {}).get('max_game_exposure_pct', 0.08) or 0.08)
    edge_gate = float((adjustments or {}).get('best_edge_threshold', 0.03) or 0.03)
    prob_gate = float((adjustments or {}).get('best_prob_threshold', 0.58) or 0.58)

    ranked = sorted(entries or [], key=lambda x: (float(x.get('edge') or -999), float(x.get('adjProb') or 0), float(x.get('stakeDollars') or 0)), reverse=True)
    accepted, rejected = [], []
    team_risk, market_risk, game_risk = {}, {}, {}
    total_risk = 0.0

    for row in ranked:
        stake = float(row.get('stakeDollars') or 0)
        edge = row.get('edge')
        prob = float(row.get('adjProb') or row.get('rawProb') or 0)
        game_key = str(row.get('gamePk'))
        team_key = row.get('team') or 'NA'
        market_key = row.get('marketKey') or 'NA'
        reason = None

        if stake <= 0:
            reason = 'No positive Kelly stake.'
        elif edge is not None:
            if float(edge) < edge_gate:
                reason = f'Edge below best-bet threshold ({edge_gate:.1%}).'
        elif prob < prob_gate:
            reason = f'Probability below fallback gate ({prob_gate:.1%}).'
        elif total_risk + stake > daily_cap + 1e-9:
            reason = 'Would breach daily risk cap.'
        elif team_risk.get(team_key, 0.0) + stake > team_cap + 1e-9:
            reason = 'Would breach team exposure cap.'
        elif market_risk.get(market_key, 0.0) + stake > market_cap + 1e-9:
            reason = 'Would breach market exposure cap.'
        elif game_risk.get(game_key, 0.0) + stake > game_cap + 1e-9:
            reason = 'Would breach game exposure cap.'

        item = dict(row)
        if reason is None:
            total_risk += stake
            team_risk[team_key] = round(team_risk.get(team_key, 0.0) + stake, 2)
            market_risk[market_key] = round(market_risk.get(market_key, 0.0) + stake, 2)
            game_risk[game_key] = round(game_risk.get(game_key, 0.0) + stake, 2)
            item['portfolioStatus'] = 'accepted'
            accepted.append(item)
        else:
            item['portfolioStatus'] = 'rejected'
            item['portfolioReason'] = reason
            rejected.append(item)

    accepted_profit = round(sum(float(x.get('profitDollars') or 0) for x in accepted if x.get('profitDollars') is not None), 2)
    accepted_graded = [x for x in accepted if x.get('grade') in ('win', 'loss', 'push')]
    accepted_roi = round(accepted_profit / max(0.01, sum(float(x.get('stakeDollars') or 0) for x in accepted_graded)), 4) if accepted_graded else 0.0

    def _top_exposure(d):
        return sorted([{'key': k, 'risk': round(v,2), 'risk_pct': round(v/max(0.01, bankroll),4)} for k,v in d.items()], key=lambda x: x['risk'], reverse=True)[:12]

    return {
        'summary': {
            'accepted_count': len(accepted),
            'rejected_count': len(rejected),
            'accepted_risk': round(total_risk, 2),
            'remaining_risk': round(max(0.0, daily_cap - total_risk), 2),
            'daily_cap': round(daily_cap, 2),
            'accepted_profit': accepted_profit,
            'accepted_roi': accepted_roi,
            'team_cap': round(team_cap, 2),
            'market_cap': round(market_cap, 2),
            'game_cap': round(game_cap, 2),
        },
        'accepted': accepted[:40],
        'rejected': rejected[:40],
        'exposure': {
            'teams': _top_exposure(team_risk),
            'markets': _top_exposure(market_risk),
            'games': _top_exposure(game_risk),
        }
    }

def api_tracker_portfolio(date_str):
    store = _tracker_store()
    day = _normalize_tracker_day(store.get(date_str))
    entries = _recalc_tracker_entries(day.get('entries', []))
    adjustments = _get_adjustments()
    return jsonify({
        'success': True,
        'date': date_str,
        'adjustments': adjustments,
        'portfolio': _portfolio_plan(entries, adjustments),
    })

def _compute_bvp_grade(bvp_data):
    """
    Grade batter vs pitcher matchup based on Sprint 2.1 rules.
    Uses OPS ratio vs batter season OPS with PA thresholds.
    """
    if not bvp_data or not bvp_data.get('success'):
        return 'D'

    pa = bvp_data.get('pa', 0)
    ratio = bvp_data.get('ops_ratio')
    if ratio is None:
        return 'D'

    if ratio >= 1.40 and pa >= 20:
        return 'A+'
    if ratio >= 1.20 and pa >= 15:
        return 'A'
    if ratio >= 1.05 and pa >= 10:
        return 'B'
    if ratio >= 0.85:
        return 'C'
    return 'D'

def _cheatsheet_matchup_grade(score_tier, pitch_adv=None, bvp_grade=None, bvp_pa=0):
    ladder = ['D', 'C', 'B', 'A', 'A+']
    tier = str(score_tier or 'C').upper()
    base_idx = {
        'A+': 4,
        'A': 3,
        'B': 2,
        'C': 1,
        'D': 0,
    }.get(tier, 1)

    status = (pitch_adv or {}).get('status', 'neutral').lower()
    if status == 'favorable':
        base_idx += 1
    elif status == 'unfavorable':
        base_idx -= 1

    pa = int(bvp_pa or 0)
    bvp = (bvp_grade or '').upper()
    if pa >= 10 and bvp in ('A+', 'A'):
        base_idx += 1
    elif pa >= 15 and bvp == 'D':
        base_idx -= 1

    return ladder[max(0, min(len(ladder) - 1, base_idx))]

def _confidence_tier(row):
    edge = float(row.get('edge') or 0)
    prob = float(row.get('adjProb') or row.get('rawProb') or 0)
    clv = float(row.get('clvEdge') or 0) if row.get('clvEdge') is not None else None
    if edge >= 0.09 and prob >= 0.67:
        return 'A'
    if edge >= 0.06 and prob >= 0.62:
        return 'B'
    if edge >= 0.04 and prob >= 0.58:
        return 'C'
    return 'D'

def _card_label(row):
    return f"{row.get('player')} OVER {row.get('line')} {row.get('marketKey')}"

def _market_sort_key(row):
    return (-float(row.get('edge') or 0), -float(row.get('adjProb') or 0), -float(row.get('stakeDollars') or 0))

def _build_bet_slip(entries, adjustments):
    plan = _portfolio_plan(entries, adjustments)
    accepted = list(plan.get('accepted', []))
    accepted.sort(key=_market_sort_key)
    singles = []
    for rank, row in enumerate(accepted, start=1):
        item = dict(row)
        item['rank'] = rank
        item['confidenceTier'] = _confidence_tier(item)
        item['cardLabel'] = _card_label(item)
        singles.append(item)

    core = [x for x in singles if x.get('confidenceTier') in ('A', 'B')][:5]
    flex = [x for x in singles if x.get('confidenceTier') in ('C', 'D')][:8]

    top2 = singles[:2]
    top3 = singles[:3]
    top4 = singles[:4]

    def _parlay(items, name):
        if len(items) < 2:
            return None
        total_risk = round(sum(float(x.get('stakeDollars') or 0) for x in items), 2)
        avg_edge = round(sum(float(x.get('edge') or 0) for x in items) / max(1, len(items)), 4)
        avg_prob = round(sum(float(x.get('adjProb') or 0) for x in items) / max(1, len(items)), 4)
        return {
            'name': name,
            'legs': [{'player': x.get('player'), 'marketKey': x.get('marketKey'), 'line': x.get('line'), 'team': x.get('team')} for x in items],
            'avg_edge': avg_edge,
            'avg_prob': avg_prob,
            'proxy_risk': total_risk,
        }

    parlays = [x for x in [_parlay(top2, 'Top 2 Lean Pair'), _parlay(top3, 'Top 3 Ladder'), _parlay(top4, 'Top 4 Longshot Mix')] if x]

    total_risk = round(sum(float(x.get('stakeDollars') or 0) for x in singles), 2)
    total_profit = round(sum(float(x.get('profitDollars') or 0) for x in singles if x.get('profitDollars') is not None), 2)
    by_tier = {}
    for x in singles:
        t = x.get('confidenceTier')
        by_tier.setdefault(t, {'count': 0, 'risk': 0.0})
        by_tier[t]['count'] += 1
        by_tier[t]['risk'] = round(by_tier[t]['risk'] + float(x.get('stakeDollars') or 0), 2)

    return {
        'summary': {
            'recommended_bets': len(singles),
            'core_bets': len(core),
            'flex_bets': len(flex),
            'total_risk': total_risk,
            'realized_profit': total_profit,
            'avg_edge': round(sum(float(x.get('edge') or 0) for x in singles) / max(1, len(singles)), 4) if singles else 0.0,
            'avg_prob': round(sum(float(x.get('adjProb') or 0) for x in singles) / max(1, len(singles)), 4) if singles else 0.0,
            'by_tier': by_tier,
        },
        'singles': singles[:20],
        'core': core,
        'flex': flex,
        'parlays': parlays,
        'portfolio': plan,
    }

def api_tracker_betslip(date_str):
    store = _tracker_store()
    day = _normalize_tracker_day(store.get(date_str))
    entries = _recalc_tracker_entries(day.get('entries', []))
    adjustments = _get_adjustments()
    return jsonify({
        'success': True,
        'date': date_str,
        'adjustments': adjustments,
        'betslip': _build_bet_slip(entries, adjustments),
    })

def _audit_bucket_init():
    return {'bets': 0, 'graded': 0, 'wins': 0, 'losses': 0, 'pushes': 0, 'risk': 0.0, 'profit': 0.0}

def _audit_bucket_add(bucket, row):
    bucket['bets'] += 1
    bucket['risk'] = round(bucket['risk'] + float(row.get('stakeDollars') or 0), 2)
    if row.get('grade') in ('win', 'loss', 'push'):
        bucket['graded'] += 1
        if row.get('grade') == 'win':
            bucket['wins'] += 1
        elif row.get('grade') == 'loss':
            bucket['losses'] += 1
        elif row.get('grade') == 'push':
            bucket['pushes'] += 1
        bucket['profit'] = round(bucket['profit'] + float(row.get('profitDollars') or 0), 2)

def _audit_bucket_finalize(bucket):
    graded_non_push = bucket['wins'] + bucket['losses']
    bucket['hit_rate'] = round(bucket['wins'] / max(1, graded_non_push), 4) if bucket['graded'] else 0.0
    bucket['roi'] = round(bucket['profit'] / max(0.01, bucket['risk']), 4) if bucket['risk'] > 0 else 0.0
    return bucket

def _bankroll_curve_dashboard(end_date_str, window_days):
    adjustments = _get_adjustments()
    bankroll = float((adjustments or {}).get('bankroll', 1000.0) or 1000.0)
    store = _tracker_store()
    dates = list(reversed(_dates_in_window(end_date_str, window_days)))
    roll = bankroll
    curve = []
    tier_audit = {k: _audit_bucket_init() for k in ['A', 'B', 'C', 'D']}
    card_audit = {k: _audit_bucket_init() for k in ['core', 'flex', 'all_singles']}

    for ds in dates:
        day = _normalize_tracker_day(store.get(ds))
        entries = _recalc_tracker_entries(day.get('entries', []))
        slip = _build_bet_slip(entries, adjustments)
        singles = slip.get('singles', [])
        core_ids = set((x.get('player'), x.get('marketKey'), x.get('line')) for x in slip.get('core', []))
        daily_profit = round(sum(float(x.get('profitDollars') or 0) for x in singles if x.get('grade') in ('win', 'loss', 'push') and x.get('profitDollars') is not None), 2)
        daily_risk = round(sum(float(x.get('stakeDollars') or 0) for x in singles), 2)
        graded = [x for x in singles if x.get('grade') in ('win', 'loss', 'push')]
        daily_roi = round(daily_profit / max(0.01, sum(float(x.get('stakeDollars') or 0) for x in graded)), 4) if graded else 0.0
        daily_hit = round(sum(1 for x in graded if x.get('grade') == 'win') / max(1, sum(1 for x in graded if x.get('grade') in ('win', 'loss'))), 4) if graded else 0.0
        roll = round(roll + daily_profit, 2)
        curve.append({'date': ds, 'profit': daily_profit, 'risk': daily_risk, 'roi': daily_roi, 'hit_rate': daily_hit, 'bankroll': roll, 'bets': len(singles)})

        for row in singles:
            tier = row.get('confidenceTier', 'D')
            if tier not in tier_audit:
                tier_audit[tier] = _audit_bucket_init()
            _audit_bucket_add(tier_audit[tier], row)
            _audit_bucket_add(card_audit['all_singles'], row)
            key = (row.get('player'), row.get('marketKey'), row.get('line'))
            if key in core_ids:
                _audit_bucket_add(card_audit['core'], row)
            else:
                _audit_bucket_add(card_audit['flex'], row)

    tier_rows = [{'bucket': k, **_audit_bucket_finalize(v)} for k, v in tier_audit.items()]
    card_rows = [{'bucket': k, **_audit_bucket_finalize(v)} for k, v in card_audit.items()]
    return {
        'summary': {
            'window_days': window_days,
            'start_bankroll': bankroll,
            'end_bankroll': round(curve[-1]['bankroll'], 2) if curve else bankroll,
            'total_profit': round(sum(x['profit'] for x in curve), 2),
            'avg_daily_roi': round(sum(x['roi'] for x in curve) / max(1, len(curve)), 4) if curve else 0.0,
            'active_days': sum(1 for x in curve if x['bets'] > 0),
        },
        'curve': curve,
        'tierAudit': tier_rows,
        'cardAudit': card_rows,
    }

def api_tracker_bankroll_dashboard(date_str):
    window = int(request.args.get('window', 14) or 14)
    return jsonify({'success': True, 'date': date_str, 'window': window, 'dashboard': _bankroll_curve_dashboard(date_str, window)})

def _attr_bucket_init():
    return {'bets': 0, 'graded': 0, 'wins': 0, 'losses': 0, 'pushes': 0, 'risk': 0.0, 'profit': 0.0, 'clv_sum': 0.0, 'clv_n': 0, 'edge_sum': 0.0, 'prob_sum': 0.0}

def _attr_bucket_add(bucket, row):
    bucket['bets'] += 1
    bucket['risk'] = round(bucket['risk'] + float(row.get('stakeDollars') or 0), 2)
    bucket['edge_sum'] = round(bucket['edge_sum'] + float(row.get('edge') or 0), 6)
    bucket['prob_sum'] = round(bucket['prob_sum'] + float(row.get('adjProb') or row.get('rawProb') or 0), 6)
    if row.get('clvEdge') is not None:
        bucket['clv_sum'] = round(bucket['clv_sum'] + float(row.get('clvEdge') or 0), 6)
        bucket['clv_n'] += 1
    if row.get('grade') in ('win', 'loss', 'push'):
        bucket['graded'] += 1
        if row.get('grade') == 'win':
            bucket['wins'] += 1
        elif row.get('grade') == 'loss':
            bucket['losses'] += 1
        else:
            bucket['pushes'] += 1
        bucket['profit'] = round(bucket['profit'] + float(row.get('profitDollars') or 0), 2)

def _attr_bucket_finalize(name, bucket):
    graded_non_push = bucket['wins'] + bucket['losses']
    return {
        'bucket': name,
        'bets': bucket['bets'],
        'graded': bucket['graded'],
        'wins': bucket['wins'],
        'losses': bucket['losses'],
        'pushes': bucket['pushes'],
        'risk': round(bucket['risk'], 2),
        'profit': round(bucket['profit'], 2),
        'roi': round(bucket['profit'] / max(0.01, bucket['risk']), 4) if bucket['risk'] > 0 else 0.0,
        'hit_rate': round(bucket['wins'] / max(1, graded_non_push), 4) if graded_non_push else 0.0,
        'avg_clv': round(bucket['clv_sum'] / max(1, bucket['clv_n']), 4) if bucket['clv_n'] else 0.0,
        'avg_edge': round(bucket['edge_sum'] / max(1, bucket['bets']), 4) if bucket['bets'] else 0.0,
        'avg_prob': round(bucket['prob_sum'] / max(1, bucket['bets']), 4) if bucket['bets'] else 0.0,
        'clv_samples': bucket['clv_n'],
    }

def _attribution_dashboard(end_date_str, window_days):
    adjustments = _get_adjustments()
    store = _tracker_store()
    dates = list(reversed(_dates_in_window(end_date_str, window_days)))
    market_buckets = {}
    tier_buckets = {k: _attr_bucket_init() for k in ['A', 'B', 'C', 'D']}
    overall = _attr_bucket_init()
    daily = []

    for ds in dates:
        day = _normalize_tracker_day(store.get(ds))
        entries = _recalc_tracker_entries(day.get('entries', []))
        slip = _build_bet_slip(entries, adjustments)
        singles = slip.get('singles', [])
        day_bucket = _attr_bucket_init()
        for row in singles:
            _attr_bucket_add(overall, row)
            _attr_bucket_add(day_bucket, row)
            mk = row.get('marketKey') or 'unknown'
            market_buckets.setdefault(mk, _attr_bucket_init())
            _attr_bucket_add(market_buckets[mk], row)
            tier = row.get('confidenceTier', 'D')
            tier_buckets.setdefault(tier, _attr_bucket_init())
            _attr_bucket_add(tier_buckets[tier], row)
        daily.append({'date': ds, **_attr_bucket_finalize(ds, day_bucket)})

    overall_row = _attr_bucket_finalize('overall', overall)
    market_rows = sorted([_attr_bucket_finalize(k, v) for k, v in market_buckets.items()], key=lambda x: (x['profit'], x['avg_clv'], x['bets']), reverse=True)
    tier_rows = [_attr_bucket_finalize(k, tier_buckets.get(k, _attr_bucket_init())) for k in ['A', 'B', 'C', 'D']]
    strongest = [x for x in market_rows if x['avg_clv'] > 0 and x['roi'] > 0][:8]
    weakest = sorted([x for x in market_rows if x['graded'] > 0], key=lambda x: (x['roi'], x['avg_clv']))[:8]
    return {
        'summary': {
            'graded': overall_row['graded'],
            'bets': overall_row['bets'],
            'risk': overall_row['risk'],
            'profit': overall_row['profit'],
            'roi': overall_row['roi'],
            'avg_clv': overall_row['avg_clv'],
            'positive_clv_rate': round(sum(1 for d in daily if d.get('avg_clv', 0) > 0) / max(1, len(daily)), 4) if daily else 0.0,
            'avg_edge': overall_row['avg_edge'],
            'avg_prob': overall_row['avg_prob'],
        },
        'daily': daily,
        'marketAudit': market_rows,
        'tierAudit': tier_rows,
        'strongestMarkets': strongest,
        'weakestMarkets': weakest,
    }

def api_tracker_attribution_dashboard(date_str):
    window = int(request.args.get('window', 14) or 14)
    return jsonify({'success': True, 'date': date_str, 'window': window, 'dashboard': _attribution_dashboard(date_str, window)})

def api_tracker_entries():
    try:
        date   = request.args.get("date", datetime.now(ET).strftime("%Y-%m-%d"))
        gamePk = request.args.get("gamePk")

        store = {}
        if os.path.exists(TRACKER_STORE):
            with open(TRACKER_STORE) as f:
                store = json.load(f)

        day     = _normalize_tracker_day(store.get(date))
        entries = day.get("entries", [])

        if gamePk:
            try:
                pk_int  = int(gamePk)
                entries = [e for e in entries
                           if e.get("gamePk") == pk_int or str(e.get("gamePk")) == str(pk_int)]
            except (ValueError, TypeError):
                pass

        return jsonify({"success": True, "date": date, "entries": entries, "total": len(entries)})

    except Exception as ex:
        print(f"[api_tracker_entries] {traceback.format_exc()}")
        return jsonify({"success": False, "error": str(ex)}), 500

def register_tracker_routes(app):
    app.add_url_rule('/api/tracker/adjustments', view_func=api_tracker_adjustments, methods=['GET', 'POST'])
    app.add_url_rule('/api/tracker/date/<date_str>', view_func=api_tracker_date)
    app.add_url_rule('/api/tracker/capture/<date_str>', view_func=api_tracker_capture, methods=['POST'])
    app.add_url_rule('/api/tracker/grade/<date_str>', view_func=api_tracker_grade, methods=['POST'])
    app.add_url_rule('/api/tracker/calibration/dashboard/<date_str>', view_func=api_tracker_calibration_dashboard)
    app.add_url_rule('/api/tracker/calibration/<date_str>', view_func=api_tracker_calibration)
    app.add_url_rule('/api/tracker/model-record', view_func=api_tracker_model_record)
    app.add_url_rule('/api/tracker/export/<date_str>', view_func=api_tracker_export)
    app.add_url_rule('/api/tracker/today', view_func=api_tracker_today)
    app.add_url_rule('/api/tracker/performance', view_func=api_tracker_performance)
    app.add_url_rule('/api/tracker/backtest', view_func=api_tracker_backtest)
    app.add_url_rule('/api/tracker/settings', view_func=api_tracker_settings, methods=['GET', 'POST'])
    app.add_url_rule('/api/tracker/pick', view_func=api_tracker_pick, methods=['POST'])
    app.add_url_rule('/api/tracker/pick/<pick_id>', view_func=api_tracker_pick_patch, methods=['PATCH'])
    app.add_url_rule('/api/tracker/pick/<pick_id>', view_func=api_tracker_pick_delete, methods=['DELETE'])
    app.add_url_rule('/api/tracker/parlay', view_func=api_tracker_parlay, methods=['POST'])
    app.add_url_rule('/api/tracker/calibration/apply', view_func=api_tracker_calibration_apply, methods=['POST'])
    app.add_url_rule('/api/tracker/close/<date_str>', view_func=api_tracker_close, methods=['POST'])
    app.add_url_rule('/api/tracker/value/dashboard/<date_str>', view_func=api_tracker_value_dashboard)
    app.add_url_rule('/api/tracker/portfolio/<date_str>', view_func=api_tracker_portfolio)
    app.add_url_rule('/api/tracker/betslip/<date_str>', view_func=api_tracker_betslip)
    app.add_url_rule('/api/tracker/bankroll/dashboard/<date_str>', view_func=api_tracker_bankroll_dashboard)
    app.add_url_rule('/api/tracker/attribution/dashboard/<date_str>', view_func=api_tracker_attribution_dashboard)
    app.add_url_rule('/api/tracker/entries', view_func=api_tracker_entries)


__all__ = ['configure_tracker_context', 'register_tracker_routes', '_append_calibration_history', '_history_in_window', '_daily_series', '_multiplier_history', '_default_adjustments', '_get_adjustments', '_market_mult', '_clamp01', '_tracker_stat_from_boxscore', '_grade_over', '_grade_side', '_hub_rating', '_projection_reason_short', '_build_tracker_rows_for_game', '_build_tracker_rows_quick', '_tracker_row_key', '_merge_tracker_entries', '_tracker_capture_continue_bg', '_tracker_auto_sync_once', '_start_tracker_auto_sync_worker', 'api_tracker_adjustments', 'api_tracker_date', 'api_tracker_capture', 'api_tracker_grade', '_tracker_store', '_coerce_tracker_entries', '_normalize_tracker_day', '_dates_in_window', '_collect_window_entries', '_market_calibration', '_overall_window_summary', '_tracker_side_label', '_tracker_live_summary', '_tracker_find_pick', '_tracker_pick_payload', '_tracker_today_payload', '_tracker_performance_payload', '_tracker_backtest_payload', '_tracker_export_rows', '_tracker_export_csv_text', '_pdf_escape', '_pdf_wrap_lines', '_simple_pdf_bytes', '_tracker_export_pdf_bytes', 'api_tracker_calibration_dashboard', 'api_tracker_calibration', 'api_tracker_model_record', 'api_tracker_export', 'api_tracker_today', 'api_tracker_performance', 'api_tracker_backtest', 'api_tracker_settings', 'api_tracker_pick', 'api_tracker_pick_patch', 'api_tracker_pick_delete', 'api_tracker_parlay', 'api_tracker_calibration_apply', '_profit_units_from_american', '_recalc_tracker_entries', 'api_tracker_close', 'api_tracker_value_dashboard', '_kelly_fraction', '_stake_profile', '_recalc_tracker_entry', '_bankroll_summary', '_value_summary', '_daily_value_series', '_tracker_summary', '_portfolio_plan', 'api_tracker_portfolio', '_compute_bvp_grade', '_cheatsheet_matchup_grade', '_confidence_tier', '_card_label', '_market_sort_key', '_build_bet_slip', 'api_tracker_betslip', '_audit_bucket_init', '_audit_bucket_add', '_audit_bucket_finalize', '_bankroll_curve_dashboard', 'api_tracker_bankroll_dashboard', '_attr_bucket_init', '_attr_bucket_add', '_attr_bucket_finalize', '_attribution_dashboard', 'api_tracker_attribution_dashboard', 'api_tracker_entries']
