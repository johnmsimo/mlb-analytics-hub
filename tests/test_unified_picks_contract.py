from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_unified_picks_route_is_the_primary_actionable_contract():
    source = (ROOT / 'intelligence_integration.py').read_text(encoding='utf-8')
    assert "@flask_app.route('/api/picks/today'" in source
    assert "'contractVersion': '4.44'" in source
    assert "'picks': picks" in source
    assert "candidates[:5]" in source
    assert "recommendationGrade" in source
    assert "marketValidation" in source


def test_picks_page_is_mobile_first_and_explains_decisions():
    source = (ROOT / 'picks.html').read_text(encoding='utf-8')
    app = (ROOT / 'app.py').read_text(encoding='utf-8')
    assert "@app.route('/picks')" in app
    assert "fetch('/api/picks/today')" in source
    assert 'viewport' in source
    assert 'WIN' in source and 'EDGE' in source and 'CONF' in source
    assert 'No actionable picks' in source
    assert 'topReasons' in source
    assert 'topRisks' in source
    assert 'clvProvenance' in source


def test_unified_route_does_not_promote_pass_rows():
    source = (ROOT / 'intelligence_integration.py').read_text(encoding='utf-8')
    assert "if str(row.get('recommendationGrade') or '').lower() == 'pass':" in source
    assert "'researchOnly': not bool(picks)" in source
    assert "projections remain research-only" in source


def test_primary_picks_contract_exposes_normalized_evidence_snapshot():
    source = (ROOT / 'intelligence_integration.py').read_text(encoding='utf-8')
    assert "'evidenceVersion': '4.44'" in source
    assert "def _pick_evidence(row, clv):" in source
    assert "row['evidence'] = _pick_evidence(row, clv)" in source
    assert "'verifiedClvEdge': clv.get('edge')" in source
