"""Flask integration for prediction, explanation, and game-card intelligence."""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from context_engine import enrich_context
from explanation_engine import explain_decisions
from game_card_intelligence import (
    CATEGORY_ORDER,
    prepare_game_card_candidates,
    select_game_card_quick_picks,
)
from intelligence_core import build_recommendations, classify_pick
from learning_engine import analyze_learning
from matchup_engine import enrich_matchups
from matchup_simulation_intelligence import simulation_audit
from cache_service import normalize_cache_key
from redis_client import get_redis
from simulation_engine import enrich_simulations


_GAME_CARD_CACHE_TTL = 300
_GAME_CARD_STALE_TTL = 3600
_GAME_CARD_JOB_RETRY_TTL = 30
_GAME_CARD_JOBS = {}
_GAME_CARD_JOBS_LOCK = threading.RLock()
# One serialized builder prevents a dashboard slate from launching several
# CPU-heavy simulations at once. Request threads only read snapshots or enqueue.
_GAME_CARD_EXECUTOR = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix='game-card-intelligence',
)


def _has_price(row, category):
    if category == 'pitcher_strikeouts':
        keys = ('bestOverPrice', 'best_over_price', 'bestUnderPrice', 'best_under_price')
    else:
        keys = (
            'bestOverPrice', 'best_over_price',
            'bestAvailablePrice', 'marketPrice',
        )
    return any(row.get(key) is not None for key in keys)


def _cache_key(game_pk, date_str):
    return normalize_cache_key('game_card_intelligence_v4351', game_pk, date_str)


def _read_cached_payload(game_pk, date_str):
    try:
        record = get_redis().get(_cache_key(game_pk, date_str))
    except Exception:
        return None
    if not isinstance(record, dict) or not isinstance(record.get('payload'), dict):
        return None
    return record


def _write_cached_payload(game_pk, date_str, payload):
    record = {'timestamp': time.time(), 'payload': dict(payload)}
    get_redis().set(
        _cache_key(game_pk, date_str),
        record,
        ttl=_GAME_CARD_STALE_TTL,
    )


def _candidate_pool_ready(rows):
    usable_counts = {
        category: sum(
            classify_pick(row) == category
            and _has_price(row, category)
            and row.get('sharedSimulationBacked') is True
            for row in rows
        )
        for category in CATEGORY_ORDER
    }
    markets_ready = all(
        count >= (2 if category == 'game_winner' else 1)
        for category, count in usable_counts.items()
    )
    pool_complete = any(
        row.get('intelligenceCandidatePoolComplete') is True for row in rows
    )
    return markets_ready and pool_complete


def _merge_candidate_rows(captured, generated):
    merged = {}
    for row in list(captured) + list(generated):
        key = (
            row.get('marketKey'), row.get('playerId'), row.get('player'),
            row.get('team'), row.get('line'), row.get('recommendedSide'),
        )
        merged[key] = dict(row)
    return list(merged.values())


def _decision_payload(game_pk, date_str, rows, all_tracker_entries, generated_count=0):
    candidates = prepare_game_card_candidates(rows)
    contextual = enrich_context(candidates)
    matchups = enrich_matchups(contextual)
    simulated = enrich_simulations(matchups)
    learning = analyze_learning(all_tracker_entries)
    audit = simulation_audit(simulated)
    simulation_backed = [
        row for row in simulated if row.get('sharedSimulationBacked') is True
    ]
    decisions = select_game_card_quick_picks(
        simulation_backed,
        learning=learning,
    )
    backed_category_counts = {
        category: sum(
            classify_pick(row) == category for row in simulation_backed
        )
        for category in CATEGORY_ORDER
    }
    required_markets_ready = all(
        count >= (2 if category == 'game_winner' else 1)
        for category, count in backed_category_counts.items()
    )
    fully_backed = (
        audit['candidateCount'] > 0
        and audit['simulationBackedCount'] == audit['candidateCount']
        and required_markets_ready
        and any(
            row.get('intelligenceCandidatePoolComplete') is True
            for row in simulation_backed
        )
    )
    # Never promote a partial market pool as a finished set of simulated picks.
    # A previously cached complete snapshot may still be served while this one
    # rebuilds, but a cold partial response is only a progress state.
    if not fully_backed:
        decisions = dict(decisions)
        decisions['quickPicks'] = []
    return {
        'success': True,
        'date': date_str,
        'gamePk': game_pk,
        'sourceCount': len(rows),
        'generatedSourceCount': generated_count,
        'quickPicksVersion': '4.35.1',
        'pickConfidenceVersion': '4.34',
        'matchupSimulationVersion': '4.35',
        'performanceVersion': '4.35.1',
        'recommendationSource': (
            'shared_game_matchup_simulation'
            if fully_backed else 'simulation_refresh_pending'
        ),
        'simulationReady': fully_backed,
        'simulationAudit': audit,
        'explanationVersion': '4.32',
        **decisions,
    }


