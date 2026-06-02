"""
draftkings_odds_routes.py — Flask route registration for DraftKings direct odds scraper.

Register in app.py immediately after the nrfi block (lines 478-479):

    from nrfi_odds_routes import register_nrfi_odds_routes
    register_nrfi_odds_routes(app)

    from draftkings_odds_routes import register_draftkings_odds_routes  # <- ADD
    register_draftkings_odds_routes(app)                                # <- ADD

Endpoints added:
  GET /api/dk/game-lines
  GET /api/dk/props?category=batter|pitcher
  GET /api/dk/props/player?name=<player>&category=batter|pitcher
  GET /api/dk/all
  GET /api/dk/categories
  GET /api/dk/cache-status
"""

import logging
from flask import jsonify, request

logger = logging.getLogger(__name__)


def register_draftkings_odds_routes(app):
    """Register all DraftKings odds routes onto a Flask app instance."""

    # ------------------------------------------------------------------
    # GET /api/dk/game-lines
    # Returns today's MLB game lines: moneyline, run line, totals.
    # ------------------------------------------------------------------
    @app.route("/api/dk/game-lines", methods=["GET"])
    def api_dk_game_lines():
        try:
            from draftkings_odds import get_game_lines
        except ImportError as exc:
            logger.error(f"[api_dk_game_lines] {exc}")
            return jsonify({"success": False, "error": "draftkings_odds module not found"}), 500
        try:
            games = get_game_lines()
        except Exception as exc:
            logger.exception(f"[api_dk_game_lines] {exc}")
            return jsonify({"success": False, "error": str(exc)}), 500
        return jsonify({"success": True, "count": len(games), "games": games})

    # ------------------------------------------------------------------
    # GET /api/dk/props?category=batter|pitcher
    # Returns all player props for the given category.
    # Each prop includes fair_over_prob / fair_under_prob / overround
    # via power devig — ready for edge calculation.
    # ------------------------------------------------------------------
    @app.route("/api/dk/props", methods=["GET"])
    def api_dk_props():
        try:
            from draftkings_odds import get_player_props
        except ImportError as exc:
            logger.error(f"[api_dk_props] {exc}")
            return jsonify({"success": False, "error": "draftkings_odds module not found"}), 500

        category = (request.args.get("category") or "batter").strip().lower()
        if category not in ("batter", "pitcher"):
            return jsonify({"success": False, "error": "category must be 'batter' or 'pitcher'"}), 400
        try:
            props = get_player_props(category)
        except Exception as exc:
            logger.exception(f"[api_dk_props] {exc}")
            return jsonify({"success": False, "error": str(exc)}), 500
        return jsonify({"success": True, "category": category, "count": len(props), "props": props})

    # ------------------------------------------------------------------
    # GET /api/dk/props/player?name=X&category=batter|pitcher
    # Returns props for a single player — feed directly into your
    # Monte Carlo / XGBoost edge pipeline.
    # ------------------------------------------------------------------
    @app.route("/api/dk/props/player", methods=["GET"])
    def api_dk_props_player():
        try:
            from draftkings_odds import get_props_for_player
        except ImportError as exc:
            logger.error(f"[api_dk_props_player] {exc}")
            return jsonify({"success": False, "error": "draftkings_odds module not found"}), 500

        name = (request.args.get("name") or "").strip()
        if not name:
            return jsonify({"success": False, "error": "name query param is required"}), 400

        category = (request.args.get("category") or "batter").strip().lower()
        if category not in ("batter", "pitcher"):
            return jsonify({"success": False, "error": "category must be 'batter' or 'pitcher'"}), 400
        try:
            props = get_props_for_player(name, category)
        except Exception as exc:
            logger.exception(f"[api_dk_props_player] {exc}")
            return jsonify({"success": False, "error": str(exc)}), 500

        if not props:
            return jsonify({
                "success":      False,
                "error":        f"No props found for '{name}' in '{category}'.",
                "player_query": name,
                "category":     category,
            }), 404

        return jsonify({
            "success":      True,
            "player_query": name,
            "category":     category,
            "count":        len(props),
            "props":        props,
        })

    # ------------------------------------------------------------------
    # GET /api/dk/all
    # Game lines + batter props + pitcher props in one payload.
    # Uses cache when fresh; re-fetches with 1.2s delays when stale.
    # ------------------------------------------------------------------
    @app.route("/api/dk/all", methods=["GET"])
    def api_dk_all():
        try:
            from draftkings_odds import get_all_mlb_odds
        except ImportError as exc:
            logger.error(f"[api_dk_all] {exc}")
            return jsonify({"success": False, "error": "draftkings_odds module not found"}), 500
        try:
            data = get_all_mlb_odds()
        except Exception as exc:
            logger.exception(f"[api_dk_all] {exc}")
            return jsonify({"success": False, "error": str(exc)}), 500
        return jsonify({"success": True, **data})

    # ------------------------------------------------------------------
    # GET /api/dk/categories
    # Discovers live DraftKings MLB offer category IDs.
    # Hit this endpoint if props/lines stop returning data after a DK update.
    # ------------------------------------------------------------------
    @app.route("/api/dk/categories", methods=["GET"])
    def api_dk_categories():
        try:
            from draftkings_odds import discover_offer_categories, _live_categories
        except ImportError as exc:
            logger.error(f"[api_dk_categories] {exc}")
            return jsonify({"success": False, "error": "draftkings_odds module not found"}), 500
        try:
            cats = discover_offer_categories()
        except Exception as exc:
            logger.exception(f"[api_dk_categories] {exc}")
            return jsonify({"success": False, "error": str(exc)}), 500
        return jsonify({
            "success":           True,
            "count":             len(cats),
            "live_category_map": _live_categories,
            "all_categories":    cats,
        })

    # ------------------------------------------------------------------
    # GET /api/dk/cache-status
    # Returns cache age, record count, TTL, and active geo per bucket.
    # ------------------------------------------------------------------
    @app.route("/api/dk/cache-status", methods=["GET"])
    def api_dk_cache_status():
        try:
            from draftkings_odds import cache_status
        except ImportError as exc:
            logger.error(f"[api_dk_cache_status] {exc}")
            return jsonify({"success": False, "error": "draftkings_odds module not found"}), 500
        try:
            status = cache_status()
        except Exception as exc:
            logger.exception(f"[api_dk_cache_status] {exc}")
            return jsonify({"success": False, "error": str(exc)}), 500
        return jsonify({"success": True, **status})
