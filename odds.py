"""Odds fetching, caching, and market transformation helpers."""

import math
import os
import re
import threading
import time
import traceback
from datetime import datetime, timezone

import requests
from flask import jsonify, request


def configure_odds_context(namespace):
    globals().update(namespace)


ODDS_API_KEY = (os.getenv('ODDS_API_KEY') or '').strip()
ODDS_REGION = (os.getenv('ODDS_REGION') or 'us').strip()
ODDS_CACHE_STORE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'odds_cache.json')


def _odds_ttl_seconds(env_key, default_seconds, min_seconds=30, max_seconds=24 * 60 * 60):
    """Read a TTL (seconds) from env with sane bounds and fallback."""
    raw = os.getenv(env_key)
    if raw is None or str(raw).strip() == '':
        return int(default_seconds)
    try:
        ttl = int(float(raw))
    except Exception:
        return int(default_seconds)
    return max(int(min_seconds), min(int(max_seconds), ttl))


def _odds_bool_env(env_key, default='1'):
    return str(os.getenv(env_key, default)).strip().lower() in ('1', 'true', 'yes')

# ── Odds API cache — prevents burning credits on every request ────────────────
# Events list: cache 6h (rarely changes intraday)
# Per-game odds snapshot: cache 24h by default (single fetch reused everywhere)
_ODDS_EVENTS_CACHE: dict = {}          # {'data': [...], 'ts': float}
_ODDS_GAME_CACHE:  dict = {}           # {event_id: {'all': [...], 'ts': float}}
_ODDS_EVENTS_TTL  = _odds_ttl_seconds('ODDS_EVENTS_TTL_SEC', 6 * 60 * 60)
_ODDS_GAME_TTL    = _odds_ttl_seconds('ODDS_GAME_TTL_SEC', 24 * 60 * 60)
_ODDS_NRFI_TTL    = _odds_ttl_seconds('ODDS_NRFI_TTL_SEC', 5 * 60)
_ODDS_DAILY_CACHE = _odds_bool_env('ODDS_DAILY_CACHE', '1')
_ODDS_SNAPSHOT_STRICT = _odds_bool_env('ODDS_SNAPSHOT_STRICT', '1')
_ODDS_SNAPSHOT_AUTOWARM = _odds_bool_env('ODDS_SNAPSHOT_AUTOWARM', '1')
_ODDS_SNAPSHOT_HEARTBEAT_SEC = _odds_ttl_seconds('ODDS_SNAPSHOT_HEARTBEAT_SEC', 5 * 60, min_seconds=60, max_seconds=60 * 60)
_ODDS_SNAPSHOT_RETRY_SEC = _odds_ttl_seconds('ODDS_SNAPSHOT_RETRY_SEC', 15 * 60, min_seconds=60, max_seconds=6 * 60 * 60)
_ODDS_CACHE_LOCK = threading.Lock()
_ODDS_SNAPSHOT_WORKER_LOCK = threading.Lock()
_ODDS_SNAPSHOT_WORKER_STARTED = False
_ODDS_SNAPSHOT_WORKER_LAST_ATTEMPT = 0.0
_ODDS_SNAPSHOT_META: dict = {
    'date': None,
    'complete': False,
    'running': False,
    'startedAt': None,
    'completedAt': None,
    'eventsCount': 0,
    'eventsFetched': 0,
    'errors': [],
}
_ODDS_ALL_MARKETS = (
    'h2h,spreads,totals,h2h_1st_1_innings,'
    'batter_hits,batter_total_bases,batter_home_runs,batter_rbis,'
    'batter_runs_scored,batter_stolen_bases,batter_hits_runs_rbis,pitcher_strikeouts'
)

# Maps (market_key, line) → precomputed MC prob field; None = use Poisson from mean
_BATTER_PROB_FIELD_FOR = {
    ('batter_hits', 0.5): 'p_1plus_hit',
    ('batter_hits', 1.5): 'p_2plus_hit',
    ('batter_total_bases', 1.5): 'p_2plus_tb',
    ('batter_home_runs', 0.5): 'p_1plus_hr',
    ('batter_rbis', 0.5): 'p_1plus_rbi',
    ('batter_runs_scored', 0.5): 'p_1plus_run',
    ('batter_hits_runs_rbis', 1.5): 'p_2plus_hrr',
    ('batter_hits_runs_rbis', 2.5): 'p_3plus_hrr',
    ('batter_hits_runs_rbis', 3.5): 'p_4plus_hrr',
    ('batter_stolen_bases', 0.5): 'p_1plus_sb',
}
_BATTER_MEAN_FIELD_FOR_MK = {
    'batter_hits': 'mean_hits',
    'batter_total_bases': 'mean_tb',
    'batter_home_runs': 'mean_hr',
    'batter_rbis': 'mean_rbi',
    'batter_runs_scored': 'mean_runs',
    'batter_hits_runs_rbis': 'mean_hrr',
    'batter_stolen_bases': 'mean_sb',
}
# Fallback lines used when no Odds API data for a player/market
_BATTER_FALLBACK_LINE = {
    'batter_hits': 0.5,
    'batter_total_bases': 1.5,
    'batter_home_runs': 0.5,
    'batter_rbis': 0.5,
    'batter_runs_scored': 0.5,
    'batter_hits_runs_rbis': 2.5,
    'batter_stolen_bases': 0.5,
}
_K_PROB_FIELD_FOR = {3.5: 'p_4plus_k', 4.5: 'p_5plus_k', 5.5: 'p_6plus_k'}

def _odds_today_key():
    return datetime.now(ET).strftime('%Y-%m-%d')

def _odds_cache_entry_fresh(entry, data_key, ttl_sec):
    if not isinstance(entry, dict) or data_key not in entry:
        return False
    if _ODDS_DAILY_CACHE and entry.get('cache_date') == _odds_today_key():
        return True
    try:
        return (time.time() - float(entry.get('ts') or 0.0)) < float(ttl_sec)
    except Exception:
        return False

