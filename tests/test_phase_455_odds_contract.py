from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_phase_455_odds_lineage_contract_is_wired():
    lineage = (ROOT / "odds_lineage.py").read_text(encoding="utf-8")
    writer = (ROOT / "tracker_writer.py").read_text(encoding="utf-8")
    validation = (ROOT / "market_validation.py").read_text(encoding="utf-8")
    picks = (ROOT / "intelligence_integration.py").read_text(encoding="utf-8")

    assert 'ODDS_LINEAGE_VERSION = "4.55"' in lineage
    assert "def build_odds_lineage(" in lineage
    assert '"opening"' in lineage and '"current"' in lineage and '"closing"' in lineage
    assert "MINIMUM_CLV_CLAIM_OBSERVATIONS = 500" in lineage
    assert "build_odds_lineage(" in writer
    assert '"currentPrice":    current_price' in writer
    assert '"oddsLineage": odds_lineage' in writer
    assert "from odds_lineage import clv_eligibility, clv_summary" in validation
    assert "if not clv_eligibility(row):" in validation
    assert '"clvGradedCount"' in validation
    assert "ODDS_LINEAGE_VERSION" in picks
    assert "'clvEligible': accepted" in picks
    assert "'oddsLineageVersion': ODDS_LINEAGE_VERSION" in picks


def test_phase_455_tracker_discloses_clv_denominator():
    tracker = (ROOT / "tracker.html").read_text(encoding="utf-8")
    assert "CLV-graded only" in tracker
    assert "CLV Graded" in tracker
    assert "clvAuditNote" in tracker
    assert "claims require 500 valid observations" in tracker


def test_unverified_clv_is_not_a_claim_eligible_metric():
    from odds_lineage import clv_summary

    summary = clv_summary([
        {"grade": "win", "clvEdge": 0.10},
        {
            "grade": "win",
            "clvEdge": 0.04,
            "oddsLineage": {"version": "4.55", "clvEligible": True},
        },
    ])
    assert summary["gradedCount"] == 2
    assert summary["clvGradedCount"] == 1
    assert summary["claimEligible"] is False
