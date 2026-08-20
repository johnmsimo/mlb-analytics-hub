"""Phase 5.4 auditable continuous-learning feedback loop.

Only immutable pregame prediction receipts may enter this learning report.
Outcomes can trigger measurement and human review, but never automatic weight,
threshold, staking, or champion-model changes.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from typing import Any


CONTINUOUS_LEARNING_VERSION = "5.4.0"
SUPPORTED_MARKETS = {
    "batter_hits",
    "batter_home_runs",
    "batter_rbis",
    "batter_total_bases",
    "pitcher_strikeouts",
}
_MARKET_ALIASES = {
    "player_hits": "batter_hits",
    "hits": "batter_hits",
    "home_runs": "batter_home_runs",
    "player_home_runs": "batter_home_runs",
    "rbi": "batter_rbis",
    "player_rbis": "batter_rbis",
    "total_bases": "batter_total_bases",
    "player_total_bases": "batter_total_bases",
    "strikeouts": "pitcher_strikeouts",
    "pitcher_ks": "pitcher_strikeouts",
}
_INVALID_BOOKS = {"", "model", "n/a", "none", "projection", "unknown", "unpriced"}


@dataclass(frozen=True)
class LearningPolicy:
    smart_consensus_sample: int = 40
    blend_learning_sample: int = 60
    calibration_learning_sample: int = 80
    model_review_sample: int = 200
    clv_claim_sample: int = 500
    recent_window_days: int = 30
    baseline_window_days: int = 90
    minimum_drift_window_rows: int = 25
    brier_drift_delta: float = 0.04
    maximum_clock_skew_seconds: int = 300


def _number(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _probability(value: Any) -> float | None:
    result = _number(value)
    if result is None:
        return None
    result = result / 100.0 if result > 1.0 else result
    return result if 0.0 < result < 1.0 else None


def _time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        result = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            result = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _now(value: datetime | None) -> datetime:
    result = value or datetime.now(timezone.utc)
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _first(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None and value != "":
            return value
    return None


def _market(row: Mapping[str, Any]) -> str:
    value = str(_first(row, "canonicalMarketKey", "marketKey", "market") or "")
    key = value.strip().lower().replace("-", "_").replace(" ", "_")
    return _MARKET_ALIASES.get(key, key)


def _side(row: Mapping[str, Any]) -> str | None:
    value = str(_first(row, "canonicalSide", "recommendedSide", "side") or "").lower()
    if value.startswith("over"):
        return "over"
    if value.startswith("under"):
        return "under"
    return None


def _outcome(row: Mapping[str, Any]) -> int | None:
    value = str(_first(row, "grade", "result", "outcome") or "").strip().lower()
    if value in {"win", "won", "w", "hit", "correct", "1", "true"}:
        return 1
    if value in {"loss", "lost", "l", "miss", "incorrect", "0", "false"}:
        return 0
    return None


def _served_probability(row: Mapping[str, Any]) -> float | None:
    probability = _probability(_first(
        row, "blendedProb", "adjProb", "canonicalProbability", "probability",
    ))
    return round(1.0 - probability, 6) if probability is not None and _side(row) == "under" else probability


def _pre_calibration_probability(row: Mapping[str, Any]) -> float | None:
    probability = _probability(_first(row, "preCalProb", "rawMultProb"))
    return round(1.0 - probability, 6) if probability is not None and _side(row) == "under" else probability


def _component_probabilities(row: Mapping[str, Any]) -> dict[str, float]:
    raw = _first(row, "componentProbabilities", "modelProbabilities", "modelProbs")
    if not isinstance(raw, Mapping):
        return {}
    result = {}
    for key, value in raw.items():
        probability = _probability(value)
        if probability is not None:
            if _side(row) == "under":
                probability = 1.0 - probability
            result[str(key)] = round(probability, 6)
    return dict(sorted(result.items()))


def _snapshot(row: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    market = _market(row)
    side = _side(row)
    line = _number(_first(row, "line", "marketLine"))
    saved_at = _time(_first(row, "savedAt", "predictionTimestamp", "generatedAt"))
    served_probability = _served_probability(row)
    pre_cal_probability = _pre_calibration_probability(row)
    opening_implied = _probability(_first(row, "openingImplied", "marketImplied"))
    book = str(_first(row, "book", "bestAvailableBook", "openingBookmaker") or "").strip()
    source = str(_first(row, "source", "modelSource") or "").strip()
    identity = str(_first(row, "id", "canonicalCandidateId") or "").strip()
    model_version = str(_first(row, "modelVersion", "modelArtifactVersion") or "").strip() or None
    components = _component_probabilities(row)
    blockers: list[str] = []
    if not identity:
        blockers.append("missing stable prediction identity")
    if market not in SUPPORTED_MARKETS:
        blockers.append("unsupported canonical market")
    if side is None:
        blockers.append("missing canonical over/under side")
    if line is None:
        blockers.append("missing exact market line")
    if served_probability is None:
        blockers.append("missing served probability")
    if saved_at is None:
        blockers.append("missing prediction timestamp")
    if not source:
        blockers.append("missing prediction source")
    snapshot = {
        "identity": identity or None,
        "gamePk": _first(row, "gamePk", "game_pk"),
        "marketKey": market or None,
        "side": side,
        "line": line,
        "servedProbability": served_probability,
        "preCalibrationProbability": pre_cal_probability,
        "openingImplied": opening_implied,
        "book": book or None,
        "source": source or None,
        "modelVersion": model_version,
        "componentProbabilities": components,
        "savedAt": saved_at.isoformat() if saved_at else None,
    }
    return snapshot, blockers


def build_prediction_receipt(row: Mapping[str, Any]) -> dict[str, Any]:
    """Freeze the pre-outcome fields used by every later learning decision."""
    snapshot, blockers = _snapshot(row)
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8")
    fingerprint = hashlib.sha256(encoded).hexdigest()
    measurement_eligible = not blockers
    pre_cal_eligible = measurement_eligible and snapshot["preCalibrationProbability"] is not None
    consensus_eligible = measurement_eligible and len(snapshot["componentProbabilities"]) >= 2
    blend_eligible = (
        pre_cal_eligible
        and snapshot["openingImplied"] is not None
        and str(snapshot["book"] or "").strip().lower() not in _INVALID_BOOKS
    )
    return {
        "version": CONTINUOUS_LEARNING_VERSION,
        "predictionFingerprint": fingerprint,
        "snapshot": snapshot,
        "measurementEligible": measurement_eligible,
        "smartConsensusEligible": consensus_eligible,
        "probabilityAdaptationEligible": pre_cal_eligible,
        "marketBlendEligible": blend_eligible,
        "blockers": blockers,
        "outcomeFieldsIncluded": False,
        "automaticAdaptation": False,
    }


def validate_observation(
    row: Mapping[str, Any],
    *,
    now: datetime | None = None,
    policy: LearningPolicy | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Validate one graded row against its immutable prediction receipt."""
    policy = policy or LearningPolicy()
    checked_at = _now(now)
    reasons: list[str] = []
    receipt = row.get("learningReceipt")
    if not isinstance(receipt, Mapping):
        return None, ["missing Phase 5.4 prediction receipt"]
    if receipt.get("version") != CONTINUOUS_LEARNING_VERSION:
        reasons.append("prediction receipt version mismatch")
    expected = build_prediction_receipt(row)
    if receipt.get("predictionFingerprint") != expected["predictionFingerprint"]:
        reasons.append("prediction receipt fingerprint mismatch")
    if receipt.get("snapshot") != expected["snapshot"]:
        reasons.append("prediction receipt snapshot mismatch")
    if receipt.get("measurementEligible") is not True:
        reasons.extend(str(value) for value in receipt.get("blockers", []) if value)
    if row.get("backfilled") is True:
        reasons.append("lookahead or backfilled outcome is excluded")
    outcome = _outcome(row)
    if outcome is None:
        reasons.append("observation is not a graded win or loss")
    saved_at = _time(expected["snapshot"].get("savedAt"))
    graded_at = _time(_first(row, "gradedAt", "outcomeCapturedAt"))
    if graded_at is None:
        reasons.append("missing outcome timestamp")
    elif saved_at is not None and graded_at <= saved_at:
        reasons.append("outcome timestamp is not after prediction")
    elif graded_at > checked_at + timedelta(seconds=policy.maximum_clock_skew_seconds):
        reasons.append("outcome timestamp is in the future")
    reasons = list(dict.fromkeys(reasons))
    if reasons:
        return None, reasons
    lineage = row.get("oddsLineage") if isinstance(row.get("oddsLineage"), Mapping) else {}
    closing_implied = _probability(_first(row, "closingImplied"))
    clv_eligible = bool(
        closing_implied is not None
        and (row.get("clvEligible") is True or lineage.get("clvEligible") is True)
    )
    return {
        "predictionFingerprint": receipt["predictionFingerprint"],
        "identity": expected["snapshot"]["identity"],
        "marketKey": expected["snapshot"]["marketKey"],
        "side": expected["snapshot"]["side"],
        "line": expected["snapshot"]["line"],
        "savedAt": expected["snapshot"]["savedAt"],
        "gradedAt": graded_at.isoformat(),
        "servedProbability": expected["snapshot"]["servedProbability"],
        "preCalibrationProbability": expected["snapshot"]["preCalibrationProbability"],
        "openingImplied": expected["snapshot"]["openingImplied"],
        "closingImplied": closing_implied,
        "outcome": outcome,
        "probabilityAdaptationEligible": receipt.get("probabilityAdaptationEligible") is True,
        "smartConsensusEligible": receipt.get("smartConsensusEligible") is True,
        "marketBlendEligible": receipt.get("marketBlendEligible") is True,
        "modelAttributed": bool(expected["snapshot"].get("modelVersion")),
        "clvEligible": clv_eligible,
    }, []


