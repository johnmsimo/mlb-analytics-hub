from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_unified_picks_route_is_the_primary_actionable_contract():
    source = (ROOT / 'intelligence_integration.py').read_text(encoding='utf-8')
    assert "@flask_app.route('/api/picks/today'" in source
    assert "'contractVersion': '4.48'" in source
    assert "'picks': picks" in source
    assert "actionable_limit = 5" in source
    assert "picks = candidates[:actionable_limit]" in source
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
    assert "'evidenceVersion': '4.45'" in source
    assert "def _pick_evidence(row, clv):" in source
    assert "evidence = _pick_evidence(row, clv)" in source
    assert "'verifiedClvEdge': clv.get('edge')" in source

    assert "'evidenceIntegrityVersion': '4.45'" in source
    assert "def _evidence_integrity(evidence):" in source
    assert "row['evidenceIntegrity'] = evidence_integrity" in source
    assert "if not evidence_integrity['verified']:" in source
    assert "'evidenceAudit': {" in source


def test_primary_picks_contract_exposes_auditable_summary():
    source = (ROOT / 'intelligence_integration.py').read_text(encoding='utf-8')
    assert "'evidenceAuditVersion': '4.48'" in source
    assert "'status': audit_status" in source
    assert "'actionableLimit': actionable_limit" in source
    assert "'capApplied': cap_applied" in source
    assert "'withheldCount': withheld_count" in source
    assert "'displayedCount': displayed_count" in source
    assert "'rejectionReasons': dict(sorted(evidence_rejection_reasons.items()))" in source


def test_primary_picks_contract_exposes_selection_ranking_audit():
    source = (ROOT / 'intelligence_integration.py').read_text(encoding='utf-8')
    assert "ranking_method = 'pickScore_desc_then_edgePct_desc'" in source
    assert "'selectionAudit'] = {" in source
    assert "'rankingVersion': '4.48'" in source
    assert "'rankingMethod': ranking_method" in source
    assert "'selectionRule': (" in source
    assert "'rankedCandidateCount': len(ranked_candidates)" in source
    assert "'disposition': (" in source


def test_picks_page_surfaces_evidence_audit_details():
    source = (ROOT / 'picks.html').read_text(encoding='utf-8')
    assert "const audit=d.evidenceAudit||{}" in source
    assert "const withheld=audit.withheldCount||0" in source
    assert "Object.entries(audit.rejectionReasons||{})" in source
    assert 'Evidence audit:' in source
    assert 'validated' in source and 'rejected' in source
    assert 'rankingMethod' in source
    assert 'RANK' in source
    assert 'selection.rank' in source
