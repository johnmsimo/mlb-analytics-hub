"""Monte Carlo game simulation helpers and route handlers."""

import statistics
import traceback

import requests
from flask import jsonify, request


DEFAULT_SIMS = 1_200
SUMMARY_SIMS = 500
BACKTEST_SIMS = 2_000


def configure_simulation_context(namespace):
    globals().update(namespace)


def _simulate_offense(lineup, opp_starter, opp_team_id, park, rng, batx_map=None,
                      wx_mults=None, ump_adj=None, relief_fatigue_woba: float = 0.0):
    """
    batx_map: dict {batter_index: batx_dict} pre-computed by _batx_for_sim().
    When provided, BAT X multipliers are applied to _derive_probs() output for
    every PA, making each simulated at-bat matchup-aware (platoon, form, BvP, etc).
    """
    stats = [_blank_batter_line(b) for b in lineup]
    starter_line = _blank_pitcher_line(opp_starter['name'])
    tiers = _bullpen_tiers(opp_starter, opp_team_id)
    relief_lines = {'Closer': _blank_pitcher_line('Closer'), 'Setup': _blank_pitcher_line('Setup'), 'Middle': _blank_pitcher_line('Middle')}
    starter_target = _starter_outs_target(opp_starter, rng)
    runs = 0; batter_ptr = 0
    pitcher_batters_faced = {}
    pitcher_pitch_counts = {}
    arsenal_cache = {}

    def _arsenal_size(pitcher_model):
        pitcher_name = pitcher_model.get('name', '')
        if pitcher_name in arsenal_cache:
            return arsenal_cache[pitcher_name]
        arsenal = (sv_pitcher(pitcher_name).get('sv_arsenal_pct') or {}) if pitcher_name else {}
        arsenal_size = len(arsenal.keys()) if isinstance(arsenal, dict) and arsenal else 3
        arsenal_cache[pitcher_name] = arsenal_size or 3
        return arsenal_cache[pitcher_name]

    _is_relief = {False}  # mutable flag: True once starter exits

    def _pa_probs(batter, pitcher_model, batx_adjustments, is_relief=False):
        pitcher_name = pitcher_model.get('name', 'Unknown')
        batters_faced = pitcher_batters_faced.get(pitcher_name, 0)
        pitches_thrown = pitcher_pitch_counts.get(pitcher_name, 0)
        ttop_val = ttop_woba_penalty(batters_faced, pitches_thrown, _arsenal_size(pitcher_model))
        # Apply bullpen fatigue wOBA penalty for relief appearances
        extra_penalty = relief_fatigue_woba if is_relief else 0.0
        probs = derive_probs_v2(
            batter,
            pitcher_model,
            park,
            batx=batx_adjustments,
            wx_mults=wx_mults,
            ump_adj=ump_adj,
            ttop_penalty_val=ttop_val + extra_penalty,
        )
        pitcher_batters_faced[pitcher_name] = batters_faced + 1
        pitcher_pitch_counts[pitcher_name] = pitches_thrown + 5
        return _normalize_pa_probs(probs)

    # Pre-compute probs against starter with BAT X; relief uses same BAT X but fresh probs
    probs_cache = {}
    for i, b in enumerate(lineup):
        bx = batx_map.get(i) if batx_map else None
        probs_cache[i] = _pa_probs(b, opp_starter, bx, is_relief=False)
    for inning in range(1, 10):
        outs = 0
        bases = [None, None, None]
        while outs < 3:
            steal_outcome = _maybe_steal(bases, lineup, stats, rng, probs_cache, outs)
            if steal_outcome == -1:
                outs += 1
                if starter_line['outs'] < starter_target:
                    starter_line['outs'] += 1
                else:
                    tier0 = _select_relief_tier(inning, runs, starter_line['outs'], tiers)
                    relief_lines[tier0['name']]['outs'] += 1
                if outs >= 3:
                    break
            b = lineup[batter_ptr]
            s = stats[batter_ptr]
            _starter_pitches = pitcher_pitch_counts.get(opp_starter.get('name', ''), 0)
            _pitch_limit = 100 + (5 if _safe_f(opp_starter.get('fg_ip'), 0) > 120 else 0)  # 100 avg, 105 workhorse
            use_starter = (starter_line['outs'] < starter_target) and (_starter_pitches < _pitch_limit)
            if use_starter:
                pm = opp_starter
                pl = starter_line
            else:
                pm = _select_relief_tier(inning, runs, starter_line['outs'], tiers)
                pl = relief_lines[pm['name']]
            bx = batx_map.get(batter_ptr) if batx_map else None
            probs = _pa_probs(b, pm, bx, is_relief=not use_starter)
            probs_cache[batter_ptr] = probs
            ev = _pick_event(probs, rng)
            s['pa'] += 1
            if ev == 'bb':
                s['bb'] += 1; pl['bb'] += 1
                runs += _advance_walk(bases, batter_ptr, stats, pl)
            elif ev in ('1b', '2b', '3b', 'hr'):
                s['ab'] += 1; s['h'] += 1; pl['h'] += 1
                if ev == '1b': s['1b'] += 1; s['tb'] += 1
                elif ev == '2b': s['2b'] += 1; s['tb'] += 2
                elif ev == '3b': s['3b'] += 1; s['tb'] += 3
                elif ev == 'hr': s['hr'] += 1; s['tb'] += 4; pl['hr'] += 1
                runs += _advance_hit(ev, bases, batter_ptr, stats, pl, rng, outs)
            else:
                s['ab'] += 1; outs += 1; pl['outs'] += 1
                if ev == 'k': s['k'] += 1; pl['k'] += 1
            batter_ptr = (batter_ptr + 1) % len(lineup)
    bullpen_tot = _blank_pitcher_line('Bullpen')
    for rl in relief_lines.values():
        bullpen_tot['outs'] += rl['outs']; bullpen_tot['h'] += rl['h']; bullpen_tot['er'] += rl['er']; bullpen_tot['bb'] += rl['bb']; bullpen_tot['k'] += rl['k']; bullpen_tot['hr'] += rl['hr']
    return {
        'batters': stats,
        'starter': starter_line,
        'bullpen': bullpen_tot,
        'relief_lines': relief_lines,
        'bullpen_models': tiers,
        'runs': runs,
        'hits': sum(x['h'] for x in stats),
        'bb': sum(x['bb'] for x in stats),
        'k': sum(x['k'] for x in stats),
        'tb': sum(x['tb'] for x in stats),
        'sb': sum(x['sb'] for x in stats),
    }

