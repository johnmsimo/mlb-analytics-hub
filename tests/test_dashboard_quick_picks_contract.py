from pathlib import Path


DASHBOARD = Path(__file__).resolve().parents[1] / 'dashboard.html'


def test_dashboard_uses_game_card_intelligence_endpoint():
    source = DASHBOARD.read_text(encoding='utf-8')

    assert "fetch('/api/intelligence/game-card/'" in source
    assert "var picks=(d&&d.quickPicks)||[]" in source
    assert 'HIGHEST-CONFIDENCE PICKS' in source
    assert 'Best positive-edge side per market' in source
    assert 'bestAvailableMinimumConfidence' in source

    prewarm_start = source.index('function prewarmQuickProps(games)')
    prewarm_end = source.index('function load(dateStr)', prewarm_start)
    prewarm_source = source[prewarm_start:prewarm_end]
    assert "fetch('/api/intelligence/game-card/'" in prewarm_source
    assert "fetch('/api/props/quick/'" not in prewarm_source


def test_save_and_parlay_preserve_recommended_side():
    source = DASHBOARD.read_text(encoding='utf-8')
    payload_start = source.index('function quickPickPayload(pick)')
    payload_end = source.index('function qpSave(', payload_start)
    payload_source = source[payload_start:payload_end]

    assert 'recommendedSide:pick.recommendedSide' in payload_source
    assert "recommendedSide:'Over'" not in payload_source
    assert "source:'dashboard_intelligence'" in payload_source


def test_pass_decisions_do_not_render_bet_actions():
    source = DASHBOARD.read_text(encoding='utf-8')

    assert "var actions=isPass?'':'<div class=\"qp-actions\">'" in source
