"""Phase 4.86 reproducible accuracy baseline and Phase 5 handoff.

The baseline is metadata-only: it never loads pickle artifacts or promotes a
model.  It proves the current champion artifact identity, temporal holdout,
serve feature parity, market contract, held-out skill, and current-season
calibration evidence before declaring the Phase 5 baseline ready.
"""

from __future__ import annotations

from hashlib import sha1, sha256
import json
import math
from pathlib import Path
from statistics import median
from typing import Any, Mapping, Sequence

from model_operations import FEATURE_ALIASES, MARKET_CONTRACTS


ACCURACY_BASELINE_VERSION = "4.86.0"
MIN_CALIBRATION_SAMPLE = 500
CALIBRATION_BINS = 10
REQUIRED_METRICS = (
    "test_auc",
    "train_auc",
    "test_brier",
    "baserate_brier",
    "test_logloss",
    "n_train",
    "n_test",
)


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _rounded(value: float) -> float:
    return round(float(value), 6)


def git_blob_sha(path: str | Path) -> str:
    data = Path(path).read_bytes()
    header = f"blob {len(data)}\0".encode("ascii")
    return sha1(header + data).hexdigest()


def feature_signature(features: Sequence[str]) -> str:
    encoded = json.dumps(list(features), separators=(",", ":"), ensure_ascii=True)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _calibration_snapshot(pairs: Any) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    if not isinstance(pairs, list):
        return None, ["calibration pairs are missing"]
    clean: list[tuple[float, float]] = []
    bins: list[list[tuple[float, float]]] = [[] for _ in range(CALIBRATION_BINS)]
    for pair in pairs:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            errors.append("calibration pair is malformed")
            continue
        probability = _finite(pair[0])
        outcome = _finite(pair[1])
        if probability is None or not 0 <= probability <= 1:
            errors.append("calibration probability is outside [0, 1]")
            continue
        if outcome not in (0.0, 1.0):
            errors.append("calibration outcome is not binary")
            continue
        clean.append((probability, outcome))
        index = min(CALIBRATION_BINS - 1, int(probability * CALIBRATION_BINS))
        bins[index].append((probability, outcome))
    if len(clean) < MIN_CALIBRATION_SAMPLE:
        errors.append(f"calibration sample must be >= {MIN_CALIBRATION_SAMPLE}")
    if not clean:
        return None, errors

    n = len(clean)
    brier = sum((probability - outcome) ** 2 for probability, outcome in clean) / n
    logloss = 0.0
    for probability, outcome in clean:
        bounded = min(1 - 1e-12, max(1e-12, probability))
        logloss += -(outcome * math.log(bounded) + (1 - outcome) * math.log(1 - bounded))
    logloss /= n
    ece = 0.0
    populated = 0
    for values in bins:
        if not values:
            continue
        populated += 1
        mean_probability = sum(item[0] for item in values) / len(values)
        mean_outcome = sum(item[1] for item in values) / len(values)
        ece += len(values) / n * abs(mean_probability - mean_outcome)
    return {
        "sampleSize": n,
        "brier": _rounded(brier),
        "logloss": _rounded(logloss),
        "ece": _rounded(ece),
        "meanProbability": _rounded(sum(item[0] for item in clean) / n),
        "observedRate": _rounded(sum(item[1] for item in clean) / n),
        "populatedBins": populated,
        "minimumSample": MIN_CALIBRATION_SAMPLE,
        "sampleReady": n >= MIN_CALIBRATION_SAMPLE,
    }, errors


