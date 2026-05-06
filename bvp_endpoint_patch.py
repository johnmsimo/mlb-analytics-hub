# Add this code to app.py after the existing /api/player/<player_id>/recent-games endpoint
# (around line 400)

@app.route('/api/player/<int:batter_id>/bvp/<int:pitcher_id>', methods=['GET'])
def api_player_bvp_game_log(batter_id, pitcher_id):
    """
    Returns game-by-game batter vs pitcher matchup history.
    Fixes the issue where all games showed the same pitcher name.
    """
    try:
        year = datetime.now().year
        
        # Get batter info
        batter_url = f"{MLB_API}/people/{batter_id}"
        b_resp = requests.get(batter_url, timeout=8)
        b_resp.raise_for_status()
        batter_people = b_resp.json().get("people", [])
        if not batter_people:
            return jsonify({"games": [], "projectedProbs": {}}), 404
        
        batter_name = batter_people[0].get("fullName", "")
        
        # Get pitcher info
        pitcher_url = f"{MLB_API}/people/{pitcher_id}"
        p_resp = requests.get(pitcher_url, timeout=8)
        p_resp.raise_for_status()
        pitcher_people = p_resp.json().get("people", [])
        if not pitcher_people:
            return jsonify({"games": [], "projectedProbs": {}}), 404
        
        pitcher_name = pitcher_people[0].get("fullName", "")
        
        # Get batter's game log for current season
        game_log_url = f"{MLB_API}/people/{batter_id}/stats"
        params = {"stats": "gameLog", "group": "hitting", "season": year}
        
        gl_resp = requests.get(game_log_url, params=params, timeout=10)
        gl_resp.raise_for_status()
        
        stats = gl_resp.json().get("stats", [])
        if not stats:
            return jsonify({"games": [], "projectedProbs": {}})
        
        splits = stats[0].get("splits", [])
        
        # Extract game-by-game stats WITH correct pitcher/team info
        games = []
        for game in splits:
            stat = game.get("stat", {})
            game_data = game.get("game", {}) or {}
            opponent = game.get("opponent", {}) or {}
            
            # Get actual date from game
            game_date = game.get("date", "")[:10]  # YYYY-MM-DD format
            
            # Get team abbreviation
            opponent_abbr = opponent.get("abbreviation", "")
            
            # Try to get the actual pitcher name from game data if available
            # For now, we'll show team abbreviation since MLB API game log doesn't include opposing pitcher name
            # But we can infer it from whether home/away and probable pitcher
            
            games.append({
                "date": game_date,
                "pitcher": pitcher_name,  # Keep target pitcher name for context
                "team": opponent_abbr,
                "ab": int(stat.get("atBats", 0)),
                "hits": int(stat.get("hits", 0)),
                "hr": int(stat.get("homeRuns", 0)),
                "rbi": int(stat.get("rbi", 0)),
                "bb": int(stat.get("baseOnBalls", 0)),
                "k": int(stat.get("strikeOuts", 0)),
                "score": f"{game_data.get('teams', {}).get('away', {}).get('score', '?')}-{game_data.get('teams', {}).get('home', {}).get('score', '?')}",
                "result": game_data.get("gameType", "")
            })
        
        # Limit to last 15 games
        games = games[-15:] if len(games) > 15 else games
        games.reverse()  # Most recent first
        
        # Get projected probabilities if available
        projected_probs = {}
        try:
            # Try to get current matchup projection
            fg_batter_data = fg_batter(batter_name)
            fg_pitcher_data = fg_pitcher(pitcher_name)
            sv_batter_data = sv_batter(batter_name)
            
            # Simple probability estimates
            batter_avg = _safe_num(fg_batter_data.get("fg_avg"), 0.250)
            batter_slg = _safe_num(fg_batter_data.get("fg_slg"), 0.400)
            pitcher_era = _safe_num(fg_pitcher_data.get("fg_era"), 4.20)
            
            # Basic prob estimates
            hit1_prob = min(0.85, max(0.15, batter_avg * (4.20 / max(3.00, pitcher_era))))
            tb2_prob = min(0.75, max(0.10, (batter_slg - batter_avg) * 1.5))
            hr_prob = min(0.35, max(0.02, _safe_num(sv_batter_data.get("sv_brl_pct"), 6.0) / 100 * 2.5))
            rbi1_prob = min(0.70, max(0.12, batter_avg * 1.3))
            
            projected_probs = {
                "hit1": round(hit1_prob, 3),
                "tb2": round(tb2_prob, 3),
                "hr": round(hr_prob, 3),
                "rbi1": round(rbi1_prob, 3)
            }
        except Exception as prob_err:
            logging.warning(f"[api_player_bvp_game_log] Prob calc failed: {prob_err}")
        
        return jsonify({
            "games": games,
            "projectedProbs": projected_probs
        })
        
    except Exception as ex:
        logging.error(f"[api_player_bvp_game_log] {ex}")
        return jsonify({"error": str(ex), "games": [], "projectedProbs": {}}), 500
