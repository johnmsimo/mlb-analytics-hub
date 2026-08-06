"""Flask integration for prediction, explanation, and game-card intelligence."""
import threading
import time
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
from simulation_engine import enrich_simulations


_GAME_CARD_CACHE = {}
_GAME_CARD_CACHE_LOCK = threading.Lock()
_GAME_CARD_CACHE_TTL = 120


def _has_price(row, category):
    if category == 'pitcher_strikeouts':
        keys = ('bestOverPrice', 'best_over_price', 'bestUnderPrice', 'best_under_price')
    else:
        keys = (
            'bestOverPrice', 'best_over_price',
            'bestAvailablePrice', 'marketPrice',
        )
    return any(row.get(key) is not None for key in keys)


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
        """Return one confidence-first hit, K-side, and moneyline decision."""
        date_str = app_module.request.args.get('date') or datetime.now(
            app_module.ET
        ).strftime('%Y-%m-%d')
        cache_key = (int(game_pk), date_str)
        refresh = app_module.request.args.get('refresh') == '1'
        now = time.time()
        if not refresh:
            with _GAME_CARD_CACHE_LOCK:
                cached = _GAME_CARD_CACHE.get(cache_key)
            if cached and now - cached['timestamp'] < _GAME_CARD_CACHE_TTL:
                return app_module.jsonify(dict(cached['payload'], cached=True))

        tracker = app_module._tracker_today_payload(date_str)
        all_tracker_entries = tracker.get('entries') or tracker.get('picks') or []
        rows = [
            dict(row) for row in all_tracker_entries
            if str(row.get('gamePk')) == str(game_pk)
        ]
        usable_counts = {
            category: sum(
                classify_pick(row) == category
                and _has_price(row, category)
                and row.get('sharedSimulationBacked') is True
                for row in rows
            )
            for category in CATEGORY_ORDER
        }
        usable = {
            category: count >= (2 if category == 'game_winner' else 1)
            for category, count in usable_counts.items()
        }
        candidate_pool_complete = any(
            row.get('intelligenceCandidatePoolComplete') is True
            for row in rows
        )

        # Tracker capture is the fastest source when it contains priced rows for
        # every required market. Otherwise rebuild this game's candidates through
        # the same full simulation and live-odds path used by tracker capture.
        generated = []
        if not all(usable.values()) or not candidate_pool_complete:
            try:
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
            except Exception:
                app_module.logging.warning(
                    '[game_card_intelligence] live generation failed for %s',
                    game_pk,
                    exc_info=True,
                )

        # Freshly generated rows replace duplicate tracker rows while preserving
        # any already-captured markets that generation could not rebuild.
        merged = {}
        for row in rows + list(generated):
            key = (
                row.get('marketKey'), row.get('playerId'), row.get('player'),
                row.get('team'), row.get('line'), row.get('recommendedSide'),
            )
            merged[key] = dict(row)
        candidates = prepare_game_card_candidates(merged.values())
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
        payload = {
            'success': True,
            'date': date_str,
            'gamePk': game_pk,
            'sourceCount': len(merged),
            'generatedSourceCount': len(generated),
            'quickPicksVersion': '4.35',
            'pickConfidenceVersion': '4.34',
            'matchupSimulationVersion': '4.35',
            'recommendationSource': (
                'shared_game_matchup_simulation'
                if fully_backed else 'simulation_unavailable_or_partial'
            ),
            'simulationReady': fully_backed,
            'simulationAudit': audit,
            'explanationVersion': '4.32',
            **decisions,
        }
        with _GAME_CARD_CACHE_LOCK:
            if len(_GAME_CARD_CACHE) >= 80:
                oldest = min(
                    _GAME_CARD_CACHE,
                    key=lambda key: _GAME_CARD_CACHE[key]['timestamp'],
                )
                _GAME_CARD_CACHE.pop(oldest, None)
            _GAME_CARD_CACHE[cache_key] = {
                'timestamp': time.time(),
                'payload': payload,
            }
        return app_module.jsonify(payload)
