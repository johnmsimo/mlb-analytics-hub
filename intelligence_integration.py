"""Flask integration for prediction, explanation, and game-card intelligence."""
import time
from datetime import datetime

from candidate_integrity import (
    INTEGRITY_VERSION,
    CandidateIntegrityPolicy,
    evaluate_candidates,
)
from context_engine import enrich_context
from explanation_engine import explain_decisions
from game_card_intelligence import (
    CATEGORY_ORDER,
    prepare_game_card_candidates,
    select_game_card_quick_picks,
)
from intelligence_core import build_recommendations, classify_pick
from learning_engine import analyze_learning
from market_validation import VALIDATION_VERSION, apply_market_gates
from matchup_engine import enrich_matchups
from matchup_simulation_intelligence import simulation_audit
from cache_service import normalize_cache_key
from redis_client import get_redis
from simulation_engine import enrich_simulations
from task_queue import (
    JobQueueUnavailable,
    enqueue_job,
    get_job_queue,
    write_durable_json,
)


_GAME_CARD_CACHE_TTL = 300
_GAME_CARD_STALE_TTL = 3600
_MAX_ACTIONABLE_CACHE_AGE = CandidateIntegrityPolicy().maximum_odds_age_seconds


def _validation_history(app_module, date_str, fallback=()):
    collector = getattr(app_module, '_collect_window_entries', None)
    if callable(collector):
        return collector(date_str, 180)
    return list(fallback or [])


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
    return normalize_cache_key('game_card_intelligence_v438', game_pk, date_str)


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
    key = _cache_key(game_pk, date_str)
    try:
        write_durable_json(key, record, ttl=_GAME_CARD_STALE_TTL)
    except JobQueueUnavailable:
        get_redis().set(key, record, ttl=_GAME_CARD_STALE_TTL)


def _candidate_pool_ready(rows):
    prepared = prepare_game_card_candidates(rows)
    integrity = evaluate_candidates(prepared)
    eligible = integrity['eligible']
    usable_counts = {
        category: sum(
            classify_pick(row) == category
            and row.get('sharedSimulationBacked') is True
            for row in eligible
        )
        for category in CATEGORY_ORDER
    }
    markets_ready = all(count >= 1 for count in usable_counts.values())
    simulated_moneyline_sides = sum(
        classify_pick(row) == 'game_winner'
        and row.get('sharedSimulationBacked') is True
        for row in prepared
    )
    pool_complete = any(
        row.get('intelligenceCandidatePoolComplete') is True for row in eligible
    )
    return markets_ready and simulated_moneyline_sides >= 2 and pool_complete


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
    integrity = evaluate_candidates(simulated)
    market_gates = apply_market_gates(integrity['eligible'], learning)
    integrity_eligible = market_gates['promoted']
    audit = simulation_audit(integrity_eligible)
    simulation_backed = [
        row for row in integrity_eligible
        if row.get('sharedSimulationBacked') is True
    ]
    decisions = select_game_card_quick_picks(
        simulation_backed,
        learning=learning,
        market_gate_rejections=market_gates['rejected'],
    )
    backed_category_counts = {
        category: sum(
            classify_pick(row) == category for row in simulation_backed
        )
        for category in CATEGORY_ORDER
    }
    promoted_categories = {
        classify_pick(row) for row in integrity_eligible
        if classify_pick(row) in CATEGORY_ORDER
    }
    required_markets_ready = bool(promoted_categories) and all(
        backed_category_counts[category] >= 1
        for category in promoted_categories
    )
    simulated_moneyline_sides = sum(
        classify_pick(row) == 'game_winner'
        and row.get('sharedSimulationBacked') is True
        for row in simulated
    )
    fully_backed = (
        audit['candidateCount'] > 0
        and audit['simulationBackedCount'] == audit['candidateCount']
        and required_markets_ready
        and (
            'game_winner' not in promoted_categories
            or simulated_moneyline_sides >= 2
        )
        and any(
            row.get('intelligenceCandidatePoolComplete') is True
            for row in simulation_backed
        )
    )
    validation_abstention = (
        not promoted_categories
        and market_gates['audit']['candidateCount'] > 0
    )
    decision_ready = fully_backed or validation_abstention
    # Never promote a partial market pool as a finished set of simulated picks.
    # A previously cached complete snapshot may still be served while this one
    # rebuilds, but a cold partial response is only a progress state.
    if not decision_ready:
        decisions = dict(decisions)
        decisions['quickPicks'] = []
    return {
        'success': True,
        'date': date_str,
        'gamePk': game_pk,
        'sourceCount': len(rows),
        'generatedSourceCount': generated_count,
        'quickPicksVersion': VALIDATION_VERSION,
        'candidateIntegrityVersion': INTEGRITY_VERSION,
        'candidateIntegrityAudit': integrity['audit'],
        'marketValidationVersion': VALIDATION_VERSION,
        'marketGateAudit': market_gates['audit'],
        'marketValidation': learning.get('marketValidation'),
        'pickConfidenceVersion': '4.34',
        'matchupSimulationVersion': '4.35',
        'performanceVersion': '4.36',
        'deliveryArchitecture': 'redis_durable_worker',
        'recommendationSource': (
            'shared_game_matchup_simulation'
            if fully_backed else 'simulation_refresh_pending'
            if not validation_abstention else 'market_validation_abstention'
        ),
        'simulationReady': fully_backed,
        'decisionReady': decision_ready,
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
        'quickPicksVersion': VALIDATION_VERSION,
        'candidateIntegrityVersion': INTEGRITY_VERSION,
        'candidateIntegrityAudit': evaluate_candidates([])['audit'],
        'pickConfidenceVersion': '4.34',
        'matchupSimulationVersion': '4.35',
        'performanceVersion': '4.36',
        'marketValidationVersion': VALIDATION_VERSION,
        'deliveryArchitecture': 'redis_durable_worker',
        'recommendationSource': 'simulation_refresh_pending',
        'simulationReady': False,
        'decisionReady': False,
        'simulationAudit': simulation_audit([]),
        'explanationVersion': '4.32',
        **decisions,
    }


