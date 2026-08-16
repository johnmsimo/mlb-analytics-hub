"""Phase 5 data-source governance and experiment/decision admission.

This module is metadata-only. It never loads model artifacts, changes live
features, promotes a challenger, or approves a decision. Model experiments must
prove historical reconstruction; Phase 5.3 decision evidence must remain
explicitly decision-only and pass its live serving contract.
"""

from __future__ import annotations

from hashlib import sha1
import json
from pathlib import Path
from typing import Any, Mapping, Sequence


DATA_INTELLIGENCE_VERSION = "5.0.0"
SAFE_LEAKAGE_MODES = {"pregame_only", "prior_games_only", "static"}


def git_blob_sha(path: str | Path) -> str:
    data = Path(path).read_bytes()
    header = f"blob {len(data)}\0".encode("ascii")
    return sha1(header + data).hexdigest()


def _positive_minutes(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _source_is_admissible(source: Mapping[str, Any]) -> bool:
    return (
        source.get("historicallyReconstructable") is True
        and _nonempty(source.get("historicalPath"))
        and source.get("liveServeAvailable") is True
        and _nonempty(source.get("livePath"))
        and _positive_minutes(source.get("freshnessMinutes"))
        and source.get("leakageMode") in SAFE_LEAKAGE_MODES
    )


def _candidate_blockers(candidate: Mapping[str, Any], model_keys: set[str]) -> list[str]:
    blockers: list[str] = []
    model_eligible = candidate.get("modelEligible") is True
    decision_eligible = candidate.get("decisionEligible") is True
    if not model_eligible and not decision_eligible:
        blockers.append("phase_routing")
    if model_eligible and (
        candidate.get("historicallyReconstructable") is not True
        or not _nonempty(candidate.get("historicalPath"))
    ):
        blockers.append("historical_reconstruction")
    if decision_eligible and (
        candidate.get("phase") != "5.3"
        or not _nonempty(candidate.get("decisionPath"))
        or model_eligible
    ):
        blockers.append("decision_only_contract")
    if candidate.get("liveServeAvailable") is not True or not _nonempty(candidate.get("livePath")):
        blockers.append("live_serve_path")
    if not _positive_minutes(candidate.get("freshnessMinutes")):
        blockers.append("freshness_policy")
    if candidate.get("leakageMode") not in SAFE_LEAKAGE_MODES:
        blockers.append("leakage_review")
    targets = candidate.get("targetModels")
    if not isinstance(targets, list) or not targets or not set(targets).issubset(model_keys):
        blockers.append("target_model_contract")
    return blockers


def build_data_intelligence_report(
    accuracy_baseline: Mapping[str, Any],
    feature_map: Mapping[str, Sequence[str]],
    source_manifest: Mapping[str, Any],
    *,
    source_blobs: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    errors: list[str] = []
    if accuracy_baseline.get("status") != "passed":
        errors.append("Phase 4.86 accuracy baseline is not passed")
    if accuracy_baseline.get("phase5Handoff", {}).get("ready") is not True:
        errors.append("Phase 4.86 handoff is not ready")
    if source_manifest.get("version") != DATA_INTELLIGENCE_VERSION:
        errors.append("source manifest version does not match Phase 5.0")

    models = accuracy_baseline.get("modelBaselines")
    if not isinstance(models, Mapping) or not models:
        models = {}
        errors.append("accuracy baseline has no champion models")
    model_keys = set(models)

    source_by_feature: dict[str, str] = {}
    source_records: dict[str, Mapping[str, Any]] = {}
    raw_sources = source_manifest.get("sourceContracts")
    if not isinstance(raw_sources, list) or not raw_sources:
        raw_sources = []
        errors.append("source manifest has no source contracts")
    for source in raw_sources:
        if not isinstance(source, Mapping):
            errors.append("source contract is malformed")
            continue
        source_id = source.get("id")
        if not isinstance(source_id, str) or not source_id:
            errors.append("source contract has no id")
            continue
        if source_id in source_records:
            errors.append(f"duplicate source contract: {source_id}")
            continue
        source_records[source_id] = source
        if not _source_is_admissible(source):
            errors.append(f"current source is not admissible: {source_id}")
        features = source.get("features")
        if not isinstance(features, list) or not features:
            errors.append(f"source has no governed features: {source_id}")
            continue
        for feature in features:
            if not isinstance(feature, str) or not feature:
                errors.append(f"source has malformed feature: {source_id}")
            elif feature in source_by_feature:
                errors.append(f"feature has multiple source contracts: {feature}")
            else:
                source_by_feature[feature] = source_id

    governed_features: set[str] = set()
    model_coverage: dict[str, Any] = {}
    source_models: dict[str, set[str]] = {key: set() for key in source_records}
    for model_key in sorted(model_keys):
        features = feature_map.get(model_key)
        if not isinstance(features, list) or not features:
            errors.append(f"missing serve features for champion: {model_key}")
            features = []
        expected_count = models[model_key].get("featureCount")
        if expected_count != len(features):
            errors.append(f"feature count drift for champion: {model_key}")
        unmapped = sorted({feature for feature in features if feature not in source_by_feature})
        if unmapped:
            errors.append(f"ungoverned serve features for {model_key}: {', '.join(unmapped)}")
        source_ids = sorted({source_by_feature[feature] for feature in features if feature in source_by_feature})
        governed_features.update(feature for feature in features if feature in source_by_feature)
        for source_id in source_ids:
            source_models[source_id].add(model_key)
        model_coverage[model_key] = {
            "featureCount": len(features),
            "governedFeatureCount": len(features) - len(unmapped),
            "sourceIds": source_ids,
            "trackerMarket": models[model_key].get("trackerMarket"),
        }

    used_features = {feature for key in model_keys for feature in feature_map.get(key, [])}
    unused_contract_features = sorted(set(source_by_feature) - used_features)
    if unused_contract_features:
        errors.append("source manifest contains non-production features: " + ", ".join(unused_contract_features))

    source_coverage = []
    for source_id in sorted(source_records):
        source = source_records[source_id]
        source_coverage.append({
            "id": source_id,
            "featureCount": len(source.get("features", [])),
            "modelCount": len(source_models[source_id]),
            "admissible": _source_is_admissible(source),
            "freshnessMinutes": source.get("freshnessMinutes"),
        })

    candidates = source_manifest.get("candidateSignals")
    if not isinstance(candidates, list):
        candidates = []
        errors.append("source manifest has no candidate signal registry")
    candidate_queue = []
    priorities: set[int] = set()
    for candidate in candidates:
        if not isinstance(candidate, Mapping) or not isinstance(candidate.get("id"), str):
            errors.append("candidate signal is malformed")
            continue
        priority = candidate.get("researchPriority")
        if not isinstance(priority, int) or isinstance(priority, bool) or priority < 1 or priority in priorities:
            errors.append(f"candidate research priority is invalid: {candidate.get('id')}")
        else:
            priorities.add(priority)
        blockers = _candidate_blockers(candidate, model_keys)
        target_models = candidate.get("targetModels") if isinstance(candidate.get("targetModels"), list) else []
        target_markets = sorted({
            models[key].get("trackerMarket") for key in target_models
            if key in models and models[key].get("trackerMarket")
        })
        candidate_queue.append({
            "id": candidate["id"],
            "researchPriority": priority,
            "admitted": not blockers,
            "blockers": blockers,
            "targetModels": target_models,
            "targetMarkets": target_markets,
            "phase": candidate.get("phase"),
            "modelEligible": candidate.get("modelEligible") is True,
            "decisionEligible": candidate.get("decisionEligible") is True,
        })
    candidate_queue.sort(key=lambda item: (item["researchPriority"] if isinstance(item["researchPriority"], int) else 10**9, item["id"]))

    phase51_queue = [item for item in candidate_queue if item["phase"] == "5.1"]
    admitted51 = [item["id"] for item in phase51_queue if item["admitted"]]
    phase53_queue = [item for item in candidate_queue if item["phase"] == "5.3"]
    admitted53 = [
        item["id"] for item in phase53_queue
        if item["admitted"] and item["decisionEligible"] and not item["modelEligible"]
    ]
    promotion = accuracy_baseline.get("phase5Handoff", {}).get("promotionContract", {})
    return {
        "version": DATA_INTELLIGENCE_VERSION,
        "status": "passed" if not errors else "failed",
        "errors": errors,
        "sourceBaselineVersion": accuracy_baseline.get("version"),
        "sourceBlobs": dict(sorted((source_blobs or {}).items())),
        "coverage": {
            "championModels": len(models),
            "trackerMarkets": len({record.get("trackerMarket") for record in models.values()}),
            "uniqueServeFeatures": len(used_features),
            "governedServeFeatures": len(governed_features),
            "sourceContracts": len(source_records),
            "candidateSignals": len(candidate_queue),
            "admittedPhase51Signals": len(admitted51),
            "admittedPhase53Signals": len(admitted53),
        },
        "modelCoverage": model_coverage,
        "sourceCoverage": source_coverage,
        "candidateQueue": candidate_queue,
        "phase51Admission": {
            "ready": bool(admitted51) and not errors,
            "admittedSignals": admitted51,
            "queue": phase51_queue,
            "changesProductionProbabilities": False,
        },
        "phase53Admission": {
            "ready": bool(admitted53) and not errors,
            "admittedSignals": admitted53,
            "queue": phase53_queue,
            "modelTrainingEligible": False,
            "automaticAction": False,
            "reviewRequired": True,
        },
        "promotionSafety": {
            "automaticPromotion": False,
            "championComparisonStillRequired": True,
            "heldOutBrierMustImprove": promotion.get("heldOutBrierMustImprove") is True,
            "heldOutLoglossMustNotRegress": promotion.get("heldOutLoglossMustNotRegress") is True,
            "heldOutAucMustNotRegress": promotion.get("heldOutAucMustNotRegress") is True,
            "marketEceMustNotRegress": promotion.get("marketEceMustNotRegress") is True,
        },
    }
