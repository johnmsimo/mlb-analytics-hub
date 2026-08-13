#!/usr/bin/env python3
"""Validate all regenerated model metadata before a weekly PR is opened."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from model_operations import evaluate_candidate  # noqa: E402


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--summary", type=Path, default=ROOT / "data/regen_summary.json")
    parser.add_argument("--metrics", type=Path, default=ROOT / "models/model_metrics.json")
    parser.add_argument("--features", type=Path, default=ROOT / "models/xgb_feature_cols.json")
    parser.add_argument("--report", type=Path, default=ROOT / "data/model_gate_report.json")
    args = parser.parse_args()

    summary = _load(args.summary)
    metrics = _load(args.metrics).get("models", {})
    feature_map = _load(args.features)
    evaluations = {}
    for model_key, metadata in sorted(summary.items()):
        merged = dict(metadata)
        merged.update(metrics.get(model_key, {}))
        candidate_features = feature_map.get(model_key)
        evaluations[model_key] = evaluate_candidate(
            model_key,
            merged,
            candidate_features=candidate_features,
            serve_feature_map=feature_map,
        ).to_dict()

    failed = [key for key, value in evaluations.items() if not value["passed"]]
    report = {
        "version": "4.61.0",
        "status": "failed" if failed else "passed",
        "failed_models": failed,
        "models": evaluations,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "models": len(evaluations), "failed_models": failed}, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