def _persist_odds_caches():
    payload = {
        'date': _odds_today_key(),
        'events': _ODDS_EVENTS_CACHE,
        'games': _ODDS_GAME_CACHE,
        'snapshot': _ODDS_SNAPSHOT_META,
        'savedAt': datetime.now(timezone.utc).isoformat(),
    }
    _save_json(ODDS_CACHE_STORE, payload)

def _restore_odds_caches():
    payload = _load_json(ODDS_CACHE_STORE, {})
    if not isinstance(payload, dict):
        return
    if payload.get('date') != _odds_today_key():
        return
    events = payload.get('events') or {}
    games = payload.get('games') or {}
    snapshot = payload.get('snapshot') or {}
    if isinstance(events, dict):
        _ODDS_EVENTS_CACHE.update(events)
    if isinstance(games, dict):
        _ODDS_GAME_CACHE.update(games)
    if isinstance(snapshot, dict):
        _ODDS_SNAPSHOT_META.update(snapshot)
        _ODDS_SNAPSHOT_META['running'] = False

def _ensure_daily_odds_snapshot():
    """Build one full odds snapshot for today, then serve cache-only for the rest of the day."""
    if not ODDS_API_KEY:
        return False

    with _ODDS_CACHE_LOCK:
        today = _odds_today_key()
        if _ODDS_SNAPSHOT_META.get('date') == today and _ODDS_SNAPSHOT_META.get('complete'):
            return True
        if _ODDS_SNAPSHOT_META.get('running'):
            return False
        _ODDS_SNAPSHOT_META.update({
            'date': today,
            'complete': False,
            'running': True,
            'startedAt': datetime.now(timezone.utc).isoformat(),
            'completedAt': None,
            'eventsCount': 0,
            'eventsFetched': 0,
            'errors': [],
        })

    try:
        now = time.time()
        r = requests.get(
            'https://api.the-odds-api.com/v4/sports/baseball_mlb/events',
            params={'apiKey': ODDS_API_KEY, 'dateFormat': 'iso'},
            timeout=12,
        )
        r.raise_for_status()
        events = r.json() or []

        with _ODDS_CACHE_LOCK:
            _ODDS_EVENTS_CACHE['data'] = events
            _ODDS_EVENTS_CACHE['ts'] = now
            _ODDS_EVENTS_CACHE['cache_date'] = _odds_today_key()
            _ODDS_SNAPSHOT_META['eventsCount'] = len(events)

        fetched = 0
        errors = []
        for ev in events:
            eid = ev.get('id')
            if not eid:
                continue
            try:
                now_ev = time.time()
                rr = requests.get(
                    f'https://api.the-odds-api.com/v4/sports/baseball_mlb/events/{eid}/odds',
                    params={
                        'apiKey': ODDS_API_KEY,
                        'regions': ODDS_REGION,
                        'markets': _ODDS_ALL_MARKETS,
                        'oddsFormat': 'american',
                        'dateFormat': 'iso',
                    },
                    timeout=15,
                )
                rr.raise_for_status()
                books = rr.json().get('bookmakers', []) or []
                with _ODDS_CACHE_LOCK:
                    _ODDS_GAME_CACHE[eid] = {
                        'all': books,
                        'ts': now_ev,
                        'cache_date': _odds_today_key(),
                    }
                fetched += 1
            except Exception as ex:
                errors.append(f'{eid}: {ex}')

        with _ODDS_CACHE_LOCK:
            _ODDS_SNAPSHOT_META['eventsFetched'] = fetched
            _ODDS_SNAPSHOT_META['errors'] = errors[:50]
            _ODDS_SNAPSHOT_META['completedAt'] = datetime.now(timezone.utc).isoformat()
            _ODDS_SNAPSHOT_META['complete'] = True
            _ODDS_SNAPSHOT_META['running'] = False
            _persist_odds_caches()

        print(f'[Odds] Daily snapshot complete: events={len(events)} fetched={fetched} errors={len(errors)}')
        return True
    except Exception as ex:
        with _ODDS_CACHE_LOCK:
            _ODDS_SNAPSHOT_META['errors'] = [str(ex)]
            _ODDS_SNAPSHOT_META['completedAt'] = datetime.now(timezone.utc).isoformat()
            _ODDS_SNAPSHOT_META['complete'] = False
            _ODDS_SNAPSHOT_META['running'] = False
            _persist_odds_caches()
        print(f'[Odds] Daily snapshot failed: {ex}')
        return False

def _odds_snapshot_ready_today():
    with _ODDS_CACHE_LOCK:
        return (
            _ODDS_SNAPSHOT_META.get('date') == _odds_today_key()
            and bool(_ODDS_SNAPSHOT_META.get('complete'))
        )

def _start_odds_snapshot_worker():
    """Continuously ensure today's odds snapshot is built without manual priming."""
    global _ODDS_SNAPSHOT_WORKER_STARTED, _ODDS_SNAPSHOT_WORKER_LAST_ATTEMPT
    with _ODDS_SNAPSHOT_WORKER_LOCK:
        if _ODDS_SNAPSHOT_WORKER_STARTED:
            return
        _ODDS_SNAPSHOT_WORKER_STARTED = True

    def _runner():
        global _ODDS_SNAPSHOT_WORKER_LAST_ATTEMPT
        # Give startup loaders a short head start.
        time.sleep(8)
        while True:
            try:
                if not _ODDS_SNAPSHOT_AUTOWARM or not ODDS_API_KEY:
                    time.sleep(5 * 60)
                    continue

                if _odds_snapshot_ready_today():
                    time.sleep(_ODDS_SNAPSHOT_HEARTBEAT_SEC)
                    continue

                now = time.time()
                if (now - _ODDS_SNAPSHOT_WORKER_LAST_ATTEMPT) < _ODDS_SNAPSHOT_RETRY_SEC:
                    time.sleep(30)
                    continue

                _ODDS_SNAPSHOT_WORKER_LAST_ATTEMPT = now
                built = _ensure_daily_odds_snapshot()
                if built:
                    print('[ODDS_WORKER] Daily snapshot ready')
                else:
                    print('[ODDS_WORKER] Snapshot attempt incomplete; will retry')
            except Exception as ex:
                print(f'[ODDS_WORKER] Snapshot loop error: {ex}')
            time.sleep(30)

    threading.Thread(target=_runner, daemon=True).start()

