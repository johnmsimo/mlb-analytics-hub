"""Flask integration for context- and matchup-aware prediction intelligence."""
from context_engine import enrich_context
from intelligence_core import build_recommendations
from matchup_engine import enrich_matchups


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
        decisions = build_recommendations(matchup_entries)
        return app_module.jsonify({
            'success': True,
            'date': tracker.get('date') or date_str,
            'sourceCount': len(entries),
            'contextVersion': '4.28',
            'matchupVersion': '4.29',
            **decisions,
        })
