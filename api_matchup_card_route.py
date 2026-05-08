"""
api_matchup_card_route.py
═══════════════════════════════════════════════════════════════════════════
Paste this route block into app.py, after the deepdive route.
All FIX comments below reference the specific bugs that were corrected.
═══════════════════════════════════════════════════════════════════════════
"""

import traceback, time

_matchup_card_cache: dict = {}
_MATCHUP_CARD_TTL = 4 * 60


@app.route('/api/matchup-card/<int:game_pk>')
def api_matchup_card(game_pk):
    cached = _matchup_card_cache.get(game_pk)
    if cached and (time.time() - cached["ts"]) < _MATCHUP_CARD_TTL:
        return jsonify(cached["data"])

    try:
        maybe_refresh_fg()
        maybe_refresh_savant()

        gdata, awaybats, homebats, awayt, homet, pitchers = props_fetch_game(game_pk)
        if not gdata:
            return jsonify({"status": "error", "message": "Game not found"}), 404

        away_team_obj = awayt.get("team", {})
        home_team_obj = homet.get("team", {})
        away_abbr = away_team_obj.get("abbreviation", "AWAY")
        home_abbr = home_team_obj.get("abbreviation", "HOME")
        home_id   = home_team_obj.get("id")
        pf        = PARKFACTORS.get(home_id, 1.0)

        ap  = pitchers.get("ap") or {}
        hp  = pitchers.get("hp") or {}
        apn = ap.get("fullName", "TBD")
        hpn = hp.get("fullName", "TBD")
        apid = ap.get("id")
        hpid = hp.get("id")

        apfg = fgpitcher(apn) or {}
        hpfg = fgpitcher(hpn) or {}
        apsv = svpitcher(apn) or {}
        hpsv = svpitcher(hpn) or {}
        apst = pitcherstatsmlb(apid) if apid else {}
        hpst = pitcherstatsmlb(hpid) if hpid else {}
        aphand = (apst.get("pitchHand") or "R").upper()[0]
        hphand = (hpst.get("pitchHand") or "R").upper()[0]

        def _pitcher_splits(pfg, psv, phand):
            def _f(v, d=0.0):
                try: return float(v) if v else d
                except: return d
            brl = _f(psv.get("svbrlpct"), 4.0)
            hr9 = _f(pfg.get("fghr9") or psv.get("svhr9"), 1.0)
            adj_brl, adj_hr9 = 0.8, 0.10
            if phand == "R":
                return {"vR": {"brl_pct": round(brl - adj_brl,1), "hr9": round(hr9-adj_hr9,2)},
                        "vL": {"brl_pct": round(brl + adj_brl,1), "hr9": round(hr9+adj_hr9,2)}}
            return {"vR": {"brl_pct": round(brl + adj_brl,1), "hr9": round(hr9+adj_hr9,2)},
                    "vL": {"brl_pct": round(brl - adj_brl,1), "hr9": round(hr9-adj_hr9,2)}}

        ap_splits = _pitcher_splits(apfg, apsv, aphand)
        hp_splits = _pitcher_splits(hpfg, hpsv, hphand)

        def _bats_side(batters, opp_fg, opp_sv, opp_hand, limit=3):
            out, seen = [], set()
            for b in (batters or [])[:9]:
                name = b.get("name","")
                if not name or name in seen: continue
                if (b.get("pos","")).upper() in ("P","SP","RP","CP"): continue
                seen.add(name)
                fgb  = fgbatter(name) or {}
                svb  = svbatter(name) or {}
                merged = {**b, **fgb, **svb}
                pid  = b.get("id")
                hot  = False
                if pid:
                    try:
                        tr = buildplayertrends(int(pid), False)
                        st = (tr or {}).get("streak") or {}
                        hot = st.get("direction") == "over" and int(st.get("length",0)) >= 3
                    except: pass
                def _sf(v, d=None):
                    try: return float(v) if v not in (None,"") else d
                    except: return d
                ms = (matchupscore(merged, opp_fg, opp_sv, pitcherhand=opp_hand) or {}).get("score", 50)
                out.append({
                    "_score":   ms,
                    "name":     name,
                    "bat_side": (merged.get("fgbats") or merged.get("bats") or "R").upper()[0],
                    "hot":      hot,
                    "splits": {
                        "ba":      _sf(merged.get("fgavg") or merged.get("avg")),
                        "brl_pct": _sf(merged.get("svbrlpct")),
                        "fb_pct":  _sf(merged.get("svfbpct")),
                        "iso":     _sf(merged.get("fgiso")),
                    },
                })
            out.sort(key=lambda x: x.pop("_score", 0), reverse=True)
            return out[:limit]

        away_bats_out = _bats_side(awaybats, hpfg, hpsv, hphand)
        home_bats_out = _bats_side(homebats, apfg, apsv, aphand)

        venue      = gdata.get("venue", {})
        park_name  = venue.get("name") or (home_team_obj.get("name", home_abbr) + " Park")
        hr_factor  = round((pf - 1.0) * 100)
        hits_factor = round((pf - 1.0) * 60)

        def _build_edge():
            lines = []
            def _f(v, d=0.0):
                try: return float(v) if v else d
                except: return d
            ap_brl  = _f(apsv.get("svbrlpct"), 4.0)
            ap_hr9  = _f(apfg.get("fghr9"), 1.0)
            ap_kpct = _f(apfg.get("fgkpct") or apsv.get("svkpct"), 0.22)
            if ap_kpct > 1: ap_kpct /= 100
            if ap_brl > 5.0 or ap_hr9 > 1.0:
                lines.append(
                    f"{apn} allows {ap_brl:.1f}% BRL and {ap_hr9:.2f} HR/9 — "
                    f"{home_abbr} power bats are in play."
                )  # FIX: was missing closing )
            if ap_kpct > 0.27:
                lines.append(
                    f"{apn}\u2019s {ap_kpct*100:.1f}% K-rate suppresses {home_abbr} hit-count props."
                )  # FIX: was missing closing )
            hp_brl  = _f(hpsv.get("svbrlpct"), 4.0)
            hp_hr9  = _f(hpfg.get("fghr9"), 1.0)
            hp_kpct = _f(hpfg.get("fgkpct") or hpsv.get("svkpct"), 0.22)
            if hp_kpct > 1: hp_kpct /= 100
            if hp_brl > 5.0 or hp_hr9 > 1.0:
                lines.append(
                    f"{hpn} allows {hp_brl:.1f}% BRL and {hp_hr9:.2f} HR/9 — "
                    f"{away_abbr} bats have TB/HR upside."
                )  # FIX: was missing closing )
            if abs(hr_factor) >= 4:
                lines.append(f"{park_name} ({hr_factor:+d}% HR PF) is a "
                             f"{'HR-friendly' if hr_factor > 0 else 'HR-suppressing'} environment.")
            top = next((b for b in (home_bats_out + away_bats_out) if b.get("name")), None)
            if top:
                sp = top.get("splits") or {}
                brl_v, iso_v = sp.get("brl_pct"), sp.get("iso")
                if brl_v is not None:
                    lines.append(
                        f"Top target: {top['name']} — {brl_v:.1f}% BRL"
                        f"{f', ISO {iso_v:.3f}' if iso_v else ''}."
                    )  # FIX: was missing closing )
            return " ".join(lines[:3]) if lines else "Full matchup analysis loading\u2026"

        payload = {
            "away_team":     away_team_obj.get("name", away_abbr),
            "away_team_abr": away_abbr,
            "home_team":     home_team_obj.get("name", home_abbr),
            "home_team_abr": home_abbr,
            "game_time":     gdata.get("gameDate", ""),
            "away_pitcher":  {"name": apn, "hand": aphand, "splits": ap_splits},
            "home_pitcher":  {"name": hpn, "hand": hphand, "splits": hp_splits},
            "bats_to_target": {"away": away_bats_out, "home": home_bats_out},
            "park":  {"name": park_name, "hr_factor": hr_factor, "hits_factor": hits_factor},
            "edge":  _build_edge(),
        }  # FIX: was missing closing }

        _matchup_card_cache[game_pk] = {"data": payload, "ts": time.time()}
        return jsonify(payload)

    except Exception as ex:
        print("api_matchup_card", traceback.format_exc())
        return jsonify({"status": "error", "message": str(ex)}), 500


# ── deepdive.html integration ─────────────────────────────────────────────────
# 1. Add mount div: <div id="matchup-card-mount" data-game-pk="{{ game_pk }}"></div>
# 2. <link rel="stylesheet" href="/static/css/matchup_card.css">
# 3. <script src="/static/js/matchup_card.js"></script>
