"""Flask integration for the Phase 4.27 intelligence decision endpoint."""
from intelligence_core import build_recommendations


def install_intelligence_api(app_module):
    flask_app = app_module.app
    if 'api_intelligence_recommendations' in flask_app.view_functions:
        return

    @flask_app.route('/api/intelligence/recommendations', methods=['GET'])
    def api_intelligence_recommendations():
        date_str = app_module.request.args.get('date') or None
        tracker = app_module._tracker_today_payload(date_str)
        entries = tracker.get('entries') or tracker.get('picks') or []
        decisions = build_recommendations(entries)
        return app_module.jsonify({
            'success': True,
            'date': tracker.get('date') or date_str,
            'sourceCount': len(entries),
            **decisions,
        })