def _norm_name(s):
    return re.sub(r'[^a-z0-9]+', '', (s or '').lower())

def _american_to_implied(price):
    try:
        p = float(price)
        if p > 0:
            return round(100.0 / (p + 100.0), 4)
        if p < 0:
            return round((-p) / ((-p) + 100.0), 4)
        return None
    except Exception:
        return None

def _find_odds_event(away_name, home_name):
    if not ODDS_API_KEY:
        return None, []
    try:
        ok = _ensure_daily_odds_snapshot()
        if _ODDS_SNAPSHOT_STRICT and (not ok or not _odds_snapshot_ready_today()):
            return None, []
        with _ODDS_CACHE_LOCK:
            events = _ODDS_EVENTS_CACHE.get('data') or []
        na = _norm_name(away_name)
        nh = _norm_name(home_name)
        for ev in events:
            if _norm_name(ev.get('away_team')) == na and _norm_name(ev.get('home_team')) == nh:
                return ev, events
        return None, events
    except Exception as ex:
        print(f'[_find_odds_event] {ex}')
        return None, []

def _best_moneyline(bookmakers, away_name, home_name):
    best = {'away': None, 'home': None}
    for bk in bookmakers or []:
        for m in bk.get('markets', []) or []:
            if m.get('key') != 'h2h':
                continue
            for o in m.get('outcomes', []) or []:
                nm = o.get('name')
                item = {'bookmaker': bk.get('title'), 'price': o.get('price'), 'implied': _american_to_implied(o.get('price'))}
                if nm == away_name:
                    if best['away'] is None or float(o.get('price', -9999)) > float(best['away']['price']):
                        best['away'] = item
                elif nm == home_name:
                    if best['home'] is None or float(o.get('price', -9999)) > float(best['home']['price']):
                        best['home'] = item
    return best

def _best_total(bookmakers):
    best = None
    for bk in bookmakers or []:
        for m in bk.get('markets', []) or []:
            if m.get('key') != 'totals':
                continue
            outs = m.get('outcomes', []) or []
            over = next((x for x in outs if str(x.get('name')).lower() == 'over'), None)
            under = next((x for x in outs if str(x.get('name')).lower() == 'under'), None)
            if not over or not under:
                continue
            cand = {
                'bookmaker': bk.get('title'),
                'line': over.get('point'),
                'over_price': over.get('price'), 'under_price': under.get('price'),
                'over_implied': _american_to_implied(over.get('price')),
                'under_implied': _american_to_implied(under.get('price')),
            }
            if best is None:
                best = cand
            else:
                # prefer widely used standard line nearest consensus price
                if abs(float(cand['over_price'] or 0)) < abs(float(best['over_price'] or 0)):
                    best = cand
    return best

def _best_spread(bookmakers, away_name, home_name):
    """Return best run-line (spreads market) for each side."""
    best = {'away': None, 'home': None}
    for bk in bookmakers or []:
        for m in bk.get('markets', []) or []:
            if m.get('key') != 'spreads':
                continue
            for o in m.get('outcomes', []) or []:
                nm = o.get('name', '')
                point = o.get('point')  # e.g. -1.5 or +1.5
                price = o.get('price')
                item = {'bookmaker': bk.get('title'), 'price': price, 'point': point,
                        'implied': _american_to_implied(price)}
                if nm == away_name:
                    if best['away'] is None or abs(float(price or 0)) < abs(float(best['away']['price'] or 0)):
                        best['away'] = item
                elif nm == home_name:
                    if best['home'] is None or abs(float(price or 0)) < abs(float(best['home']['price'] or 0)):
                        best['home'] = item
    return best

def _load_event_odds(event_id, featured_only=False):
    if not ODDS_API_KEY or not event_id:
        return []
    try:
        ok = _ensure_daily_odds_snapshot()
        if _ODDS_SNAPSHOT_STRICT and (not ok or not _odds_snapshot_ready_today()):
            return []
        with _ODDS_CACHE_LOCK:
            cached = _ODDS_GAME_CACHE.get(event_id)
            return (cached or {}).get('all') or []
    except Exception as ex:
        print(f'[_load_event_odds] {ex}')
        with _ODDS_CACHE_LOCK:
            cached = _ODDS_GAME_CACHE.get(event_id)
        return (cached or {}).get('all', [])

def _load_event_market_odds(event_id, markets, cache_key, ttl_sec):
    # Keep signature for compatibility; serve subset from unified per-event cache.
    if not ODDS_API_KEY or not event_id or not markets:
        return []
    books = _load_event_odds(event_id)
    if not books:
        return []
    wanted = {m.strip() for m in str(markets).split(',') if m.strip()}
    if not wanted:
        return books
    filtered = []
    for bk in books:
        mkts = [m for m in (bk.get('markets') or []) if (m.get('key') or '') in wanted]
        if not mkts:
            continue
        b = dict(bk)
        b['markets'] = mkts
        filtered.append(b)
    return filtered

def _refresh_odds_events_cache():
    if not ODDS_API_KEY:
        return []
    ok = _ensure_daily_odds_snapshot()
    if _ODDS_SNAPSHOT_STRICT and (not ok or not _odds_snapshot_ready_today()):
        return []
    with _ODDS_CACHE_LOCK:
        return _ODDS_EVENTS_CACHE.get('data') or []

