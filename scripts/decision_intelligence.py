#!/usr/bin/env python3
"""Verify the Phase 5.3 decision-intelligence safety contract."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from decision_intelligence import (  # noqa: E402
    DECISION_INTELLIGENCE_VERSION,
    DecisionPolicy,
    evaluate_decision,
    market_thresholds,
)


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _candidate() -> dict:
    return {
        "canonicalCandidateId": "phase53-contract",
        "canonicalMarketKey": "batter_hits",
        "canonicalSide": "over",
        "line": 1.5,
        "canonicalProbability": 0.62,
        "marketGatePromoted": True,
        "marketGateStatus": "promoted",
        "marketSideGateStatus": "promoted",
        "bankroll": 1000,
    }


def _quotes(now: datetime) -> list[dict]:
    return [
        {
            "book": "Book A",
            "source": "phase53-contract",
            "capturedAt": (now - timedelta(seconds=30)).isoformat(),
            "line": 1.5,
            "overPrice": 105,
            "underPrice": -125,
        },
        {
            "book": "Book B",
            "source": "phase53-contract",
            "capturedAt": (now - timedelta(seconds=45)).isoformat(),
            "line": 1.5,
            "overPrice": 110,
            "underPrice": -130,
        },
    ]


def verify_contract(root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    contract = _load(root / "data/decision_intelligence_contract.json")
    manifest = _load(root / "data/feature_source_manifest.json")
    report = _load(root / "data/data_intelligence_report.json")
    expected_policy = DecisionPolicy()

    if contract.get("version") != DECISION_INTELLIGENCE_VERSION:
        errors.append("decision contract version does not match implementation")
    if contract.get("role") != "decision_only" or contract.get("modelEligible") is not False:
        errors.append("sportsbook consensus must remain decision-only")
    if contract.get("automaticAction") is not False or contract.get("decisionReviewRequired") is not True:
        errors.append("decision contract must prohibit automatic action")
    if contract.get("minimumBooks") != expected_policy.minimum_books:
        errors.append("minimum book count drifted")
    if contract.get("freshnessSeconds") != expected_policy.maximum_quote_age_seconds:
        errors.append("quote freshness contract drifted")
    if contract.get("maximumConsensusSpread") != expected_policy.maximum_consensus_spread:
        errors.append("consensus spread contract drifted")
    expected_staking = {
        "kellyFraction": expected_policy.kelly_fraction,
        "maximumStakePct": expected_policy.maximum_stake_pct,
        "unitPct": expected_policy.unit_pct,
    }
    if contract.get("staking") != expected_staking:
        errors.append("staking safety contract drifted")
    expected_thresholds = {
        market: market_thresholds(market)
        for market in sorted(contract.get("marketThresholds", {}))
    }
    if contract.get("marketThresholds") != expected_thresholds:
        errors.append("market-specific thresholds drifted")

    candidates = {
        item.get("id"): item for item in manifest.get("candidateSignals", [])
        if isinstance(item, dict)
    }
    consensus = candidates.get(contract.get("signalId"), {})
    if consensus.get("decisionEligible") is not True:
        errors.append("sportsbook consensus is not admitted for decisions")
    if consensus.get("modelEligible") is not False:
        errors.append("sportsbook consensus became model-eligible")
    if consensus.get("decisionPath") != "decision_intelligence.evaluate_decision":
        errors.append("sportsbook consensus decision path drifted")

    admission = report.get("phase53Admission", {})
    if admission.get("ready") is not True:
        errors.append("Phase 5.3 data-intelligence admission is not ready")
    if contract.get("signalId") not in admission.get("admittedSignals", []):
        errors.append("sportsbook consensus is missing from Phase 5.3 admission")
    if admission.get("modelTrainingEligible") is not False:
        errors.append("Phase 5.3 admission permits model training")
    if admission.get("automaticAction") is not False or admission.get("reviewRequired") is not True:
        errors.append("Phase 5.3 admission bypasses review")

    now = datetime(2026, 8, 16, 16, 0, tzinfo=timezone.utc)
    decision = evaluate_decision(_candidate(), _quotes(now), now=now)
    if decision.get("decisionQualified") is not True:
        errors.append("known-good consensus does not qualify for review")
    if decision.get("decisionApproved") is not False or decision.get("actionable") is not False:
        errors.append("qualified decision bypasses review")
    if decision.get("decisionReviewRequired") is not True:
        errors.append("qualified decision does not require review")
    if decision.get("stakePreview", {}).get("stakePct", 1) > expected_policy.maximum_stake_pct:
        errors.append("stake preview exceeds the hard bankroll cap")

    stale = _quotes(now)
    stale[0]["capturedAt"] = (now - timedelta(seconds=301)).isoformat()
    stale[1]["capturedAt"] = (now - timedelta(seconds=301)).isoformat()
    no_bet = evaluate_decision(_candidate(), stale, now=now)
    if no_bet.get("decisionStatus") != "no_bet" or no_bet.get("actionable") is not False:
        errors.append("stale consensus did not fail closed")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-contract", action="store_true")
    args = parser.parse_args()
    if not args.check_contract:
        parser.error("--check-contract is required")
    errors = verify_contract()
    print(json.dumps({
        "version": DECISION_INTELLIGENCE_VERSION,
        "status": "failed" if errors else "passed",
        "errors": errors,
    }, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