def _generate_game_card_payload(app_module, game_pk, date_str):
    tracker = app_module._tracker_today_payload(date_str)
    current_entries = tracker.get('entries') or tracker.get('picks') or []
    all_tracker_entries = _validation_history(
        app_module, date_str, current_entries,
    )
    captured = [
        dict(row) for row in current_entries
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
        decision_only=True,
    ) or []
    merged = _merge_candidate_rows(captured, generated)
    return _decision_payload(
        game_pk,
        date_str,
        merged,
        all_tracker_entries,
        generated_count=len(generated),
    )


def _schedule_game_card_refresh(_app_module, game_pk, date_str):
    cache_key = _cache_key(game_pk, date_str)
    try:
        job = enqueue_job(
            'game_card',
            {'gamePk': int(game_pk), 'date': date_str},
            dedupe_key=cache_key,
            timeout_seconds=300,
            max_attempts=2,
        )
        return get_job_queue().snapshot(job)
    except JobQueueUnavailable:
        return {
            'status': 'unavailable',
            'elapsedSeconds': 0,
            'error': 'Durable simulation worker is unavailable.',
        }


def run_game_card_job(app_module, args):
    """Worker entry point; never called by a Flask request thread."""
    game_pk = int(args['gamePk'])
    date_str = str(args['date'])
    payload = _generate_game_card_payload(app_module, game_pk, date_str)
    if not payload.get('decisionReady'):
        raise RuntimeError('shared simulation candidate pool is incomplete')
    _write_cached_payload(game_pk, date_str, payload)
    return payload


def _clv_provenance(row):
    """Expose only auditable CLV evidence on the primary Picks contract."""
    receipt = row.get('closingIntegrity')
    has_receipt = isinstance(receipt, dict)
    accepted = (
        has_receipt
        and receipt.get('accepted') is True
        and receipt.get('fresh') is True
    )
    status = (
        'verified'
        if accepted
        else 'rejected'
        if has_receipt and receipt.get('accepted') is False
        else 'unverified'
    )
    return {
        'status': status,
        'verified': accepted,
        'edge': row.get('clvEdge') if accepted else None,
        'source': receipt.get('source') if has_receipt else None,
        'book': row.get('closingBook') if has_receipt else None,
        'capturedAt': (
            row.get('closingCapturedAt')
            or receipt.get('capturedAt')
            if has_receipt else row.get('closingCapturedAt')
        ),
        'reason': receipt.get('reason') if has_receipt else 'missing_integrity_receipt',
    }



