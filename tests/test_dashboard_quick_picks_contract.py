from pathlib import Path


DASHBOARD = Path(__file__).resolve().parents[1] / 'dashboard.html'
APP = Path(__file__).resolve().parents[1] / 'app.py'
DEEPDIVE = Path(__file__).resolve().parents[1] / 'deepdive.html'


def test_dashboard_uses_game_card_intelligence_endpoint():
    source = DASHBOARD.read_text(encoding='utf-8')

    assert "apiFetchJsonTimeout('/api/intelligence/game-card/'" in source
    assert "var picks=(d&&d.quickPicks)||[]" in source
    assert 'TOP PICKS TO TAKE' in source
    assert 'Ranked by final Pick Score' in source
    assert 'Shared batter-vs-pitcher game simulation' in source
    assert 'd&&d.simulationAudit' in source
    assert 'p.matchupSimulationSource' in source
    assert "SIM <strong>" in source
    assert 'd&&d.simulationReady===false' in source
    assert 'Matchup simulation is unavailable or incomplete' in source
    assert 'picks.length' in source
    assert 'p.modelProbabilityPct' in source
    assert 'p.estimatedEdgePct' in source
    assert 'p.modelReliabilityScore' in source
    assert 'd.unavailableMarkets' in source

    prewarm_start = source.index('function prewarmQuickProps(games)')
    prewarm_end = source.index('function seedDashboardLineups(games)', prewarm_start)
    prewarm_source = source[prewarm_start:prewarm_end]
    assert '/api/intelligence/game-card/' not in prewarm_source
    assert "fetch('/api/props/quick/'" not in prewarm_source
    assert 'page load must never start one build per game' in source
    assert 'Building the linked batter-vs-pitcher simulation in the background' in source
    assert 'retryAfterSeconds' in source
    assert "refreshStatus.status==='error'" in source
    assert "(!(d&&d.computing)&&unavailable.length)" in source


def test_dashboard_lineups_render_cached_schedule_data_before_refresh():
    source = DASHBOARD.read_text(encoding='utf-8')

    assert 'function seedDashboardLineups(games)' in source
    assert 'g.awayLineup||[]' in source
    assert 'g.homeLineup||[]' in source
    assert "apiFetchJsonTimeout('/api/lineup/'" in source
    assert 'Lineup refresh is taking longer than expected' in source


def test_lineup_api_never_runs_live_enrichment_in_the_request():
    source = APP.read_text(encoding='utf-8')
    route_start = source.index("@app.route('/api/lineup/<int:game_pk>')")
    route_end = source.index('# ── Slate Capture & Parlays', route_start)
    route_source = source[route_start:route_end]

    assert '_schedule_lineup_payload(game_pk, date_str)' in route_source
    assert '_schedule_lineup_payload_from_game(game_pk, date_str)' in route_source
    assert '_build_lineup_payload(game_pk)' not in route_source
    assert "'computing': True" in route_source
    assert 'awayLineup' in source
    assert 'homeLineup' in source


def test_save_and_parlay_preserve_recommended_side():
    source = DASHBOARD.read_text(encoding='utf-8')
    payload_start = source.index('function quickPickPayload(pick)')
    payload_end = source.index('function qpSave(', payload_start)
    payload_source = source[payload_start:payload_end]

    assert 'recommendedSide:pick.recommendedSide' in payload_source
    assert 'player:pick.player||pick.team' in payload_source
    assert "recommendedSide:'Over'" not in payload_source
    assert "source:'dashboard_intelligence'" in payload_source
    assert 'pickScore:pick.pickScore' in payload_source
    assert 'modelReliabilityScore:pick.modelReliabilityScore' in payload_source
    assert 'modelProbabilityPct:pick.modelProbabilityPct' in payload_source
    assert 'estimatedEdgePct:pick.estimatedEdgePct' in payload_source
    assert 'sharedSimulationBacked:pick.sharedSimulationBacked' in payload_source
    assert 'matchupSimulationVersion:pick.matchupSimulationVersion' in payload_source
    assert 'gameSimProbability:pick.gameSimProbability' in payload_source
    assert 'gameSimN:pick.gameSimN' in payload_source


def test_pass_decisions_are_not_rendered_as_pick_cards():
    source = DASHBOARD.read_text(encoding='utf-8')

    assert "var picks=(d&&d.quickPicks)||[]" in source
    assert 'No qualifying play:' in source
    assert "selection='<span class=\"qp-side\" style=\"color:var(--mu)\">PASS" not in source


def test_deep_dive_never_fabricates_an_unfinished_simulation():
    source = DEEPDIVE.read_text(encoding='utf-8')
    app_source = APP.read_text(encoding='utf-8')

    assert "success: false" in source
    assert "simulationReady: false" in source
    assert 'No generic estimate is being substituted.' in source
    assert 'away_mean_runs: 4.2' not in source
    assert "opts.sims || 1500" in source
    assert "loadSimulation({sims:5000" in source
    assert "'requestedSims': int(sims or 0)" in app_source
    assert "'away_mean_runs': 4.2" not in app_source
    assert "'mean_total': 8.5" not in app_source


def test_startup_does_not_compete_with_user_requested_simulations():
    source = APP.read_text(encoding='utf-8')

    assert "STARTUP_PROJECTIONS_PREWARM', '0'" in source
    assert 'full-slate projections prewarm disabled' in source
    assert '@serialized_simulation\ndef _do_simulate' in source
