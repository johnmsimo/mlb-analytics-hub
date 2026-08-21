"""Phase 6.1-6.5 read-only accuracy and intelligence control plane.

Every analysis in this module is derived from graded observations whose Phase
5.4 prediction receipt, Phase 6.0 closing benchmark receipt, and Phase 6.5
intelligence evidence receipt all verify.  The public contract exposes bounded
aggregates only and never promotes a model or changes a policy automatically.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from datetime import date, datetime, time, timedelta, timezone
from itertools import combinations
import hashlib
import json
import math
import os
from typing import Any

from accuracy_control_plane import closing_benchmark_is_intact
from continuous_learning import build_prediction_receipt, validate_observation


INTELLIGENCE_CONTROL_PLANE_VERSION = "6.5"
INTELLIGENCE_EVIDENCE_VERSION = "6.5.0"
ERROR_ATLAS_VERSION = "6.1"
CHAMPION_CHALLENGER_VERSION = "6.2"
DRIFT_CONTROL_VERSION = "6.3"
SIMULATION_CALIBRATION_VERSION = "6.4"
POLICY_LAB_VERSION = "6.5"

DEFAULT_WINDOW_DAYS = 120
MAX_WINDOW_DAYS = 366
MIN_CONTEXT_SAMPLE = 30
MIN_CONTEXT_CLAIM_SAMPLE = 100
MIN_CHALLENGER_TOTAL = 300
MIN_CHALLENGER_HOLDOUT = 100
MIN_DRIFT_RECENT = 30
MIN_DRIFT_BASELINE = 100
MIN_SIMULATION_SAMPLE = 100
MIN_CORRELATION_PAIRS = 50
MIN_POLICY_REFERENCE = 150
MIN_POLICY_HOLDOUT = 75
CURRENT_MINIMUM_EDGE = 0.03
CURRENT_MINIMUM_EXPECTED_VALUE = 0.03
EDGE_GRID = (0.0, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10)
EXPECTED_VALUE_GRID = (0.0, 0.02, 0.03, 0.04, 0.06, 0.08, 0.10, 0.15)

SUPPORTED_MARKETS = frozenset({
    "batter_hits",
    "batter_home_runs",
    "batter_rbis",
    "batter_total_bases",
    "pitcher_strikeouts",
})
CONTEXT_DIMENSIONS = (
    "market",
    "side",
    "lineBand",
    "sportsbook",
    "lineupStatus",
    "pitcherHand",
    "parkBand",
    "weatherBand",
    "umpireBand",
    "modelVersion",
    "confidenceTier",
    "freshnessBand",
    "providerState",
)

DRIFT_FEATURE_DIMENSIONS = (
    "lineBand",
    "sportsbook",
    "lineupStatus",
    "pitcherHand",
    "parkBand",
    "weatherBand",
    "umpireBand",
    "confidenceTier",
    "freshnessBand",
    "providerState",
)


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
    if result > 1:
        result /= 100.0
    return result if 0 < result < 1 else None


def _time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _first(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None and value != "":
            return value
    return None


def _side(row: Mapping[str, Any]) -> str | None:
    value = str(_first(row, "canonicalSide", "recommendedSide", "side") or "").lower()
    if value.startswith("over"):
        return "over"
    if value.startswith("under"):
        return "under"
    return None


def _safe_label(value: Any, *, default: str = "unknown", maximum: int = 48) -> str:
    text = str(value or "").strip().lower().replace(" ", "_")
    allowed = "abcdefghijklmnopqrstuvwxyz0123456789._:-"
    cleaned = "".join(character for character in text if character in allowed)
    return cleaned[:maximum] or default


def _side_probability(value: Any, side: str | None) -> float | None:
    probability = _probability(value)
    if probability is None:
        return None
    return round(1 - probability, 6) if side == "under" else round(probability, 6)


def _line_band(value: Any) -> str:
    line = _number(value)
    if line is None:
        return "unknown"
    if line < 1:
        return "under_1"
    if line < 2:
        return "1_to_1.5"
    if line < 4:
        return "2_to_3.5"
    if line < 7:
        return "4_to_6.5"
    return "7_plus"


def _lineup_status(row: Mapping[str, Any]) -> str:
    explicit = str(_first(row, "lineupStatus", "lineup_status") or "").lower()
    if "confirm" in explicit:
        return "confirmed"
    if "project" in explicit or "expected" in explicit:
        return "projected"
    flags = [
        row.get("lineupConfirmed"),
        row.get("awayLineupConfirmed"),
        row.get("homeLineupConfirmed"),
    ]
    if any(value is True for value in flags):
        return "confirmed"
    if any(value is False for value in flags):
        return "projected"
    return "unknown"


def _park_band(row: Mapping[str, Any]) -> str:
    factor = _number(_first(row, "parkFactor", "park_factor"))
    if factor is None:
        return "unknown"
    if factor >= 1.05:
        return "hitter_friendly"
    if factor <= 0.95:
        return "pitcher_friendly"
    return "neutral"


def _weather_band(row: Mapping[str, Any]) -> str:
    weather = row.get("weather") if isinstance(row.get("weather"), Mapping) else {}
    if weather.get("dome") is True or str(weather.get("condition") or "").lower() == "dome":
        return "indoor"
    impact = row.get("weatherImpact") if isinstance(row.get("weatherImpact"), Mapping) else {}
    score = _number(_first(impact, "total", "score", "runDelta"))
    if score is None:
        score = _number(_first(row, "weatherAdjustment", "wxAdj"))
    if score is None:
        return "unknown"
    if score >= 0.15:
        return "hitter_friendly"
    if score <= -0.15:
        return "pitcher_friendly"
    return "neutral"


def _umpire_band(row: Mapping[str, Any]) -> str:
    multiplier = _number(_first(row, "umpireKMult", "umpire_k_mult"))
    if multiplier is None:
        context = row.get("gameContext") if isinstance(row.get("gameContext"), Mapping) else {}
        multiplier = _number(context.get("umpireKMult"))
    if multiplier is None:
        return "unknown"
    if multiplier >= 1.03:
        return "high_strikeout"
    if multiplier <= 0.97:
        return "low_strikeout"
    return "neutral"


def _freshness_band(row: Mapping[str, Any]) -> str:
    age = _number(_first(row, "quoteAgeSeconds", "oddsAgeSeconds"))
    if age is None:
        saved_at = _time(_first(row, "savedAt", "predictionTimestamp", "generatedAt"))
        observed_at = _time(_first(
            row, "oddsObservedAt", "openingCapturedAt", "quoteCapturedAt",
        ))
        if saved_at is not None and observed_at is not None:
            age = max(0.0, (saved_at - observed_at).total_seconds())
    lineage = row.get("oddsLineage") if isinstance(row.get("oddsLineage"), Mapping) else {}
    if age is None:
        current = lineage.get("currentFreshness") if isinstance(lineage.get("currentFreshness"), Mapping) else {}
        age = _number(current.get("ageSeconds"))
    if age is None:
        return "unknown"
    if age <= 60:
        return "under_1m"
    if age <= 300:
        return "1_to_5m"
    if age <= 900:
        return "5_to_15m"
    return "stale"


def _provider_state(row: Mapping[str, Any]) -> str:
    state = _first(row, "oddsProviderState", "providerState")
    health = row.get("oddsProviderHealth")
    if state is None and isinstance(health, Mapping):
        state = health.get("state")
    shopping = row.get("multiBookShopping")
    if state is None and isinstance(shopping, Mapping):
        provider = shopping.get("providerHealth")
        if isinstance(provider, Mapping):
            state = provider.get("state")
    normalized = _safe_label(state)
    return normalized if normalized in {
        "ready", "computing", "partial", "stale", "failed", "unavailable",
    } else "unknown"


def _context_snapshot(row: Mapping[str, Any], prediction: Mapping[str, Any]) -> dict[str, str]:
    tier = _safe_label(_first(row, "confidenceTier", "recommendationGrade"))
    if tier not in {"a", "b", "c", "strong_play", "value_play", "lean", "pass"}:
        tier = "other" if tier != "unknown" else tier
    hand = str(_first(row, "pitcherHand", "pitcher_hand", "throws") or "").upper()
    hand = hand if hand in {"L", "R"} else "unknown"
    return {
        "market": _safe_label(prediction.get("marketKey")),
        "side": _safe_label(prediction.get("side")),
        "lineBand": _line_band(prediction.get("line")),
        "sportsbook": _safe_label(prediction.get("book")),
        "lineupStatus": _lineup_status(row),
        "pitcherHand": hand.lower(),
        "parkBand": _park_band(row),
        "weatherBand": _weather_band(row),
        "umpireBand": _umpire_band(row),
        "modelVersion": _safe_label(prediction.get("modelVersion")),
        "confidenceTier": tier,
        "freshnessBand": _freshness_band(row),
        "providerState": _provider_state(row),
    }


def _challenger_snapshot(row: Mapping[str, Any], prediction: Mapping[str, Any]) -> dict[str, float]:
    side = prediction.get("side")
    challengers: dict[str, float] = {}
    pre_calibration = _probability(prediction.get("preCalibrationProbability"))
    if pre_calibration is not None:
        challengers["pre_calibration"] = round(pre_calibration, 6)
    components = prediction.get("componentProbabilities")
    if isinstance(components, Mapping):
        for name, value in sorted(components.items()):
            probability = _probability(value)
            if probability is not None:
                challengers["component:" + _safe_label(name)] = round(probability, 6)
    simulation_probability = _first(
        row, "gameSimProbability", "mc_prob_over", "simulationProbability",
    )
    simulation = _side_probability(simulation_probability, side)
    if simulation is not None:
        challengers["simulation"] = simulation
    return challengers


def _simulation_snapshot(row: Mapping[str, Any], side: str | None) -> dict[str, Any] | None:
    base_probability = _probability(_first(
        row, "gameSimProbability", "mc_prob_over", "simulationProbability",
    ))
    trials = _number(_first(row, "gameSimN", "mc_n_sims", "simulationSampleSize"))
    if base_probability is None or trials is None or trials <= 0:
        return None
    probability = 1 - base_probability if side == "under" else base_probability
    probability_low = _probability(_first(row, "gameSimPlo", "simulationPlo"))
    probability_high = _probability(_first(row, "gameSimPhi", "simulationPhi"))
    if side == "under" and probability_low is not None and probability_high is not None:
        probability_low, probability_high = 1 - probability_high, 1 - probability_low
    return {
        "version": _safe_label(_first(row, "matchupSimulationVersion", "simulationVersion")),
        "mode": _safe_label(_first(row, "matchupSimulationMode", "simulationMode")),
        "probability": round(probability, 6),
        "probabilityLow": round(probability_low, 6) if probability_low is not None else None,
        "probabilityHigh": round(probability_high, 6) if probability_high is not None else None,
        "trials": int(trials),
        "mean": _number(_first(row, "gameSimMean", "mc_mean", "simulationMean")),
        "p10": _number(_first(row, "mc_p10", "simulationP10")),
        "p90": _number(_first(row, "mc_p90", "simulationP90")),
    }


def _decision_snapshot(row: Mapping[str, Any], prediction: Mapping[str, Any]) -> dict[str, Any]:
    price = _number(_first(row, "openingPrice", "canonicalPrice", "bestAvailablePrice"))
    return {
        "edge": _number(_first(row, "canonicalEdge", "edge", "edgePct")),
        "expectedValue": _number(_first(row, "canonicalEv", "evPct", "expectedValue")),
        "openingPrice": int(round(price)) if price is not None else None,
        "openingImplied": _probability(prediction.get("openingImplied")),
        "hubRating": _number(row.get("hubRating")),
        "decisionClass": _safe_label(_first(
            row, "recommendationClass", "recommendationGrade", "confidenceTier",
        )),
    }


def build_intelligence_evidence_receipt(row: Mapping[str, Any]) -> dict[str, Any]:
    """Freeze all Phase 6.1-6.5 inputs before the outcome is available."""

    prediction_receipt = row.get("learningReceipt")
    prediction = (
        prediction_receipt.get("snapshot")
        if isinstance(prediction_receipt, Mapping)
        and isinstance(prediction_receipt.get("snapshot"), Mapping)
        else {}
    )
    side = prediction.get("side") or _side(row)
    blockers: list[str] = []
    expected_prediction = build_prediction_receipt(row)
    if not isinstance(prediction_receipt, Mapping):
        blockers.append("missing_prediction_receipt")
    elif (
        prediction_receipt.get("predictionFingerprint")
        != expected_prediction.get("predictionFingerprint")
        or prediction_receipt.get("snapshot") != expected_prediction.get("snapshot")
        or prediction_receipt.get("measurementEligible") is not True
    ):
        blockers.append("prediction_receipt_not_intact")
    if str(prediction.get("marketKey") or "") not in SUPPORTED_MARKETS:
        blockers.append("unsupported_market")
    snapshot = {
        "predictionFingerprint": (
            prediction_receipt.get("predictionFingerprint")
            if isinstance(prediction_receipt, Mapping) else None
        ),
        "identity": prediction.get("identity") or _first(row, "id", "canonicalCandidateId"),
        "gamePk": prediction.get("gamePk") or _first(row, "gamePk", "game_pk"),
        "savedAt": prediction.get("savedAt"),
        "context": _context_snapshot(row, prediction),
        "challengers": _challenger_snapshot(row, prediction),
        "simulation": _simulation_snapshot(row, side),
        "decision": _decision_snapshot(row, prediction),
    }
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "version": INTELLIGENCE_EVIDENCE_VERSION,
        "evidenceFingerprint": hashlib.sha256(encoded).hexdigest(),
        "snapshot": snapshot,
        "accepted": not blockers,
        "blockers": blockers,
        "outcomeFieldsIncluded": False,
        "closingFieldsIncluded": False,
        "automaticPolicyChange": False,
        "failClosed": True,
    }


def intelligence_evidence_is_intact(row: Mapping[str, Any]) -> bool:
    receipt = row.get("intelligenceEvidenceReceipt")
    if not isinstance(receipt, Mapping):
        return False
    expected = build_intelligence_evidence_receipt(row)
    return bool(
        receipt.get("version") == INTELLIGENCE_EVIDENCE_VERSION
        and receipt.get("evidenceFingerprint") == expected["evidenceFingerprint"]
        and receipt.get("snapshot") == expected["snapshot"]
        and receipt.get("accepted") is True
        and receipt.get("outcomeFieldsIncluded") is False
        and receipt.get("closingFieldsIncluded") is False
        and receipt.get("automaticPolicyChange") is False
        and receipt.get("failClosed") is True
        and expected["accepted"] is True
    )


def _ece(rows: list[Mapping[str, Any]], key: str) -> float | None:
    pairs = [
        (_probability(row.get(key)), int(row["outcome"]))
        for row in rows
    ]
    pairs = [(probability, outcome) for probability, outcome in pairs if probability is not None]
    if not pairs:
        return None
    bins: dict[int, list[tuple[float, int]]] = defaultdict(list)
    for probability, outcome in pairs:
        bins[min(9, int(probability * 10))].append((probability, outcome))
    count = len(pairs)
    result = sum(
        len(values) / count * abs(
            sum(probability for probability, _ in values) / len(values)
            - sum(outcome for _, outcome in values) / len(values)
        )
        for values in bins.values()
    )
    return round(result, 6)


def _brier(rows: list[Mapping[str, Any]], key: str) -> float | None:
    values = [
        (probability - int(row["outcome"])) ** 2
        for row in rows
        if (probability := _probability(row.get(key))) is not None
    ]
    return round(sum(values) / len(values), 6) if values else None


def _mean(values: Iterable[Any]) -> float | None:
    numbers = [number for value in values if (number := _number(value)) is not None]
    return sum(numbers) / len(numbers) if numbers else None


def _mean_interval(values: Iterable[Any]) -> dict[str, float | None]:
    numbers = [number for value in values if (number := _number(value)) is not None]
    if not numbers:
        return {"lower": None, "upper": None, "confidenceLevel": 0.95}
    average = sum(numbers) / len(numbers)
    if len(numbers) < 2:
        return {
            "lower": round(average, 6),
            "upper": round(average, 6),
            "confidenceLevel": 0.95,
        }
    variance = sum((number - average) ** 2 for number in numbers) / (len(numbers) - 1)
    margin = 1.96 * math.sqrt(variance / len(numbers))
    return {
        "lower": round(average - margin, 6),
        "upper": round(average + margin, 6),
        "confidenceLevel": 0.95,
    }


def _wilson(successes: int, count: int) -> dict[str, float | None]:
    if count <= 0:
        return {"lower": None, "upper": None, "confidenceLevel": 0.95}
    z = 1.96
    rate = successes / count
    denominator = 1 + z * z / count
    center = (rate + z * z / (2 * count)) / denominator
    margin = z * math.sqrt(
        rate * (1 - rate) / count + z * z / (4 * count * count)
    ) / denominator
    return {
        "lower": round(max(0.0, center - margin), 6),
        "upper": round(min(1.0, center + margin), 6),
        "confidenceLevel": 0.95,
    }


def _paired_error_differences(
    rows: list[Mapping[str, Any]], left: str, right: str,
) -> list[float]:
    differences = []
    for row in rows:
        left_probability = _probability(row.get(left))
        right_probability = _probability(row.get(right))
        if left_probability is None or right_probability is None:
            continue
        outcome = int(row["outcome"])
        differences.append(
            (left_probability - outcome) ** 2
            - (right_probability - outcome) ** 2
        )
    return differences


def _profit_units(outcome: int, price: Any) -> float | None:
    american = _number(price)
    if american is None or american == 0 or abs(american) < 100:
        return None
    if not outcome:
        return -1.0
    return american / 100 if american > 0 else 100 / abs(american)


def _maximum_drawdown(values: Iterable[float]) -> float:
    total = 0.0
    peak = 0.0
    drawdown = 0.0
    for value in values:
        total += value
        peak = max(peak, total)
        drawdown = max(drawdown, peak - total)
    return round(drawdown, 6)


def _public_rejection_codes(reasons: Iterable[Any]) -> set[str]:
    codes = set()
    for value in reasons:
        reason = str(value or "").lower()
        if "missing phase 5.4" in reason:
            codes.add("prediction_receipt_missing")
        elif "receipt" in reason and "mismatch" in reason:
            codes.add("prediction_receipt_tampered")
        elif "backfilled" in reason or "lookahead" in reason:
            codes.add("backfilled")
        elif "graded" in reason:
            codes.add("ungraded")
        elif "timestamp" in reason or "future" in reason:
            codes.add("invalid_timing")
        else:
            codes.add("prediction_ineligible")
    return codes or {"prediction_ineligible"}


def _verified_observations(
    entries: Iterable[Mapping[str, Any]],
    *,
    checked_at: datetime,
    cutoff: date,
    anchor: date,
) -> tuple[list[dict[str, Any]], int, Counter[str]]:
    accepted: list[dict[str, Any]] = []
    rejected_rows = 0
    rejected: Counter[str] = Counter()
    seen: set[str] = set()
    for row in entries or ():
        if not isinstance(row, Mapping):
            rejected_rows += 1
            rejected["malformed_row"] += 1
            continue
        observation, reasons = validate_observation(row, now=checked_at)
        if observation is None:
            rejected_rows += 1
            for reason in _public_rejection_codes(reasons):
                rejected[reason] += 1
            continue
        graded_at = _time(observation.get("gradedAt"))
        if graded_at is None or not cutoff <= graded_at.date() <= anchor:
            rejected_rows += 1
            rejected["outside_window"] += 1
            continue
        fingerprint = str(observation.get("predictionFingerprint") or "")
        if fingerprint in seen:
            rejected_rows += 1
            rejected["duplicate_prediction_receipt"] += 1
            continue
        seen.add(fingerprint)
        if not closing_benchmark_is_intact(row):
            rejected_rows += 1
            rejected["closing_receipt_missing_or_invalid"] += 1
            continue
        if not intelligence_evidence_is_intact(row):
            rejected_rows += 1
            rejected["intelligence_receipt_missing_or_invalid"] += 1
            continue
        evidence = row["intelligenceEvidenceReceipt"]["snapshot"]
        closing = row["closingBenchmarkReceipt"]["snapshot"]
        market_probability = _probability(closing.get("selectedFairProbability"))
        model_probability = _probability(observation.get("servedProbability"))
        if model_probability is None or market_probability is None:
            rejected_rows += 1
            rejected["paired_probability_missing"] += 1
            continue
        actual = _number(row.get("actual"))
        clv_edge = _number(row.get("clvEdge"))
        decision = evidence.get("decision") if isinstance(evidence.get("decision"), Mapping) else {}
        accepted.append({
            "fingerprint": fingerprint,
            "identity": observation.get("identity"),
            "gamePk": evidence.get("gamePk"),
            "market": observation.get("marketKey"),
            "side": observation.get("side"),
            "line": observation.get("line"),
            "savedAt": _time(observation.get("savedAt")),
            "gradedAt": graded_at,
            "outcome": int(observation["outcome"]),
            "actual": actual,
            "modelProbability": model_probability,
            "marketProbability": market_probability,
            "context": dict(evidence.get("context") or {}),
            "challengers": dict(evidence.get("challengers") or {}),
            "simulation": dict(evidence.get("simulation") or {}) if evidence.get("simulation") else None,
            "decision": dict(decision),
            "clvEdge": clv_edge,
            "clvEligible": bool(
                clv_edge is not None
                and (row.get("clvEligible") is True or (
                    isinstance(row.get("oddsLineage"), Mapping)
                    and row["oddsLineage"].get("clvEligible") is True
                ))
            ),
        })
    return accepted, rejected_rows, rejected


def _error_atlas(rows: list[dict[str, Any]]) -> dict[str, Any]:
    cohorts: list[dict[str, Any]] = []
    suppressed = 0
    for dimension in CONTEXT_DIMENSIONS:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            value = str((row.get("context") or {}).get(dimension) or "unknown")
            grouped[value].append(row)
        for value, values in sorted(grouped.items()):
            if len(values) < MIN_CONTEXT_SAMPLE:
                suppressed += 1
                continue
            differences = _paired_error_differences(
                values, "modelProbability", "marketProbability",
            )
            interval = _mean_interval(differences)
            delta = _mean(differences)
            if len(values) < MIN_CONTEXT_CLAIM_SAMPLE:
                state = "exploratory"
            elif interval["lower"] is not None and interval["lower"] > 0:
                state = "model_worse"
            elif interval["upper"] is not None and interval["upper"] < 0:
                state = "model_better"
            else:
                state = "inconclusive"
            cohorts.append({
                "dimension": dimension,
                "cohort": value,
                "sampleSize": len(values),
                "state": state,
                "modelBrier": _brier(values, "modelProbability"),
                "closingMarketBrier": _brier(values, "marketProbability"),
                "pairedBrierDelta": round(delta, 6) if delta is not None else None,
                "pairedBrierDeltaInterval": interval,
                "modelEce": _ece(values, "modelProbability"),
                "closingMarketEce": _ece(values, "marketProbability"),
                "realizedRate": round(sum(row["outcome"] for row in values) / len(values), 6),
            })
    cohorts.sort(key=lambda item: (
        item["state"] != "model_worse",
        -(item.get("pairedBrierDelta") or -1),
        -item["sampleSize"],
        item["dimension"],
        item["cohort"],
    ))
    risks = [
        {
            "dimension": item["dimension"],
            "cohort": item["cohort"],
            "sampleSize": item["sampleSize"],
            "pairedBrierDelta": item["pairedBrierDelta"],
        }
        for item in cohorts if item["state"] == "model_worse"
    ][:10]
    return {
        "version": ERROR_ATLAS_VERSION,
        "state": "ready" if cohorts else "insufficient_sample",
        "minimumVisibleSample": MIN_CONTEXT_SAMPLE,
        "minimumDirectionalSample": MIN_CONTEXT_CLAIM_SAMPLE,
        "cohorts": cohorts[:120],
        "principalRisks": risks,
        "suppressedCohortCount": suppressed,
        "rawRowsIncluded": False,
        "diagnosisOnly": True,
    }


def _challenger_metrics(
    rows: list[dict[str, Any]], challenger: str,
) -> dict[str, Any]:
    prepared = []
    for row in rows:
        probability = _probability((row.get("challengers") or {}).get(challenger))
        if probability is None:
            continue
        prepared.append({**row, "challengerProbability": probability})
    prepared.sort(key=lambda row: (row["gradedAt"], row["fingerprint"]))
    split = max(1, int(len(prepared) * 0.70)) if prepared else 0
    reference = prepared[:split]
    holdout = prepared[split:]
    versus_champion = _paired_error_differences(
        holdout, "challengerProbability", "modelProbability",
    )
    versus_close = _paired_error_differences(
        holdout, "challengerProbability", "marketProbability",
    )
    champion_interval = _mean_interval(versus_champion)
    close_interval = _mean_interval(versus_close)
    challenger_ece = _ece(holdout, "challengerProbability")
    champion_ece = _ece(holdout, "modelProbability")
    total_ready = len(prepared) >= MIN_CHALLENGER_TOTAL
    holdout_ready = len(holdout) >= MIN_CHALLENGER_HOLDOUT
    if not total_ready or not holdout_ready:
        state = "insufficient_sample"
    elif (
        champion_interval["upper"] is not None
        and champion_interval["upper"] < 0
        and close_interval["upper"] is not None
        and close_interval["upper"] < 0
        and challenger_ece is not None
        and champion_ece is not None
        and challenger_ece <= champion_ece + 0.01
    ):
        state = "review_candidate"
    elif champion_interval["lower"] is not None and champion_interval["lower"] > 0:
        state = "regressed"
    else:
        state = "inconclusive"
    return {
        "challenger": challenger,
        "state": state,
        "totalSampleSize": len(prepared),
        "referenceSampleSize": len(reference),
        "holdoutSampleSize": len(holdout),
        "championBrier": _brier(holdout, "modelProbability"),
        "challengerBrier": _brier(holdout, "challengerProbability"),
        "closingMarketBrier": _brier(holdout, "marketProbability"),
        "challengerMinusChampion": round(_mean(versus_champion), 6) if versus_champion else None,
        "challengerMinusChampionInterval": champion_interval,
        "challengerMinusClose": round(_mean(versus_close), 6) if versus_close else None,
        "challengerMinusCloseInterval": close_interval,
        "championEce": champion_ece,
        "challengerEce": challenger_ece,
        "temporalHoldout": True,
        "promotionEligible": state == "review_candidate",
        "humanReviewRequired": True,
    }


def _champion_challenger(rows: list[dict[str, Any]]) -> dict[str, Any]:
    names = sorted({
        name for row in rows for name in (row.get("challengers") or {})
    })
    reports = [_challenger_metrics(rows, name) for name in names]
    reports.sort(key=lambda report: (
        report["state"] != "review_candidate",
        report.get("challengerMinusChampion") is None,
        report.get("challengerMinusChampion") or 0,
        report["challenger"],
    ))
    return {
        "version": CHAMPION_CHALLENGER_VERSION,
        "state": (
            "review_candidate" if any(report["promotionEligible"] for report in reports)
            else "ready" if reports else "insufficient_sample"
        ),
        "minimumTotalSample": MIN_CHALLENGER_TOTAL,
        "minimumHoldoutSample": MIN_CHALLENGER_HOLDOUT,
        "challengers": reports,
        "automaticPromotion": False,
        "serveParityRequired": True,
        "temporalHoldoutRequired": True,
        "marketBaselineRequired": True,
        "humanReviewRequired": True,
    }


def _metrics_for_drift(rows: list[dict[str, Any]]) -> dict[str, Any]:
    paired = _paired_error_differences(rows, "modelProbability", "marketProbability")
    clv = [row["clvEdge"] for row in rows if row.get("clvEligible")]
    beat_close = sum(value > 0 for value in clv)
    return {
        "sampleSize": len(rows),
        "modelBrier": _brier(rows, "modelProbability"),
        "modelEce": _ece(rows, "modelProbability"),
        "meanModelProbability": round(
            _mean(row["modelProbability"] for row in rows), 6,
        ) if rows else None,
        "pairedBrierDelta": round(_mean(paired), 6) if paired else None,
        "clvSampleSize": len(clv),
        "beatCloseRate": round(beat_close / len(clv), 6) if clv else None,
        "averageClv": round(_mean(clv), 6) if clv else None,
        "freshQuoteRate": round(sum(
            (row.get("context") or {}).get("freshnessBand")
            in {"under_1m", "1_to_5m", "5_to_15m"}
            for row in rows
        ) / len(rows), 6) if rows else None,
        "readyProviderRate": round(sum(
            (row.get("context") or {}).get("providerState") == "ready"
            for row in rows
        ) / len(rows), 6) if rows else None,
    }


def _categorical_total_variation(
    recent: list[dict[str, Any]],
    baseline: list[dict[str, Any]],
    dimension: str,
) -> float | None:
    if not recent or not baseline:
        return None
    recent_counts = Counter(
        str((row.get("context") or {}).get(dimension) or "unknown")
        for row in recent
    )
    baseline_counts = Counter(
        str((row.get("context") or {}).get(dimension) or "unknown")
        for row in baseline
    )
    labels = set(recent_counts) | set(baseline_counts)
    distance = 0.5 * sum(
        abs(recent_counts[label] / len(recent) - baseline_counts[label] / len(baseline))
        for label in labels
    )
    return round(distance, 6)


def _drift_control(rows: list[dict[str, Any]], anchor: date) -> dict[str, Any]:
    recent_cutoff = anchor - timedelta(days=13)
    baseline_cutoff = recent_cutoff - timedelta(days=60)
    markets: dict[str, dict[str, Any]] = {}
    state_order = {"insufficient_sample": 0, "stable": 1, "watch": 2, "degraded": 3, "suppressed": 4}
    overall_state = "insufficient_sample"
    for market in sorted({row["market"] for row in rows}):
        market_rows = [row for row in rows if row["market"] == market]
        recent = [row for row in market_rows if row["gradedAt"].date() >= recent_cutoff]
        baseline = [
            row for row in market_rows
            if baseline_cutoff <= row["gradedAt"].date() < recent_cutoff
        ]
        recent_metrics = _metrics_for_drift(recent)
        baseline_metrics = _metrics_for_drift(baseline)
        brier_delta = (
            recent_metrics["modelBrier"] - baseline_metrics["modelBrier"]
            if recent_metrics["modelBrier"] is not None and baseline_metrics["modelBrier"] is not None
            else None
        )
        ece_delta = (
            recent_metrics["modelEce"] - baseline_metrics["modelEce"]
            if recent_metrics["modelEce"] is not None and baseline_metrics["modelEce"] is not None
            else None
        )
        skill_delta = (
            recent_metrics["pairedBrierDelta"] - baseline_metrics["pairedBrierDelta"]
            if recent_metrics["pairedBrierDelta"] is not None and baseline_metrics["pairedBrierDelta"] is not None
            else None
        )
        probability_delta = (
            abs(
                recent_metrics["meanModelProbability"]
                - baseline_metrics["meanModelProbability"]
            )
            if recent_metrics["meanModelProbability"] is not None
            and baseline_metrics["meanModelProbability"] is not None
            else None
        )
        clv_delta = (
            recent_metrics["averageClv"] - baseline_metrics["averageClv"]
            if recent_metrics["averageClv"] is not None
            and baseline_metrics["averageClv"] is not None
            else None
        )
        feature_drift = {
            dimension: _categorical_total_variation(recent, baseline, dimension)
            for dimension in DRIFT_FEATURE_DIMENSIONS
        }
        top_feature, feature_drift_score = max(
            (
                (dimension, score)
                for dimension, score in feature_drift.items()
                if score is not None
            ),
            key=lambda item: item[1],
            default=(None, None),
        )
        reasons = []
        if len(recent) < MIN_DRIFT_RECENT or len(baseline) < MIN_DRIFT_BASELINE:
            state = "insufficient_sample"
            action = "none"
        else:
            severe_clv = (
                recent_metrics["clvSampleSize"] >= MIN_DRIFT_RECENT
                and recent_metrics["beatCloseRate"] is not None
                and recent_metrics["beatCloseRate"] < 0.45
            )
            provider_degraded = bool(
                recent_metrics["readyProviderRate"] is not None
                and recent_metrics["readyProviderRate"] < 0.80
                and baseline_metrics["readyProviderRate"] is not None
                and baseline_metrics["readyProviderRate"] >= 0.90
            )
            freshness_degraded = bool(
                recent_metrics["freshQuoteRate"] is not None
                and recent_metrics["freshQuoteRate"] < 0.80
                and baseline_metrics["freshQuoteRate"] is not None
                and baseline_metrics["freshQuoteRate"] >= 0.90
            )
            if (brier_delta or 0) >= 0.06 or (skill_delta or 0) >= 0.05 or severe_clv:
                state, action = "suppressed", "no_bet"
            elif (
                (brier_delta or 0) >= 0.04
                or (ece_delta or 0) >= 0.05
                or (skill_delta or 0) >= 0.035
                or provider_degraded
                or freshness_degraded
            ):
                state, action = "degraded", "research_only"
            elif (
                (brier_delta or 0) >= 0.02
                or (ece_delta or 0) >= 0.03
                or (skill_delta or 0) >= 0.02
                or (probability_delta or 0) >= 0.08
                or (feature_drift_score or 0) >= 0.20
                or (clv_delta is not None and clv_delta <= -0.03)
            ):
                state, action = "watch", "downgrade_confidence"
            else:
                state, action = "stable", "none"
            if brier_delta is not None and brier_delta > 0:
                reasons.append("recent_brier_degradation")
            if ece_delta is not None and ece_delta > 0:
                reasons.append("recent_calibration_degradation")
            if skill_delta is not None and skill_delta > 0:
                reasons.append("closing_skill_deterioration")
            if severe_clv:
                reasons.append("beat_close_below_45_percent")
            if clv_delta is not None and clv_delta <= -0.03:
                reasons.append("clv_decay")
            if probability_delta is not None and probability_delta >= 0.08:
                reasons.append("probability_distribution_drift")
            if feature_drift_score is not None and feature_drift_score >= 0.20:
                reasons.append("feature_distribution_drift")
            if provider_degraded:
                reasons.append("provider_health_degradation")
            if freshness_degraded:
                reasons.append("quote_freshness_degradation")
        markets[market] = {
            "state": state,
            "recommendedAction": action,
            "recentWindowDays": 14,
            "baselineWindowDays": 60,
            "recent": recent_metrics,
            "baseline": baseline_metrics,
            "deltas": {
                "brier": round(brier_delta, 6) if brier_delta is not None else None,
                "ece": round(ece_delta, 6) if ece_delta is not None else None,
                "pairedClosingSkill": round(skill_delta, 6) if skill_delta is not None else None,
                "meanModelProbability": round(probability_delta, 6) if probability_delta is not None else None,
                "averageClv": round(clv_delta, 6) if clv_delta is not None else None,
            },
            "featureDrift": {
                "maximumTotalVariation": feature_drift_score,
                "topDimension": top_feature,
                "byDimension": feature_drift,
            },
            "providerHealth": {
                "recentReadyRate": recent_metrics["readyProviderRate"],
                "baselineReadyRate": baseline_metrics["readyProviderRate"],
                "recentFreshQuoteRate": recent_metrics["freshQuoteRate"],
                "baselineFreshQuoteRate": baseline_metrics["freshQuoteRate"],
            },
            "reasonCodes": reasons,
        }
        if state_order[state] > state_order[overall_state]:
            overall_state = state
    return {
        "version": DRIFT_CONTROL_VERSION,
        "state": overall_state,
        "recentMinimumSample": MIN_DRIFT_RECENT,
        "baselineMinimumSample": MIN_DRIFT_BASELINE,
        "markets": markets,
        "mayDowngradeConfidence": True,
        "maySuppressMarket": True,
        "mayRetrainModel": False,
        "mayPromoteModel": False,
        "failClosed": True,
    }


def _correlation_pair_key(left: Mapping[str, Any], right: Mapping[str, Any]) -> str:
    labels = sorted((
        f"{left.get('market')}:{left.get('side')}",
        f"{right.get('market')}:{right.get('side')}",
    ))
    return "|".join(labels)


def _simulation_calibration(rows: list[dict[str, Any]]) -> dict[str, Any]:
    simulated = []
    distribution_rows = []
    for row in rows:
        simulation = row.get("simulation")
        if not isinstance(simulation, Mapping):
            continue
        probability = _probability(simulation.get("probability"))
        if probability is None:
            continue
        prepared = {**row, "simulationProbability": probability}
        simulated.append(prepared)
        if (
            row.get("actual") is not None
            and _number(simulation.get("p10")) is not None
            and _number(simulation.get("p90")) is not None
        ):
            distribution_rows.append(prepared)
    simulation_brier = _brier(simulated, "simulationProbability")
    simulation_ece = _ece(simulated, "simulationProbability")
    champion_brier = _brier(simulated, "modelProbability")
    coverage_count = sum(
        float(row["simulation"]["p10"]) <= float(row["actual"])
        <= float(row["simulation"]["p90"])
        for row in distribution_rows
    )
    interval_coverage = (
        coverage_count / len(distribution_rows) if distribution_rows else None
    )
    mean_error = _mean(
        abs(float(row["simulation"]["mean"]) - float(row["actual"]))
        for row in distribution_rows
        if _number(row["simulation"].get("mean")) is not None
    )
    if len(simulated) < MIN_SIMULATION_SAMPLE:
        simulation_state = "insufficient_sample"
    elif (
        simulation_ece is not None and simulation_ece <= 0.08
        and (
            interval_coverage is None
            or 0.70 <= interval_coverage <= 0.90
        )
    ):
        simulation_state = "verified"
    else:
        simulation_state = "review"

    by_game: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("gamePk") is not None:
            by_game[str(row["gamePk"])].append(row)
    pair_values: dict[str, list[dict[str, float]]] = defaultdict(list)
    for game_rows in by_game.values():
        unique = {str(row["identity"]): row for row in game_rows}
        for left, right in combinations(unique.values(), 2):
            key = _correlation_pair_key(left, right)
            expected = left["modelProbability"] * right["modelProbability"]
            joint = float(left["outcome"] and right["outcome"])
            pair_values[key].append({"expected": expected, "joint": joint})
    correlation_pairs = []
    suppressed_pairs = 0
    for key, values in sorted(pair_values.items()):
        if len(values) < MIN_CORRELATION_PAIRS:
            suppressed_pairs += 1
            continue
        expected = _mean(value["expected"] for value in values) or 0
        joint = _mean(value["joint"] for value in values) or 0
        differences = [value["joint"] - value["expected"] for value in values]
        interval = _mean_interval(differences)
        factor = max(0.50, min(1.50, joint / expected)) if expected > 0 else 1.0
        if interval["lower"] is not None and interval["lower"] > 0:
            state = "positive"
        elif interval["upper"] is not None and interval["upper"] < 0:
            state = "negative"
        else:
            state = "inconclusive"
        correlation_pairs.append({
            "pairKey": key,
            "sampleSize": len(values),
            "state": state,
            "independentExpectedRate": round(expected, 6),
            "observedJointRate": round(joint, 6),
            "jointLift": round(joint - expected, 6),
            "jointLiftInterval": interval,
            "factor": round(factor, 6),
            "verified": state != "inconclusive",
        })
    return {
        "version": SIMULATION_CALIBRATION_VERSION,
        "state": simulation_state,
        "minimumSimulationSample": MIN_SIMULATION_SAMPLE,
        "simulation": {
            "sampleSize": len(simulated),
            "state": simulation_state,
            "brier": simulation_brier,
            "ece": simulation_ece,
            "championBrierOnSameRows": champion_brier,
            "distributionSampleSize": len(distribution_rows),
            "p10P90Coverage": round(interval_coverage, 6) if interval_coverage is not None else None,
            "meanAbsoluteOutcomeError": round(mean_error, 6) if mean_error is not None else None,
        },
        "correlationMinimumPairs": MIN_CORRELATION_PAIRS,
        "correlationPairs": correlation_pairs,
        "suppressedCorrelationPairCount": suppressed_pairs,
        "unverifiedCorrelationTrackable": False,
        "rawRowsIncluded": False,
    }


def _policy_metrics(
    rows: list[dict[str, Any]],
    minimum_edge: float,
    minimum_expected_value: float,
) -> dict[str, Any]:
    selected = [
        row for row in rows
        if _number((row.get("decision") or {}).get("edge")) is not None
        and float(row["decision"]["edge"]) >= minimum_edge
        and _number((row.get("decision") or {}).get("expectedValue")) is not None
        and float(row["decision"]["expectedValue"]) >= minimum_expected_value
    ]
    profits = []
    clv_values = []
    for row in selected:
        profit = _profit_units(
            row["outcome"], (row.get("decision") or {}).get("openingPrice"),
        )
        if profit is not None:
            profits.append(profit)
        if row.get("clvEligible"):
            clv_values.append(row["clvEdge"])
    roi = sum(profits) / len(profits) if profits else None
    average_clv = _mean(clv_values)
    score = (
        (roi or -1)
        + 2 * (average_clv or 0)
        - (_maximum_drawdown(profits) / max(1, len(profits)))
    )
    return {
        "minimumEdge": round(minimum_edge, 4),
        "minimumExpectedValue": round(minimum_expected_value, 4),
        "selectedCount": len(selected),
        "roiEligibleCount": len(profits),
        "roi": round(roi, 6) if roi is not None else None,
        "clvEligibleCount": len(clv_values),
        "averageClv": round(average_clv, 6) if average_clv is not None else None,
        "maximumDrawdownUnits": _maximum_drawdown(profits),
        "shadowScore": round(score, 6),
    }


def _policy_lab(rows: list[dict[str, Any]]) -> dict[str, Any]:
    proposals = []
    for market in sorted({row["market"] for row in rows}):
        values = [
            row for row in rows
            if row["market"] == market
            and _number((row.get("decision") or {}).get("edge")) is not None
            and _number((row.get("decision") or {}).get("expectedValue")) is not None
        ]
        values.sort(key=lambda row: (row["gradedAt"], row["fingerprint"]))
        split = max(1, int(len(values) * 0.70)) if values else 0
        reference = values[:split]
        holdout = values[split:]
        reference_grid = [
            _policy_metrics(reference, edge, expected_value)
            for edge in EDGE_GRID
            for expected_value in EXPECTED_VALUE_GRID
        ]
        eligible_reference = [
            result for result in reference_grid
            if result["selectedCount"] >= MIN_POLICY_REFERENCE
        ]
        selected_policy = (
            max(eligible_reference, key=lambda result: (
                result["shadowScore"], result["selectedCount"],
                -result["minimumEdge"], -result["minimumExpectedValue"],
            ))
            if eligible_reference else {
                "minimumEdge": CURRENT_MINIMUM_EDGE,
                "minimumExpectedValue": CURRENT_MINIMUM_EXPECTED_VALUE,
            }
        )
        selected_edge = selected_policy["minimumEdge"]
        selected_expected_value = selected_policy["minimumExpectedValue"]
        baseline = _policy_metrics(
            holdout, CURRENT_MINIMUM_EDGE, CURRENT_MINIMUM_EXPECTED_VALUE,
        )
        proposed = _policy_metrics(
            holdout, selected_edge, selected_expected_value,
        )
        if len(reference) < MIN_POLICY_REFERENCE or len(holdout) < MIN_POLICY_HOLDOUT:
            state = "insufficient_sample"
        elif (
            proposed["selectedCount"] >= MIN_POLICY_HOLDOUT
            and proposed["roi"] is not None and proposed["roi"] > 0
            and proposed["averageClv"] is not None and proposed["averageClv"] > 0
            and proposed["maximumDrawdownUnits"] <= max(10.0, baseline["maximumDrawdownUnits"])
            and proposed["shadowScore"] > baseline["shadowScore"]
            and (
                selected_edge != CURRENT_MINIMUM_EDGE
                or selected_expected_value != CURRENT_MINIMUM_EXPECTED_VALUE
            )
        ):
            state = "review_candidate"
        else:
            state = "hold_current"
        proposals.append({
            "marketKey": market,
            "state": state,
            "currentMinimumEdge": CURRENT_MINIMUM_EDGE,
            "proposedMinimumEdge": selected_edge,
            "currentMinimumExpectedValue": CURRENT_MINIMUM_EXPECTED_VALUE,
            "proposedMinimumExpectedValue": selected_expected_value,
            "referenceSampleSize": len(reference),
            "holdoutSampleSize": len(holdout),
            "baselineHoldout": baseline,
            "proposedHoldout": proposed,
            "temporalHoldout": True,
            "selectionBiasWarning": (
                "Thresholds are evaluated only on prospectively tracked decisions; "
                "unpublished candidate outcomes are not inferred."
            ),
            "automaticApplication": False,
            "humanReviewRequired": True,
        })
    return {
        "version": POLICY_LAB_VERSION,
        "state": (
            "review_candidate" if any(item["state"] == "review_candidate" for item in proposals)
            else "ready" if proposals else "insufficient_sample"
        ),
        "objective": "holdout_roi_plus_clv_minus_drawdown_over_edge_and_ev",
        "minimumReferenceSample": MIN_POLICY_REFERENCE,
        "minimumHoldoutSample": MIN_POLICY_HOLDOUT,
        "proposals": proposals,
        "automaticThresholdChange": False,
        "automaticStakingChange": False,
        "humanReviewRequired": True,
    }


def build_intelligence_control_plane(
    entries: Iterable[Mapping[str, Any]],
    *,
    as_of: date | datetime | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> dict[str, Any]:
    """Build the aggregate Phase 6.1-6.5 control plane."""

    if isinstance(as_of, datetime):
        checked_at = as_of
        if checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=timezone.utc)
        checked_at = checked_at.astimezone(timezone.utc)
        anchor = checked_at.date()
    else:
        anchor = as_of or datetime.now(timezone.utc).date()
        checked_at = datetime.combine(anchor, time.max, tzinfo=timezone.utc)
    window = max(1, min(int(window_days), MAX_WINDOW_DAYS))
    cutoff = anchor - timedelta(days=window - 1)
    rows, rejected_count, rejected = _verified_observations(
        entries,
        checked_at=checked_at,
        cutoff=cutoff,
        anchor=anchor,
    )
    phases = {
        "errorAtlas": _error_atlas(rows),
        "championChallenger": _champion_challenger(rows),
        "driftControl": _drift_control(rows, anchor),
        "simulationCalibration": _simulation_calibration(rows),
        "policyLab": _policy_lab(rows),
    }
    return {
        "success": True,
        "version": INTELLIGENCE_CONTROL_PLANE_VERSION,
        "state": "ready" if rows else "insufficient_sample",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "window": {
            "days": window,
            "from": cutoff.isoformat(),
            "through": anchor.isoformat(),
        },
        "coverage": {
            "verifiedObservationCount": len(rows),
            "rejectedObservationCount": rejected_count,
            "rejectedReasonCounts": dict(sorted(rejected.items())),
            "rawRowsIncluded": False,
        },
        "phases": phases,
        "safety": {
            "readOnly": True,
            "failClosed": True,
            "privateTrackerFieldsIncluded": False,
            "automaticModelPromotion": False,
            "automaticRetraining": False,
            "automaticProbabilityChange": False,
            "automaticThresholdChange": False,
            "automaticStakingChange": False,
            "humanReviewRequired": True,
        },
        "serverMutation": False,
    }


def apply_drift_interventions(
    candidates: Iterable[Mapping[str, Any]],
    control_plane: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Apply only evidence-backed downgrade/suppression recommendations."""

    payload = control_plane if isinstance(control_plane, Mapping) else {}
    phases = payload.get("phases") if isinstance(payload.get("phases"), Mapping) else {}
    drift = phases.get("driftControl") if isinstance(phases.get("driftControl"), Mapping) else {}
    markets = drift.get("markets") if isinstance(drift.get("markets"), Mapping) else {}
    promoted = []
    rejected = []
    counts: Counter[str] = Counter()
    for source in candidates or ():
        row = dict(source)
        market = str(row.get("canonicalMarketKey") or row.get("marketKey") or "").lower()
        evidence = markets.get(market) if isinstance(markets.get(market), Mapping) else {}
        state = str(evidence.get("state") or "insufficient_sample")
        action = str(evidence.get("recommendedAction") or "none")
        receipt = {
            "version": DRIFT_CONTROL_VERSION,
            "marketKey": market or None,
            "state": state,
            "recommendedAction": action,
            "reasonCodes": list(evidence.get("reasonCodes") or []),
            "probabilityChanged": False,
            "modelChanged": False,
        }
        row["phase6Intervention"] = receipt
        if state in {"suppressed", "degraded"}:
            row["actionable"] = False
            row["actionabilityStage"] = "filtered"
            row["phase6DecisionState"] = (
                "no_bet" if state == "suppressed" else "research_only"
            )
            row["marketGateReasons"] = list(dict.fromkeys(
                list(row.get("marketGateReasons") or [])
                + [f"Phase 6.3 drift control: {state}"]
            ))
            rejected.append(row)
            counts[state] += 1
            continue
        if state == "watch":
            tier = str(row.get("confidenceTier") or "").upper()
            downgraded = {"A": "B", "B": "C", "C": "C"}.get(tier)
            if downgraded:
                row["phase6OriginalConfidenceTier"] = tier
                row["confidenceTier"] = downgraded
                row["phase6ConfidenceDowngraded"] = True
            counts["watch"] += 1
        promoted.append(row)
    return {
        "version": DRIFT_CONTROL_VERSION,
        "promoted": promoted,
        "rejected": rejected,
        "audit": {
            "sourceCount": len(promoted) + len(rejected),
            "promotedCount": len(promoted),
            "rejectedCount": len(rejected),
            "interventionCounts": dict(sorted(counts.items())),
            "probabilitiesChanged": False,
            "modelsChanged": False,
        },
    }


