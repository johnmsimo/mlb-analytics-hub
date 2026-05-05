# ═══════════════════════════════════════════════════════════════════════════
# PASTE THIS ENTIRE BLOCK into app.py
# Location: directly after the @app.route('/api/gameside-deepdive/<int:game_pk>')
#           route definition (search for  'apigamesidedeepdive'  to find it).
# This file exists only as a reference — it is NOT auto-imported by Flask.
# ═══════════════════════════════════════════════════════════════════════════

# ── In-memory cache (4-min TTL, invalidated on lineup change) ─────────────────
_matchup_card_cache: dict = {}
_MATCHUP_CARD_TTL = 4 * 60  # seconds


@app.route('/api/matchup-card/<int:game_pk>')
def api_matchup_card(game_pk):
    """
    Matchup Card component endpoint.
    Returns enriched game data for the <div id=matchupCardWrap> component
    defined in matchup_card.html.

    Response shape (all keys required by the JS renderer):
      away_team, away_team_abr, home_team, home_team_abr  — strings
      game_time      — ISO-8601 string (fed to new Date() in JS)
      away_pitcher / home_pitcher:
          name, hand, splits: { vR: {brl_pct, hr9}, vL: {brl_pct, hr9} }
      bats_to_target:
          away: [ {name, bat_side, hot, splits:{ba,brl_pct,fb_pct,iso}} ]
          home: [ ... ]
      park:  { name, hr_factor (int %), hits_factor (int %) }
      edge:  narrative string
    """
    import time

    # ── Cache hit ───────────────────────────────────────────────────────────────────
    cached = _matchup_card_cache.get(game_pk)
    if cached and (time.time() - cached['ts']) < _MATCHUP_CARD_TTL:
        return jsonify(cached['data'])

    try:
        maybe_refresh_fg()
        maybe_refresh_savant()

        # ── Core game data ─────────────────────────────────────────────────────────────
        gdata, awaybats, homebats, awayt, homet, pitchers = props_fetch_game(game_pk)
        if not gdata:
            return jsonify({'status': 'error', 'message': 'Game not found'}), 404

        away_team_obj = awayt.get('team', {})
        home_team_obj = homet.get('team', {})
        away_abbr     = away_team_obj.get('abbreviation', 'AWAY')
        home_abbr     = home_team_obj.get('abbreviation', 'HOME')
        home_id       = home_team_obj.get('id')
        pf            = PARKFACTORS.get(home_id, 1.0)

        ap   = pitchers.get('ap') or {}
        hp   = pitchers.get('hp') or {}
        apn  = ap.get('fullName', 'TBD')
        hpn  = hp.get('fullName', 'TBD')
        apid = ap.get('id')
        hpid = hp.get('id')

        # ── Pitcher stat dicts ──────────────────────────────────────────────────────────
        apfg = fgpitcher(apn)           or {}
        hpfg = fgpitcher(hpn)           or {}
        apsv = svpitcher(apn)           or {}
        hpsv = svpitcher(hpn)           or {}
        apst = pitcherstatsmlb(apid)    if apid else {}
        hpst = pitcherstatsmlb(hpid)    if hpid else {}
        aphand = (apst.get('pitchHand') or 'R').upper()[0]
        hphand = (hpst.get('pitchHand') or 'R').upper()[0]

        # ── vR / vL split estimator ────────────────────────────────────────────────────
        # FanGraphs pitcher splits (vR/vL) are not scraped individually in this
        # app, so we approximate from overall barrel% and HR/9 using a
        # platoon-advantage offset (RHP → harder on RHB, easier on LHB).
        def _pitcher_splits(pfg: dict, psv: dict, phand: str) -> dict:
            def _f(v, d=0.0):
                try:    return float(v) if v else d
                except: return d

            brl = _f(psv.get('svbrlpct'), 4.0)   # % (e.g. 4.8)
            hr9 = _f(pfg.get('fghr9') or psv.get('svhr9'), 1.0)

            # Platoon offset: same-hand matchup favours pitcher
            adj_brl, adj_hr9 = 0.8, 0.10
            if phand == 'R':          # RHP easier for LHB
                vR = {'brl_pct': round(brl - adj_brl, 1),
                      'hr9':    round(hr9  - adj_hr9, 2)}
                vL = {'brl_pct': round(brl + adj_brl, 1),
                      'hr9':    round(hr9  + adj_hr9, 2)}
            else:                     # LHP easier for RHB
                vR = {'brl_pct': round(brl + adj_brl, 1),
                      'hr9':    round(hr9  + adj_hr9, 2)}
                vL = {'brl_pct': round(brl - adj_brl, 1),
                      'hr9':    round(hr9  - adj_hr9, 2)}
            return {'vR': vR, 'vL': vL}

        ap_splits = _pitcher_splits(apfg, apsv, aphand)
        hp_splits = _pitcher_splits(hpfg, hpsv, hphand)

        # ── Batter selector (top-N per side by matchup score) ───────────────────
        def _bats_side(batters, opp_fg, opp_sv, opp_hand, limit=3):
            out  = []
            seen = set()
            for b in (batters or [])[:9]:
                name = b.get('name', '')
                pos  = (b.get('pos') or '').upper()
                if not name or name in seen:
                    continue
                if pos in ('P', 'SP', 'RP', 'CP'):
                    continue
                seen.add(name)

                fgb    = fgbatter(name) or {}
                svb    = svbatter(name) or {}
                merged = {**b, **fgb, **svb}

                # Hot flag: L5 consecutive over-streak on hits≥3 games
                pid = b.get('id')
                hot = False
                if pid:
                    try:
                        tr     = buildplayertrends(int(pid), False)
                        streak = (tr or {}).get('streak') or {}
                        hot    = (streak.get('direction') == 'over'
                                  and int(streak.get('length', 0)) >= 3)
                    except Exception:
                        pass

                def _sf(v, d=None):
                    try:    return float(v) if v not in (None, '') else d
                    except: return d

                ba      = _sf(merged.get('fgavg') or merged.get('avg'))
                brl_pct = _sf(merged.get('svbrlpct'))
                fb_pct  = _sf(merged.get('svfbpct') or merged.get('fbpct'))
                iso     = _sf(merged.get('fgiso'))
                bat_side = (merged.get('fgbats') or
                            merged.get('bats') or 'R').upper()[0]

                # Matchup score for ranking (existing helper)
                score_dict = matchupscore(merged, opp_fg, opp_sv,
                                          pitcherhand=opp_hand)
                ms = score_dict.get('score', 50) if score_dict else 50

                out.append({
                    '_score':   ms,
                    'name':     name,
                    'bat_side': bat_side,
                    'hot':      hot,
                    'splits': {
                        'ba':      ba,
                        'brl_pct': brl_pct,
                        'fb_pct':  fb_pct,
                        'iso':     iso,
                    },
                })

            out.sort(key=lambda x: x.pop('_score', 0), reverse=True)
            return out[:limit]

        # Away bats face HP; home bats face AP
        away_bats_out = _bats_side(awaybats, hpfg, hpsv, hphand)
        home_bats_out = _bats_side(homebats, apfg, apsv, aphand)

        # ── Park factors (convert 1.04 float → +4 int %) ───────────────────────
        venue     = gdata.get('venue', {})
        park_name = (venue.get('name')
                     or home_team_obj.get('name', home_abbr) + ' Park')
        hr_factor   = round((pf - 1.0) * 100)
        hits_factor = round((pf - 1.0) *  60)  # hits correlate less than HRs

        # ── Edge narrative ─────────────────────────────────────────────────────────────
        def _build_edge() -> str:
            lines = []

            def _f(v, d=0.0):
                try:    return float(v) if v else d
                except: return d

            # Away pitcher (home bats face him)
            ap_brl  = _f(apsv.get('svbrlpct'), 4.0)
            ap_hr9  = _f(apfg.get('fghr9'), 1.0)
            ap_kpct = _f(apfg.get('fgkpct') or apsv.get('svkpct'), 0.22)
            if ap_kpct > 1: ap_kpct /= 100

            if ap_brl > 5.0 or ap_hr9 > 1.0:
                lines.append(
                    f"{apn} allows {ap_brl:.1f}% BRL and {ap_hr9:.2f} HR/9 — "
                    f"{home_abbr} power bats are in play."
                )
            if ap_kpct > 0.27:
                lines.append(
                    f"{apn}\u2019s {ap_kpct*100:.1f}% K-rate suppresses "
                    f"{home_abbr} hit-count props."
                )

            # Home pitcher (away bats face him)
            hp_brl  = _f(hpsv.get('svbrlpct'), 4.0)
            hp_hr9  = _f(hpfg.get('fghr9'), 1.0)
            hp_kpct = _f(hpfg.get('fgkpct') or hpsv.get('svkpct'), 0.22)
            if hp_kpct > 1: hp_kpct /= 100

            if hp_brl > 5.0 or hp_hr9 > 1.0:
                lines.append(
                    f"{hpn} allows {hp_brl:.1f}% BRL and {hp_hr9:.2f} HR/9 — "
                    f"{away_abbr} bats have TB/HR upside."
                )
            if hp_kpct > 0.27:
                lines.append(
                    f"{hpn}\u2019s {hp_kpct*100:.1f}% K-rate is elevated — "
                    f"fade {away_abbr} individual hit props."
                )

            # Park context
            if abs(hr_factor) >= 4:
                tag = 'HR-friendly' if hr_factor > 0 else 'HR-suppressing'
                lines.append(
                    f"{park_name} ({hr_factor:+d}% HR PF) is a {tag} environment."
                )

            # Top-ranked batter callout (home side first, then away)
            top_bat = next(
                (b for b in (home_bats_out + away_bats_out) if b.get('name')),
                None
            )
            if top_bat:
                tb_name = top_bat['name']
                sp = top_bat.get('splits') or {}
                brl_v = sp.get('brl_pct')
                iso_v = sp.get('iso')
                if brl_v is not None:
                    lines.append(
                        f"Top target: {tb_name} — "
                        f"{brl_v:.1f}% BRL"
                        f"{f', ISO {iso_v:.3f}' if iso_v else ''}."
                    )

            return ' '.join(lines[:3]) if lines else 'Full matchup analysis loading…'

        # ── Final payload ────────────────────────────────────────────────────────────────
        payload = {
            'away_team':     away_team_obj.get('name', away_abbr),
            'away_team_abr': away_abbr,
            'home_team':     home_team_obj.get('name', home_abbr),
            'home_team_abr': home_abbr,
            'game_time':     gdata.get('gameDate', ''),
            'away_pitcher':  {
                'name':   apn,
                'hand':   aphand,
                'splits': ap_splits,
            },
            'home_pitcher':  {
                'name':   hpn,
                'hand':   hphand,
                'splits': hp_splits,
            },
            'bats_to_target': {
                'away': away_bats_out,
                'home': home_bats_out,
            },
            'park': {
                'name':        park_name,
                'hr_factor':   hr_factor,
                'hits_factor': hits_factor,
            },
            'edge': _build_edge(),
        }

        _matchup_card_cache[game_pk] = {'data': payload, 'ts': time.time()}
        return jsonify(payload)

    except Exception as ex:
        print('api_matchup_card', traceback.format_exc())
        return jsonify({'status': 'error', 'message': str(ex)}), 500


