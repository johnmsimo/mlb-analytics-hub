"""Flask integration for prediction, explanation, and game-card intelligence."""
import math
import time
from datetime import date, datetime

from actionability import ACTIONABILITY_VERSION, evaluate_actionability
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
    select_game_card_projection_picks,
    select_game_card_quick_picks,
)
from intelligence_core import build_recommendations, classify_pick
from intelligence_control_plane import (
    apply_drift_interventions,
    build_intelligence_control_plane,
)
from learning_engine import analyze_learning
from market_validation import VALIDATION_VERSION, apply_market_gates
from odds_lineage import ODDS_LINEAGE_VERSION, clv_summary
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
_GAME_CARD_JOB_TIMEOUT_SECONDS = 150
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
    # Version the cache whenever the terminal response contract changes so a
    # deploy cannot keep serving an older indefinitely-computing payload.
    return normalize_cache_key('game_card_intelligence_v439', game_pk, date_str)


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


def _phase6_report(app_module, date_str, entries):
    cached_builder = getattr(
        app_module, '_current_phase6_intelligence_report', None,
    ) if app_module is not None else None
    if callable(cached_builder):
        return cached_builder(date_str)
    return build_intelligence_control_plane(
        entries,
        as_of=date.fromisoformat(date_str),
        window_days=120,
    )


def _decision_payload(
    game_pk,
    date_str,
    rows,
    all_tracker_entries,
    generated_count=0,
    phase6_report=None,
):
    candidates = prepare_game_card_candidates(rows)
    contextual = enrich_context(candidates)
    matchups = enrich_matchups(contextual)
    simulated = enrich_simulations(matchups)
    learning = analyze_learning(all_tracker_entries)
    integrity = evaluate_candidates(simulated)
    market_gates = apply_market_gates(integrity['eligible'], learning)
    phase6 = phase6_report or _phase6_report(
        None, date_str, all_tracker_entries,
    )
    interventions = apply_drift_interventions(market_gates['promoted'], phase6)
    integrity_eligible = interventions['promoted']
    promoted_categories = {
        classify_pick(row) for row in integrity_eligible
        if classify_pick(row) in CATEGORY_ORDER
    }
    watchlist_sources = [
        row for row in (
            list(market_gates['rejected']) + list(interventions['rejected'])
        )
        if row.get('sharedSimulationBacked') is True
        and classify_pick(row) in CATEGORY_ORDER
        and classify_pick(row) not in promoted_categories
    ]
    watchlist_decisions = select_game_card_quick_picks(
        watchlist_sources,
        learning=learning,
    )
    watchlist_picks = []
    for source in watchlist_decisions.get('quickPicks') or []:
        row = dict(source)
        gate_reasons = (
            row.get('promotionReasons')
            or row.get('marketGateReasons')
            or row.get('actionabilityReasons')
            or ['Historical market validation has not promoted this signal.']
        )
        row.update({
            'recommendationGrade': 'Watchlist',
            'selectionMode': 'research_only',
            'isActionable': False,
            'actionable': False,
            'promotionStatus': 'research_only',
            'watchlistReason': str(gate_reasons[0]),
            'watchlistReasons': list(gate_reasons)[:3],
            'recommendedAction': (
                'Analysis only. Do not track or add this signal to a parlay '
                'until its market-validation gate passes.'
            ),
        })
        watchlist_picks.append(row)
    covered_watchlist_categories = {
        classify_pick(row) for row in watchlist_picks
    }
    projection_picks = [
        row for row in select_game_card_projection_picks(
            integrity['rejected'],
            learning=learning,
        )
        if classify_pick(row) not in covered_watchlist_categories
    ]
    watchlist_picks.extend(projection_picks)
    audit = simulation_audit(integrity_eligible)
    simulation_backed = [
        row for row in integrity_eligible
        if row.get('sharedSimulationBacked') is True
    ]
    decisions = select_game_card_quick_picks(
        simulation_backed,
        learning=learning,
        market_gate_rejections=(
            list(market_gates['rejected']) + list(interventions['rejected'])
        ),
    )
    backed_category_counts = {
        category: sum(
            classify_pick(row) == category for row in simulation_backed
        )
        for category in CATEGORY_ORDER
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
    drift_abstention = bool(
        not promoted_categories and interventions['rejected']
    )
    analysis_ready = bool(watchlist_picks) or fully_backed
    decision_ready = fully_backed or validation_abstention or analysis_ready
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
        'actionabilityVersion': ACTIONABILITY_VERSION,
        'actionabilityAudit': (market_gates['audit'].get('actionabilityAudit') or {}),
        'marketValidationVersion': VALIDATION_VERSION,
        'marketGateAudit': market_gates['audit'],
        'phase6IntelligenceVersion': phase6['version'],
        'phase6DriftAudit': interventions['audit'],
        'marketValidation': learning.get('marketValidation'),
        'pickConfidenceVersion': '4.34',
        'matchupSimulationVersion': '4.35',
        'performanceVersion': '4.36',
        'deliveryArchitecture': 'redis_durable_worker',
        'recommendationSource': (
            'shared_game_matchup_simulation'
            if fully_backed else 'simulation_refresh_pending'
            if not decision_ready else 'phase6_drift_abstention'
            if drift_abstention else 'market_validation_abstention'
            if validation_abstention else 'projection_analysis'
        ),
        'simulationReady': fully_backed,
        'analysisReady': analysis_ready,
        'decisionReady': decision_ready,
        'computationState': 'ready' if decision_ready else 'computing',
        'simulationAudit': audit,
        'explanationVersion': '4.32',
        'watchlistPicks': watchlist_picks,
        'watchlistCount': len(watchlist_picks),
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
        'actionabilityVersion': ACTIONABILITY_VERSION,
        'actionabilityAudit': {
            'version': ACTIONABILITY_VERSION,
            'sourceCount': 0,
            'actionableCount': 0,
            'rejectedCount': 0,
            'stageCounts': {},
            'rejectionReasons': {},
        },
        'pickConfidenceVersion': '4.34',
        'matchupSimulationVersion': '4.35',
        'performanceVersion': '4.36',
        'marketValidationVersion': VALIDATION_VERSION,
        'deliveryArchitecture': 'redis_durable_worker',
        'recommendationSource': 'simulation_refresh_pending',
        'simulationReady': False,
        'analysisReady': False,
        'decisionReady': False,
        'computationState': 'computing',
        'simulationAudit': simulation_audit([]),
        'explanationVersion': '4.32',
        'watchlistPicks': [],
        'watchlistCount': 0,
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
        phase6_report=_phase6_report(
            app_module, date_str, all_tracker_entries,
        ),
    )