def verified_correlation_pairs(control_plane: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    payload = control_plane if isinstance(control_plane, Mapping) else {}
    phases = payload.get("phases") if isinstance(payload.get("phases"), Mapping) else {}
    calibration = phases.get("simulationCalibration") if isinstance(phases.get("simulationCalibration"), Mapping) else {}
    return [
        dict(row) for row in calibration.get("correlationPairs") or []
        if isinstance(row, Mapping)
        and row.get("verified") is True
        and int(row.get("sampleSize") or 0) >= MIN_CORRELATION_PAIRS
    ]


def install_intelligence_control_plane(app_module: Any) -> None:
    """Register the Phase 6.1-6.5 aggregate endpoint once per worker."""

    flask_app = app_module.app
    if getattr(flask_app, "_phase_615_intelligence_control_plane_installed", False):
        return
    from flask import Blueprint, jsonify, request
    from public_verification import load_tracker_entries

    blueprint = Blueprint("intelligence_control_plane", __name__)

    @blueprint.get("/api/accuracy/intelligence")
    def intelligence_control_plane_api():
        try:
            window = int(request.args.get("window", DEFAULT_WINDOW_DAYS))
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "window must be an integer"}), 400
        requested_date = str(request.args.get("date") or "").strip()
        try:
            anchor = date.fromisoformat(requested_date) if requested_date else datetime.now(timezone.utc).date()
        except ValueError:
            return jsonify({"success": False, "error": "date must use YYYY-MM-DD"}), 400
        tracker_path = getattr(app_module, "TRACKER_STORE", None)
        if not tracker_path:
            tracker_path = os.path.join(os.path.dirname(__file__), "data", "daily_tracker.json")
        payload = build_intelligence_control_plane(
            load_tracker_entries(tracker_path), as_of=anchor, window_days=window,
        )
        response = jsonify(payload)
        response.headers["Cache-Control"] = "public, max-age=30, stale-while-revalidate=60"
        response.headers["X-Intelligence-Contract"] = INTELLIGENCE_CONTROL_PLANE_VERSION
        return response

    flask_app.register_blueprint(blueprint)
    setattr(flask_app, "_phase_615_intelligence_control_plane_installed", True)