def _pending_payload(game_pk, date_str, source_count):
    decisions = select_game_card_quick_picks([], learning={})
    return {
        'success': True,
        'date': date_str,
        'gamePk': game_pk,
        'sourceCount': source_count,
        'generatedSourceCount': 0,
        'quickPicksVersion': '4.35.1',
        'pickConfidenceVersion': '4.34',
        'matchupSimulationVersion': '4.35',
        'performanceVersion': '4.35.1',
        'recommendationSource': 'simulation_refresh_pending',
        'simulationReady': False,
        'simulationAudit': simulation_audit([]),
        'explanationVersion': '4.32',
        **decisions,
    }


def _generate_game_card_payload(app_module, game_pk, date_str):
    tracker = app_module._tracker_today_payload(date_str)
    all_tracker_entries = tracker.get('entries') or tracker.get('picks') or []
    captured = [
        dict(row) for row in all_tracker_entries
        if str(row.get('gamePk')) == str(game_pk)
    ]
    schedule = app_module.fetch_schedule(date_str)
    adjustments = dict(app_module._get_adjustments() or {})
    adjustments['captured_per_game'] = max(
        250,
        int(adjustments.get('captured_per_game') or 0),
    )
    generated = app_module._build_tracker_rows_for_game(
        game_pk,
        date_str,
        adjustments=adjustments,
        _sched=schedule,
        include_odds=True,
    ) or []
    merged = _merge_candidate_rows(captured, generated)
    return _decision_payload(
        game_pk,
        date_str,
        merged,
        all_tracker_entries,
        generated_count=len(generated),
    )


def _job_snapshot(cache_key):
    with _GAME_CARD_JOBS_LOCK:
        job = dict(_GAME_CARD_JOBS.get(cache_key) or {})
    if not job:
        return None
    started = float(job.get('started') or time.time())
    return {
        'status': job.get('status') or 'queued',
        'elapsedSeconds': max(0, int(time.time() - started)),
        'error': job.get('error'),
    }


def _schedule_game_card_refresh(app_module, game_pk, date_str):
    cache_key = _cache_key(game_pk, date_str)
    now = time.time()
    with _GAME_CARD_JOBS_LOCK:
        current = _GAME_CARD_JOBS.get(cache_key)
        if current and current.get('status') in {'queued', 'running'}:
            return _job_snapshot(cache_key)
        if (
            current
            and current.get('status') == 'error'
            and now - float(current.get('finished') or now) < _GAME_CARD_JOB_RETRY_TTL
        ):
            return _job_snapshot(cache_key)
        _GAME_CARD_JOBS[cache_key] = {
            'status': 'queued',
            'started': now,
            'error': None,
        }

    def _run():
        with _GAME_CARD_JOBS_LOCK:
            _GAME_CARD_JOBS[cache_key]['status'] = 'running'
        try:
            payload = _generate_game_card_payload(app_module, game_pk, date_str)
            if not payload.get('simulationReady'):
                raise RuntimeError('shared simulation candidate pool is incomplete')
            _write_cached_payload(game_pk, date_str, payload)
            with _GAME_CARD_JOBS_LOCK:
                _GAME_CARD_JOBS[cache_key].update({
                    'status': 'done',
                    'finished': time.time(),
                })
        except Exception as exc:
            logger = getattr(app_module, 'logging', logging)
            logger.warning(
                '[game_card_intelligence] background refresh failed for %s',
                game_pk,
                exc_info=True,
            )
            with _GAME_CARD_JOBS_LOCK:
                _GAME_CARD_JOBS[cache_key].update({
                    'status': 'error',
                    'finished': time.time(),
                    'error': str(exc)[:180],
                })

    _GAME_CARD_EXECUTOR.submit(_run)
    return _job_snapshot(cache_key)