def _odds_cache_status_payload():
    now = time.time()
    with _ODDS_CACHE_LOCK:
        events_cached = int(len(_ODDS_EVENTS_CACHE.get('data') or []))
        events_ts = float(_ODDS_EVENTS_CACHE.get('ts') or 0.0)
        events_date = _ODDS_EVENTS_CACHE.get('cache_date')
        game_entries = _ODDS_GAME_CACHE
        game_cached = int(len(game_entries or {}))
        game_rows = []
        for eid, ent in (game_entries or {}).items():
            if not isinstance(ent, dict):
                continue
            game_rows.append({
                'eventId': eid,
                'bookmakers': len(ent.get('all') or []),
                'cacheDate': ent.get('cache_date'),
                'ageSec': round(max(0.0, now - float(ent.get('ts') or 0.0)), 1),
            })
        game_rows.sort(key=lambda x: x.get('ageSec', 0.0))

    file_exists = os.path.exists(ODDS_CACHE_STORE)
    file_meta = {
        'exists': file_exists,
        'path': ODDS_CACHE_STORE,
        'sizeBytes': os.path.getsize(ODDS_CACHE_STORE) if file_exists else 0,
        'modifiedAt': datetime.fromtimestamp(os.path.getmtime(ODDS_CACHE_STORE), tz=timezone.utc).isoformat() if file_exists else None,
    }
    return {
        'success': True,
        'keyConfigured': bool(ODDS_API_KEY),
        'dailyCacheEnabled': _ODDS_DAILY_CACHE,
        'snapshotStrict': _ODDS_SNAPSHOT_STRICT,
        'snapshotAutowarm': _ODDS_SNAPSHOT_AUTOWARM,
        'todayET': _odds_today_key(),
        'ttl': {
            'eventsSec': _ODDS_EVENTS_TTL,
            'gameSec': _ODDS_GAME_TTL,
            'nrfiSec': _ODDS_NRFI_TTL,
        },
        'eventsCache': {
            'count': events_cached,
            'cacheDate': events_date,
            'ageSec': round(max(0.0, now - events_ts), 1) if events_ts else None,
        },
        'gameCache': {
            'count': game_cached,
            'entries': game_rows[:30],
        },
        'snapshot': dict(_ODDS_SNAPSHOT_META),
        'snapshotWorker': {
            'started': _ODDS_SNAPSHOT_WORKER_STARTED,
            'retrySec': _ODDS_SNAPSHOT_RETRY_SEC,
            'heartbeatSec': _ODDS_SNAPSHOT_HEARTBEAT_SEC,
        },
        'file': file_meta,
    }

def _clear_odds_caches_locked():
    _ODDS_EVENTS_CACHE.clear()
    _ODDS_GAME_CACHE.clear()
    _ODDS_SNAPSHOT_META.update({
        'date': None,
        'complete': False,
        'running': False,
        'startedAt': None,
        'completedAt': None,
        'eventsCount': 0,
        'eventsFetched': 0,
        'errors': [],
    })
    _persist_odds_caches()

def api_odds_cache_status():
    try:
        return jsonify(_odds_cache_status_payload())
    except Exception as ex:
        print(f'[api_odds_cache_status] {traceback.format_exc()}')
        return jsonify({'success': False, 'error': 'Internal server error'}), 500

def api_odds_cache_refresh():
    payload = request.get_json(silent=True) or {}
    mode = str(payload.get('mode') or request.args.get('mode') or 'snapshot').strip().lower()
    date_str = str(payload.get('date') or request.args.get('date') or datetime.now(ET).strftime('%Y-%m-%d')).strip()
    event_id = payload.get('eventId') or request.args.get('eventId')
    game_pk = payload.get('gamePk') or request.args.get('gamePk')
    clear_first = str(payload.get('clearFirst', request.args.get('clearFirst', '1'))).strip().lower() in ('1', 'true', 'yes')

    if not ODDS_API_KEY:
        return jsonify({'success': False, 'error': 'ODDS_API_KEY is not configured'}), 400

    try:
        with _ODDS_CACHE_LOCK:
            if clear_first:
                _clear_odds_caches_locked()

        events = _refresh_odds_events_cache()
        result = {
            'success': True,
            'mode': mode,
            'date': date_str,
            'eventsRefreshed': len(events),
            'prefetchedEventIds': [],
            'prefetchedCount': 0,
            'clearFirst': clear_first,
        }

        if mode in ('snapshot', 'build'):
            snapshot_ok = _odds_snapshot_ready_today()
            result['snapshotBuilt'] = snapshot_ok
            result['prefetchedEventIds'] = list((_ODDS_GAME_CACHE or {}).keys())[:50]
            result['prefetchedCount'] = len(_ODDS_GAME_CACHE or {})
        elif mode in ('today', 'all'):
            sched = fetch_schedule(date_str)
            event_map = {
                (_norm_name(ev.get('away_team')), _norm_name(ev.get('home_team'))): ev
                for ev in events
            }
            seen = set()
            for g in sched or []:
                away_name = (((g.get('teams') or {}).get('away') or {}).get('team') or {}).get('name')
                home_name = (((g.get('teams') or {}).get('home') or {}).get('team') or {}).get('name')
                ev = event_map.get((_norm_name(away_name), _norm_name(home_name)))
                if not ev:
                    continue
                eid = ev.get('id')
                if not eid or eid in seen:
                    continue
                seen.add(eid)
                _load_event_odds(eid)
                result['prefetchedEventIds'].append(eid)
            result['prefetchedCount'] = len(result['prefetchedEventIds'])
        elif mode in ('event', 'single'):
            target_event_id = event_id
            if not target_event_id and game_pk:
                try:
                    gpk = int(game_pk)
                except Exception:
                    return jsonify({'success': False, 'error': 'gamePk must be an integer'}), 400
                sched = fetch_schedule(date_str)
                g = next((x for x in (sched or []) if x.get('gamePk') == gpk), None)
                if not g:
                    return jsonify({'success': False, 'error': f'gamePk {gpk} not found for {date_str}'}), 404
                away_name = (((g.get('teams') or {}).get('away') or {}).get('team') or {}).get('name')
                home_name = (((g.get('teams') or {}).get('home') or {}).get('team') or {}).get('name')
                ev, _ = _find_odds_event(away_name, home_name)
                target_event_id = (ev or {}).get('id') if ev else None
            if not target_event_id:
                return jsonify({'success': False, 'error': 'event mode requires eventId or gamePk'}), 400
            _load_event_odds(target_event_id)
            result['prefetchedEventIds'] = [target_event_id]
            result['prefetchedCount'] = 1

        result['status'] = _odds_cache_status_payload()
        return jsonify(result)
    except Exception as ex:
        print(f'[api_odds_cache_refresh] {traceback.format_exc()}')
        return jsonify({'success': False, 'error': 'Internal server error'}), 500

