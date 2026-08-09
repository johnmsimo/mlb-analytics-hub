"""Walk-forward validation and fail-closed promotion gates for MLB markets.

Phase 4.37 answers whether one candidate is structurally eligible.  This module
answers the separate question Phase 4.38 introduces: has the *market* earned
the right to promote any eligible candidate as a bet?

Only time-ordered holdout rows are used for gate decisions.  Research rows stay
available, but ``apply_market_gates`` marks candidates from unproven or failing
markets non-actionable before they reach picks, edges, parlays, or the tracker.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
import math
from typing import Any, Iterable, Mapping

from actionability import ACTIONABILITY_VERSION, evaluate_actionability
from candidate_integrity import SUPPORTED_MARKETS, canonical_market_key
from odds_lineage import clv_eligibility, clv_summary


VALIDATION_VERSION = "4.38"
CALIBRATION_VERSION = "4.54"


@dataclass(frozen=True)
class ValidationPolicy:
    window_days: int = 180
    minimum_training_days: int = 14
    validation_block_days: int = 7
    minimum_validation_rows: int = 40
    minimum_validation_days: int = 7
    minimum_market_baseline_rows: int = 20
    minimum_priced_rows: int = 30
    minimum_clv_rows: int = 15
    maximum_calibration_error: float = 0.08
    minimum_brier_skill_vs_market: float = 0.0
    minimum_roi: float = 0.0
    minimum_average_clv: float = 0.0
    maximum_drawdown_units: float = 10.0
    calibration_confidence_level: float = 0.95
    drift_warning_delta: float = 0.05
    drift_failure_delta: float = 0.10


def _number(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _probability(value: Any) -> float | None:
    number = _number(value)
    if number is None:
        return None
    number = number / 100.0 if number > 1.0 else number
    return number if 0.0 < number < 1.0 else None


def _row_probability(row: Mapping[str, Any]) -> float | None:
    for key in (
        "blendedProb", "adjProb", "canonicalProbability", "probability",
        "rawProb", "modelProb",
    ):
        probability = _probability(row.get(key))
        if probability is not None:
            return max(0.001, min(0.999, probability))
    return None


def _outcome(row: Mapping[str, Any]) -> int | None:
    grade = str(
        row.get("grade") or row.get("result") or row.get("outcome") or ""
    ).strip().lower()
    if grade in {"win", "won", "w", "hit", "correct", "1", "true"}:
        return 1
    if grade in {"loss", "lost", "l", "miss", "incorrect", "0", "false"}:
        return 0
    return None


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day)
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = datetime.strptime(text[:10], "%Y-%m-%d")
            except ValueError:
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _prediction_time(row: Mapping[str, Any]) -> datetime | None:
    for key in (
        "savedAt", "generatedAt", "predictionTimestamp", "oddsUpdatedAt",
        "timestamp", "date",
    ):
        parsed = _parse_datetime(row.get(key))
        if parsed is not None:
            return parsed
    return None


def _side(row: Mapping[str, Any], market: str | None = None) -> str:
    value = str(
        row.get("canonicalSide") or row.get("recommendedSide")
        or row.get("side") or "unknown"
    ).strip().lower()
    if value.startswith("over"):
        return "over"
    if value.startswith("under"):
        return "under"
    if market in {"h2h", "totals", "f5_h2h", "f5_totals", "nrfi", "yrfi"}:
        return "selection"
    return value or "unknown"


def _recommendation_grade(row: Mapping[str, Any]) -> str:
    value = (
        row.get("recommendationGrade") or row.get("confidenceTier")
        or row.get("stackVerdict") or row.get("pickScoreTier") or "ungraded"
    )
    return str(value).strip().lower().replace(" ", "_") or "ungraded"


def _book(row: Mapping[str, Any]) -> str:
    value = (
        row.get("openingBookmaker") or row.get("bestAvailableBook")
        or row.get("bookmaker") or row.get("canonicalBook") or "unknown"
    )
    return str(value).strip().lower() or "unknown"


def _taken_price(row: Mapping[str, Any]) -> float | None:
    for key in (
        "openingPrice", "marketPrice", "bestAvailablePrice", "canonicalPrice",
        "price",
    ):
        value = _number(row.get(key))
        if value is not None and value != 0:
            return value
    return None


def _odds_range(row: Mapping[str, Any]) -> str:
    price = _taken_price(row)
    if price is None:
        return "unpriced"
    if price >= 100:
        return "underdog_+100_or_longer"
    if price >= -109:
        return "near_even_-109_to_+99"
    if price >= -149:
        return "favorite_-110_to_-149"
    return "heavy_favorite_-150_or_shorter"


def _market_probability(row: Mapping[str, Any]) -> float | None:
    for key in (
        "marketFairProbability", "openingImplied", "marketImplied",
        "quotedMarketImplied",
    ):
        probability = _probability(row.get(key))
        if probability is not None:
            return probability
    return None


def _profit_units(row: Mapping[str, Any], outcome: int) -> float | None:
    stored = _number(row.get("profitUnits"))
    if stored is not None:
        return stored
    price = _taken_price(row)
    if price is None:
        return None
    if outcome == 0:
        return -1.0
    return price / 100.0 if price > 0 else 100.0 / abs(price)


def _clv(row: Mapping[str, Any]) -> float | None:
    if not clv_eligibility(row):
        return None
    for key in ("clvEdge", "closingLineValue", "clv"):
        value = _number(row.get(key))
        if value is not None:
            return value / 100.0 if abs(value) > 1.0 else value
    return None


def _normalized_rows(
    entries: Iterable[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], int, int]:
    rows: list[dict[str, Any]] = []
    skipped = 0
    backfilled = 0
    for source in entries:
        if source.get("backfilled") is True or str(
            source.get("backfilled") or ""
        ).strip().lower() in {"1", "true", "yes"}:
            # Historical captures used current-season caches and were stamped
            # for possible look-ahead bias. They can inform research, but they
            # must never decide a production promotion gate.
            backfilled += 1
            continue
        market = canonical_market_key(source)
        probability = _row_probability(source)
        outcome = _outcome(source)
        predicted_at = _prediction_time(source)
        if (
            market not in SUPPORTED_MARKETS
            or probability is None
            or outcome is None
            or predicted_at is None
        ):
            skipped += 1
            continue
        row = dict(source)
        validation_side = _side(source, market)
        row.update({
            "canonicalMarketKey": market,
            "validationProbability": probability,
            "validationOutcome": outcome,
            "validationTimestamp": predicted_at,
            "validationDate": predicted_at.date().isoformat(),
            "validationSide": validation_side,
            "validationMarketSide": f"{market}|{validation_side}",
            "validationGrade": _recommendation_grade(source),
            "validationBook": _book(source),
            "validationOddsRange": _odds_range(source),
            "validationMarketProbability": _market_probability(source),
        })
        row["validationProfitUnits"] = _profit_units(source, outcome)
        row["validationClv"] = _clv(source)
        row["validationGraded"] = True
        row["validationClvEligible"] = clv_eligibility(source)
        rows.append(row)
    rows.sort(key=lambda row: (
        row["validationTimestamp"],
        row["canonicalMarketKey"],
        str(row.get("id") or ""),
    ))
    return rows, skipped, backfilled


def _calibration_error(rows: list[Mapping[str, Any]]) -> float | None:
    if not rows:
        return None
    buckets: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        bucket = min(9, int(float(row["validationProbability"]) * 10))
        buckets[bucket].append(row)
    total = len(rows)
    return sum(
        (len(values) / total)
        * abs(
            sum(float(row["validationProbability"]) for row in values)
            / len(values)
            - sum(int(row["validationOutcome"]) for row in values)
            / len(values)
        )
        for values in buckets.values()
    )


def _wilson_interval(successes: int, total: int, confidence: float = 0.95) -> dict[str, Any] | None:
    if total <= 0:
        return None
    # 95% Wilson interval; the explicit level is part of the public audit.
    z = 1.959964 if confidence >= 0.95 else 1.644854
    proportion = successes / total
    denominator = 1.0 + z * z / total
    centre = (proportion + z * z / (2.0 * total)) / denominator
    margin = (
        z * math.sqrt(
            proportion * (1.0 - proportion) / total
            + z * z / (4.0 * total * total)
        ) / denominator
    )
    return {
        "level": round(confidence, 3),
        "lower": round(max(0.0, centre - margin), 4),
        "upper": round(min(1.0, centre + margin), 4),
    }


def _drift_summary(rows: list[Mapping[str, Any]], policy: ValidationPolicy) -> dict[str, Any]:
    values = sorted(rows, key=lambda row: row["validationTimestamp"])
    if len(values) < 2:
        return {
            "status": "unknown",
            "baselineCount": 0,
            "recentCount": len(values),
            "eceDelta": None,
            "brierDelta": None,
        }
    recent_count = max(1, len(values) // 3)
    baseline = values[:-recent_count]
    recent = values[-recent_count:]
    baseline_metrics = summarize_rows(baseline)
    recent_metrics = summarize_rows(recent)
    ece_delta = (
        float(recent_metrics["calibrationError"])
        - float(baseline_metrics["calibrationError"])
    )
    brier_delta = (
        float(recent_metrics["brierScore"])
        - float(baseline_metrics["brierScore"])
    )
    if (
        len(recent) < max(5, policy.minimum_validation_rows // 4)
        or len(baseline) < max(5, policy.minimum_validation_rows // 2)
    ):
        status = "unknown"
    elif (
        ece_delta >= policy.drift_failure_delta
        or brier_delta >= policy.drift_failure_delta
    ):
        status = "drifted"
    elif (
        ece_delta >= policy.drift_warning_delta
        or brier_delta >= policy.drift_warning_delta / 2.0
    ):
        status = "watch"
    else:
        status = "stable"
    return {
        "status": status,
        "baselineCount": len(baseline),
        "recentCount": len(recent),
        "eceDelta": round(ece_delta, 4),
        "brierDelta": round(brier_delta, 5),
        "baselineEnd": baseline[-1]["validationDate"],
        "recentStart": recent[0]["validationDate"],
    }


def _calibration_metadata(rows: list[Mapping[str, Any]], policy: ValidationPolicy) -> dict[str, Any]:
    values = list(rows)
    count = len(values)
    wins = sum(int(row["validationOutcome"]) for row in values)
    summary = summarize_rows(values)
    return {
        "version": CALIBRATION_VERSION,
        "sampleSize": count,
        "brierScore": summary["brierScore"],
        "ece": summary["calibrationError"],
        "confidenceInterval": {
            "winRate": _wilson_interval(
                wins, count, policy.calibration_confidence_level,
            ),
            "level": round(policy.calibration_confidence_level, 3),
        },
        "driftStatus": _drift_summary(values, policy),
    }


def _annotated_group_summary(
    rows: Iterable[Mapping[str, Any]], key: str, policy: ValidationPolicy,
) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get(key) or "unknown")].append(row)
    result = {}
    for name, values in sorted(groups.items()):
        summary = summarize_rows(values)
        calibration = _calibration_metadata(values, policy)
        summary.update({
            "sampleSize": calibration["sampleSize"],
            "expectedCalibrationError": calibration["ece"],
            "confidenceInterval": calibration["confidenceInterval"],
            "driftStatus": calibration["driftStatus"]["status"],
            "drift": calibration["driftStatus"],
            "calibration": calibration,
        })
        result[name] = summary
    return result


def _maximum_drawdown(values: list[float]) -> float | None:
    if not values:
        return None
    equity = 0.0
    peak = 0.0
    maximum = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        maximum = max(maximum, peak - equity)
    return maximum


def summarize_rows(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    values = list(rows)
    if not values:
        return {
            "count": 0, "wins": 0, "losses": 0, "winRate": None,
            "averageProbability": None, "brierScore": None,
            "marketBrierScore": None, "brierSkillVsMarket": None,
            "logLoss": None, "calibrationError": None, "pricedCount": 0,
            "profitUnits": None, "roi": None, "clvCount": 0,
            "averageClv": None, "maximumDrawdownUnits": None,
            "gradedCount": 0, "clvEligibleCount": 0, "clvGradedCount": 0,
            "clvDenominator": "clvGradedCount", "beatCloseCount": 0,
            "beatCloseRate": None, "clvClaimStatus": "insufficient_sample",
            "clvClaimEligible": False,
            "days": 0,
        }
    count = len(values)
    wins = sum(int(row["validationOutcome"]) for row in values)
    probabilities = [float(row["validationProbability"]) for row in values]
    brier = sum(
        (probability - int(row["validationOutcome"])) ** 2
        for probability, row in zip(probabilities, values)
    ) / count
    log_loss = -sum(
        int(row["validationOutcome"]) * math.log(probability)
        + (1 - int(row["validationOutcome"])) * math.log(1 - probability)
        for probability, row in zip(probabilities, values)
    ) / count

    market_rows = [
        row for row in values
        if row.get("validationMarketProbability") is not None
    ]
    market_brier = None
    brier_skill = None
    if market_rows:
        market_brier = sum(
            (
                float(row["validationMarketProbability"])
                - int(row["validationOutcome"])
            ) ** 2
            for row in market_rows
        ) / len(market_rows)
        model_market_brier = sum(
            (
                float(row["validationProbability"])
                - int(row["validationOutcome"])
            ) ** 2
            for row in market_rows
        ) / len(market_rows)
        if market_brier > 0:
            brier_skill = 1.0 - model_market_brier / market_brier

    profits = [
        float(row["validationProfitUnits"])
        for row in values if row.get("validationProfitUnits") is not None
    ]
    clv_values = [
        float(row["validationClv"])
        for row in values if row.get("validationClv") is not None
    ]
    clv_audit = clv_summary(values)
    return {
        "count": count,
        "wins": wins,
        "losses": count - wins,
        "winRate": round(wins / count, 4),
        "averageProbability": round(sum(probabilities) / count, 4),
        "brierScore": round(brier, 5),
        "marketBaselineCount": len(market_rows),
        "marketBrierScore": (
            round(market_brier, 5) if market_brier is not None else None
        ),
        "brierSkillVsMarket": (
            round(brier_skill, 5) if brier_skill is not None else None
        ),
        "logLoss": round(log_loss, 5),
        "calibrationError": round(_calibration_error(values) or 0.0, 4),
        "pricedCount": len(profits),
        "profitUnits": round(sum(profits), 3) if profits else None,
        "roi": round(sum(profits) / len(profits), 4) if profits else None,
        "clvCount": len(clv_values),
        "gradedCount": clv_audit["gradedCount"],
        "clvEligibleCount": clv_audit["clvEligibleCount"],
        "clvGradedCount": clv_audit["clvGradedCount"],
        "clvDenominator": clv_audit["clvDenominator"],
        "beatCloseCount": clv_audit["beatCloseCount"],
        "beatCloseRate": clv_audit["beatCloseRate"],
        "clvClaimStatus": clv_audit["claimStatus"],
        "clvClaimEligible": clv_audit["claimEligible"],
        "clvAudit": clv_audit,
        "averageClv": (
            round(sum(clv_values) / len(clv_values), 4)
            if clv_values else None
        ),
        "maximumDrawdownUnits": (
            round(_maximum_drawdown(profits) or 0.0, 3) if profits else None
        ),
        "days": len({row["validationDate"] for row in values}),
    }


def _group_summary(
    rows: Iterable[Mapping[str, Any]], key: str,
) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get(key) or "unknown")].append(row)
    return {
        name: summarize_rows(values)
        for name, values in sorted(groups.items())
    }


def _walk_forward_rows(
    rows: list[dict[str, Any]], policy: ValidationPolicy,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    dates = sorted({row["validationDate"] for row in rows})
    folds: list[dict[str, Any]] = []
    validation_rows: list[dict[str, Any]] = []
    start = policy.minimum_training_days
    while start < len(dates):
        validation_dates = dates[start:start + policy.validation_block_days]
        if not validation_dates:
            break
        training_dates = set(dates[:start])
        validation_set = set(validation_dates)
        training = [
            row for row in rows if row["validationDate"] in training_dates
        ]
        holdout = [
            row for row in rows if row["validationDate"] in validation_set
        ]
        if holdout:
            validation_rows.extend(holdout)
            folds.append({
                "fold": len(folds) + 1,
                "trainingStart": dates[0],
                "trainingEnd": dates[start - 1],
                "validationStart": validation_dates[0],
                "validationEnd": validation_dates[-1],
                "trainingRows": len(training),
                "validationRows": len(holdout),
                "trainingDays": len(training_dates),
                "validationDays": len(validation_set),
                "strictTimeSeparation": dates[start - 1] < validation_dates[0],
            })
        start += policy.validation_block_days
    return validation_rows, folds


def _gate(market: str, metrics: Mapping[str, Any], folds: int,
          policy: ValidationPolicy) -> dict[str, Any]:
    insufficient: list[str] = []
    failed: list[str] = []
    if folds <= 0:
        insufficient.append("no completed walk-forward holdout fold")
    if int(metrics.get("count") or 0) < policy.minimum_validation_rows:
        insufficient.append(
            f"validation sample below {policy.minimum_validation_rows}"
        )
    if int(metrics.get("days") or 0) < policy.minimum_validation_days:
        insufficient.append(
            f"validation history below {policy.minimum_validation_days} days"
        )
    if int(metrics.get("marketBaselineCount") or 0) < policy.minimum_market_baseline_rows:
        insufficient.append(
            f"market baseline sample below {policy.minimum_market_baseline_rows}"
        )
    if int(metrics.get("pricedCount") or 0) < policy.minimum_priced_rows:
        insufficient.append(f"priced sample below {policy.minimum_priced_rows}")
    if int(metrics.get("clvCount") or 0) < policy.minimum_clv_rows:
        insufficient.append(f"CLV sample below {policy.minimum_clv_rows}")

    calibration_error = _number(metrics.get("calibrationError"))
    if (
        calibration_error is not None
        and calibration_error > policy.maximum_calibration_error
    ):
        failed.append("calibration error exceeds policy")
    skill = _number(metrics.get("brierSkillVsMarket"))
    if (
        skill is not None
        and skill <= policy.minimum_brier_skill_vs_market
    ):
        failed.append("model Brier score does not beat the market baseline")
    roi = _number(metrics.get("roi"))
    if roi is not None and roi <= policy.minimum_roi:
        failed.append("holdout ROI is not positive")
    average_clv = _number(metrics.get("averageClv"))
    if average_clv is not None and average_clv <= policy.minimum_average_clv:
        failed.append("average closing-line value is not positive")
    drawdown = _number(metrics.get("maximumDrawdownUnits"))
    if drawdown is not None and drawdown > policy.maximum_drawdown_units:
        failed.append("maximum drawdown exceeds policy")
    if str(metrics.get("driftStatus") or "unknown") == "drifted":
        failed.append("calibration drift exceeds policy")

    if insufficient:
        status = "warming_up"
    elif failed:
        status = "disabled"
    else:
        status = "promoted"
    return {
        "marketKey": market,
        "status": status,
        "promoted": status == "promoted",
        "reasons": insufficient + failed,
        "metrics": dict(metrics),
    }


def build_validation_report(
    entries: Iterable[Mapping[str, Any]],
    *,
    policy: ValidationPolicy | None = None,
) -> dict[str, Any]:
    """Measure strictly future holdout rows and return market promotion gates."""
    policy = policy or ValidationPolicy()
    history, skipped, excluded_backfill = _normalized_rows(entries)
    validation_rows, folds = _walk_forward_rows(history, policy)
    validation_by_market = _annotated_group_summary(
        validation_rows, "canonicalMarketKey", policy,
    )
    validation_by_market_side = _annotated_group_summary(
        validation_rows, "validationMarketSide", policy,
    )
    observed_markets = set(validation_by_market) | {
        row["canonicalMarketKey"] for row in history
    }
    gates = {
        market: _gate(
            market,
            validation_by_market.get(market, summarize_rows([])),
            len(folds),
            policy,
        )
        for market in sorted(observed_markets | set(SUPPORTED_MARKETS))
    }
    observed_market_sides = {
        row["validationMarketSide"] for row in history
    }
    market_side_gates = {
        key: _gate(
            key,
            validation_by_market_side.get(key, summarize_rows([])),
            len(folds),
            policy,
        )
        for key in sorted(observed_market_sides)
    }
    return {
        "version": VALIDATION_VERSION,
        "calibrationVersion": CALIBRATION_VERSION,
        "mode": "strict_walk_forward_holdout",
        "adaptiveWeightsEnabled": False,
        "historyCount": len(history),
        "validationCount": len(validation_rows),
        "skippedCount": skipped,
        "excludedBackfillCount": excluded_backfill,
        "foldCount": len(folds),
        "folds": folds,
        "policy": asdict(policy),
        "overall": summarize_rows(validation_rows),
        "byMarket": validation_by_market,
        "bySide": _group_summary(validation_rows, "validationSide"),
        "byMarketSide": validation_by_market_side,
        "byGrade": _group_summary(validation_rows, "validationGrade"),
        "byOddsRange": _group_summary(
            validation_rows, "validationOddsRange"
        ),
        "bySportsbook": _group_summary(
            validation_rows, "validationBook"
        ),
        "historyByMarket": _group_summary(history, "canonicalMarketKey"),
        "marketGates": gates,
        "marketSideGates": market_side_gates,
        "promotedMarkets": [
            market for market, gate in gates.items() if gate["promoted"]
        ],
        "disabledMarkets": [
            market for market, gate in gates.items()
            if gate["status"] == "disabled"
        ],
        "warmingMarkets": [
            market for market, gate in gates.items()
            if gate["status"] == "warming_up"
        ],
        "clvAudit": clv_summary(validation_rows),
        "calibrationAudit": {
            "version": CALIBRATION_VERSION,
            "marketStatuses": {
                market: gate["status"] for market, gate in gates.items()
            },
            "failingMarkets": [
                market for market, gate in gates.items()
                if gate["status"] == "disabled"
            ],
            "driftedMarkets": [
                market for market, gate in gates.items()
                if str((gate.get("metrics") or {}).get("driftStatus") or "") == "drifted"
            ],
        },
    }


def _report(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    report = value or {}
    nested = report.get("marketValidation")
    return nested if isinstance(nested, Mapping) else report


def gate_for_market(
    report: Mapping[str, Any] | None, market: str,
) -> Mapping[str, Any]:
    gates = _report(report).get("marketGates") or {}
    gate = gates.get(canonical_market_key({"marketKey": market}))
    if isinstance(gate, Mapping):
        return gate
    return {
        "marketKey": market,
        "status": "warming_up",
        "promoted": False,
        "reasons": ["market has no walk-forward validation result"],
        "metrics": summarize_rows([]),
    }


def apply_market_gates(
    candidates: Iterable[Mapping[str, Any]],
    report: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Annotate candidates and return only markets that passed validation."""
    promoted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    counts: dict[str, int] = defaultdict(int)
    rejection_reasons: Counter[str] = Counter()
    stage_counts: Counter[str] = Counter()
    actionability_reasons: Counter[str] = Counter()
    for source in candidates:
        row = dict(source)
        market = canonical_market_key(row)
        gate = gate_for_market(report, market)
        side = _side(row, market)
        side_key = f"{market}|{side}"
        side_gates = _report(report).get("marketSideGates") or {}
        side_gate = side_gates.get(side_key)
        if not isinstance(side_gate, Mapping):
            side_gate = {
                "status": "warming_up", "promoted": False,
                "reasons": ["market side has no walk-forward validation result"],
                "metrics": summarize_rows([]),
            }
        market_status = str(gate.get("status") or "warming_up")
        side_status = str(side_gate.get("status") or "warming_up")
        if "disabled" in {market_status, side_status}:
            status = "disabled"
        elif "warming_up" in {market_status, side_status}:
            status = "warming_up"
        else:
            status = "promoted"
        reasons = [
            f"market: {reason}" for reason in gate.get("reasons") or []
        ] + [
            f"side: {reason}" for reason in side_gate.get("reasons") or []
        ]
        is_promoted = (
            gate.get("promoted") is True
            and side_gate.get("promoted") is True
        )
        market_metrics = dict(gate.get("metrics") or {})
        calibration_drift = str(market_metrics.get("driftStatus") or "unknown")
        is_promoted = (
            gate.get("promoted") is True
            and side_gate.get("promoted") is True
        )
        row.update({
            "marketValidationVersion": VALIDATION_VERSION,
            "calibrationVersion": CALIBRATION_VERSION,
            "calibrationStatus": "passed" if is_promoted else status,
            "calibrationDriftStatus": calibration_drift,
            "calibrationEvidence": {
                "sampleSize": market_metrics.get("sampleSize", market_metrics.get("count", 0)),
                "brierScore": market_metrics.get("brierScore"),
                "ece": market_metrics.get("expectedCalibrationError", market_metrics.get("calibrationError")),
                "confidenceInterval": market_metrics.get("confidenceInterval"),
                "driftStatus": calibration_drift,
            },
            "marketGateStatus": status,
            "marketGatePromoted": is_promoted,
            "marketGateReasons": reasons,
            "marketGateMetrics": dict(gate.get("metrics") or {}),
            "marketSideGateStatus": side_status,
            "marketSideGateMetrics": dict(side_gate.get("metrics") or {}),
        })
        if status == "disabled" or calibration_drift == "drifted":
            strong_fields = (
                "recommendationGrade", "confidenceTier",
                "stackVerdict", "pickScoreTier",
            )
            strong_labels = {
                "high", "high_conf", "high_confidence",
                "high conf", "strong", "strong_bet", "strong bet",
            }
            for field in strong_fields:
                original = row.get(field)
                if str(original or "").strip().lower() in strong_labels:
                    row[f"calibrationDowngradedFrom_{field}"] = original
                    row[field] = "CAUTION"
            row["calibrationDowngraded"] = True

        decision = evaluate_actionability(
            row,
            require_market_validation=True,
        )
        row.update(decision)
        stage_counts[decision["actionabilityStage"]] += 1
        counts[status] += 1
        if decision["actionable"] and is_promoted:
            row["promotionStatus"] = "promoted"
            promoted.append(row)
        else:
            row["actionable"] = False
            row["promotionStatus"] = "research_only"
            row["promotionReasons"] = list(dict.fromkeys(
                reasons
                + (decision["actionabilityReasons"] if not is_promoted else [])
                or ["market has not passed walk-forward validation"]
            ))
            rejection_reasons.update(row["promotionReasons"])
            actionability_reasons.update(decision["actionabilityReasons"])
            rejected.append(row)
    return {
        "version": VALIDATION_VERSION,
        "promoted": promoted,
        "rejected": rejected,
        "audit": {
            "version": VALIDATION_VERSION,
            "candidateCount": len(promoted) + len(rejected),
            "promotedCount": len(promoted),
            "rejectedCount": len(rejected),
            "statusCounts": dict(sorted(counts.items())),
            "rejectionReasons": dict(sorted(rejection_reasons.items())),
            "actionabilityVersion": ACTIONABILITY_VERSION,
            "actionabilityAudit": {
                "version": ACTIONABILITY_VERSION,
                "stageCounts": dict(sorted(stage_counts.items())),
                "rejectionReasons": dict(sorted(actionability_reasons.items())),
            },
        },
    }
