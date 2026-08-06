"""Flask integration for prediction intelligence and learning analytics."""
from context_engine import enrich_context
from explanation_engine import explain_decisions
from intelligence_core import build_recommendations
from learning_engine import analyze_learning
from matchup_engine import enrich_matchups
from simulation_engine import enrich_simulations


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
