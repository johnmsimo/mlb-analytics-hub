from datetime import datetime, timedelta, timezone

from market_validation import (
    CALIBRATION_VERSION,
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


def calibration_history(*, drifted=False):
    start = datetime(2026, 7, 1, 12, tzinfo=timezone.utc)
    rows = []
    for day in range(12):
        for pick in range(2):
            bad_recent = drifted and day >= 8
            outcome = 0 if ((day + pick) % 3 == 0) else 1
            if bad_recent:
                outcome = 1 - outcome
            rows.append({
                "id": f"cal-{day}-{pick}",
                "date": (start + timedelta(days=day)).date().isoformat(),
                "savedAt": (start + timedelta(days=day, minutes=pick)).isoformat(),
                "market": "Pitcher Strikeouts",
                "recommendedSide": "Over",
                "adjProb": 0.80,
                "openingImplied": 0.52,
                "openingPrice": 100,
                "bestAvailableBook": "Book A",
                "confidenceTier": "HIGH CONF",
                "recommendationGrade": "STRONG BET",
                "grade": "win" if outcome else "loss",
                "clvEdge": 0.02,
            })
    return rows


def test_market_calibration_exposes_required_evidence():
    report = build_validation_report(calibration_history(), policy=POLICY)
    metrics = report["marketGates"]["pitcher_strikeouts"]["metrics"]

    assert report["calibrationVersion"] == CALIBRATION_VERSION
    assert metrics["sampleSize"] == metrics["count"]
    assert metrics["brierScore"] is not None
    assert metrics["expectedCalibrationError"] == metrics["calibrationError"]
    assert metrics["confidenceInterval"]["level"] == 0.95
    assert metrics["confidenceInterval"]["winRate"]["lower"] <= metrics["winRate"]
    assert metrics["confidenceInterval"]["winRate"]["upper"] >= metrics["winRate"]
    assert metrics["driftStatus"] in {"stable", "watch", "unknown"}


def test_calibration_drift_disables_market_and_downgrades_strong_labels():
    report = build_validation_report(calibration_history(drifted=True), policy=POLICY)
    gate = report["marketGates"]["pitcher_strikeouts"]
    assert gate["status"] == "disabled"
    assert "calibration drift exceeds policy" in gate["reasons"] or "calibration error exceeds policy" in gate["reasons"]

    gated = apply_market_gates([{
        "marketKey": "pitcher_strikeouts",
        "recommendedSide": "Over",
        "recommendationGrade": "STRONG BET",
        "confidenceTier": "HIGH CONF",
    }], report)
    rejected = gated["rejected"][0]
    assert rejected["actionable"] is False
    assert rejected["recommendationGrade"] == "CAUTION"
    assert rejected["confidenceTier"] == "CAUTION"
    assert rejected["calibrationDowngraded"] is True


def test_calibration_audit_lists_failing_markets():
    report = build_validation_report(calibration_history(drifted=True), policy=POLICY)
    assert "pitcher_strikeouts" in report["calibrationAudit"]["failingMarkets"]
    assert report["calibrationAudit"]["version"] == CALIBRATION_VERSION