def install_intelligence_api(app_module):
    flask_app = app_module.app
    if 'api_intelligence_recommendations' in flask_app.view_functions:
        return

    @flask_app.route('/api/intelligence/recommendations', methods=['GET'])
    def api_intelligence_recommendations():
        date_str = app_module.request.args.get('date') or None
        tracker = app_module._tracker_today_payload(date_str)
        entries = tracker.get('entries') or tracker.get('picks') or []
        contextual_entries = enrich_context(entries)
        matchup_entries = enrich_matchups(contextual_entries)
        simulated_entries = enrich_simulations(matchup_entries)
        learning = analyze_learning(simulated_entries)
        decisions = explain_decisions(
            build_recommendations(simulated_entries),
            learning=learning,
        )
        return app_module.jsonify({
            'success': True,
            'date': tracker.get('date') or date_str,
            'sourceCount': len(entries),
            'contextVersion': '4.28',
            'matchupVersion': '4.29',
            'simulationVersion': '4.30',
            'learningVersion': '4.31',
            'explanationVersion': '4.32',
            **decisions,
        })

    @flask_app.route('/api/intelligence/learning', methods=['GET'])
    def api_intelligence_learning():
        date_str = app_module.request.args.get('date') or None
        tracker = app_module._tracker_today_payload(date_str)
        entries = tracker.get('entries') or tracker.get('picks') or []
        analytics = analyze_learning(entries)
        return app_module.jsonify({
            'success': True,
            'date': tracker.get('date') or date_str,
            'sourceCount': len(entries),
            'learningVersion': '4.31',
            **analytics,
        })

    @flask_app.route('/api/intelligence/game-card/<int:game_pk>', methods=['GET'])
    def api_intelligence_game_card(game_pk):
        """Serve cached decisions instantly and rebuild heavy simulations off-thread."""
        date_str = app_module.request.args.get('date') or datetime.now(
            app_module.ET
        ).strftime('%Y-%m-%d')
        refresh = app_module.request.args.get('refresh') == '1'
        now = time.time()
        cached = _read_cached_payload(game_pk, date_str)
        if cached:
            age = max(0, int(now - float(cached.get('timestamp') or now)))
            if not refresh and age < _GAME_CARD_CACHE_TTL:
                return app_module.jsonify(dict(
                    cached['payload'],
                    cached=True,
                    cacheAgeSec=age,
                    computing=False,
                ))
            job = _schedule_game_card_refresh(app_module, game_pk, date_str)
            return app_module.jsonify(dict(
                cached['payload'],
                cached=True,
                stale=True,
                cacheAgeSec=age,
                computing=True,
                refreshStatus=job,
                retryAfterSeconds=4,
            ))

        tracker = app_module._tracker_today_payload(date_str)
        all_tracker_entries = tracker.get('entries') or tracker.get('picks') or []
        rows = [
            dict(row) for row in all_tracker_entries
            if str(row.get('gamePk')) == str(game_pk)
        ]
        payload = (
            _decision_payload(
                game_pk,
                date_str,
                rows,
                all_tracker_entries,
                generated_count=0,
            )
            if _candidate_pool_ready(rows)
            else _pending_payload(game_pk, date_str, len(rows))
        )
        if payload.get('simulationReady') and not refresh:
            _write_cached_payload(game_pk, date_str, payload)
            return app_module.jsonify(dict(payload, cached=False, computing=False))

        job = _schedule_game_card_refresh(app_module, game_pk, date_str)
        payload.update({
            'computing': True,
            'refreshStatus': job,
            'retryAfterSeconds': 4,
            'message': (
                'Refreshing the linked matchup simulation in the background. '
                'This card will update automatically.'
            ),
        })
        return app_module.jsonify(payload)