def _poisson_over_prob(mean, line):
    """P(X > line) = P(X >= floor(line)+1) via Poisson(mean). Works for any half-integer line."""
    k = int(line) + 1
    if mean <= 0 or k < 1:
        return 0.0
    lam = float(mean)
    log_p = -lam
    cdf = math.exp(log_p)
    for x in range(1, k):
        log_p += math.log(lam) - math.log(x)
        cdf += math.exp(log_p)
    return max(0.0, min(1.0, 1.0 - cdf))

def _market_lines_for_player(market_props, player, mk):
    """Sorted list of distinct lines actually offered in the market for a player/market combo."""
    seen = set()
    for item in market_props:
        if item.get('player') == player and item.get('market_key') == mk:
            try:
                seen.add(round(float(item.get('line')), 2))
            except (TypeError, ValueError):
                pass
    return sorted(seen)

def _parse_prop_markets(bookmakers, valid_names):
    grouped = {}
    for bk in bookmakers or []:
        bkt = bk.get('title')
        for m in bk.get('markets', []) or []:
            mk = m.get('key')
            outs = m.get('outcomes', []) or []
            for o in outs:
                player = o.get('description') or o.get('name')
                if mk.startswith('pitcher_'):
                    player = o.get('description') or o.get('name')
                if not player or player not in valid_names:
                    continue
                side = str(o.get('name', '')).lower()
                if side not in ('over', 'under'):
                    continue
                point = o.get('point')
                key = (player, mk, point, bkt)
                if key not in grouped:
                    grouped[key] = {'player': player, 'market_key': mk, 'line': point, 'bookmaker': bkt, 'over_price': None, 'under_price': None}
                grouped[key][f'{side}_price'] = o.get('price')
    out = []
    for item in grouped.values():
        item['over_implied'] = _american_to_implied(item.get('over_price'))
        item['under_implied'] = _american_to_implied(item.get('under_price'))
        out.append(item)
    return out

def _find_best_available_price(market_props, player, mk, line, side='over'):
    """
    Phase 3: Find best available price across all books for a given prop.
    Returns: (best_price, best_bookmaker) or (None, None) if no match found.
    """
    candidates = [
        (float(item.get(f'{side}_price', -999) or -999), item.get('bookmaker'))
        for item in market_props
        if item.get('player') == player and item.get('market_key') == mk and float(item.get('line', 0)) == float(line)
        and item.get(f'{side}_price') is not None
    ]
    
    if not candidates:
        return None, None
    
    if side == 'over':
        # For positive prices, higher is better; for negative, closer to 0 is better
        best_price, best_book = max(candidates, key=lambda x: x[0] if x[0] > 0 else (1000 + x[0]))
    else:
        best_price, best_book = max(candidates, key=lambda x: x[0] if x[0] > 0 else (1000 + x[0]))
    
    return best_price, best_book

def _market_price_summary(market_props, player, mk, line):
    """Return cross-book pricing summary for a player market at a target line."""
    all_items = [
        item for item in (market_props or [])
        if item.get('player') == player and item.get('market_key') == mk
    ]
    same_line = [
        item for item in all_items
        if float(item.get('line', 0) or 0) == float(line)
    ]

    if not all_items:
        return {
            'best_over_price': None,
            'best_over_book': None,
            'best_under_price': None,
            'best_under_book': None,
            'line_range': None,
            'book_count': 0,
            'market_implied': None,
            'market_bookmaker': None,
            'line_varies': False,
        }

    def _best_side(items, side_key):
        candidates = []
        for it in items:
            px = it.get(side_key)
            if px is None:
                continue
            candidates.append((px, it.get('bookmaker')))
        if not candidates:
            return None, None
        best_px, best_book = max(candidates, key=lambda x: _american_price_score(x[0]))
        return best_px, best_book

    best_over_price, best_over_book = _best_side(same_line, 'over_price')
    best_under_price, best_under_book = _best_side(same_line, 'under_price')

    line_vals = []
    for it in all_items:
        try:
            line_vals.append(float(it.get('line')))
        except Exception:
            continue
    line_range = None
    if line_vals:
        line_range = [round(min(line_vals), 2), round(max(line_vals), 2)]

    books = {it.get('bookmaker') for it in all_items if it.get('bookmaker')}

    return {
        'best_over_price': best_over_price,
        'best_over_book': best_over_book,
        'best_under_price': best_under_price,
        'best_under_book': best_under_book,
        'line_range': line_range,
        'book_count': len(books),
        'market_implied': _american_to_implied(best_over_price),
        'market_bookmaker': best_over_book,
        'line_varies': bool(line_range and line_range[0] != line_range[1]),
    }