def _metrics(rows: list[Mapping[str, Any]], probability_key: str = "servedProbability") -> dict[str, Any]:
    pairs = [
        (float(row[probability_key]), int(row["outcome"]))
        for row in rows if row.get(probability_key) is not None
    ]
    if not pairs:
        return {"count": 0, "wins": 0, "winRate": None, "meanProbability": None,
                "brierScore": None, "logLoss": None, "ece": None}
    count = len(pairs)
    wins = sum(outcome for _, outcome in pairs)
    mean_probability = sum(probability for probability, _ in pairs) / count
    brier = sum((probability - outcome) ** 2 for probability, outcome in pairs) / count
    logloss = -sum(
        outcome * math.log(max(1e-6, probability))
        + (1 - outcome) * math.log(max(1e-6, 1 - probability))
        for probability, outcome in pairs
    ) / count
    bins: dict[int, list[tuple[float, int]]] = defaultdict(list)
    for probability, outcome in pairs:
        bins[min(9, int(probability * 10))].append((probability, outcome))
    ece = sum(
        len(values) / count * abs(
            sum(probability for probability, _ in values) / len(values)
            - sum(outcome for _, outcome in values) / len(values)
        ) for values in bins.values()
    )
    return {
        "count": count,
        "wins": wins,
        "winRate": round(wins / count, 6),
        "meanProbability": round(mean_probability, 6),
        "brierScore": round(brier, 6),
        "logLoss": round(logloss, 6),
        "ece": round(ece, 6),
    }


