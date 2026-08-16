import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from decision_intelligence import evaluate_decision, evaluate_decisions


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 16, 16, 0, tzinfo=timezone.utc)


def candidate(**changes):
    value = {
        "canonicalCandidateId": "player-hits-over-1.5",
        "canonicalMarketKey": "batter_hits",
        "canonicalSide": "over",
        "line": 1.5,
        "canonicalProbability": 0.62,
        "marketGatePromoted": True,
        "marketGateStatus": "promoted",
        "marketSideGateStatus": "promoted",
        "bankroll": 1000,
    }
    value.update(changes)
    return value


def quote(book, over_price, under_price, *, age=30, line=1.5, source="odds-feed"):
    return {
        "book": book,
        "source": source,
        "capturedAt": (NOW - timedelta(seconds=age)).isoformat(),
        "line": line,
        "overPrice": over_price,
        "underPrice": under_price,
    }


def healthy_quotes():
    return [
        quote("Book A", 105, -125),
        quote("Book B", 110, -130, age=45),
    ]


def test_fresh_two_book_consensus_price_shops_and_qualifies_for_review_only():
    result = evaluate_decision(candidate(), healthy_quotes(), now=NOW)
    assert result["decisionStatus"] == "qualified"
    assert result["consensus"]["acceptedBookCount"] == 2
    assert result["priceShopping"]["bestAvailableBook"] == "Book B"
    assert result["priceShopping"]["bestAvailablePrice"] == 110
    assert result["modelEdge"] > result["thresholds"]["minimumEdge"]
    assert result["expectedValue"] > result["thresholds"]["minimumExpectedValue"]
    assert result["decisionReviewRequired"] is True
    assert result["decisionApproved"] is False
    assert result["actionable"] is False
    assert 0 < result["stakePreview"]["stakePct"] <= 0.01


def test_one_book_and_duplicate_book_fail_the_independence_gate():
    one = healthy_quotes()[:1]
    duplicate = [one[0], quote("book a", 115, -135, age=20)]
    for quotes in (one, duplicate):
        result = evaluate_decision(candidate(), quotes, now=NOW)
        assert result["decisionStatus"] == "no_bet"
        assert "consensus requires at least 2 fresh books" in result["decisionReasons"]
        assert result["actionable"] is False


def test_stale_quotes_and_mismatched_lines_are_rejected():
    cases = [
        [quote("Book A", 105, -125, age=301), quote("Book B", 110, -130, age=301)],
        [quote("Book A", 105, -125, line=2.5), quote("Book B", 110, -130, line=2.5)],
    ]
    for quotes in cases:
        result = evaluate_decision(candidate(), quotes, now=NOW)
        assert result["decisionStatus"] == "no_bet"
        assert result["consensus"]["acceptedBookCount"] == 0
        assert result["consensus"]["rejectedQuoteCount"] == 2


def test_excessive_consensus_dispersion_fails_closed():
    result = evaluate_decision(
        candidate(),
        [quote("Book A", 300, -500), quote("Book B", -300, 240)],
        now=NOW,
    )
    assert result["decisionStatus"] == "no_bet"
    assert "sportsbook consensus dispersion exceeds limit" in result["decisionReasons"]


def test_market_specific_edge_no_bet_zone_is_enforced():
    result = evaluate_decision(
        candidate(canonicalProbability=0.48), healthy_quotes(), now=NOW,
    )
    assert result["decisionStatus"] == "no_bet"
    assert "model edge is inside the no-bet zone" in result["decisionReasons"]
    assert result["stakePreview"]["stakePct"] == 0


def test_unpromoted_market_or_side_cannot_qualify():
    for change in (
        {"marketGatePromoted": False},
        {"marketGateStatus": "blocked"},
        {"marketSideGateStatus": "blocked"},
    ):
        result = evaluate_decision(candidate(**change), healthy_quotes(), now=NOW)
        assert result["decisionStatus"] == "no_bet"
        assert result["actionable"] is False


def test_batch_audit_never_reports_approval_or_actionability():
    row = candidate()
    batch = evaluate_decisions(
        [row], {row["canonicalCandidateId"]: healthy_quotes()}, now=NOW,
    )
    assert batch["audit"]["qualifiedForReviewCount"] == 1
    assert batch["audit"]["approvedCount"] == 0
    assert batch["audit"]["actionableCount"] == 0


def test_committed_contract_preserves_decision_only_safety():
    contract = json.loads(
        (ROOT / "data/decision_intelligence_contract.json").read_text(encoding="utf-8")
    )
    report = json.loads(
        (ROOT / "data/data_intelligence_report.json").read_text(encoding="utf-8")
    )
    assert contract["version"] == "5.3.0"
    assert contract["role"] == "decision_only"
    assert contract["modelEligible"] is False
    assert contract["automaticAction"] is False
    assert contract["decisionReviewRequired"] is True
    assert report["phase53Admission"]["ready"] is True
    assert report["phase53Admission"]["modelTrainingEligible"] is False
    assert report["phase53Admission"]["automaticAction"] is False


def test_quality_and_documentation_enforce_phase_53_contract():
    quality = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    roadmap = (ROOT / "docs/MLB_ANALYTICS_HUB_ROADMAP.md").read_text(encoding="utf-8")
    docs = (ROOT / "docs/decision_intelligence.md").read_text(encoding="utf-8")
    assert "python scripts/decision_intelligence.py --check-contract" in quality
    assert "### Phase 5.3 — Decision intelligence foundation" in roadmap
    assert "decision-only evidence" in docs
    assert "`actionable: false`" in docs