def _summarize_player(lines):
    def arr(k): return [x[k] for x in lines]
    hits = arr('h'); hr = arr('hr'); rbi = arr('rbi'); runs = arr('r'); bb = arr('bb'); k = arr('k'); tb = arr('tb'); sb = arr('sb')
    hrr = [(x.get('h', 0) + x.get('r', 0) + x.get('rbi', 0)) for x in lines]
    return {
        'mean_hits': round(statistics.mean(hits), 3), 'median_hits': statistics.median(hits),
        'mean_hr': round(statistics.mean(hr), 3), 'mean_rbi': round(statistics.mean(rbi), 3),
        'mean_runs': round(statistics.mean(runs), 3), 'mean_bb': round(statistics.mean(bb), 3),
        'mean_k': round(statistics.mean(k), 3), 'mean_tb': round(statistics.mean(tb), 3), 'mean_sb': round(statistics.mean(sb), 3),
        'mean_hrr': round(statistics.mean(hrr), 3),
        'p10_hits': round(_pct(hits, 0.10), 2), 'p50_hits': round(_pct(hits, 0.50), 2), 'p90_hits': round(_pct(hits, 0.90), 2),
        'p10_tb': round(_pct(tb, 0.10), 2), 'p50_tb': round(_pct(tb, 0.50), 2), 'p90_tb': round(_pct(tb, 0.90), 2),
        'p10_k': round(_pct(k, 0.10), 2), 'p50_k': round(_pct(k, 0.50), 2), 'p90_k': round(_pct(k, 0.90), 2),
        'p_1plus_hit': round(sum(1 for x in hits if x >= 1) / len(hits), 3),
        'p_2plus_hit': round(sum(1 for x in hits if x >= 2) / len(hits), 3),
        'p_1plus_hr': round(sum(1 for x in hr if x >= 1) / len(hr), 3),
        'p_1plus_rbi': round(sum(1 for x in rbi if x >= 1) / len(rbi), 3),
        'p_1plus_run': round(sum(1 for x in runs if x >= 1) / len(runs), 3),
        'p_2plus_tb': round(sum(1 for x in tb if x >= 2) / len(tb), 3),
        'p_1plus_bb': round(sum(1 for x in bb if x >= 1) / len(bb), 3),
        'p_1plus_k': round(sum(1 for x in k if x >= 1) / len(k), 3),
        'p_1plus_sb': round(sum(1 for x in sb if x >= 1) / len(sb), 3),
        'p_2plus_hrr': round(sum(1 for x in hrr if x >= 2) / len(hrr), 3),
        'p_3plus_hrr': round(sum(1 for x in hrr if x >= 3) / len(hrr), 3),
        'p_4plus_hrr': round(sum(1 for x in hrr if x >= 4) / len(hrr), 3),
    }