# ═══════════════════════════════════════════════════════════════════════════
#  DEEPDIVE.HTML  —  drop-in integration snippet
# ═══════════════════════════════════════════════════════════════════════════
#
#  STEP 1 ─ Add the mount div to your game-panel HTML:
#
#    <div class="mc-card-wrap" id="matchupCardWrap"></div>
#
#  Suggested placement: directly below your existing pitcher-matchup panel
#  or at the top of the sidebar column in deepdive.html.
#
#  STEP 2 ─ Include the CSS (one-time, in <head>):
#
#    Copy the entire <style> block from matchup_card.html into deepdive.html’s
#    <head>, OR link it as a separate file:
#
#      <link rel="stylesheet" href="/static/matchup-card.css">
#
#    If you use the file approach, extract just the CSS from matchup_card.html
#    into  static/matchup-card.css.
#
#  STEP 3 ─ Include the IBM Plex Mono font (one-time, in <head>):
#
#    <link rel="preconnect" href="https://fonts.googleapis.com">
#    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
#    <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600;700&display=swap" rel="stylesheet">
#
#  STEP 4 ─ Include the JS (before </body>):
#
#    Copy the entire <script> IIFE from matchup_card.html into deepdive.html,
#    or keep it as a separate file and import it:
#
#      <script src="/static/matchup-card.js" defer></script>
#
#  STEP 5 ─ Hook to your existing game-selector (search for
#           ‘selectedGamePk’ or ‘loadGame’ in deepdive.html):
#
#    Wherever you set the active gamePk, add ONE LINE:
#
#      loadMatchupCard(gamePk);   // or loadMatchupCard(selectedGamePk)
#
#    Example — if deepdive.html has a click handler like:
#
#      function loadGame(gamePk) {
#          selectedGamePk = gamePk;
#          fetchPitcherMatchup(gamePk);   // existing
#          fetchPropsData(gamePk);        // existing
#          loadMatchupCard(gamePk);       // <── ADD THIS
#      }
#
#  STEP 6 ─ Auto-fire on page load:
#
#    The IIFE already listens for DOMContentLoaded and fires automatically
#    if  selectedGamePk  is truthy at that moment.  No extra code needed
#    if your page sets selectedGamePk before the scripts execute.
#
#  STEP 7 (optional) ─ Invalidate the 4-min cache on lineup change:
#
#    In your /api/lineup-status handler in app.py, add:
#
#      if len(all_changes) > 0:
#          _matchup_card_cache.pop(game_pk, None)   # force fresh render
#
# ═══════════════════════════════════════════════════════════════════════════
