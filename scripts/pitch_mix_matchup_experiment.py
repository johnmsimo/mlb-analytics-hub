#!/usr/bin/env python3
"""Verify or run the Phase 5.2 pitch-mix/contact challengers."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pitch_mix_matchup import (  # noqa: E402
    PITCH_MIX_CONTACT_FEATURE,
    PITCH_MIX_MATCHUP_VERSION,
)
from rbi_opportunity import compare_frozen_champion  # noqa: E402


TARGET_MODELS = ("hits_1.5", "tb_2.5", "tb_3.5", "hits", "tb", "hr")


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def check_contract() -> list[str]:
    from regenerate_models import MARKETS, PITCH_MIX_CHALLENGER_FEATURES
    from savant_arsenal import get_batter_pitch_type_stats
    from xgb_prop_scorer import _build_batter_market_features, _build_hit_features

    errors: list[str] = []
    contract = _load(ROOT / "data/pitch_mix_matchup_experiment.json")
    intelligence = _load(ROOT / "data/data_intelligence_report.json")
    production_features = _load(ROOT / "models/xgb_feature_cols.json")

    if contract.get("version") != PITCH_MIX_MATCHUP_VERSION:
        errors.append("pitch-mix experiment contract version mismatch")
    admitted = intelligence.get("phase51Admission", {}).get("admittedSignals", [])
    if contract.get("experimentId") not in admitted:
        errors.append("pitch-mix/contact signal is not admitted by Phase 5.0")
    if tuple(contract.get("targetModels", ())) != TARGET_MODELS:
        errors.append("target models must preserve the frozen weakness order")
    for model_key in TARGET_MODELS:
        production = list(MARKETS[model_key]["features"])
        challenger = PITCH_MIX_CHALLENGER_FEATURES.get(model_key)
        if challenger != [*production, PITCH_MIX_CONTACT_FEATURE]:
            errors.append(f"{model_key} challenger must append exactly one pitch-mix feature")
        if PITCH_MIX_CONTACT_FEATURE in production_features.get(model_key, []):
            errors.append(f"{model_key} pitch-mix feature entered production early")
    safety = contract.get("safety", {})
    if safety.get("writesChampionArtifacts") is not False:
        errors.append("experiment must not write champion artifacts")
    if safety.get("changesProductionProbabilities") is not False:
        errors.append("experiment must not change production probabilities")
    if safety.get("automaticPromotion") is not False:
        errors.append("automatic promotion must remain disabled")
    if not callable(get_batter_pitch_type_stats):
        errors.append("live batter pitch-type source is unavailable")

    batter = {"pitchMixContactEdge": 0.037}
    hit_vector = _build_hit_features(batter, {}, [PITCH_MIX_CONTACT_FEATURE])
    power_vector = _build_batter_market_features(
        batter, {}, [PITCH_MIX_CONTACT_FEATURE]
    )
    for label, vector in (("hit", hit_vector), ("power", power_vector)):
        if (
            vector is None
            or vector.shape != (1, 1)
            or abs(float(vector[0, 0]) - 0.037) > 1e-6
        ):
            errors.append(f"{label} scorer does not preserve pitch-mix feature scale")
    return errors


def run_experiment(report_path: Path) -> dict:
    import pandas as pd
    from regenerate_models import (
        ALL_SEASONS,
        MARKETS,
        PITCH_MIX_CHALLENGER_FEATURES,
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
    if batter_matrix.empty or PITCH_MIX_CONTACT_FEATURE not in batter_matrix:
        raise RuntimeError("pitch-mix/contact feature matrix was not built")

    comparisons = {}
    challenger_metrics = {}
    for model_key in TARGET_MODELS:
        config = dict(MARKETS[model_key])
        config["features"] = list(PITCH_MIX_CHALLENGER_FEATURES[model_key])
        result = train_market(
            f"{model_key}_pitch_mix_challenger",
            config,
            batter_matrix.copy(),
        )
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

    feature = pd.to_numeric(
        batter_matrix[PITCH_MIX_CONTACT_FEATURE], errors="coerce"
    )
    observed = (
        (batter_matrix["pitch_mix_batter_pa"] >= 15)
        & (batter_matrix["pitch_mix_pitcher_pitches"] >= 50)
    )
    report = {
        "version": PITCH_MIX_MATCHUP_VERSION,
        "experimentId": "pitch_mix_contact_matchup",
        "status": "completed",
        "generatedAtUtc": datetime.now(timezone.utc).isoformat(),
        "split": {
            "trainSeasons": TRAIN_SEASONS,
            "testSeason": TEST_SEASON,
            "strictlyTemporal": True,
        },
        "featureEvidence": {
            "name": PITCH_MIX_CONTACT_FEATURE,
            "rows": int(len(batter_matrix)),
            "nonNullRows": int(feature.notna().sum()),
            "observedRows": int(observed.sum()),
            "neutralFallbackRows": int((~observed).sum()),
            "mean": round(float(feature.mean()), 6),
            "minimum": round(float(feature.min()), 6),
            "maximum": round(float(feature.max()), 6),
        },
        "challengerMetrics": challenger_metrics,
        "comparisons": comparisons,
        "shadowEligibleModels": [
            key for key in TARGET_MODELS
            if comparisons.get(key, {}).get("shadowEligible") is True
        ],
        "promotionEligibleModels": [],
        "writesChampionArtifacts": False,
        "changesProductionProbabilities": False,
        "automaticPromotion": False,
        "nextGate": "market ECE from shadow calibration",
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-contract", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument(
        "--report",
        type=Path,
        default=ROOT / "artifacts/pitch_mix_matchup_experiment_report.json",
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
        "version": PITCH_MIX_MATCHUP_VERSION,
        "status": "passed" if not errors else "failed",
        "errors": errors,
    }, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