def build_continuous_learning_report(
    entries: Iterable[Mapping[str, Any]],
    *,
    now: datetime | None = None,
    policy: LearningPolicy | None = None,
) -> dict[str, Any]:
    """Measure trusted outcomes and expose review gates without self-modifying."""
    policy = policy or LearningPolicy()
    checked_at = _now(now)
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate(entries or ()):
        observation, reasons = validate_observation(row, now=checked_at, policy=policy)
        if observation is None:
            rejected.append({"index": index, "id": row.get("id"), "reasons": reasons})
            continue
        fingerprint = observation["predictionFingerprint"]
        if fingerprint in seen:
            rejected.append({"index": index, "id": row.get("id"), "reasons": ["duplicate prediction receipt"]})
            continue
        seen.add(fingerprint)
        accepted.append(observation)

    by_market: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for observation in accepted:
        by_market[observation["marketKey"]].append(observation)
    recent_cutoff = checked_at - timedelta(days=policy.recent_window_days)
    baseline_cutoff = checked_at - timedelta(days=policy.baseline_window_days)
    market_reports: dict[str, Any] = {}
    review_queue = []
    for market in sorted(by_market):
        rows = by_market[market]
        recent = [row for row in rows if _time(row["gradedAt"]) >= recent_cutoff]
        baseline = [row for row in rows if _time(row["gradedAt"]) >= baseline_cutoff]
        overall_metrics = _metrics(rows)
        recent_metrics = _metrics(recent)
        baseline_metrics = _metrics(baseline)
        pre_cal_rows = [row for row in rows if row["probabilityAdaptationEligible"]]
        consensus_rows = [row for row in rows if row["smartConsensusEligible"]]
        blend_rows = [row for row in rows if row["marketBlendEligible"]]
        model_rows = [row for row in rows if row["modelAttributed"]]
        clv_rows = [row for row in rows if row["clvEligible"]]
        drift_delta = None
        drift_status = "insufficient_sample"
        if (
            recent_metrics["count"] >= policy.minimum_drift_window_rows
            and baseline_metrics["count"] > recent_metrics["count"]
            and recent_metrics["brierScore"] is not None
            and baseline_metrics["brierScore"] is not None
        ):
            drift_delta = recent_metrics["brierScore"] - baseline_metrics["brierScore"]
            drift_status = "review" if drift_delta > policy.brier_drift_delta else "stable"
        readiness = {
            "smartConsensusReviewReady": len(consensus_rows) >= policy.smart_consensus_sample,
            "blendLearningReviewReady": len(blend_rows) >= policy.blend_learning_sample,
            "calibrationLearningReviewReady": len(pre_cal_rows) >= policy.calibration_learning_sample,
            "modelRetrainingReviewReady": len(model_rows) >= policy.model_review_sample,
            "industryClvClaimReady": len(clv_rows) >= policy.clv_claim_sample,
        }
        reasons = []
        if drift_status == "review":
            reasons.append("recent Brier degradation exceeds the review threshold")
        if readiness["modelRetrainingReviewReady"]:
            reasons.append("trusted sample is ready for a shadow retraining review")
        if reasons:
            review_queue.append({"marketKey": market, "reasons": reasons})
        market_reports[market] = {
            "metrics": overall_metrics,
            "preCalibrationMetrics": _metrics(pre_cal_rows, "preCalibrationProbability"),
            "recentWindow": {"days": policy.recent_window_days, "metrics": recent_metrics},
            "baselineWindow": {"days": policy.baseline_window_days, "metrics": baseline_metrics},
            "drift": {
                "status": drift_status,
                "brierDelta": round(drift_delta, 6) if drift_delta is not None else None,
                "threshold": policy.brier_drift_delta,
            },
            "eligibleCounts": {
                "measurement": len(rows),
                "smartConsensus": len(consensus_rows),
                "probabilityAdaptation": len(pre_cal_rows),
                "marketBlend": len(blend_rows),
                "modelAttributed": len(model_rows),
                "verifiedClv": len(clv_rows),
            },
            "layerReadiness": readiness,
        }
    reason_counts = Counter(
        reason for item in rejected for reason in item["reasons"]
    )
    return {
        "version": CONTINUOUS_LEARNING_VERSION,
        "mode": "measurement_and_review_only",
        "checkedAt": checked_at.isoformat(),
        "acceptedObservationCount": len(accepted),
        "rejectedObservationCount": len(rejected),
        "rejectedReasonCounts": dict(sorted(reason_counts.items())),
        "rejectedObservations": rejected,
        "overall": _metrics(accepted),
        "markets": market_reports,
        "reviewQueue": review_queue,
        "safety": {
            "automaticProbabilityAdaptation": False,
            "automaticWeightChange": False,
            "automaticThresholdChange": False,
            "automaticStakingChange": False,
            "automaticModelPromotion": False,
            "humanReviewRequired": True,
        },
    }
