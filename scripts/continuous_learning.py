#!/usr/bin/env python3
"""Verify or generate the Phase 5.4 continuous-learning audit."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from continuous_learning import (  # noqa: E402
    CONTINUOUS_LEARNING_VERSION,
    LearningPolicy,
    build_continuous_learning_report,
    build_prediction_receipt,
)


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _flatten_tracker(payload):
    if isinstance(payload, list):
        return payload
    rows = []
    if isinstance(payload, dict):
        for date_key, day in payload.items():
            if not isinstance(day, dict):
                continue
            for source in day.get("entries", []):
                row = dict(source)
                row.setdefault("date", date_key)
                rows.append(row)
    return rows


def _synthetic(now: datetime):
    row = {
        "id": "phase54-contract",
        "gamePk": 54,
        "marketKey": "batter_hits",
        "recommendedSide": "Over",
        "line": 0.5,
        "savedAt": (now - timedelta(hours=4)).isoformat(),
        "source": "xgb",
        "modelVersion": "champion-test",
        "preCalProb": 0.61,
        "adjProb": 0.63,
        "openingImplied": 0.52,
        "book": "Book A",
    }
    row["learningReceipt"] = build_prediction_receipt(row)
    row["grade"] = "win"
    row["gradedAt"] = (now - timedelta(hours=1)).isoformat()
    return row


def check_contract() -> list[str]:
    contract = _load(ROOT / "data/continuous_learning_contract.json")
    policy = LearningPolicy()
    errors = []
    if contract.get("version") != CONTINUOUS_LEARNING_VERSION:
        errors.append("contract version does not match implementation")
    expected_gates = {
        "smartConsensus": policy.smart_consensus_sample,
        "blendLearning": policy.blend_learning_sample,
        "calibrationLearning": policy.calibration_learning_sample,
        "modelRetrainingReview": policy.model_review_sample,
        "industryClvClaim": policy.clv_claim_sample,
    }
    if contract.get("sampleGates") != expected_gates:
        errors.append("sample gates drifted")
    expected_windows = {
        "recentDays": policy.recent_window_days,
        "baselineDays": policy.baseline_window_days,
        "minimumDriftRows": policy.minimum_drift_window_rows,
        "brierDriftDelta": policy.brier_drift_delta,
    }
    if contract.get("windows") != expected_windows:
        errors.append("learning windows drifted")
    for key in (
        "automaticModelPromotion", "automaticProbabilityAdaptation",
        "automaticStakingChange", "automaticThresholdChange", "automaticWeightChange",
    ):
        if contract.get(key) is not False:
            errors.append(f"{key} must remain disabled")
    if contract.get("humanReviewRequired") is not True:
        errors.append("human review is not required")
    now = datetime(2026, 8, 20, 16, tzinfo=timezone.utc)
    row = _synthetic(now)
    report = build_continuous_learning_report([row], now=now)
    if report.get("acceptedObservationCount") != 1:
        errors.append("known-good observation was not accepted")
    if any(value is not False for key, value in report.get("safety", {}).items() if key.startswith("automatic")):
        errors.append("runtime report enables automatic changes")
    tampered = dict(row)
    tampered["adjProb"] = 0.91
    rejected = build_continuous_learning_report([tampered], now=now)
    if rejected.get("acceptedObservationCount") != 0:
        errors.append("tampered prediction receipt did not fail closed")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-contract", action="store_true")
    parser.add_argument("--tracker", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.check_contract:
        errors = check_contract()
        print(json.dumps({
            "version": CONTINUOUS_LEARNING_VERSION,
            "status": "failed" if errors else "passed",
            "errors": errors,
        }, indent=2))
        return 1 if errors else 0
    if not args.tracker or not args.report:
        parser.error("use --check-contract or provide --tracker and --report")
    report = build_continuous_learning_report(_flatten_tracker(_load(args.tracker)))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "version": report["version"],
        "accepted": report["acceptedObservationCount"],
        "rejected": report["rejectedObservationCount"],
        "reviewMarkets": len(report["reviewQueue"]),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
