import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from continuous_learning import (
    LearningPolicy,
    build_continuous_learning_report,
    build_prediction_receipt,
    validate_observation,
)
from learning_engine import analyze_learning
from tracker_writer import build_pick_payload


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 20, 16, tzinfo=timezone.utc)


def observation(index=0, *, probability=0.65, outcome=1, days_ago=1):
    saved = NOW - timedelta(days=days_ago, hours=4)
    row = {
        "id": f"phase54-{index}",
        "gamePk": 5400 + index,
        "marketKey": "batter_hits",
        "recommendedSide": "Over",
        "line": 0.5,
        "savedAt": saved.isoformat(),
        "source": "xgb",
        "modelVersion": "champion-test",
        "componentProbabilities": {"xgb": probability - 0.03, "analytic": probability + 0.02},
        "preCalProb": probability - 0.02,
        "adjProb": probability,
        "openingImplied": 0.52,
        "book": "Book A",
    }
    row["learningReceipt"] = build_prediction_receipt(row)
    row["grade"] = "win" if outcome else "loss"
    row["gradedAt"] = (saved + timedelta(hours=3)).isoformat()
    return row


def test_prediction_receipt_is_immutable_and_excludes_outcome_fields():
    row = observation()
    receipt = row["learningReceipt"]
    assert receipt["measurementEligible"] is True
    assert receipt["probabilityAdaptationEligible"] is True
    assert receipt["marketBlendEligible"] is True
    assert receipt["outcomeFieldsIncluded"] is False
    before = receipt["predictionFingerprint"]
    row["grade"] = "loss"
    row["gradedAt"] = (NOW - timedelta(minutes=1)).isoformat()
    assert build_prediction_receipt(row)["predictionFingerprint"] == before


def test_tampered_prediction_snapshot_fails_closed():
    row = observation()
    row["adjProb"] = 0.91
    value, reasons = validate_observation(row, now=NOW)
    assert value is None
    assert "prediction receipt fingerprint mismatch" in reasons
    assert "prediction receipt snapshot mismatch" in reasons


def test_under_probabilities_are_bound_to_the_recommended_selection():
    row = observation()
    row["recommendedSide"] = "Under"
    row["adjProb"] = 0.30
    row["preCalProb"] = 0.32
    row["componentProbabilities"] = {"xgb": 0.30, "analytic": 0.35}
    receipt = build_prediction_receipt(row)
    assert receipt["snapshot"]["servedProbability"] == 0.70
    assert receipt["snapshot"]["preCalibrationProbability"] == 0.68
    assert receipt["snapshot"]["componentProbabilities"] == {
        "analytic": 0.65,
        "xgb": 0.70,
    }


def test_missing_or_lookahead_outcome_evidence_is_rejected():
    missing = observation()
    missing["gradedAt"] = None
    backfill = observation(1)
    backfill["backfilled"] = True
    for row in (missing, backfill):
        report = build_continuous_learning_report([row], now=NOW)
        assert report["acceptedObservationCount"] == 0
        assert report["rejectedObservationCount"] == 1


def test_duplicate_prediction_receipts_count_once():
    row = observation()
    report = build_continuous_learning_report([row, dict(row)], now=NOW)
    assert report["acceptedObservationCount"] == 1
    assert report["rejectedReasonCounts"]["duplicate prediction receipt"] == 1


def test_market_metrics_and_sample_gates_use_only_trusted_observations():
    rows = [
        observation(i, probability=0.70 if i % 2 == 0 else 0.30, outcome=i % 2)
        for i in range(80)
    ]
    report = build_continuous_learning_report(rows, now=NOW)
    market = report["markets"]["batter_hits"]
    assert report["acceptedObservationCount"] == 80
    assert market["metrics"]["count"] == 80
    assert market["metrics"]["brierScore"] is not None
    assert market["metrics"]["logLoss"] is not None
    assert market["metrics"]["ece"] is not None
    assert market["layerReadiness"]["smartConsensusReviewReady"] is True
    assert market["layerReadiness"]["blendLearningReviewReady"] is True
    assert market["layerReadiness"]["calibrationLearningReviewReady"] is True
    assert market["layerReadiness"]["modelRetrainingReviewReady"] is False
    assert market["layerReadiness"]["industryClvClaimReady"] is False


def test_recent_brier_degradation_creates_review_not_automatic_change():
    policy = LearningPolicy(minimum_drift_window_rows=5)
    rows = []
    for index in range(10):
        rows.append(observation(index, probability=0.85, outcome=1, days_ago=60))
    for index in range(10, 20):
        rows.append(observation(index, probability=0.85, outcome=0, days_ago=5))
    report = build_continuous_learning_report(rows, now=NOW, policy=policy)
    market = report["markets"]["batter_hits"]
    assert market["drift"]["status"] == "review"
    assert report["reviewQueue"][0]["marketKey"] == "batter_hits"
    assert all(
        value is False for key, value in report["safety"].items()
        if key.startswith("automatic")
    )
    assert report["safety"]["humanReviewRequired"] is True


def test_tracker_writer_attaches_phase54_receipt_at_prediction_time():
    payload = build_pick_payload(
        player="Test Player",
        market_key="batter_hits",
        line=0.5,
        side="Over",
        game_pk=54,
        adj_prob=0.64,
        opening_price=110,
        opening_implied=0.49,
        book="Book A",
        source="xgb",
        extra={"preCalProb": 0.62, "modelVersion": "champion-test"},
    )
    assert payload["learningReceipt"]["version"] == "5.4.0"
    assert payload["learningReceipt"]["measurementEligible"] is True
    assert payload["learningReceipt"]["snapshot"]["marketKey"] == "batter_hits"
    assert payload["learningReceipt"]["outcomeFieldsIncluded"] is False


def test_learning_engine_exposes_strict_phase54_audit_alongside_legacy_metrics():
    row = observation()
    result = analyze_learning([row])
    assert result["gradedCount"] == 1
    assert result["continuousLearningVersion"] == "5.4.0"
    assert result["continuousLearning"]["acceptedObservationCount"] == 1
    assert result["continuousLearning"]["safety"]["automaticModelPromotion"] is False


def test_contract_ci_and_docs_preserve_review_only_boundary():
    contract = json.loads(
        (ROOT / "data/continuous_learning_contract.json").read_text(encoding="utf-8")
    )
    quality = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    roadmap = (ROOT / "docs/MLB_ANALYTICS_HUB_ROADMAP.md").read_text(encoding="utf-8")
    docs = (ROOT / "docs/continuous_learning.md").read_text(encoding="utf-8")
    assert contract["version"] == "5.4.0"
    assert contract["automaticModelPromotion"] is False
    assert contract["humanReviewRequired"] is True
    assert "python scripts/continuous_learning.py --check-contract" in quality
    assert "### Phase 5.4 — Continuous learning foundation" in roadmap
    assert "never changes a" in docs