def _summarize_pitcher(lines):
    def arr(k): return [x[k] for x in lines]
    outs = arr('outs'); er = arr('er'); ks = arr('k'); hs = arr('h'); bbs = arr('bb')
    return {
        'mean_outs': round(statistics.mean(outs), 2), 'median_outs': statistics.median(outs),
        'mean_er': round(statistics.mean(er), 2), 'mean_k': round(statistics.mean(ks), 2),
        'mean_h': round(statistics.mean(hs), 2), 'mean_bb': round(statistics.mean(bbs), 2),
        'p10_outs': round(_pct(outs, 0.10), 2), 'p50_outs': round(_pct(outs, 0.50), 2), 'p90_outs': round(_pct(outs, 0.90), 2),
        'p10_k': round(_pct(ks, 0.10), 2), 'p50_k': round(_pct(ks, 0.50), 2), 'p90_k': round(_pct(ks, 0.90), 2),
        'p10_er': round(_pct(er, 0.10), 2), 'p50_er': round(_pct(er, 0.50), 2), 'p90_er': round(_pct(er, 0.90), 2),
        'p_12plus_outs': round(sum(1 for x in outs if x >= 12)/len(outs), 3),
        'p_15plus_outs': round(sum(1 for x in outs if x >= 15)/len(outs), 3),
        'p_18plus_outs': round(sum(1 for x in outs if x >= 18)/len(outs), 3),
        'p_4plus_k': round(sum(1 for x in ks if x >= 4)/len(ks), 3),
        'p_5plus_k': round(sum(1 for x in ks if x >= 5)/len(ks), 3),
        'p_6plus_k': round(sum(1 for x in ks if x >= 6)/len(ks), 3),
        'p_2plus_er': round(sum(1 for x in er if x >= 2)/len(er), 3),
        'p_3plus_er': round(sum(1 for x in er if x >= 3)/len(er), 3),
    }

def _pearson_corr(xs, ys):
    if not xs or not ys or len(xs) != len(ys):
        return 0.0
    n = len(xs)
    if n < 2:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    num = 0.0
    dx2 = 0.0
    dy2 = 0.0
    for x, y in zip(xs, ys):
        dx = x - mx
        dy = y - my
        num += dx * dy
        dx2 += dx * dx
        dy2 += dy * dy
    den = (dx2 * dy2) ** 0.5
    if den <= 1e-12:
        return 0.0
    return max(-1.0, min(1.0, num / den))

def _corr_strength_label(r):
    if r >= 0.55:
        return 'STRONG+'
    if r >= 0.25:
        return 'CORRELATED'
    if r <= -0.45:
        return 'STRONG-'
    if r <= -0.20:
        return 'NEGATIVE'
    return 'NEUTRAL'

def _corr_warning(row_a, row_b, r):
    if r > -0.25:
        return None
    mk_a = row_a.get('market')
    mk_b = row_b.get('market')
    if (
        mk_a == 'pitcher_strikeouts' and mk_b in {'batter_hits', 'batter_total_bases', 'batter_runs', 'batter_rbis'}
    ) or (
        mk_b == 'pitcher_strikeouts' and mk_a in {'batter_hits', 'batter_total_bases', 'batter_runs', 'batter_rbis'}
    ):
        if row_a.get('team') and row_b.get('team') and row_a.get('team') != row_b.get('team'):
            return 'High K game suppresses opposing contact outcomes'
    if r <= -0.45:
        return 'Strong negative relationship can reduce same-game parlay hit rate'
    return 'Negative correlation warning for combined legs'

def _game_lineup_signature(game_obj, away_lineup, home_lineup):
    away_pitcher = ((game_obj or {}).get('teams', {}).get('away', {}).get('probablePitcher', {}) or {}).get('fullName', '')
    home_pitcher = ((game_obj or {}).get('teams', {}).get('home', {}).get('probablePitcher', {}) or {}).get('fullName', '')
    away_names = ','.join((p.get('name') or p.get('fullName') or '').strip() for p in (away_lineup or [])[:9])
    home_names = ','.join((p.get('name') or p.get('fullName') or '').strip() for p in (home_lineup or [])[:9])
    return f"{away_pitcher}|{home_pitcher}|{away_names}|{home_names}"