def _group_line_shopping(market_props):
    """Group raw prop prices by player+market with all book lines preserved."""
    label_map = {
        'batter_hits': 'Hits',
        'batter_home_runs': 'Home Runs',
        'batter_total_bases': 'Total Bases',
        'batter_rbis': 'RBI',
        'batter_runs_scored': 'Runs',
        'batter_hits_runs_rbis': 'H+R+RBI',
        'batter_stolen_bases': 'Stolen Bases',
        'pitcher_strikeouts': 'Pitcher Strikeouts',
        'pitcher_walks': 'Pitcher Walks',
        'batter_strikeouts': 'Batter Strikeouts',
    }

    grouped = {}
    for item in market_props or []:
        player = item.get('player')
        mk = item.get('market_key')
        if not player or not mk:
            continue
        key = (player, mk)
        line_val = item.get('line')
        try:
            line_val = float(line_val)
        except Exception:
            line_val = None
        row = {
            'bookmaker': item.get('bookmaker'),
            'line': round(line_val, 2) if isinstance(line_val, float) else None,
            'over_price': item.get('over_price'),
            'under_price': item.get('under_price'),
            'over_implied': item.get('over_implied'),
            'under_implied': item.get('under_implied'),
        }
        grouped.setdefault(key, []).append(row)

    out = []
    for (player, mk), books in grouped.items():
        best_over = None
        best_under = None
        line_vals = []
        uniq_books = set()
        for b in books:
            if b.get('bookmaker'):
                uniq_books.add(b.get('bookmaker'))
            if b.get('line') is not None:
                line_vals.append(float(b.get('line')))
            op = b.get('over_price')
            up = b.get('under_price')
            if op is not None and (best_over is None or _american_price_score(op) > _american_price_score(best_over)):
                best_over = op
            if up is not None and (best_under is None or _american_price_score(up) > _american_price_score(best_under)):
                best_under = up

        line_range = None
        if line_vals:
            line_range = [round(min(line_vals), 2), round(max(line_vals), 2)]

        normalized_books = []
        for b in books:
            is_best_over = b.get('over_price') is not None and best_over is not None and b.get('over_price') == best_over
            is_best_under = b.get('under_price') is not None and best_under is not None and b.get('under_price') == best_under
            normalized_books.append({
                **b,
                'is_best_over': is_best_over,
                'is_best_under': is_best_under,
            })

        out.append({
            'player': player,
            'market_key': mk,
            'market_label': label_map.get(mk, mk),
            'best_over_price': best_over,
            'best_under_price': best_under,
            'line_range': line_range,
            'book_count': len(uniq_books),
            'line_varies': bool(line_range and line_range[0] != line_range[1]),
            'books': normalized_books,
        })

    out.sort(key=lambda x: (x.get('player', ''), x.get('market_key', '')))
    by_player = {}
    for g in out:
        by_player.setdefault(g['player'], []).append(g)

    return {'groups': out, 'by_player': by_player}

def _american_price_score(price):
    try:
        p = float(price)
        return p if p > 0 else (1000 + p)
    except Exception:
        return -999999

def _extract_first_inning_era(pitcher_id):
    if not pitcher_id:
        return 4.50
    try:
        year = datetime.now().year
        r = requests.get(
            f"{MLB_API}/people/{pitcher_id}/stats",
            params={"stats": "byInning", "group": "pitching", "season": year},
            timeout=8,
        )
        r.raise_for_status()
        splits = (r.json().get('stats') or [{}])[0].get('splits', []) or []
        for sp in splits:
            label = (sp.get('split', {}).get('description') or sp.get('split', {}).get('value') or '').lower()
            if '1st' not in label and 'first' not in label and label not in ('1', 'inning 1'):
                continue
            stat = sp.get('stat', {}) or {}
            era = _num(stat.get('era'), None)
            if era is not None and era > 0:
                return _clamp(era, 1.20, 12.00)
            ip = _num(stat.get('inningsPitched'), 0.0)
            er = _num(stat.get('earnedRuns'), 0.0)
            if ip > 0:
                return _clamp((er / ip) * 9.0, 1.20, 12.00)
    except Exception:
        pass
    season = pitcher_stats_mlb(pitcher_id) or {}
    return _clamp(_num(season.get('era'), 4.50), 1.20, 12.00)

def _leadoff_handedness_adj(leadoff, opp_pitch_hand):
    if not leadoff:
        return 1.0, {}
    split = hitter_split_profile(leadoff.get('id')) or {}
    code = 'vl' if str(opp_pitch_hand or 'R').upper() == 'L' else 'vr'
    prof = split.get(code) or split.get('vr') or split.get('vl') or {}
    obp = _clamp(_num(prof.get('obp'), 0.320), 0.240, 0.450)
    avg = _clamp(_num(prof.get('avg'), 0.250), 0.170, 0.360)
    pa = int(prof.get('pa') or 0)
    quality = (0.62 * obp) + (0.38 * avg)
    adj = _clamp(0.84 + ((quality - 0.300) * 2.30), 0.78, 1.28)
    if pa and pa < 40:
        adj = 1.0 + ((adj - 1.0) * 0.55)
    return adj, {'obp': round(obp, 3), 'avg': round(avg, 3), 'pa': pa}

def _nrfi_market_snapshot(away_name, home_name):
    if not ODDS_API_KEY:
        return {}
    event, _ = _find_odds_event(away_name, home_name)
    if not event:
        return {}
    event_id = event.get('id')
    if not event_id:
        return {}
    books = _load_event_market_odds(
        event_id,
        markets='h2h_1st_1_innings',
        cache_key='nrfi',
        ttl_sec=_ODDS_NRFI_TTL,
    )
    if not books:
        return {}

    nrfi_prices = []
    yrfi_prices = []
    for bk in books:
        title = bk.get('title')
        for m in bk.get('markets', []) or []:
            mkey = (m.get('key') or '').lower()
            if '1st' not in mkey and 'first' not in mkey:
                continue
            for o in m.get('outcomes', []) or []:
                name = (o.get('name') or '').strip().lower()
                price = o.get('price')
                implied = _american_to_implied(price)
                if implied is None:
                    continue
                rec = {'price': price, 'implied': implied, 'book': title}
                if name in ('nrfi', 'no', 'no run first inning', 'no runs first inning', 'no runs in first inning'):
                    nrfi_prices.append(rec)
                elif name in ('yrfi', 'yes', 'run first inning', 'run in first inning', 'yes run first inning'):
                    yrfi_prices.append(rec)

    out = {}
    if nrfi_prices:
        out['nrfi_implied'] = round(sum(x['implied'] for x in nrfi_prices) / len(nrfi_prices), 4)
        best_nrfi = max(nrfi_prices, key=lambda x: _american_price_score(x['price']))
        out['nrfi_price'] = best_nrfi['price']
        out['nrfi_book'] = best_nrfi['book']
    if yrfi_prices:
        out['yrfi_implied'] = round(sum(x['implied'] for x in yrfi_prices) / len(yrfi_prices), 4)
        best_yrfi = max(yrfi_prices, key=lambda x: _american_price_score(x['price']))
        out['yrfi_price'] = best_yrfi['price']
        out['yrfi_book'] = best_yrfi['book']
    return out

