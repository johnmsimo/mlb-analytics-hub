"""
nrfi_odds_routes.py — Flask route registration for NRFI/YRFI live odds + devig.

Called from app.py with:  from nrfi_odds_routes import register_nrfi_odds_routes
                          register_nrfi_odds_routes(app)

Endpoints added:
  GET  /api/nrfi/odds?away=<team>&home=<team>&nrfi_prob=<0.0-1.0>
  GET  /api/nrfi/devig?nrfi_american=<int>&yrfi_american=<int>
  GET  /api/nrfi/odds-cache-status
"""

from flask import jsonify, request
import logging

logger = logging.getLogger(__name__)


def register_nrfi_odds_routes(app):
    """Register all NRFI odds routes onto a Flask app instance."""

    # ------------------------------------------------------------------
    # GET /api/nrfi/odds
    # Query params:
    #   away        - away team name (e.g. "New York Yankees")
    #   home        - home team name (e.g. "Boston Red Sox")
    #   nrfi_prob   - model NRFI probability 0–1 (optional, enables edge calc)
    # ------------------------------------------------------------------
    @app.route("/api/nrfi/odds", methods=["GET"])
    def api_nrfi_odds():
        """
        Returns live NRFI/YRFI odds, devig fair probs, and edge vs. your model.

        Example:
          /api/nrfi/odds?away=Yankees&home=Red+Sox&nrfi_prob=0.72

        Response shape:
          {
            success: true,
            book_price: -130,               # Best NRFI American line found
            bookmaker: "DraftKings",
            market_nrfi_implied: 0.5652,    # Raw implied prob from that line
            market_yrfi_implied: 0.4878,
            fair_nrfi_prob: 0.5363,         # Power-deviggged true probability
            nrfi_edge: 0.0837,              # model_prob - fair_prob (positive = value)
            yrfi_edge: -0.0837,
            devig: { overround, methods: {additive, multiplicative, power, worst_case}, recommended },
            all_books: [ {book, nrfi_american, yrfi_american, nrfi_implied, yrfi_implied}, ... ],
            odds_api_remaining: "498"
          }
        """
        try:
            from nrfi_odds import get_nrfi_odds_for_game
        except ImportError as ie:
            logger.error(f"[api_nrfi_odds] nrfi_odds module missing: {ie}")
            return jsonify({"success": False, "error": "nrfi_odds module not found"}), 500

        away = (request.args.get("away") or "").strip()
        home = (request.args.get("home") or "").strip()
        if not away or not home:
            return jsonify({"success": False, "error": "away and home query params are required"}), 400

        nrfi_prob_raw = request.args.get("nrfi_prob")
        model_nrfi_prob = None
        if nrfi_prob_raw:
            try:
                model_nrfi_prob = float(nrfi_prob_raw)
                model_nrfi_prob = max(0.001, min(0.999, model_nrfi_prob))
            except ValueError:
                return jsonify({"success": False, "error": "nrfi_prob must be a float 0–1"}), 400

        try:
            result = get_nrfi_odds_for_game(away, home, model_nrfi_prob)
        except Exception as ex:
            logger.exception(f"[api_nrfi_odds] {ex}")
            return jsonify({"success": False, "error": str(ex)}), 500

        if not result:
            return jsonify({
                "success": False,
                "error":   "No odds found for this game. Check team names or ODDS_API_KEY.",
                "away":    away,
                "home":    home,
            }), 404

        return jsonify({"success": True, **result})

    # ------------------------------------------------------------------
    # GET /api/nrfi/devig
    # Query params:
    #   nrfi_american  - NRFI American odds integer (e.g. -130)
    #   yrfi_american  - YRFI American odds integer (e.g. +110)
    # ------------------------------------------------------------------
    @app.route("/api/nrfi/devig", methods=["GET"])
    def api_nrfi_devig():
        """
        Standalone devig endpoint. Pass any NRFI/YRFI American odds pair
        and get all four devig methods back.

        Example:
          /api/nrfi/devig?nrfi_american=-130&yrfi_american=110

        Response shape:
          {
            success: true,
            nrfi_american: -130,
            yrfi_american: 110,
            overround: 0.0209,
            methods: {
              additive:       { nrfi_fair_prob, yrfi_fair_prob, nrfi_fair_american, yrfi_fair_american },
              multiplicative: { ... },
              power:          { ... },
              worst_case:     { ... }
            },
            recommended: { ... }   // power devig
          }
        """
        try:
            from nrfi_odds import devig_all, american_to_prob
        except ImportError as ie:
            return jsonify({"success": False, "error": "nrfi_odds module not found"}), 500

        try:
            nrfi_american = float(request.args.get("nrfi_american", 0))
            yrfi_american = float(request.args.get("yrfi_american", 0))
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "nrfi_american and yrfi_american must be numeric"}), 400

        if nrfi_american == 0 or yrfi_american == 0:
            return jsonify({"success": False, "error": "Both nrfi_american and yrfi_american are required"}), 400

        result = devig_all(nrfi_american, yrfi_american)
        if not result:
            return jsonify({"success": False, "error": "Devig calculation failed"}), 500

        return jsonify({
            "success":       True,
            "nrfi_american": nrfi_american,
            "yrfi_american": yrfi_american,
            **result,
        })

    # ------------------------------------------------------------------
    # GET /api/nrfi/odds-cache-status
    # Returns how stale the odds cache is and remaining API quota.
    # ------------------------------------------------------------------
    @app.route("/api/nrfi/odds-cache-status", methods=["GET"])
    def api_nrfi_odds_cache_status():
        try:
            from nrfi_odds import _odds_cache, NRFI_CACHE_TTL, ODDS_API_KEY
            import time as _time
            age_sec = round(_time.time() - _odds_cache["ts"], 1)
            return jsonify({
                "success":          True,
                "cache_age_sec":    age_sec,
                "cache_ttl_sec":    NRFI_CACHE_TTL,
                "is_fresh":         age_sec < NRFI_CACHE_TTL,
                "games_cached":     len(_odds_cache["data"]),
                "odds_api_remaining": _odds_cache.get("remaining"),
                "api_key_set":      bool(ODDS_API_KEY),
            })
        except Exception as ex:
            return jsonify({"success": False, "error": str(ex)}), 500
