#!/usr/bin/env python3
"""Verify or run the Phase 5.1 RBI opportunity-context challenger."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rbi_opportunity import (  # noqa: E402
    RBI_OPPORTUNITY_VERSION,
    RBI_TRAFFIC_FEATURE,
    compare_frozen_champion,
)


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def check_contract() -> list[str]:
    from regenerate_models import RBI_CHALLENGER_FEATURES, RBI_FEATURES
    from xgb_prop_scorer import _build_rbi_features

    errors: list[str] = []
    contract = _load(ROOT / "data/rbi_opportunity_experiment.json")
    intelligence = _load(ROOT / "data/data_intelligence_report.json")
    production_features = _load(ROOT / "models/xgb_feature_cols.json")

    if contract.get("version") != RBI_OPPORTUNITY_VERSION:
        errors.append("experiment contract version mismatch")
    admitted = intelligence.get("phase51Admission", {}).get("admittedSignals", [])
    if contract.get("experimentId") not in admitted:
        errors.append("RBI opportunity signal is not admitted by Phase 5.0")
    if RBI_CHALLENGER_FEATURES != [*RBI_FEATURES, RBI_TRAFFIC_FEATURE]:
        errors.append("challenger feature order must append exactly one RBI traffic feature")
    if any(RBI_TRAFFIC_FEATURE in production_features.get(key, []) for key in ("rbi", "rbi_1.5")):
        errors.append("RBI traffic feature entered production before experiment approval")
    safety = contract.get("safety", {})
    if safety.get("writesChampionArtifacts") is not False:
        errors.append("experiment must not write champion artifacts")
    if safety.get("automaticPromotion") is not False:
        errors.append("automatic promotion must remain disabled")

    vector = _build_rbi_features(
        {"rbiTrafficObp": 0.357},
        {},
        [RBI_TRAFFIC_FEATURE],
    )
    if vector is None or vector.shape != (1, 1) or abs(float(vector[0, 0]) - 0.357) > 1e-6:
        errors.append("live scorer does not preserve RBI traffic feature scale")
    return errors


def run_experiment(report_path: Path) -> dict:
    import pandas as pd
    from regenerate_models import (
        ALL_SEASONS,
        MARKETS,
        RBI_CHALLENGER_FEATURES,
        TEST_SEASON,
        TRAIN_SEASONS,
        build_batter_matrix,
        fetch_game_logs,
        train_market,
    )

    errors = check_contract()
    if errors:
        raise RuntimeError("; ".join(errors))
    baseline = _load(ROOT / "data/accuracy_baseline.json")
    game_rows, _, _ = fetch_game_logs(ALL_SEASONS)
    if game_rows.empty:
        raise RuntimeError("no historical batter game rows were available")
    batter_matrix = build_batter_matrix(game_rows)
    if batter_matrix.empty or RBI_TRAFFIC_FEATURE not in batter_matrix:
        raise RuntimeError("RBI opportunity feature matrix was not built")

    comparisons = {}
    challenger_metrics = {}
    for model_key in ("rbi_1.5", "rbi"):
        config = dict(MARKETS[model_key])
        config["features"] = list(RBI_CHALLENGER_FEATURES)
        result = train_market(f"{model_key}_opportunity_challenger", config, batter_matrix.copy())
        if not result:
            comparisons[model_key] = {
                "modelKey": model_key,
                "status": "held",
                "errors": ["challenger training failed"],
                "shadowEligible": False,
                "promotionEligible": False,
                "automaticPromotion": False,
            }
            continue
        metrics = dict(result["artifact"]["meta"])
        challenger_metrics[model_key] = metrics
        comparisons[model_key] = compare_frozen_champion(
            model_key,
            baseline["modelBaselines"][model_key],
            metrics,
        )

    traffic = pd.to_numeric(batter_matrix[RBI_TRAFFIC_FEATURE], errors="coerce")
    report = {
        "version": RBI_OPPORTUNITY_VERSION,
        "experimentId": "rbi_opportunity_context",
        "status": "completed",
        "generatedAtUtc": datetime.now(timezone.utc).isoformat(),
        "split": {
            "trainSeasons": TRAIN_SEASONS,
            "testSeason": TEST_SEASON,
            "strictlyTemporal": True,
        },
        "featureEvidence": {
            "name": RBI_TRAFFIC_FEATURE,
            "rows": int(len(batter_matrix)),
            "nonNullRows": int(traffic.notna().sum()),
            "rowsWithThreeObservedPredecessors": int(
                (batter_matrix["rbi_traffic_observed_slots"] == 3).sum()
            ),
            "fallbackOnlyRows": int(
                (batter_matrix["rbi_traffic_observed_slots"] == 0).sum()
            ),
            "mean": round(float(traffic.mean()), 6),
            "minimum": round(float(traffic.min()), 6),
            "maximum": round(float(traffic.max()), 6),
        },
        "challengerMetrics": challenger_metrics,
        "comparisons": comparisons,
        "shadowEligibleModels": sorted(
            key for key, comparison in comparisons.items()
            if comparison.get("shadowEligible") is True
        ),
        "promotionEligibleModels": [],
        "writesChampionArtifacts": False,
        "changesProductionProbabilities": False,
        "automaticPromotion": False,
        "nextGate": "market ECE from shadow calibration",
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-contract", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument(
        "--report",
        type=Path,
        default=ROOT / "artifacts/rbi_opportunity_experiment_report.json",
    )
    args = parser.parse_args()
    if args.run:
        report = run_experiment(args.report)
        print(json.dumps({
            "status": report["status"],
            "shadowEligibleModels": report["shadowEligibleModels"],
            "promotionEligibleModels": report["promotionEligibleModels"],
            "report": str(args.report),
        }, indent=2))
        return 0
    errors = check_contract()
    print(json.dumps({
        "version": RBI_OPPORTUNITY_VERSION,
        "status": "passed" if not errors else "failed",
        "errors": errors,
    }, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
