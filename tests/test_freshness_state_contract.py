from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_shared_freshness_contract_defines_all_states_and_bounded_retry():
    source = (ROOT / 'static' / 'freshness-state.js').read_text(encoding='utf-8')
    for state in ('ready', 'computing', 'partial', 'stale', 'failed', 'unavailable'):
        assert state in source
    assert 'MAX_RETRIES = 3' in source
    assert 'function normalize(payload)' in source
    assert 'function shouldRetry(key)' in source


def test_affected_surfaces_use_shared_freshness_contract():
    for name in ('value_bets.html', 'edge_lab.html', 'consistency.html'):
        source = (ROOT / name).read_text(encoding='utf-8')
        assert '/static/freshness-state.js' in source
        assert 'FreshnessState' in source


def test_roadmap_marks_phase_453_in_progress():
    source = (ROOT / 'docs' / 'MLB_ANALYTICS_HUB_ROADMAP.md').read_text(encoding='utf-8')
    assert 'Phase 4.53' in source
    assert 'shared freshness/computation contract' in source