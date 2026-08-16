#!/usr/bin/env python3
"""Generate or verify the deterministic Phase 5.0 data-intelligence report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_intelligence import build_data_intelligence_report, git_blob_sha  # noqa: E402


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, default=ROOT / "data/accuracy_baseline.json")
    parser.add_argument("--features", type=Path, default=ROOT / "models/xgb_feature_cols.json")
    parser.add_argument("--manifest", type=Path, default=ROOT / "data/feature_source_manifest.json")
    parser.add_argument("--report", type=Path, default=ROOT / "data/data_intelligence_report.json")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    inputs = {
        "accuracyBaseline": args.baseline,
        "serveFeatures": args.features,
        "sourceManifest": args.manifest,
    }
    report = build_data_intelligence_report(
        _load(args.baseline),
        _load(args.features),
        _load(args.manifest),
        source_blobs={name: git_blob_sha(path) for name, path in inputs.items()},
    )
    if args.check:
        if _load(args.report) != report:
            print("Phase 5.0 data-intelligence report is stale; run scripts/data_intelligence.py")
            return 1
    else:
        _write(args.report, report)

    print(json.dumps({
        "version": report["version"],
        "status": report["status"],
        "features": report["coverage"]["governedServeFeatures"],
        "sources": report["coverage"]["sourceContracts"],
        "phase51_ready": report["phase51Admission"]["ready"],
        "admitted_signals": report["phase51Admission"]["admittedSignals"],
    }, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