def _schedule_game_card_refresh(_app_module, game_pk, date_str):
    cache_key = _cache_key(game_pk, date_str)
    try:
        job = enqueue_job(
            'game_card',
            {'gamePk': int(game_pk), 'date': date_str},
            dedupe_key=cache_key,
            timeout_seconds=_GAME_CARD_JOB_TIMEOUT_SECONDS,
            max_attempts=1,
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
        # A completed worker must always leave a terminal cache record.  A
        # missing or incomplete simulation is an explicit unavailable answer,
        # not a retry loop that leaves the game card "updating" indefinitely.
        payload.update({
            'computing': False,
            'computationState': 'unavailable',
            'recommendationSource': 'simulation_unavailable',
            'message': (
                'No complete linked player-prop simulation is available for '
                'this game. Refresh after lineup or source data updates.'
            ),
        })
    _write_cached_payload(game_pk, date_str, payload)
    return payload


def _clv_provenance(row):
    """Expose only canonical, auditable CLV evidence on the Picks contract."""
    receipt = row.get('closingIntegrity')
    lineage = row.get('oddsLineage')
    has_receipt = isinstance(receipt, dict)
    has_lineage = isinstance(lineage, dict)
    accepted = (
        has_receipt
        and has_lineage
        and lineage.get('version') == ODDS_LINEAGE_VERSION
        and lineage.get('clvEligible') is True
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
    snapshots = lineage.get('snapshots') if has_lineage else {}
    current = snapshots.get('current') if isinstance(snapshots, dict) else {}
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
        'reason': (
            lineage.get('clvReason')
            if has_lineage and not accepted
            else receipt.get('reason')
            if has_receipt and not accepted
            else 'missing_integrity_receipt'
            if not has_lineage
            else None
        ),
        'lineageVersion': lineage.get('version') if has_lineage else None,
        'clvEligible': accepted,
        'currentFreshness': current.get('freshness') if isinstance(current, dict) else None,
        'opening': snapshots.get('opening') if isinstance(snapshots, dict) else None,
        'current': current,
        'closing': snapshots.get('closing') if isinstance(snapshots, dict) else None,
    }



def _pick_evidence(row, clv):
    """Return the normalized decision inputs carried by an actionable pick."""
    return {
        'market': row.get('marketKey') or row.get('categoryLabel') or row.get('intelligenceCategory'),
        'side': row.get('recommendedSide') or row.get('side'),
        'line': row.get('line'),
        'openingPrice': row.get('openingPrice'),
        'currentPrice': row.get('currentPrice'),
        'closingPrice': row.get('closingPrice'),
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
        'clvDenominator': 'clvGradedCount',
        'oddsLineageVersion': clv.get('lineageVersion'),
        'currentOddsFreshness': clv.get('currentFreshness'),
    }


def _evidence_integrity(evidence):
    """Fail closed when an actionable pick lacks decision-ready evidence."""
    required = (
        'market', 'side', 'price', 'book', 'probabilityPct',
        'edgePct', 'freshnessSeconds', 'lineupStatus',
    )
    reasons = []
    for field in required:
        value = evidence.get(field)
        if value is None or (
            isinstance(value, str) and not value.strip()
        ):
            reasons.append(f'missing {field}')

    for field in ('price', 'probabilityPct', 'edgePct'):
        value = evidence.get(field)
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            reasons.append(f'invalid {field}')
            continue
        if not math.isfinite(number):
            reasons.append(f'invalid {field}')

    freshness = evidence.get('freshnessSeconds')
    if freshness is not None:
        try:
            freshness_value = float(freshness)
        except (TypeError, ValueError):
            reasons.append('invalid freshnessSeconds')
        else:
            if (
                not math.isfinite(freshness_value)
                or freshness_value < 0
                or freshness_value > _MAX_ACTIONABLE_CACHE_AGE
            ):
                reasons.append('freshnessSeconds exceeds actionable window')

    lineup_status = str(evidence.get('lineupStatus') or '').strip().lower()
    if lineup_status in {'out', 'inactive', 'unknown', 'unconfirmed'}:
        reasons.append('lineup is not confirmed or projected')

    return {
        'version': '4.45',
        'status': 'verified' if not reasons else 'rejected',
        'verified': not reasons,
        'reasons': reasons,
    }
 
 
def _ranking_number(row, *keys):
    """Return a finite numeric ranking value without letting bad input abort Picks."""
    for key in keys:
        value = row.get(key)
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            return number
    return 0.0


def _stable_candidate_key(row):
    """Build a deterministic tie-break key from the candidate identity fields."""
    return "|".join(
        str(row.get(key) or "").strip().lower()
        for key in (
            "canonicalCandidateId", "id", "gamePk", "marketKey", "playerId",
            "player", "team", "line", "recommendedSide", "side", "book", "price",
        )
    )


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
        phase6 = _phase6_report(app_module, effective_date, history)
        interventions = apply_drift_interventions(
            market_gates['promoted'], phase6,
        )
        decisions = explain_decisions(
            build_recommendations(interventions['promoted']),
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
            'phase6IntelligenceVersion': phase6['version'],
            'phase6DriftAudit': interventions['audit'],
            'marketValidation': learning.get('marketValidation'),
            'calibrationVersion': '4.54',
            'oddsLineageVersion': ODDS_LINEAGE_VERSION,
            'clvAudit': (learning.get('marketValidation') or {}).get('clvAudit') or {},
            'calibrationAudit': (learning.get('marketValidation') or {}).get('calibrationAudit') or {},
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
        actionability_rejections = []
        actionability_rejection_reasons = {}
        evidence_rejections = []
        evidence_rejection_reasons = {}
        for pick in payload.get('card') or []:
            row = dict(pick)
            if str(row.get('recommendationGrade') or '').lower() == 'pass':
                continue
            actionability = evaluate_actionability(
                row,
                require_market_validation=True,
            )
            row.update(actionability)
            if not actionability['actionable']:
                actionability_rejections.append(row)
                for reason in actionability['actionabilityReasons']:
                    actionability_rejection_reasons[reason] = (
                        actionability_rejection_reasons.get(reason, 0) + 1
                    )
                continue
            row.setdefault('book', row.get('bestAvailableBook') or row.get('bestBook'))
            row.setdefault('price', row.get('bestAvailablePrice') or row.get('marketPrice'))
            row.setdefault('probabilityPct', row.get('modelProbabilityPct'))
            row.setdefault('edgePct', row.get('estimatedEdgePct'))
            row.setdefault('lineupStatus', row.get('lineupSource') or row.get('lineup_status'))
            row.setdefault('freshnessSeconds', row.get('oddsAgeSeconds'))
            clv = _clv_provenance(row)
            row['clvProvenance'] = clv
            evidence = _pick_evidence(row, clv)
            evidence_integrity = _evidence_integrity(evidence)
            row['evidence'] = evidence
            row['evidenceIntegrity'] = evidence_integrity
            if not evidence_integrity['verified']:
                evidence_rejections.append(row)
                for reason in evidence_integrity['reasons']:
                    evidence_rejection_reasons[reason] = (
                        evidence_rejection_reasons.get(reason, 0) + 1
                    )
                continue
            candidates.append(row)
        ranking_method = (
            'pickScore_desc_then_edgePct_desc_then_candidateKey_asc'
        )
        candidates.sort(key=lambda row: (
            -_ranking_number(row, 'pickScore', 'decisionScore'),
            -_ranking_number(row, 'estimatedEdgePct', 'edgePct', 'edge'),
            _stable_candidate_key(row),
        ))
        actionable_limit = 5
        ranked_candidates = []
        for rank, candidate in enumerate(candidates, start=1):
            ranked = dict(candidate)
            primary_score = _ranking_number(
                candidate, 'pickScore', 'decisionScore'
            )
            edge_score = _ranking_number(
                candidate, 'estimatedEdgePct', 'edgePct', 'edge'
            )
            stable_key = _stable_candidate_key(candidate)
            ranked['selectionAudit'] = {
                'version': '4.49',
                'rank': rank,
                'rankedBy': ranking_method,
                'rankingScore': primary_score,
                'tieBreakEdgePct': edge_score,
                'stableOrderKey': stable_key,
                'disposition': (
                    'displayed'
                    if rank <= actionable_limit
                    else 'withheld_by_actionable_cap'
                ),
            }
            ranked_candidates.append(ranked)
        displayed_ranks = [
            item['selectionAudit']['rank']
            for item in ranked_candidates[:actionable_limit]
        ]
        withheld_ranks = [
            item['selectionAudit']['rank']
            for item in ranked_candidates[actionable_limit:]
        ]
        cap_boundary = None
        if len(ranked_candidates) >= actionable_limit:
            boundary = ranked_candidates[actionable_limit - 1]
            boundary_audit = boundary['selectionAudit']
            cap_boundary = {
                'rank': boundary_audit['rank'],
                'rankingScore': boundary_audit['rankingScore'],
                'tieBreakEdgePct': boundary_audit['tieBreakEdgePct'],
                'stableOrderKey': boundary_audit['stableOrderKey'],
            }
        picks = ranked_candidates[:actionable_limit]
        displayed_count = len(picks)
        rejected_count = len(evidence_rejections)
        withheld_count = max(0, len(candidates) - displayed_count)
        cap_applied = withheld_count > 0
        if not displayed_count:
            audit_status = 'rejected' if rejected_count else 'unavailable'
        elif rejected_count:
            audit_status = 'partial'
        elif cap_applied:
            audit_status = 'capped'
        else:
            audit_status = 'verified'
        actionability_stage_counts = {}
        for row in actionability_rejections:
            stage = str(row.get('actionabilityStage') or 'Validated')
            actionability_stage_counts[stage] = (
                actionability_stage_counts.get(stage, 0) + 1
            )
        actionability_stage_counts['Actionable'] = len(candidates)
        return app_module.jsonify({
            'success': True,
            'contractVersion': '4.52',
            'actionabilityVersion': ACTIONABILITY_VERSION,
            'evidenceVersion': '4.45',
            'evidenceIntegrityVersion': '4.45',
            'evidenceAuditVersion': '4.50',
            'date': payload.get('date'),
            'picks': picks,
            'count': len(picks),
            'researchOnly': not bool(picks),
            'selectionAuditVersion': '4.50',
            'actionabilityAudit': {
                'version': ACTIONABILITY_VERSION,
                'candidateCount': (
                    len(actionability_rejections)
                    + len(candidates)
                    + len(evidence_rejections)
                ),
                'actionableCount': len(candidates),
                'rejectedCount': (
                    len(actionability_rejections)
                    + len(evidence_rejections)
                ),
                'stageCounts': dict(sorted(actionability_stage_counts.items())),
                'rejectionReasons': dict(sorted(actionability_rejection_reasons.items())),
                'evidenceRejectedCount': len(evidence_rejections),
                'evidenceRejectionReasons': dict(
                    sorted(evidence_rejection_reasons.items())
                ),
            },
            'evidenceAudit': {
                'version': '4.49',
                'status': audit_status,
                'actionableLimit': actionable_limit,
                'capApplied': cap_applied,
                'withheldCount': withheld_count,
                'rankingVersion': '4.49',
                'rankingMethod': ranking_method,
                'deterministic': True,
                'selectionRule': (
                    'stable deterministic ranking, then highest-ranked validated '
                    'candidates up to actionableLimit'
                ),
                'rankedCandidateCount': len(ranked_candidates),
                'candidateCount': len(candidates) + len(evidence_rejections),
                'acceptedCount': len(candidates),
                'rejectedCount': rejected_count,
                'displayedCount': displayed_count,
                'rejectionReasons': dict(sorted(evidence_rejection_reasons.items())),
            },
            'selectionAudit': {
                'version': '4.50',
                'displayedRanks': displayed_ranks,
                'withheldRanks': withheld_ranks,
                'capBoundary': cap_boundary,
                'capBoundaryRank': (
                    cap_boundary['rank'] if cap_boundary else None
                ),
                'selectionRule': (
                    'displayedRanks are the highest-ranked validated candidates; '
                    'withheldRanks begin immediately after actionableLimit'
                ),
            },
            'passes': len(payload.get('passes') or payload.get('rejected') or []),
            'marketValidation': payload.get('marketValidation'),
            'calibrationVersion': '4.54',
            'oddsLineageVersion': ODDS_LINEAGE_VERSION,
            'clvAudit': (payload.get('marketValidation') or {}).get('clvAudit') or {},
            'calibrationAudit': (payload.get('marketValidation') or {}).get('calibrationAudit') or {},
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
                safe_projection_picks = [
                    dict(row) for row in (
                        cached['payload'].get('watchlistPicks') or []
                    )
                    if row.get('selectionMode') == 'projection_only'
                    and row.get('isActionable') is False
                ]
                cached_payload = _pending_payload(
                    game_pk,
                    date_str,
                    int(cached['payload'].get('sourceCount') or 0),
                )
                if safe_projection_picks:
                    cached_payload.update({
                        'analysisReady': True,
                        'watchlistPicks': safe_projection_picks,
                        'watchlistCount': len(safe_projection_picks),
                        'recommendationSource': 'cached_projection_analysis',
                    })
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
        # Tracker capture is not guaranteed to run before a user opens a game.
        # Reuse the durable slate producer that already powers My Hub so Quick
        # Props can return analyzed rows immediately while its complete
        # per-game candidate pool refreshes in the worker.
        if not rows:
            scan_loader = getattr(app_module, '_props_scan_today_payload', None)
            if callable(scan_loader):
                scan = scan_loader(date_str) or {}
                rows = [
                    dict(row) for row in (scan.get('props') or [])
                    if str(row.get('gamePk')) == str(game_pk)
                ]
        pool_ready = _candidate_pool_ready(rows) if rows else False
        payload = (
            _decision_payload(
                game_pk,
                date_str,
                rows,
                all_tracker_entries,
                generated_count=0,
                phase6_report=_phase6_report(
                    app_module, date_str, all_tracker_entries,
                ),
            )
            if rows
            else _pending_payload(game_pk, date_str, len(rows))
        )
        if payload.get('decisionReady') and pool_ready and not refresh:
            _write_cached_payload(game_pk, date_str, payload)
            return app_module.jsonify(dict(payload, cached=False, computing=False))

        job = _schedule_game_card_refresh(app_module, game_pk, date_str)
        computing = bool(job and job.get('status') in {'queued', 'running'})
        payload.update({
            'computing': computing,
            'refreshStatus': job,
            'retryAfterSeconds': 4,
            'message': (
                (
                    'Analysis is available now; refreshing the complete linked '
                    'matchup simulation in the background.'
                    if payload.get('analysisReady') else
                    'Refreshing the linked matchup simulation in the background. '
                    'This card will update automatically.'
                )
                if computing else
                ((job or {}).get('error') or 'Matchup simulation unavailable.')
            ),
        })
        return app_module.jsonify(payload)