def _compute_nrfi(game_pk):
    g = fetch_schedule_game(game_pk)
    if not g:
        return {'success': False, 'error': 'Game not found'}

    away_team = g.get('teams', {}).get('away', {}).get('team', {})
    home_team = g.get('teams', {}).get('home', {}).get('team', {})
    away_name = away_team.get('name', 'Away')
    home_name = home_team.get('name', 'Home')
    away_abbr = away_team.get('abbreviation', 'AWY')
    home_abbr = home_team.get('abbreviation', 'HME')

    away_p = g.get('teams', {}).get('away', {}).get('probablePitcher', {}) or {}
    home_p = g.get('teams', {}).get('home', {}).get('probablePitcher', {}) or {}
    away_pid = away_p.get('id')
    home_pid = home_p.get('id')
    away_sp_name = away_p.get('fullName', 'Away SP')
    home_sp_name = home_p.get('fullName', 'Home SP')

    away_sp_hand = player_profile(away_pid).get('throws', 'R')
    home_sp_hand = player_profile(home_pid).get('throws', 'R')
    away_i1_era = _extract_first_inning_era(away_pid)
    home_i1_era = _extract_first_inning_era(home_pid)

    park = PARK_FACTORS.get(home_team.get('id'), 1.0)

    venue = g.get('venue', {}) or {}
    venue_id = venue.get('id')
    vloc = (venue.get('location') or {})
    coords = vloc.get('defaultCoordinates', {}) or {}
    lat = coords.get('latitude')
    lon = coords.get('longitude')
    try:
        dt_utc = datetime.fromisoformat(g.get('gameDate', '').replace('Z', '+00:00'))
        utc_off = VENUE_UTC_OFFSET.get(venue_id, -5)
        ghour = (dt_utc + timedelta(hours=utc_off)).hour
    except Exception:
        ghour = 13
    wx = get_weather(lat, lon, ghour, venue_id=venue_id)

    box = requests.get(f"{MLB_API}/game/{game_pk}/boxscore", timeout=10).json().get('teams', {})
    away_lineup = get_batters_from_boxscore(box.get('away', {}), 'away')
    home_lineup = get_batters_from_boxscore(box.get('home', {}), 'home')
    today = datetime.now(ET).strftime('%Y-%m-%d')
    lineup_signature = _game_lineup_signature(g, away_lineup, home_lineup)
    cached = _nrfi_cache.get(game_pk)
    if cached and cached.get('date') == today and cached.get('signature') == lineup_signature:
        return dict(cached.get('payload') or {'success': False, 'error': 'Cached NRFI payload missing'})
    away_leadoff = next((b for b in away_lineup if int(b.get('slot') or 0) == 1), (away_lineup[0] if away_lineup else {}))
    home_leadoff = next((b for b in home_lineup if int(b.get('slot') or 0) == 1), (home_lineup[0] if home_lineup else {}))

    away_hand_adj, away_split = _leadoff_handedness_adj(away_leadoff, home_sp_hand)
    home_hand_adj, home_split = _leadoff_handedness_adj(home_leadoff, away_sp_hand)

    temp = _num(wx.get('temp'), 72)
    wind = _num(wx.get('wind_speed'), 7)
    weather_mult = 1.0
    weather_mult += _clamp((temp - 72.0) * 0.0025, -0.05, 0.08)
    weather_mult += _clamp((wind - 8.0) * 0.0030, -0.03, 0.06)
    weather_mult = _clamp(weather_mult, 0.90, 1.14)

    away_score_rate_i1 = _clamp((home_i1_era / 9.0) * park * away_hand_adj * weather_mult, 0.03, 0.62)
    home_score_rate_i1 = _clamp((away_i1_era / 9.0) * park * home_hand_adj * weather_mult, 0.03, 0.62)

    nrfi_prob = _clamp((1.0 - away_score_rate_i1) * (1.0 - home_score_rate_i1), 0.03, 0.97)
    yrfi_prob = round(1.0 - nrfi_prob, 4)

    odds = _nrfi_market_snapshot(away_name, home_name)
    market_nrfi_implied = odds.get('nrfi_implied')
    market_yrfi_implied = odds.get('yrfi_implied')
    nrfi_edge = None if market_nrfi_implied is None else round(nrfi_prob - market_nrfi_implied, 4)
    yrfi_edge = None if market_yrfi_implied is None else round(yrfi_prob - market_yrfi_implied, 4)

    factors = []
    if home_i1_era <= 3.35 or away_i1_era <= 3.35:
        factors.append('Ace on mound suppresses early offense')
    if home_i1_era >= 5.10 or away_i1_era >= 5.10:
        factors.append('Volatile first-inning starter profile')
    if away_hand_adj <= 0.93:
        factors.append('Weak away leadoff split versus starter hand')
    if home_hand_adj <= 0.93:
        factors.append('Weak home leadoff split versus starter hand')
    if weather_mult <= 0.96:
        factors.append('Run environment dampened by weather')
    if weather_mult >= 1.06:
        factors.append('Weather boosts first-inning scoring risk')
    if park <= 0.97:
        factors.append('Pitcher-friendly park factor')
    elif park >= 1.03:
        factors.append('Hitter-friendly park factor')
    if not factors:
        factors.append('Balanced setup with neutral first-inning profile')

    payload = {
        'success': True,
        'gamePk': game_pk,
        'game': f"{away_abbr} @ {home_abbr}",
        'away': away_abbr,
        'home': home_abbr,
        'away_sp': away_sp_name,
        'home_sp': home_sp_name,
        'away_sp_i1_era': round(away_i1_era, 2),
        'home_sp_i1_era': round(home_i1_era, 2),
        'away_leadoff': away_leadoff.get('name', 'TBD'),
        'home_leadoff': home_leadoff.get('name', 'TBD'),
        'away_score_rate_i1': round(away_score_rate_i1, 4),
        'home_score_rate_i1': round(home_score_rate_i1, 4),
        'nrfi_prob': round(nrfi_prob, 4),
        'yrfi_prob': yrfi_prob,
        'market_nrfi_implied': market_nrfi_implied,
        'market_yrfi_implied': market_yrfi_implied,
        'nrfi_edge': nrfi_edge,
        'yrfi_edge': yrfi_edge,
        'book_price': odds.get('nrfi_price'),
        'bookmaker': odds.get('nrfi_book'),
        'yrfi_book_price': odds.get('yrfi_price'),
        'yrfi_bookmaker': odds.get('yrfi_book'),
        'park_factor': round(park, 3),
        'weather': wx,
        'leadoff_context': {
            'away': away_split,
            'home': home_split,
        },
        'key_factors': factors[:4],
    }
    _nrfi_cache[game_pk] = {
        'date': today,
        'signature': lineup_signature,
        'payload': payload,
    }
    return payload