def build_accuracy_baseline(
    metrics_payload: Mapping[str, Any],
    regeneration_payload: Mapping[str, Any],
    feature_map: Mapping[str, Sequence[str]],
    champion_manifest: Mapping[str, Any],
    calibration_payload: Mapping[str, Any],
    *,
    actual_artifact_blobs: Mapping[str, str],
    source_blobs: Mapping[str, str],
) -> dict[str, Any]:
    errors: list[str] = []
    models_payload = metrics_payload.get("models")
    if not isinstance(models_payload, Mapping):
        models_payload = {}
        errors.append("model metrics payload is missing models")
    champions = champion_manifest.get("models")
    if not isinstance(champions, Mapping):
        champions = {}
        errors.append("champion manifest is missing models")

    expected_models = tuple(sorted(MARKET_CONTRACTS))
    expected_tracker_markets = tuple(
        sorted({str(value["tracker_market"]) for value in MARKET_CONTRACTS.values()})
    )
    if set(models_payload) != set(expected_models):
        errors.append("model metrics do not exactly cover the production market contract")
    if set(regeneration_payload) != set(expected_models):
        errors.append("regeneration metadata do not exactly cover the production market contract")
    if set(champions) != set(expected_models):
        errors.append("champion manifest does not exactly cover the production market contract")

    model_baselines: dict[str, dict[str, Any]] = {}
    for model_key in expected_models:
        contract = MARKET_CONTRACTS[model_key]
        metrics = models_payload.get(model_key)
        metadata = regeneration_payload.get(model_key)
        champion = champions.get(model_key)
        model_errors: list[str] = []
        if not isinstance(metrics, Mapping):
            metrics = {}
            model_errors.append("held-out metrics are missing")
        if not isinstance(metadata, Mapping):
            metadata = {}
            model_errors.append("regeneration metadata are missing")
        if not isinstance(champion, Mapping):
            champion = {}
            model_errors.append("champion record is missing")

        values = {name: _finite(metrics.get(name)) for name in REQUIRED_METRICS}
        for name, value in values.items():
            if value is None:
                model_errors.append(f"{name} is missing or non-finite")
        test_auc = values["test_auc"]
        train_auc = values["train_auc"]
        test_brier = values["test_brier"]
        baserate_brier = values["baserate_brier"]
        if test_auc is not None and not 0.5 <= test_auc <= 1:
            model_errors.append("test_auc is outside [0.5, 1]")
        if train_auc is not None and not 0.5 <= train_auc <= 1:
            model_errors.append("train_auc is outside [0.5, 1]")
        if (
            test_brier is not None
            and baserate_brier is not None
            and not test_brier < baserate_brier
        ):
            model_errors.append("held-out Brier does not beat the base-rate Brier")
        if values["n_train"] is not None and values["n_train"] < 1:
            model_errors.append("training sample is empty")
        if values["n_test"] is not None and values["n_test"] < 1:
            model_errors.append("held-out sample is empty")

        target = str(metadata.get("target") or "")
        line = _finite(metadata.get("line"))
        tracker_market = str(metrics.get("tracker_market") or "")
        if target != contract["target"]:
            model_errors.append("target does not match the production market contract")
        if line != float(contract["line"]):
            model_errors.append("line does not match the production market contract")
        if tracker_market != contract["tracker_market"]:
            model_errors.append("tracker market does not match the production market contract")

        train_seasons = metadata.get("train_seasons")
        test_season = metadata.get("test_season")
        if not isinstance(train_seasons, list) or not train_seasons:
            model_errors.append("training seasons are missing")
            train_seasons = []
        temporal_split = bool(
            train_seasons
            and all(isinstance(value, int) for value in train_seasons)
            and isinstance(test_season, int)
            and max(train_seasons) < test_season
            and test_season not in train_seasons
        )
        if not temporal_split:
            model_errors.append("training and held-out seasons are not strictly temporal")

        candidate_features = feature_map.get(model_key)
        if not isinstance(candidate_features, list) or not candidate_features:
            model_errors.append("candidate feature order is missing")
            candidate_features = []
        if target in candidate_features:
            model_errors.append("target leakage: target appears in candidate features")
        aliases = FEATURE_ALIASES.get(model_key, (model_key,))
        for alias in aliases:
            if list(feature_map.get(alias) or []) != list(candidate_features):
                model_errors.append(f"serve feature alias {alias} does not match candidate order")

        artifact_ref = str(champion.get("artifact_ref") or "")
        expected_blob = str(champion.get("git_blob_sha") or "")
        actual_blob = str(actual_artifact_blobs.get(model_key) or "")
        if not artifact_ref.startswith("models/") or not artifact_ref.endswith(".pkl"):
            model_errors.append("champion artifact reference is invalid")
        if len(expected_blob) != 40 or actual_blob != expected_blob:
            model_errors.append("champion artifact identity does not match the frozen manifest")

        brier_skill = None
        auc_lift = None
        generalization_gap = None
        if test_brier is not None and baserate_brier not in (None, 0):
            brier_skill = 1 - test_brier / baserate_brier
        if test_auc is not None:
            auc_lift = test_auc - 0.5
        if train_auc is not None and test_auc is not None:
            generalization_gap = train_auc - test_auc

        model_baselines[model_key] = {
            "status": "passed" if not model_errors else "blocked",
            "trackerMarket": tracker_market or None,
            "target": target or None,
            "line": line,
            "artifactRef": artifact_ref or None,
            "artifactGitBlobSha": expected_blob or None,
            "featureCount": len(candidate_features),
            "featureSignature": feature_signature(candidate_features),
            "split": {
                "trainSeasons": train_seasons,
                "testSeason": test_season,
                "strictlyTemporal": temporal_split,
                "nTrain": int(values["n_train"]) if values["n_train"] is not None else None,
                "nTest": int(values["n_test"]) if values["n_test"] is not None else None,
            },
            "heldOut": {
                "auc": test_auc,
                "trainAuc": train_auc,
                "aucLiftOverRandom": _rounded(auc_lift) if auc_lift is not None else None,
                "brier": test_brier,
                "baseRateBrier": baserate_brier,
                "brierSkillScore": _rounded(brier_skill) if brier_skill is not None else None,
                "logloss": values["test_logloss"],
                "generalizationGap": (
                    _rounded(generalization_gap) if generalization_gap is not None else None
                ),
            },
            "calibrationMethod": metadata.get("calibration"),
            "modelType": metadata.get("model_type"),
            "exportedAtUtc": metadata.get("exported_at_utc"),
            "errors": model_errors,
        }
        errors.extend(f"{model_key}: {message}" for message in model_errors)

    calibration_markets = calibration_payload.get("markets")
    if not isinstance(calibration_markets, Mapping):
        calibration_markets = {}
        errors.append("current-season calibration payload is missing markets")
    calibration_baselines: dict[str, dict[str, Any] | None] = {}
    for tracker_market in expected_tracker_markets:
        snapshot, calibration_errors = _calibration_snapshot(
            calibration_markets.get(tracker_market)
        )
        calibration_baselines[tracker_market] = snapshot
        errors.extend(f"{tracker_market}: {message}" for message in calibration_errors)

    market_baselines: dict[str, dict[str, Any]] = {}
    for tracker_market in expected_tracker_markets:
        keys = [
            key
            for key, value in model_baselines.items()
            if value["trackerMarket"] == tracker_market
        ]
        held = [model_baselines[key]["heldOut"] for key in keys]
        market_baselines[tracker_market] = {
            "modelKeys": keys,
            "modelCount": len(keys),
            "heldOutCohortSize": max(
                (model_baselines[key]["split"]["nTest"] or 0 for key in keys),
                default=0,
            ),
            "medianTestAuc": _rounded(median(item["auc"] for item in held)) if held else None,
            "weakestBrierSkillScore": (
                min(item["brierSkillScore"] for item in held) if held else None
            ),
            "maximumGeneralizationGap": (
                max(item["generalizationGap"] for item in held) if held else None
            ),
            "currentSeasonCalibration": calibration_baselines[tracker_market],
        }

    weakness_ranking = sorted(
        model_baselines,
        key=lambda key: (
            model_baselines[key]["heldOut"]["brierSkillScore"]
            if model_baselines[key]["heldOut"]["brierSkillScore"] is not None
            else math.inf,
            model_baselines[key]["heldOut"]["auc"]
            if model_baselines[key]["heldOut"]["auc"] is not None
            else math.inf,
            -(model_baselines[key]["heldOut"]["generalizationGap"] or 0),
            key,
        ),
    )
    status = "passed" if not errors else "failed"
    return {
        "version": ACCURACY_BASELINE_VERSION,
        "status": status,
        "sourceMainSha": champion_manifest.get("frozen_from_main_sha"),
        "sourceBlobs": dict(sorted(source_blobs.items())),
        "coverage": {
            "expectedModels": len(expected_models),
            "baselineModels": len(model_baselines),
            "expectedTrackerMarkets": len(expected_tracker_markets),
            "calibrationMarkets": sum(
                value is not None for value in calibration_baselines.values()
            ),
        },
        "modelBaselines": model_baselines,
        "marketBaselines": market_baselines,
        "weaknessRanking": weakness_ranking,
        "observationalMetrics": {
            "clv": {
                "status": "collecting",
                "minimumValidObservations": 500,
                "industryClaimAllowed": False,
                "promotionMetric": False,
            },
            "roi": {
                "status": "descriptive_only",
                "promotionMetric": False,
            },
        },
        "phase5Handoff": {
            "ready": status == "passed",
            "priorityModels": weakness_ranking[:5],
            "promotionContract": {
                "heldOutBrierMustImprove": True,
                "heldOutLoglossMustNotRegress": True,
                "heldOutAucMustNotRegress": True,
                "marketEceMustNotRegress": True,
                "strictTemporalSplitRequired": True,
                "serveParityRequired": True,
                "marketValidationRequired": True,
                "automaticPromotion": False,
            },
        },
        "errors": errors,
    }