def _simulation_fallback_payload(game_obj, game_pk, sims=0, warning=''):
    """Build a minimal-but-valid simulation response when full Monte Carlo fails."""
    g = game_obj or {}
    away_t = (g.get('teams', {}).get('away', {}) or {}).get('team', {}) or {}
    home_t = (g.get('teams', {}).get('home', {}) or {}).get('team', {}) or {}
    away_abbr = away_t.get('abbreviation', 'AWAY')
    home_abbr = home_t.get('abbreviation', 'HOME')
    away_name = ((g.get('teams', {}).get('away', {}) or {}).get('probablePitcher', {}) or {}).get('fullName', 'Away SP')
    home_name = ((g.get('teams', {}).get('home', {}) or {}).get('probablePitcher', {}) or {}).get('fullName', 'Home SP')

    lineups = g.get('lineups') or {}

    def _sample_batters(hitters):
        out = []
        for i, p in enumerate(hitters or [], start=1):
            nm = (p.get('fullName') or p.get('name') or '').strip()
            if not nm:
                continue
            out.append({
                'slot': i,
                'name': nm,
                'pos': ((p.get('primaryPosition') or {}).get('abbreviation') or '?'),
                'ab': 4,
                'h': 1,
                'hr': 0,
                'rbi': 0,
                'r': 0,
                'bb': 0,
                'k': 1,
                'tb': 1,
            })
            if len(out) >= 9:
                break
        return out

    away_batters = _sample_batters(lineups.get('awayBatters'))
    home_batters = _sample_batters(lineups.get('homeBatters'))

    return {
        'success': True,
        'fallback': True,
        'warning': warning or 'Simulation fallback returned due to upstream data issue.',
        'meta': {
            'sims': int(sims or 0),
            'awayAbbr': away_abbr,
            'homeAbbr': home_abbr,
            'parkFactor': PARK_FACTORS.get(home_t.get('id'), 1.0),
            'gamePk': game_pk,
        },
        'team': {
            'away_mean_runs': 4.2,
            'home_mean_runs': 4.3,
            'mean_total': 8.5,
            'median_total': 8.0,
            'p_8plus_total': 0.53,
            'p_9plus_total': 0.46,
            'p_10plus_total': 0.35,
            'away_win_pct': 0.48,
            'home_win_pct': 0.49,
            'tie_pct': 0.03,
        },
        'handedness': {
            'awayLineup': {'L': 0, 'R': 0, 'S': 0},
            'homeLineup': {'L': 0, 'R': 0, 'S': 0},
            'awayStarterHand': 'R',
            'homeStarterHand': 'R',
        },
        'playerProps': {'away': [], 'home': []},
        'pitcherProps': {
            'awayStarter': {'name': away_name, 'mean_k': 0, 'mean_outs': 0, 'mean_er': 0, 'mean_h': 0, 'mean_bb': 0},
            'homeStarter': {'name': home_name, 'mean_k': 0, 'mean_outs': 0, 'mean_er': 0, 'mean_h': 0, 'mean_bb': 0},
            'awayBullpen': {'name': away_abbr + ' Bullpen', 'mean_k': 0, 'mean_outs': 0, 'mean_er': 0, 'mean_h': 0, 'mean_bb': 0},
            'homeBullpen': {'name': home_abbr + ' Bullpen', 'mean_k': 0, 'mean_outs': 0, 'mean_er': 0, 'mean_h': 0, 'mean_bb': 0},
            'awayBullpenTiers': {'closer': {}, 'setup': {}, 'middle': {}},
            'homeBullpenTiers': {'closer': {}, 'setup': {}, 'middle': {}},
        },
        'correlations': [],
        'top_sgp_combos': [],
        'sampleBoxscore': {
            'away': {'batters': away_batters, 'runs': 4, 'hits': 8, 'bb': 3, 'k': 8},
            'home': {'batters': home_batters, 'runs': 4, 'hits': 8, 'bb': 3, 'k': 8},
            'away_abbr': away_abbr,
            'home_abbr': home_abbr,
            'away_pitcher': away_name,
            'home_pitcher': home_name,
        },
    }

def _parse_sched_lineup(hitters):
    out = []
    for i, p in enumerate(hitters or [], start=1):
        name = (p.get('fullName') or p.get('name') or '').strip()
        pid = p.get('id') or p.get('playerId')
        pos = (p.get('primaryPosition') or {}).get('abbreviation', '?')
        if not name:
            continue
        fgb = fg_batter(name)
        svb = sv_batter(name)
        bio = _bio_cache.get(pid) or {}
        out.append({
            'slot': i,
            'id': pid,
            'name': name,
            'pos': pos,
            'bats': bio.get('bats', 'R'),
            'avg': fgb.get('fg_avg', '.---'),
            'obp': fgb.get('fg_obp', '.---'),
            'slg': fgb.get('fg_slg', '.---'),
            'ops': fgb.get('fg_ops', '.---'),
            'fg_sb': fgb.get('fg_sb', 0),
            'fg_woba': fgb.get('fg_woba', 'N/A'),
            'fg_wrc': fgb.get('fg_wrc', 'N/A'),
            'sv_xba': svb.get('sv_xba', 'N/A'),
            'sv_xslg': svb.get('sv_xslg', 'N/A'),
            'sv_xwoba': svb.get('sv_xwoba', 'N/A'),
            'sv_ev': svb.get('sv_ev', 'N/A'),
            'sv_hh_pct': svb.get('sv_hh_pct', 'N/A'),
            'sv_brl_pct': svb.get('sv_brl_pct', 'N/A'),
        })
    return out


def _roster_lineup(team_id):
    if not team_id:
        return []
    try:
        out = []
        for entry in _get_active_roster(team_id):
            pos = ((entry.get('position') or {}).get('abbreviation') or '?')
            if pos in ('P', 'SP', 'RP', 'CP'):
                continue
            name = ((entry.get('person') or {}).get('fullName') or '').strip()
            pid = (entry.get('person') or {}).get('id')
            if not name:
                continue
            fgb = fg_batter(name)
            svb = sv_batter(name)
            bio = _bio_cache.get(pid) or {}
            out.append({
                'slot': len(out) + 1,
                'id': pid,
                'name': name,
                'pos': pos,
                'bats': bio.get('bats', 'R'),
                'avg': fgb.get('fg_avg', '.---'),
                'obp': fgb.get('fg_obp', '.---'),
                'slg': fgb.get('fg_slg', '.---'),
                'ops': fgb.get('fg_ops', '.---'),
                'fg_sb': fgb.get('fg_sb', 0),
                'fg_woba': fgb.get('fg_woba', 'N/A'),
                'fg_wrc': fgb.get('fg_wrc', 'N/A'),
                'sv_xba': svb.get('sv_xba', 'N/A'),
                'sv_xslg': svb.get('sv_xslg', 'N/A'),
                'sv_xwoba': svb.get('sv_xwoba', 'N/A'),
                'sv_ev': svb.get('sv_ev', 'N/A'),
                'sv_hh_pct': svb.get('sv_hh_pct', 'N/A'),
                'sv_brl_pct': svb.get('sv_brl_pct', 'N/A'),
            })
            if len(out) >= 9:
                break
        return out
    except Exception:
        return []