def api_nrfi(game_pk):
    try:
        out = _compute_nrfi(game_pk)
        if not out.get('success'):
            return jsonify(out), 404
        return jsonify(out)
    except Exception as ex:
        print('[api_nrfi]', traceback.format_exc())
        return jsonify({'success': False, 'error': 'Internal server error'}), 500

def api_market(game_pk):
    try:
        g = fetch_schedule_game(game_pk)
        if not g:
            return jsonify({'success': False, 'error': 'Game not found'}), 404

        away_team = g.get('teams', {}).get('away', {}).get('team', {})
        home_team = g.get('teams', {}).get('home', {}).get('team', {})
        away_name = away_team.get('name', '')
        home_name = home_team.get('name', '')
        away_abbr = away_team.get('abbreviation', 'AWAY')
        home_abbr = home_team.get('abbreviation', 'HOME')

        box = requests.get(f"{MLB_API}/game/{game_pk}/boxscore", timeout=10).json().get('teams', {})
        away_lineup = get_batters_from_boxscore(box.get('away', {}), 'away')
        home_lineup = get_batters_from_boxscore(box.get('home', {}), 'home')
        away_confirmed = len(away_lineup) >= 9
        home_confirmed = len(home_lineup) >= 9

        valid_names = set([x.get('name') for x in away_lineup + home_lineup if x.get('name')])
        away_p = g.get('teams', {}).get('away', {}).get('probablePitcher', {})
        home_p = g.get('teams', {}).get('home', {}).get('probablePitcher', {})
        if away_p.get('fullName'): valid_names.add(away_p.get('fullName'))
        if home_p.get('fullName'): valid_names.add(home_p.get('fullName'))

        event, events = _find_odds_event(away_name, home_name)
        featured = _load_event_odds(event.get('id') if event else None, featured_only=True) if event else []
        props_books = _load_event_odds(event.get('id') if event else None, featured_only=False) if event else []
        props = _parse_prop_markets(props_books, valid_names)

        market = {
            'moneyline': _best_moneyline(featured, away_name, home_name),
            'total': _best_total(featured),
        }

        return jsonify({
            'success': True,
            'meta': {
                'oddsApiConfigured': bool(ODDS_API_KEY),
                'oddsEventFound': bool(event),
                'eventId': event.get('id') if event else None,
                'bookmakersFeatured': len(featured),
                'bookmakersProps': len(props_books),
                'awayAbbr': away_abbr,
                'homeAbbr': home_abbr,
            },
            'lineup': {
                'awayConfirmed': away_confirmed,
                'homeConfirmed': home_confirmed,
                'awayCount': len(away_lineup),
                'homeCount': len(home_lineup),
                'status': g.get('status', {}).get('detailedState', 'Scheduled'),
            },
            'lines': market,
            'playerProps': props[:220],
        })
    except Exception as ex:
        print('[api_market]', traceback.format_exc())
        return jsonify({'success': False, 'error': 'Internal server error'}), 500

def initialize_odds_module():
    _restore_odds_caches()


def register_odds_routes(app):
    app.add_url_rule('/api/odds/cache/status', view_func=api_odds_cache_status)
    app.add_url_rule('/api/odds/cache/refresh', view_func=api_odds_cache_refresh, methods=['POST'])
    app.add_url_rule('/api/nrfi/<int:game_pk>', view_func=api_nrfi)
    app.add_url_rule('/api/market/<int:game_pk>', view_func=api_market)


__all__ = [
    'configure_odds_context', 'initialize_odds_module', 'register_odds_routes',
    'ODDS_API_KEY', 'ODDS_REGION', 'ODDS_CACHE_STORE',
    '_ODDS_EVENTS_CACHE', '_ODDS_GAME_CACHE', '_ODDS_EVENTS_TTL', '_ODDS_GAME_TTL', '_ODDS_NRFI_TTL',
    '_ODDS_DAILY_CACHE', '_ODDS_SNAPSHOT_STRICT', '_ODDS_SNAPSHOT_AUTOWARM', '_ODDS_SNAPSHOT_HEARTBEAT_SEC',
    '_ODDS_SNAPSHOT_RETRY_SEC', '_ODDS_CACHE_LOCK', '_ODDS_SNAPSHOT_WORKER_LOCK', '_ODDS_SNAPSHOT_META',
    '_ODDS_ALL_MARKETS', '_BATTER_PROB_FIELD_FOR', '_BATTER_MEAN_FIELD_FOR_MK', '_BATTER_FALLBACK_LINE', '_K_PROB_FIELD_FOR',
    '_odds_ttl_seconds', '_odds_bool_env', '_odds_today_key', '_odds_cache_entry_fresh', '_persist_odds_caches', '_restore_odds_caches', '_ensure_daily_odds_snapshot', '_odds_snapshot_ready_today', '_start_odds_snapshot_worker', '_norm_name', '_american_to_implied', '_find_odds_event', '_best_moneyline', '_best_total', '_best_spread', '_load_event_odds', '_load_event_market_odds', '_refresh_odds_events_cache', '_odds_cache_status_payload', '_clear_odds_caches_locked', 'api_odds_cache_status', 'api_odds_cache_refresh', '_poisson_over_prob', '_market_lines_for_player', '_parse_prop_markets', '_find_best_available_price', '_market_price_summary', '_group_line_shopping', '_american_price_score', '_extract_first_inning_era', '_leadoff_handedness_adj', '_nrfi_market_snapshot', '_compute_nrfi', 'api_nrfi', 'api_market'
]
