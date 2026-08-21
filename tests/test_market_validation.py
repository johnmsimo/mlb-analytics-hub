from datetime import datetime, timedelta, timezone

from market_validation import (
    VALIDATION_VERSION,
    ValidationPolicy,
    apply_market_gates,
    build_validation_report,
)


POLICY = ValidationPolicy(
    minimum_training_days=3,
    validation_block_days=2,
    minimum_validation_rows=4,
    minimum_validation_days=2,
    minimum_market_baseline_rows=4,
    minimum_priced_rows=4,
    minimum_clv_rows=4,
    maximum_calibration_error=0.20,
    maximum_drawdown_units=5.0,
)


def history(market="Pitcher Strikeouts", *, strong=True):
    start = datetime(2026, 7, 1, 12, tzinfo=timezone.utc)
    rows = []
    for day in range(9):
        for pick in range(2):
            outcome = 0 if (day + pick) % 3 == 0 else 1
            probability = (
                0.90 if outcome else 0.10
            ) if strong else (
                0.90 if not outcome else 0.10
            )
            rows.append({
                "id": f"{market}-{day}-{pick}",
                "date": (start + timedelta(days=day)).date().isoformat(),
                "savedAt": (start + timedelta(days=day, minutes=pick)).isoformat(),
                "market": market,
                "recommendedSide": "Over",
                "adjProb": probability,
                "openingImplied": 0.52,
                "openingPrice": 100,
                "bestAvailableBook": "Book A",
                "confidenceTier": "HIGH",
                "grade": "win" if outcome else "loss",
                "clvEdge": 0.02 if strong else -0.02,
                "oddsLineage": {"version": "4.55", "clvEligible": True},
            })
    return rows


def test_walk_forward_gates_use_only_strict_future_holdouts():
    report = build_validation_report(history(), policy=POLICY)

    assert report["version"] == VALIDATION_VERSION
    assert report["mode"] == "strict_walk_forward_holdout"
    assert report["foldCount"] == 3
    assert report["validationCount"] < report["historyCount"]
    assert all(fold["strictTimeSeparation"] for fold in report["folds"])
    assert all(
        fold["trainingEnd"] < fold["validationStart"]
        for fold in report["folds"]
    )
    gate = report["marketGates"]["pitcher_strikeouts"]
    side_gate = report["marketSideGates"]["pitcher_strikeouts|over"]
    assert gate["status"] == "promoted"
    assert side_gate["status"] == "promoted"
    assert gate["metrics"]["brierSkillVsMarket"] > 0
    assert gate["metrics"]["roi"] > 0
    assert gate["metrics"]["averageClv"] > 0
    assert gate["metrics"]["gradedCount"] == gate["metrics"]["clvGradedCount"]
    assert gate["metrics"]["clvDenominator"] == "clvGradedCount"


def test_failing_market_is_disabled_and_cannot_promote_candidates():
    report = build_validation_report(
        history("Player Hits", strong=False), policy=POLICY,
    )
    gate = report["marketGates"]["batter_hits"]

    assert gate["status"] == "disabled"
    assert "model Brier score does not beat the market baseline" in gate["reasons"]
    gated = apply_market_gates([{
        "marketKey": "batter_hits", "recommendedSide": "Over",
    }], report)
    assert gated["promoted"] == []
    assert gated["rejected"][0]["actionable"] is False
    assert gated["rejected"][0]["marketGateStatus"] == "disabled"
    assert gated["rejected"][0]["promotionStatus"] == "research_only"


def test_market_attribution_and_required_segments_are_canonical():
    report = build_validation_report(history(), policy=POLICY)

    assert "pitcher_strikeouts" in report["byMarket"]
    assert "unknown" not in report["byMarket"]
    assert "over" in report["bySide"]
    assert "pitcher_strikeouts|over" in report["byMarketSide"]
    assert "high" in report["byGrade"]
    assert "underdog_+100_or_longer" in report["byOddsRange"]
    assert "book a" in report["bySportsbook"]


def test_insufficient_history_fails_closed_as_warming_up():
    report = build_validation_report(history()[:2], policy=POLICY)
    gate = report["marketGates"]["pitcher_strikeouts"]

    assert gate["status"] == "warming_up"
    assert gate["promoted"] is False
    assert "no completed walk-forward holdout fold" in gate["reasons"]


def test_measured_failure_is_disabled_even_when_clv_is_still_missing():
    policy = ValidationPolicy(
        **{
            **POLICY.__dict__,
            "minimum_clv_rows": 100,
        }
    )
    report = build_validation_report(
        history("Player Hits", strong=False), policy=policy,
    )
    gate = report["marketGates"]["batter_hits"]

    assert gate["status"] == "disabled"
    assert gate["reasons"][0] != "CLV sample below 100"
    assert "model Brier score does not beat the market baseline" in gate["reasons"]
    assert "CLV sample below 100" in gate["reasons"]


def test_lookahead_backfills_are_excluded_from_every_promotion_gate():
    rows = history()
    rows[0]["backfilled"] = True
    report = build_validation_report(rows, policy=POLICY)

    assert report["excludedBackfillCount"] == 1
    assert report["historyCount"] == len(rows) - 1