def _event_meta(market):
    if market == 'batter_hits':
        return {'line': 0.5, 'side': 'Over', 'short': 'H'}
    if market == 'batter_home_runs':
        return {'line': 0.5, 'side': 'Over', 'short': 'HR'}
    if market == 'batter_total_bases':
        return {'line': 1.5, 'side': 'Over', 'short': 'TB'}
    if market == 'batter_rbis':
        return {'line': 0.5, 'side': 'Over', 'short': 'RBI'}
    if market == 'batter_runs':
        return {'line': 0.5, 'side': 'Over', 'short': 'R'}
    if market == 'batter_hits_runs_rbis':
        return {'line': 1.5, 'side': 'Over', 'short': 'H+R+RBI'}
    if market == 'pitcher_strikeouts':
        return {'line': 4.5, 'side': 'Over', 'short': 'K'}
    return {'line': 0.5, 'side': 'Over', 'short': 'PROP'}


def _add_batter_market_vectors(market_vectors, lineup, store, team_abbr):
    batter_markets = {
        'batter_hits': ('h', 1),
        'batter_home_runs': ('hr', 1),
        'batter_total_bases': ('tb', 2),
        'batter_rbis': ('rbi', 1),
        'batter_runs': ('r', 1),
        'batter_hits_runs_rbis': (lambda x: (x.get('h', 0) + x.get('r', 0) + x.get('rbi', 0)), 2),
    }
    for idx, batter in enumerate(lineup):
        lines = store.get(idx, [])
        if not lines:
            continue
        for market, (stat_source, event_floor) in batter_markets.items():
            values = [stat_source(x) for x in lines] if callable(stat_source) else [x.get(stat_source, 0) for x in lines]
            if not values:
                continue
            meta = _event_meta(market)
            event_prob = sum(1 for x in values if x >= event_floor) / len(values)
            market_vectors.append({
                'player': batter.get('name', ''),
                'team': team_abbr,
                'market': market,
                'values': values,
                'event_floor': event_floor,
                'event_prob': round(event_prob, 4),
                'line': meta['line'],
                'side': meta['side'],
                'leg_label': f"{batter.get('name', '')} {meta['short']}",
            })


def _load_simulation_inputs(game_pk):
    g = fetch_schedule_game(game_pk)
    if not g:
        raw = fetch_schedule(datetime.now(ET).strftime('%Y-%m-%d'))
        g = next((x for x in raw if x.get('gamePk') == game_pk), None)
    if not g:
        return None, None, None, None, None

    away_team = g.get('teams', {}).get('away', {}).get('team', {})
    home_team = g.get('teams', {}).get('home', {}).get('team', {})
    away_team_id = away_team.get('id')
    home_team_id = home_team.get('id')

    box = {}
    try:
        r_box = requests.get(f"{MLB_API}/game/{game_pk}/boxscore", timeout=10)
        if r_box.ok:
            box = (r_box.json() or {}).get('teams', {}) or {}
    except Exception:
        box = {}

    away_lineup = get_batters_from_boxscore(box.get('away', {}), 'away')
    home_lineup = get_batters_from_boxscore(box.get('home', {}), 'home')
    if not away_lineup or not home_lineup:
        lineups = (g.get('lineups') or {})
        if not away_lineup:
            away_lineup = _parse_sched_lineup(lineups.get('awayBatters'))
        if not home_lineup:
            home_lineup = _parse_sched_lineup(lineups.get('homeBatters'))
        if len(away_lineup) < 5:
            away_lineup = _roster_lineup(away_team_id)
        if len(home_lineup) < 5:
            home_lineup = _roster_lineup(home_team_id)

    return g, away_team, home_team, away_lineup, home_lineup


