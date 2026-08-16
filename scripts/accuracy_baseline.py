#!/usr/bin/env python3
"""Generate or verify the deterministic Phase 4.86 accuracy baseline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from accuracy_baseline import build_accuracy_baseline, git_blob_sha  # noqa: E402


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metrics", type=Path, default=ROOT / "models/model_metrics.json")
    parser.add_argument("--summary", type=Path, default=ROOT / "data/regen_summary.json")
    parser.add_argument("--features", type=Path, default=ROOT / "models/xgb_feature_cols.json")
    parser.add_argument("--calibration", type=Path, default=ROOT / "data/calibration_backfill.json")
    parser.add_argument("--champions", type=Path, default=ROOT / "data/champion_manifest.json")
    parser.add_argument("--report", type=Path, default=ROOT / "data/accuracy_baseline.json")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--refresh-champions", action="store_true")
    parser.add_argument("--source-sha")
    args = parser.parse_args()

    champions = _load(args.champions)
    if args.refresh_champions:
        if not args.source_sha or len(args.source_sha) != 40:
            parser.error("--refresh-champions requires a 40-character --source-sha")
        champions["frozen_from_main_sha"] = args.source_sha
        for model_key, record in champions.get("models", {}).items():
            artifact = ROOT / str(record.get("artifact_ref") or "")
            if not artifact.is_file():
                raise SystemExit(f"missing champion artifact for {model_key}: {artifact}")
            record["git_blob_sha"] = git_blob_sha(artifact)
        _write(args.champions, champions)

    actual_artifact_blobs = {}
    for model_key, record in champions.get("models", {}).items():
        artifact = ROOT / str(record.get("artifact_ref") or "")
        actual_artifact_blobs[model_key] = git_blob_sha(artifact) if artifact.is_file() else ""

    inputs = {
        "modelMetrics": args.metrics,
        "regenerationSummary": args.summary,
        "serveFeatures": args.features,
        "calibrationBackfill": args.calibration,
    }
    report = build_accuracy_baseline(
        _load(args.metrics),
        _load(args.summary),
        _load(args.features),
        champions,
        _load(args.calibration),
        actual_artifact_blobs=actual_artifact_blobs,
        source_blobs={name: git_blob_sha(path) for name, path in inputs.items()},
    )

    if args.check:
        committed = _load(args.report)
        if committed != report:
            print("Phase 4.86 baseline is stale; regenerate scripts/accuracy_baseline.py")
            return 1
    else:
        _write(args.report, report)

    print(json.dumps({
        "version": report["version"],
        "status": report["status"],
        "models": report["coverage"]["baselineModels"],
        "markets": report["coverage"]["calibrationMarkets"],
        "phase5_ready": report["phase5Handoff"]["ready"],
        "priority_models": report["phase5Handoff"]["priorityModels"],
    }, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
