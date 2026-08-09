from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_primary_picks_contract_fails_closed_for_clv_provenance():
    source = (ROOT / 'intelligence_integration.py').read_text(encoding='utf-8')
    assert 'def _clv_provenance(row):' in source
    assert "'status': status" in source
    assert "'verified': accepted" in source
    assert "'edge': row.get('clvEdge') if accepted else None" in source
    assert "'missing_integrity_receipt'" in source


def test_picks_ui_discloses_clv_status_without_inventing_an_edge():
    source = (ROOT / 'picks.html').read_text(encoding='utf-8')
    assert "const clv=p.clvProvenance||{status:'unverified'}" in source
    assert 'CLV <b' in source
    assert "clv.edge!=null" in source