def _run_simulation(game_pk, sims):
    g, away_team, home_team, away_lineup, home_lineup = _load_simulation_inputs(game_pk)
    if not g:
        return _simulation_fallback_payload(
            {},
            game_pk,
            sims=sims,
            warning='Game not found for simulation; returned fallback payload.',
        )
    if not away_lineup or not home_lineup:
        raise ValueError('Lineups unavailable for simulation')

    away_team_id = away_team.get('id')
    home_team_id = home_team.get('id')
    today = datetime.now(ET).strftime('%Y-%m-%d')
    away_abbr = away_team.get('abbreviation', 'AWAY')
    home_abbr = home_team.get('abbreviation', 'HOME')

    away_p = g.get('teams', {}).get('away', {}).get('probablePitcher', {})
    home_p = g.get('teams', {}).get('home', {}).get('probablePitcher', {})
    away_pitcher = _pitcher_model(away_p.get('fullName', 'Away SP'), away_p.get('id'), away_team_id)
    home_pitcher = _pitcher_model(home_p.get('fullName', 'Home SP'), home_p.get('id'), home_team_id)
    park = PARK_FACTORS.get(home_team_id, 1.0)
    away_relief_fatigue_woba = _team_relief_fatigue_woba(home_team_id)
    home_relief_fatigue_woba = _team_relief_fatigue_woba(away_team_id)

    ven = g.get('venue', {})
    venue_id = ven.get('id')
    vloc = ven.get('location', {}) or {}
    coords = vloc.get('defaultCoordinates', {}) or {}
    lat = coords.get('latitude')
    lon = coords.get('longitude')
    try:
        dt_utc = datetime.fromisoformat(g.get('gameDate', '').replace('Z', '+00:00'))
        utc_off = VENUE_UTC_OFFSET.get(venue_id, -5)
        ghour = (dt_utc + timedelta(hours=utc_off)).hour
    except Exception:
        ghour = 13
    try:
        wx = get_weather(lat, lon, ghour, venue_id=venue_id)
    except Exception:
        wx = {}

    ump_data = None
    try:
        game_date_str = (g.get('gameDate') or today).split('T')[0]
        official_games = _fetch_schedule_with_officials(game_date_str, game_date_str)
        official_game = next((item for item in official_games if item.get('gamePk') == game_pk), None)
        ump = _get_hp_umpire(official_game or {})
        if ump and ump.get('id'):
            ump_data = _get_cached_ump(ump.get('id'), ump.get('fullName', ''))
    except Exception:
        ump_data = None

    ump_cache = {}
    if ump_data:
        ump_cache[game_pk] = {'date': datetime.now(ET).date(), 'data': ump_data}
    sim_env = get_sim_env_adjustments(
        game_pk,
        g,
        wx or {},
        home_team_id,
        ump_cache=ump_cache,
        DOME_VENUES=DOME_VENUES,
        PARK_FACTORS=PARK_FACTORS,
    )
    wx_mults = sim_env['wx_mults']
    ump_adj = sim_env['ump_adj']

    away_batx_map = {i: _batx_for_sim(b, home_pitcher, park, wx) for i, b in enumerate(away_lineup)}
    home_batx_map = {i: _batx_for_sim(b, away_pitcher, park, wx) for i, b in enumerate(home_lineup)}

    rng = AntitheticRandom(game_pk + int(datetime.now().strftime('%Y%m%d')) + 6)

    away_store = {i: [] for i in range(len(away_lineup))}
    home_store = {i: [] for i in range(len(home_lineup))}
    away_team_runs, home_team_runs, totals = [], [], []
    away_starter_lines, home_starter_lines = [], []
    away_bullpen_lines, home_bullpen_lines = [], []
    away_relief = {'Closer': [], 'Setup': [], 'Middle': []}
    home_relief = {'Closer': [], 'Setup': [], 'Middle': []}
    away_win = 0
    home_win = 0
    ties = 0
    sample = None

    for sim in range(sims):
        if sim % 2 == 1:
            rng.start_antithetic()
        away_off = _simulate_offense(
            away_lineup, home_pitcher, home_team_id, park, rng,
            batx_map=away_batx_map, wx_mults=wx_mults, ump_adj=ump_adj,
            relief_fatigue_woba=away_relief_fatigue_woba,
        )
        home_off = _simulate_offense(
            home_lineup, away_pitcher, away_team_id, park, rng,
            batx_map=home_batx_map, wx_mults=wx_mults, ump_adj=ump_adj,
            relief_fatigue_woba=home_relief_fatigue_woba,
        )
        if sim % 2 == 1:
            rng.stop_antithetic()
        if sample is None:
            sample = {
                'away': away_off, 'home': home_off,
                'away_abbr': away_abbr, 'home_abbr': home_abbr,
                'away_pitcher': away_pitcher['name'], 'home_pitcher': home_pitcher['name'],
            }
        for i, line in enumerate(away_off['batters']):
            away_store[i].append(line)
        for i, line in enumerate(home_off['batters']):
            home_store[i].append(line)
        home_starter_lines.append(away_off['starter'])
        away_starter_lines.append(home_off['starter'])
        home_bullpen_lines.append(away_off['bullpen'])
        away_bullpen_lines.append(home_off['bullpen'])
        for k in away_relief.keys():
            away_relief[k].append(home_off['relief_lines'][k])
            home_relief[k].append(away_off['relief_lines'][k])
        away_team_runs.append(away_off['runs'])
        home_team_runs.append(home_off['runs'])
        totals.append(away_off['runs'] + home_off['runs'])
        if away_off['runs'] > home_off['runs']:
            away_win += 1
        elif home_off['runs'] > away_off['runs']:
            home_win += 1
        else:
            ties += 1

    away_props = []
    for i, b in enumerate(away_lineup):
        s = _summarize_player(away_store[i])
        s.update({'id': b.get('id'), 'name': b.get('name'), 'slot': b.get('slot'), 'pos': b.get('pos'), 'bats': b.get('bats', 'S')})
        bx = away_batx_map.get(i, {})
        s['batx_composite'] = round(bx.get('composite', 1.0), 4)
        s['batx_hit_mult'] = round(bx.get('hit_mult', 1.0), 4)
        s['batx_hr_mult'] = round(bx.get('hr_mult', 1.0), 4)
        away_props.append(s)
    home_props = []
    for i, b in enumerate(home_lineup):
        s = _summarize_player(home_store[i])
        s.update({'id': b.get('id'), 'name': b.get('name'), 'slot': b.get('slot'), 'pos': b.get('pos'), 'bats': b.get('bats', 'S')})
        bx = home_batx_map.get(i, {})
        s['batx_composite'] = round(bx.get('composite', 1.0), 4)
        s['batx_hit_mult'] = round(bx.get('hit_mult', 1.0), 4)
        s['batx_hr_mult'] = round(bx.get('hr_mult', 1.0), 4)
        home_props.append(s)

    away_pitch_summary = _summarize_pitcher(away_starter_lines)
    away_pitch_summary.update({'name': away_pitcher['name'], 'pitchHand': away_pitcher['pitchHand']})
    home_pitch_summary = _summarize_pitcher(home_starter_lines)
    home_pitch_summary.update({'name': home_pitcher['name'], 'pitchHand': home_pitcher['pitchHand']})
    away_bullpen_summary = _summarize_pitcher(away_bullpen_lines)
    away_bullpen_summary.update({'name': away_abbr + ' Bullpen'})
    home_bullpen_summary = _summarize_pitcher(home_bullpen_lines)
    home_bullpen_summary.update({'name': home_abbr + ' Bullpen'})

    away_tiers = {k.lower(): _summarize_pitcher(v) for k, v in away_relief.items()}
    home_tiers = {k.lower(): _summarize_pitcher(v) for k, v in home_relief.items()}

    away_l = sum(1 for b in away_lineup if (b.get('bats') or 'S') == 'L')
    away_r = sum(1 for b in away_lineup if (b.get('bats') or 'S') == 'R')
    away_s = sum(1 for b in away_lineup if (b.get('bats') or 'S') == 'S')
    home_l = sum(1 for b in home_lineup if (b.get('bats') or 'S') == 'L')
    home_r = sum(1 for b in home_lineup if (b.get('bats') or 'S') == 'R')
    home_s = sum(1 for b in home_lineup if (b.get('bats') or 'S') == 'S')

    market_vectors = []
    _add_batter_market_vectors(market_vectors, away_lineup, away_store, away_abbr)
    _add_batter_market_vectors(market_vectors, home_lineup, home_store, home_abbr)

    for p_name, p_team, p_lines in ((away_pitcher['name'], away_abbr, away_starter_lines), (home_pitcher['name'], home_abbr, home_starter_lines)):
        values = [x.get('k', 0) for x in p_lines]
        if values:
            meta = _event_meta('pitcher_strikeouts')
            floor = 5
            market_vectors.append({
                'player': p_name,
                'team': p_team,
                'market': 'pitcher_strikeouts',
                'values': values,
                'event_floor': floor,
                'event_prob': round(sum(1 for x in values if x >= floor) / len(values), 4),
                'line': meta['line'],
                'side': meta['side'],
                'leg_label': f"{p_name} {meta['short']}",
            })

    correlations = []
    combo_candidates = []
    for i in range(len(market_vectors)):
        row_a = market_vectors[i]
        for j in range(i + 1, len(market_vectors)):
            row_b = market_vectors[j]
            r = _pearson_corr(row_a['values'], row_b['values'])
            if abs(r) < 0.10:
                continue
            strength = _corr_strength_label(r)
            warning = _corr_warning(row_a, row_b, r)
            corr_item = {
                'playerA': row_a['player'],
                'marketA': row_a['market'],
                'teamA': row_a['team'],
                'playerB': row_b['player'],
                'marketB': row_b['market'],
                'teamB': row_b['team'],
                'r': round(r, 3),
                'strength': strength,
            }
            if warning:
                corr_item['warning'] = warning
            correlations.append(corr_item)

            if r > 0:
                event_a = [1 if x >= row_a['event_floor'] else 0 for x in row_a['values']]
                event_b = [1 if x >= row_b['event_floor'] else 0 for x in row_b['values']]
                joint_hits = sum(1 for a, b in zip(event_a, event_b) if a and b)
                combined_prob = joint_hits / sims
                indep_prob = row_a['event_prob'] * row_b['event_prob']
                combo_candidates.append({
                    'legs': [row_a['leg_label'], row_b['leg_label']],
                    'legs_meta': [
                        {'player': row_a['player'], 'market': row_a['market'], 'line': row_a['line'], 'side': row_a['side'], 'team': row_a['team']},
                        {'player': row_b['player'], 'market': row_b['market'], 'line': row_b['line'], 'side': row_b['side'], 'team': row_b['team']},
                    ],
                    'combined_prob': round(combined_prob, 3),
                    'r': round(r, 3),
                    'strength': strength,
                    'combined_ev_pct': round((combined_prob - indep_prob) * 100, 2),
                    'score': (combined_prob * 0.65) + (r * 0.35),
                })

    correlations = sorted(correlations, key=lambda x: abs(x['r']), reverse=True)[:240]
    top_sgp_combos = sorted(combo_candidates, key=lambda x: x['score'], reverse=True)[:5]
    for combo in top_sgp_combos:
        combo.pop('score', None)

    return {
        'success': True,
        'meta': {'sims': sims, 'awayAbbr': away_abbr, 'homeAbbr': home_abbr, 'parkFactor': park},
        'team': {
            'away_mean_runs': round(statistics.mean(away_team_runs), 2),
            'home_mean_runs': round(statistics.mean(home_team_runs), 2),
            'mean_total': round(statistics.mean(totals), 2),
            'median_total': statistics.median(totals),
            'p_8plus_total': round(sum(1 for x in totals if x >= 8) / len(totals), 3),
            'p_9plus_total': round(sum(1 for x in totals if x >= 9) / len(totals), 3),
            'p_10plus_total': round(sum(1 for x in totals if x >= 10) / len(totals), 3),
            'away_win_pct': round(away_win / sims, 3),
            'home_win_pct': round(home_win / sims, 3),
            'tie_pct': round(ties / sims, 3),
        },
        'handedness': {
            'awayLineup': {'L': away_l, 'R': away_r, 'S': away_s},
            'homeLineup': {'L': home_l, 'R': home_r, 'S': home_s},
            'awayStarterHand': away_pitcher['pitchHand'],
            'homeStarterHand': home_pitcher['pitchHand'],
        },
        'playerProps': {'away': away_props, 'home': home_props},
        'pitcherProps': {
            'awayStarter': away_pitch_summary,
            'homeStarter': home_pitch_summary,
            'awayBullpen': away_bullpen_summary,
            'homeBullpen': home_bullpen_summary,
            'awayBullpenTiers': away_tiers,
            'homeBullpenTiers': home_tiers,
        },
        'correlations': correlations,
        'top_sgp_combos': top_sgp_combos,
        'sampleBoxscore': sample,
    }