def _pick_evidence(row, clv):
    """Return the normalized decision inputs carried by an actionable pick."""
    return {
        'market': row.get('marketKey') or row.get('categoryLabel') or row.get('intelligenceCategory'),
        'side': row.get('recommendedSide') or row.get('side'),
        'line': row.get('line'),
        'price': row.get('price'),
        'book': row.get('book'),
        'probabilityPct': row.get('probabilityPct'),
        'edgePct': row.get('edgePct'),
        'freshnessSeconds': row.get('freshnessSeconds'),
        'lineupStatus': row.get('lineupStatus'),
        'clvStatus': clv.get('status'),
        'verifiedClvEdge': clv.get('edge'),
        'clvSource': clv.get('source'),
        'clvCapturedAt': clv.get('capturedAt'),
    }

def install_intelligence_api(app_module):
    flask_app = app_module.app
    if 'api_intelligence_recommendations' in flask_app.view_functions:
        return

    def _recommendation_payload():
        date_str = app_module.request.args.get('date') or None
        tracker = app_module._tracker_today_payload(date_str)
        entries = tracker.get('entries') or tracker.get('picks') or []
        contextual_entries = enrich_context(entries)
        matchup_entries = enrich_matchups(contextual_entries)
        simulated_entries = enrich_simulations(matchup_entries)
        effective_date = tracker.get('date') or date_str or datetime.now(
            app_module.ET
        ).strftime('%Y-%m-%d')
        history = _validation_history(app_module, effective_date, entries)
        learning = analyze_learning(history)
        market_gates = apply_market_gates(simulated_entries, learning)
        decisions = explain_decisions(
            build_recommendations(market_gates['promoted']),
            learning=learning,
        )
        return {
            'success': True,
            'date': tracker.get('date') or date_str,
            'sourceCount': len(entries),
            'contextVersion': '4.28',
            'matchupVersion': '4.29',
            'simulationVersion': '4.30',
            'learningVersion': VALIDATION_VERSION,
            'marketValidationVersion': VALIDATION_VERSION,
            'marketGateAudit': market_gates['audit'],
            'marketValidation': learning.get('marketValidation'),
            'explanationVersion': '4.32',
            **decisions,
        }

    @flask_app.route('/api/intelligence/recommendations', methods=['GET'])
    def api_intelligence_recommendations():
        return app_module.jsonify(_recommendation_payload())

    @flask_app.route('/api/picks/today', methods=['GET'])
    def api_picks_today():
        """Single authoritative, actionable Picks contract.

        The older recommendation endpoints remain available for research and
        compatibility, but this route is the only surface intended to drive
        the primary betting experience. It deliberately caps the actionable
        set and carries the evidence needed to make a decision without
        opening another page.
        """
        payload = _recommendation_payload()
        candidates = []
        for pick in payload.get('card') or []:
            row = dict(pick)
            if str(row.get('recommendationGrade') or '').lower() == 'pass':
                continue
            row.setdefault('book', row.get('bestAvailableBook') or row.get('bestBook'))
            row.setdefault('price', row.get('bestAvailablePrice') or row.get('marketPrice'))
            row.setdefault('probabilityPct', row.get('modelProbabilityPct'))
            row.setdefault('edgePct', row.get('estimatedEdgePct'))
            row.setdefault('lineupStatus', row.get('lineupSource') or row.get('lineup_status'))
            row.setdefault('freshnessSeconds', row.get('oddsAgeSeconds'))
            clv = _clv_provenance(row)
            row['clvProvenance'] = clv
            row['evidence'] = _pick_evidence(row, clv)
            candidates.append(row)
        candidates.sort(key=lambda row: (
            -float(row.get('pickScore') or row.get('decisionScore') or 0),
            -float(row.get('estimatedEdgePct') or row.get('edge') or 0),
        ))
        picks = candidates[:5]
        return app_module.jsonify({
            'success': True,
            'contractVersion': '4.44',
            'evidenceVersion': '4.44',
            'date': payload.get('date'),
            'picks': picks,
            'count': len(picks),
            'researchOnly': not bool(picks),
            'passes': len(payload.get('passes') or payload.get('rejected') or []),
            'marketValidation': payload.get('marketValidation'),
            'marketGateAudit': payload.get('marketGateAudit'),
            'sourceCount': payload.get('sourceCount', 0),
            'message': ('No market currently passes the validation gate; projections remain research-only.'
                        if not picks else 'Only the highest-ranked validated plays are shown.'),
        })

    @flask_app.route('/api/intelligence/learning', methods=['GET'])
    def api_intelligence_learning():
        date_str = app_module.request.args.get('date') or None
        tracker = app_module._tracker_today_payload(date_str)
        effective_date = tracker.get('date') or date_str or datetime.now(
            app_module.ET
        ).strftime('%Y-%m-%d')
        try:
            window = max(30, min(365, int(
                app_module.request.args.get('window', 180) or 180
            )))
        except (TypeError, ValueError):
            window = 180
        collector = getattr(app_module, '_collect_window_entries', None)
        entries = (
            collector(effective_date, window)
            if callable(collector)
            else tracker.get('entries') or tracker.get('picks') or []
        )
        analytics = analyze_learning(entries)
        return app_module.jsonify({
            'success': True,
            'date': effective_date,
            'window': window,
            'sourceCount': len(entries),
            'learningVersion': VALIDATION_VERSION,
            **analytics,
        })

    @flask_app.route('/api/intelligence/validation', methods=['GET'])
    def api_intelligence_validation():
        date_str = app_module.request.args.get('date') or datetime.now(
            app_module.ET
        ).strftime('%Y-%m-%d')
        report = app_module._current_market_validation_report(date_str)
        return app_module.jsonify({
            'success': True,
            'date': date_str,
            'marketValidationVersion': VALIDATION_VERSION,
            **report,
        })

    @flask_app.route('/api/intelligence/game-card/<int:game_pk>', methods=['GET'])
    def api_intelligence_game_card(game_pk):
        """Serve cached decisions instantly and enqueue rebuilds for the worker."""
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
            computing = bool(job and job.get('status') in {'queued', 'running'})
            cached_payload = cached['payload']
            integrity_expired = age > _MAX_ACTIONABLE_CACHE_AGE
            if integrity_expired:
                # Keep stale-while-refresh responsive without presenting an
                # expired sportsbook snapshot as a current recommendation.
                cached_payload = _pending_payload(
                    game_pk,
                    date_str,
                    int(cached['payload'].get('sourceCount') or 0),
                )
            return app_module.jsonify(dict(
                cached_payload,
                cached=True,
                stale=True,
                integrityExpired=integrity_expired,
                cacheAgeSec=age,
                computing=computing,
                refreshStatus=job,
                retryAfterSeconds=4,
                message=(
                    'Sportsbook prices expired; refreshing verified picks in '
                    'the background.'
                    if integrity_expired else
                    'Refreshing verified picks in the background.'
                ),
            ))

        tracker = app_module._tracker_today_payload(date_str)
        current_entries = tracker.get('entries') or tracker.get('picks') or []
        all_tracker_entries = _validation_history(
            app_module, date_str, current_entries,
        )
        rows = [
            dict(row) for row in current_entries
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
        if payload.get('decisionReady') and not refresh:
            _write_cached_payload(game_pk, date_str, payload)
            return app_module.jsonify(dict(payload, cached=False, computing=False))

        job = _schedule_game_card_refresh(app_module, game_pk, date_str)
        computing = bool(job and job.get('status') in {'queued', 'running'})
        payload.update({
            'computing': computing,
            'refreshStatus': job,
            'retryAfterSeconds': 4,
            'message': (
                'Refreshing the linked matchup simulation in the background. '
                'This card will update automatically.'
                if computing else
                ((job or {}).get('error') or 'Matchup simulation unavailable.')
            ),
        })
        return app_module.jsonify(payload)
