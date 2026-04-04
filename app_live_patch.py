

# ── Live Game Data Route ────────────────────────────────────────────────────
@app.route("/api/game/livedata/<int:game_pk>")
def api_game_livedata(game_pk):
    try:
        r = requests.get(
            f"https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live",
            timeout=10
        )
        r.raise_for_status()
        data = r.json()

        ls       = data.get("liveData", {}).get("linescore", {})
        box      = data.get("liveData", {}).get("boxscore",  {})
        gdata    = data.get("gameData", {})
        status_d = gdata.get("status", {}).get("detailedState", "")

        # Inning label
        inning_num  = ls.get("currentInning", 0)
        inning_half = ls.get("inningHalf", "Top")
        if any(x in status_d for x in ["Middle", "Mid ", "Between"]):
            inning_label = f"MID {inning_num}"
        elif inning_half == "Bottom":
            inning_label = f"BOT {inning_num}"
        else:
            inning_label = f"TOP {inning_num}"

        # Count / outs
        balls   = ls.get("balls",   0)
        strikes = ls.get("strikes", 0)
        outs    = ls.get("outs",    0)

        # R/H/E
        ht = ls.get("teams", {}).get("home", {})
        at = ls.get("teams", {}).get("away", {})

        # Bases occupied
        offense = ls.get("offense", {})
        bases = {
            "first":  bool(offense.get("first")),
            "second": bool(offense.get("second")),
            "third":  bool(offense.get("third")),
        }

        # Batter / due-up
        batter_id   = (offense.get("batter")  or {}).get("id")
        batter_name = (offense.get("batter")  or {}).get("fullName", "")
        on_deck     = (offense.get("onDeck")  or {}).get("fullName", "")
        in_hole     = (offense.get("inHole")  or {}).get("fullName", "")

        # Defense pitcher
        defense      = ls.get("defense", {})
        pitcher_id   = (defense.get("pitcher") or {}).get("id")
        pitcher_name = (defense.get("pitcher") or {}).get("fullName", "")

        # Per-game stats from boxscore
        pitcher_ip = "—"
        pitcher_er = "—"
        batter_ab  = 0
        batter_h   = 0
        batter_ops = "—"

        box_teams = box.get("teams", {})
        for side in ("home", "away"):
            players = box_teams.get(side, {}).get("players", {})
            if pitcher_id:
                ps = players.get(f"ID{pitcher_id}", {})
                st = ps.get("stats", {}).get("pitching", {})
                if st:
                    pitcher_ip = st.get("inningsPitched", "—")
                    pitcher_er = st.get("earnedRuns", "—")
            if batter_id:
                bs = players.get(f"ID{batter_id}", {})
                bst = bs.get("stats", {}).get("batting", {})
                bss = bs.get("seasonStats", {}).get("batting", {})
                if bst:
                    batter_ab  = bst.get("atBats", 0)
                    batter_h   = bst.get("hits",   0)
                if bss:
                    batter_ops = bss.get("ops", "—")

        # Current inning score for each team (from plays)
        try:
            innings = data.get("liveData", {}).get("linescore", {}).get("innings", [])
            inning_scores = []
            for inn in innings:
                inning_scores.append({
                    "num": inn.get("num", 0),
                    "away": inn.get("away", {}).get("runs", ""),
                    "home": inn.get("home", {}).get("runs", ""),
                })
        except:
            inning_scores = []

        return jsonify({
            "success":      True,
            "gamePk":       game_pk,
            "statusDetail": status_d,
            "inningLabel":  inning_label,
            "balls":   balls, "strikes": strikes, "outs": outs,
            "awayRuns": at.get("runs",   0), "awayHits": at.get("hits",   0), "awayErrors": at.get("errors", 0),
            "homeRuns": ht.get("runs",   0), "homeHits": ht.get("hits",   0), "homeErrors": ht.get("errors", 0),
            "bases": bases,
            "pitcher": {"name": pitcher_name, "ip": pitcher_ip, "er": pitcher_er},
            "batter":  {"name": batter_name,  "ab": batter_ab,  "h": batter_h, "ops": batter_ops},
            "dueUp":   [on_deck, in_hole],
            "inningScores": inning_scores,
        })
    except Exception as ex:
        print("[api_game_livedata]", traceback.format_exc())
        return jsonify({"success": False, "error": str(ex)}), 500