def api_simulate(game_pk):
    try:
        try:
            requested_sims = int(request.args.get('sims', DEFAULT_SIMS) or DEFAULT_SIMS)
        except Exception:
            requested_sims = DEFAULT_SIMS
        sims = max(200, min(BACKTEST_SIMS, requested_sims))
        refresh = request.args.get('refresh') == '1'

        g, _, _, away_lineup, home_lineup = _load_simulation_inputs(game_pk)
        if not g:
            return jsonify(_simulation_fallback_payload({}, game_pk, sims=sims, warning='Game not found for simulation; returned fallback payload.'))
        if not away_lineup or not home_lineup:
            return jsonify({'success': False, 'error': 'Lineups unavailable for simulation'}), 400

        today = datetime.now(ET).strftime('%Y-%m-%d')
        lineup_signature = _game_lineup_signature(g, away_lineup, home_lineup)
        cache_signature = f"{lineup_signature}|sims:{sims}"
        cached = _correlation_cache.get(game_pk)
        if cached and not refresh and cached.get('date') == today and cached.get('signature') == cache_signature:
            return jsonify(cached.get('payload') or {'success': False, 'error': 'Cached simulation payload missing'})

        payload = _run_simulation(game_pk, sims)
        _correlation_cache[game_pk] = {'date': today, 'signature': cache_signature, 'payload': payload}
        return jsonify(payload)
    except Exception as ex:
        print('[api_simulate]', traceback.format_exc())
        try:
            g = fetch_schedule_game(game_pk) or {}
            fallback = _simulation_fallback_payload(g, game_pk, sims=sims, warning='Fallback mode: simulation error')
            return jsonify(fallback)
        except Exception as fallback_ex:
            print('[api_simulate:fallback]', traceback.format_exc())
            emergency = _simulation_fallback_payload({}, game_pk, sims=sims, warning='Emergency fallback: simulation error; fallback error')
            return jsonify(emergency)


def register_simulation_routes(app):
    app.add_url_rule('/api/simulate/<int:game_pk>', view_func=api_simulate)


__all__ = [
    'DEFAULT_SIMS', 'SUMMARY_SIMS', 'BACKTEST_SIMS',
    '_simulate_offense', '_summarize_player', '_summarize_pitcher',
    '_pearson_corr', '_corr_strength_label', '_corr_warning',
    '_game_lineup_signature', '_simulation_fallback_payload',
    '_parse_sched_lineup', '_roster_lineup', '_event_meta', '_add_batter_market_vectors',
    '_run_simulation', 'api_simulate', 'configure_simulation_context', 'register_simulation_routes',
]
