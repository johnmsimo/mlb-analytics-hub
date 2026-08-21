"""Phase 6.0 verified closing benchmark and market-skill control plane.

The app may claim model-vs-market accuracy only from paired, graded rows whose
pregame prediction receipt and two-way closing benchmark receipt both verify.
No row-level private Tracker data crosses the public API boundary.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import math
import os
from typing import Any

from continuous_learning import validate_observation
from odds_lineage import clv_eligibility
from value_engine import american_to_implied, devig_two_way


ACCURACY_CONTROL_PLANE_VERSION = "6.0"
CLOSING_BENCHMARK_VERSION = "6.0"
DEFAULT_WINDOW_DAYS = 90
MAX_WINDOW_DAYS = 366
MINIMUM_INDUSTRY_CLAIM_SAMPLE = 500
INDUSTRY_BEAT_CLOSE_TARGET = 0.524
SUPPORTED_MARKETS = frozenset({
    "batter_hits",
    "batter_home_runs",
    "batter_rbis",
    "batter_total_bases",
    "pitcher_strikeouts",
})
INVALID_BOOKS = frozenset({
    "", "model", "n/a", "na", "none", "projection", "research",
    "sim", "simulation", "unknown", "unpriced",
})
_CLOSING_BLOCKER_CODES = {
    "missing stable prediction identity": "missing_identity",
    "unsupported canonical market": "unsupported_market",
    "missing canonical over/under side": "missing_side",
    "closing line does not match prediction": "line_mismatch",
    "closing benchmark lacks a complete two-way price": "incomplete_two_way_price",
    "closing benchmark cannot be de-vigged": "devig_failed",
    "closing overround is outside the accepted range": "overround_out_of_range",
    "missing real closing sportsbook": "missing_sportsbook",
    "missing closing odds source": "missing_source",
    "missing closing quote timestamp": "missing_timestamp",
    "closing integrity receipt is not accepted": "integrity_rejected",
    "closing timestamp does not match integrity receipt": "integrity_timestamp_mismatch",
    "closing source does not match integrity receipt": "integrity_source_mismatch",
}


def _number(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        text_value = str(value or "").strip()
        if not text_value:
            return None
        try:
            parsed = datetime.fromisoformat(text_value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _side(value: Any) -> str | None:
    normalized = str(value or "").strip().lower()
    if normalized.startswith("over"):
        return "over"
    if normalized.startswith("under"):
        return "under"
    return None


def _american(value: Any) -> int | None:
    result = _number(value)
    if result is None or result == 0 or abs(result) < 100:
        return None
    return int(round(result))


def _prediction_snapshot(row: Mapping[str, Any]) -> Mapping[str, Any]:
    receipt = row.get("learningReceipt")
    if not isinstance(receipt, Mapping):
        return {}
    snapshot = receipt.get("snapshot")
    return snapshot if isinstance(snapshot, Mapping) else {}


def _closing_snapshot(
    row: Mapping[str, Any],
    *,
    closing_integrity: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    prediction = _prediction_snapshot(row)
    integrity = closing_integrity
    if integrity is None and isinstance(row.get("closingIntegrity"), Mapping):
        integrity = row.get("closingIntegrity")
    integrity = integrity or {}

    identity = str(
        prediction.get("identity")
        or row.get("id")
        or row.get("canonicalCandidateId")
        or ""
    ).strip()
    market = str(
        prediction.get("marketKey")
        or row.get("canonicalMarketKey")
        or row.get("marketKey")
        or ""
    ).strip().lower()
    side = _side(
        prediction.get("side")
        or row.get("canonicalSide")
        or row.get("recommendedSide")
        or row.get("side")
    )
    line = _number(prediction.get("line") if prediction.get("line") is not None else row.get("line"))
    over_price = _american(row.get("closingOverPrice"))
    under_price = _american(row.get("closingUnderPrice"))
    book = str(row.get("closingBook") or row.get("closingBookmaker") or "").strip()
    source = str(
        row.get("closingSource")
        or integrity.get("source")
        or ""
    ).strip()
    captured_at = _time(
        row.get("closingCapturedAt") or integrity.get("capturedAt")
    )
    quote_line = _number(row.get("closingLine"))

    fair = (
        devig_two_way(over_price, under_price, method="power")
        if over_price is not None and under_price is not None
        else None
    )
    fair_over = float(fair[0]) if fair else None
    fair_under = float(fair[1]) if fair else None
    selected_price = (
        over_price if side == "over" else under_price if side == "under" else None
    )
    selected_fair = (
        fair_over if side == "over" else fair_under if side == "under" else None
    )
    raw_over = american_to_implied(over_price) if over_price is not None else None
    raw_under = american_to_implied(under_price) if under_price is not None else None
    overround = (
        float(raw_over + raw_under)
        if raw_over is not None and raw_under is not None
        else None
    )

    blockers: list[str] = []
    if not identity:
        blockers.append("missing stable prediction identity")
    if market not in SUPPORTED_MARKETS:
        blockers.append("unsupported canonical market")
    if side is None:
        blockers.append("missing canonical over/under side")
    if line is None or quote_line is None or abs(line - quote_line) > 1e-9:
        blockers.append("closing line does not match prediction")
    if over_price is None or under_price is None:
        blockers.append("closing benchmark lacks a complete two-way price")
    if fair is None or selected_fair is None:
        blockers.append("closing benchmark cannot be de-vigged")
    if overround is None or not 0.98 <= overround <= 1.25:
        blockers.append("closing overround is outside the accepted range")
    if book.lower() in INVALID_BOOKS:
        blockers.append("missing real closing sportsbook")
    if not source:
        blockers.append("missing closing odds source")
    if captured_at is None:
        blockers.append("missing closing quote timestamp")
    if integrity.get("accepted") is not True or integrity.get("fresh") is not True:
        blockers.append("closing integrity receipt is not accepted")
    integrity_time = _time(integrity.get("capturedAt"))
    if captured_at is not None and integrity_time != captured_at:
        blockers.append("closing timestamp does not match integrity receipt")
    integrity_source = str(integrity.get("source") or "").strip().lower()
    if source and integrity_source and source.lower() != integrity_source:
        blockers.append("closing source does not match integrity receipt")

    snapshot = {
        "predictionIdentity": identity or None,
        "marketKey": market or None,
        "side": side,
        "line": line,
        "overPrice": over_price,
        "underPrice": under_price,
        "selectedPrice": selected_price,
        "fairOverProbability": round(fair_over, 6) if fair_over is not None else None,
        "fairUnderProbability": round(fair_under, 6) if fair_under is not None else None,
        "selectedFairProbability": round(selected_fair, 6) if selected_fair is not None else None,
        "overround": round(overround, 6) if overround is not None else None,
        "book": book or None,
        "source": source or None,
        "capturedAt": captured_at.isoformat() if captured_at else None,
    }
    return snapshot, list(dict.fromkeys(blockers))


def build_closing_benchmark_receipt(
    row: Mapping[str, Any],
    *,
    closing_integrity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Freeze a side-correct, two-way, de-vigged closing market benchmark."""

    snapshot, blockers = _closing_snapshot(
        row,
        closing_integrity=closing_integrity,
    )
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "version": CLOSING_BENCHMARK_VERSION,
        "benchmarkFingerprint": hashlib.sha256(encoded).hexdigest(),
        "snapshot": snapshot,
        "accepted": not blockers,
        "blockers": blockers,
        "outcomeFieldsIncluded": False,
        "modelProbabilityIncluded": False,
        "failClosed": True,
    }


def closing_benchmark_is_intact(row: Mapping[str, Any]) -> bool:
    receipt = row.get("closingBenchmarkReceipt")
    if not isinstance(receipt, Mapping):
        return False
    expected = build_closing_benchmark_receipt(row)
    return bool(
        receipt.get("version") == CLOSING_BENCHMARK_VERSION
        and receipt.get("benchmarkFingerprint") == expected["benchmarkFingerprint"]
        and receipt.get("snapshot") == expected["snapshot"]
        and receipt.get("accepted") is True
        and receipt.get("outcomeFieldsIncluded") is False
        and receipt.get("modelProbabilityIncluded") is False
        and receipt.get("failClosed") is True
        and expected["accepted"] is True
    )


def _prediction_rejection_codes(reasons: Iterable[Any]) -> set[str]:
    codes: set[str] = set()
    for value in reasons:
        reason = str(value or "").lower()
        if "missing phase 5.4" in reason:
            codes.add("missing_receipt")
        elif "fingerprint mismatch" in reason or "snapshot mismatch" in reason or "version mismatch" in reason:
            codes.add("receipt_tampered")
        elif "backfilled" in reason or "lookahead" in reason:
            codes.add("backfilled")
        elif "not a graded" in reason:
            codes.add("ungraded")
        elif "timestamp" in reason or "future" in reason:
            codes.add("invalid_timing")
        else:
            codes.add("ineligible")
    return codes or {"ineligible"}


def _ece(pairs: list[tuple[float, int]]) -> float | None:
    if not pairs:
        return None
    bins: dict[int, list[tuple[float, int]]] = defaultdict(list)
    for probability, outcome in pairs:
        bins[min(9, int(probability * 10))].append((probability, outcome))
    count = len(pairs)
    value = sum(
        len(values) / count * abs(
            sum(probability for probability, _ in values) / len(values)
            - sum(outcome for _, outcome in values) / len(values)
        )
        for values in bins.values()
    )
    return round(value, 6)


def _mean_interval(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"lower": None, "upper": None, "confidenceLevel": 0.95}
    mean = sum(values) / len(values)
    if len(values) < 2:
        return {
            "lower": round(mean, 6),
            "upper": round(mean, 6),
            "confidenceLevel": 0.95,
        }
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    margin = 1.96 * math.sqrt(variance / len(values))
    return {
        "lower": round(mean - margin, 6),
        "upper": round(mean + margin, 6),
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


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(rows)
    model_pairs = [(row["modelProbability"], row["outcome"]) for row in rows]
    market_pairs = [(row["marketProbability"], row["outcome"]) for row in rows]
    model_errors = [(probability - outcome) ** 2 for probability, outcome in model_pairs]
    market_errors = [(probability - outcome) ** 2 for probability, outcome in market_pairs]
    paired = [model - market for model, market in zip(model_errors, market_errors)]
    model_brier = sum(model_errors) / count if count else None
    market_brier = sum(market_errors) / count if count else None
    delta = sum(paired) / count if count else None
    interval = _mean_interval(paired)
    if not count:
        brier_evidence = "insufficient_sample"
    elif interval["upper"] is not None and interval["upper"] < 0:
        brier_evidence = "model_better"
    elif interval["lower"] is not None and interval["lower"] > 0:
        brier_evidence = "market_better"
    else:
        brier_evidence = "inconclusive"

    clv = [row for row in rows if row.get("beatClose") is not None]
    beat_close_count = sum(1 for row in clv if row["beatClose"] is True)
    beat_close_rate = beat_close_count / len(clv) if clv else None
    beat_close_interval = _wilson(beat_close_count, len(clv))

    if count < MINIMUM_INDUSTRY_CLAIM_SAMPLE:
        state = "insufficient_sample"
    elif brier_evidence != "model_better":
        state = "not_market_leading"
    elif len(clv) < MINIMUM_INDUSTRY_CLAIM_SAMPLE:
        state = "insufficient_clv_sample"
    elif (
        beat_close_rate is None
        or beat_close_rate <= INDUSTRY_BEAT_CLOSE_TARGET
        or beat_close_interval["lower"] is None
        or beat_close_interval["lower"] <= 0.5
    ):
        state = "not_market_leading"
    else:
        state = "market_leading"

    return {
        "state": state,
        "claimEligible": state == "market_leading",
        "pairedSampleSize": count,
        "modelBrier": round(model_brier, 6) if model_brier is not None else None,
        "closingMarketBrier": round(market_brier, 6) if market_brier is not None else None,
        "pairedBrierDelta": round(delta, 6) if delta is not None else None,
        "pairedBrierDeltaInterval": interval,
        "relativeBrierSkillVsClose": (
            round(1 - model_brier / market_brier, 6)
            if model_brier is not None and market_brier not in (None, 0)
            else None
        ),
        "brierEvidence": brier_evidence,
        "modelEce": _ece(model_pairs),
        "closingMarketEce": _ece(market_pairs),
        "clvGradedCount": len(clv),
        "beatCloseCount": beat_close_count,
        "beatCloseRate": round(beat_close_rate, 6) if beat_close_rate is not None else None,
        "beatCloseInterval": beat_close_interval,
        "minimumClaimSample": MINIMUM_INDUSTRY_CLAIM_SAMPLE,
        "beatCloseTarget": INDUSTRY_BEAT_CLOSE_TARGET,
    }


def build_accuracy_control_plane(
    entries: Iterable[Mapping[str, Any]],
    *,
    as_of: date | datetime | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> dict[str, Any]:
    """Build a public aggregate scorecard from strictly paired evidence."""

    if isinstance(as_of, datetime):
        checked_at = as_of.astimezone(timezone.utc)
        anchor = checked_at.date()
    else:
        anchor = as_of or datetime.now(timezone.utc).date()
        checked_at = datetime.combine(anchor, time.max, tzinfo=timezone.utc)
    window = max(1, min(int(window_days), MAX_WINDOW_DAYS))
    cutoff = anchor - timedelta(days=window - 1)
    accepted: list[dict[str, Any]] = []
    rejected: Counter[str] = Counter()
    rejected_rows = 0
    seen: set[str] = set()

    for row in entries or ():
        if not isinstance(row, Mapping):
            rejected_rows += 1
            rejected["malformed_row"] += 1
            continue
        observation, reasons = validate_observation(row, now=checked_at)
        if observation is None:
            rejected_rows += 1
            for code in _prediction_rejection_codes(reasons):
                rejected[f"prediction_{code}"] += 1
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
            receipt = row.get("closingBenchmarkReceipt")
            expected = build_closing_benchmark_receipt(row)
            if not isinstance(receipt, Mapping):
                rejected["closing_missing_receipt"] += 1
            elif expected["blockers"]:
                for blocker in set(expected["blockers"]):
                    rejected[
                        "closing_" + _CLOSING_BLOCKER_CODES.get(blocker, "ineligible")
                    ] += 1
            else:
                rejected["closing_receipt_tampered"] += 1
            continue
        benchmark = row["closingBenchmarkReceipt"]["snapshot"]
        market_probability = _number(benchmark.get("selectedFairProbability"))
        model_probability = _number(observation.get("servedProbability"))
        if market_probability is None or model_probability is None:
            rejected_rows += 1
            rejected["missing paired probability"] += 1
            continue
        clv_edge = _number(row.get("clvEdge"))
        beat_close = (
            clv_edge > 0
            if clv_edge is not None and clv_eligibility(row)
            else None
        )
        accepted.append({
            "marketKey": observation["marketKey"],
            "modelProbability": model_probability,
            "marketProbability": market_probability,
            "outcome": int(observation["outcome"]),
            "beatClose": beat_close,
        })

    by_market: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in accepted:
        by_market[row["marketKey"]].append(row)
    overall = _summary(accepted)
    return {
        "success": True,
        "version": ACCURACY_CONTROL_PLANE_VERSION,
        "state": overall["state"],
        "readOnly": True,
        "failClosed": True,
        "industryClaimMade": overall["claimEligible"],
        "window": {
            "days": window,
            "from": cutoff.isoformat(),
            "through": anchor.isoformat(),
        },
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "overall": overall,
        "byMarket": {
            market: _summary(values)
            for market, values in sorted(by_market.items())
        },
        "coverage": {
            "pairedEligibleCount": len(accepted),
            "rejectedCount": rejected_rows,
            "rejectedReasonCounts": dict(sorted(rejected.items())),
            "rawRowsIncluded": False,
        },
        "benchmark": {
            "version": CLOSING_BENCHMARK_VERSION,
            "type": "side_correct_two_way_power_devig_close",
            "requiresExactLine": True,
            "requiresAcceptedClosingIntegrity": True,
            "outcomesFrozenAfterPrediction": True,
        },
        "claimPolicy": {
            "minimumPairedSample": MINIMUM_INDUSTRY_CLAIM_SAMPLE,
            "minimumClvSample": MINIMUM_INDUSTRY_CLAIM_SAMPLE,
            "beatCloseTarget": INDUSTRY_BEAT_CLOSE_TARGET,
            "requiresModelBrierConfidenceUpperBelowZero": True,
            "requiresBeatCloseWilsonLowerAboveHalf": True,
        },
        "privateTrackerFieldsIncluded": False,
        "automaticModelChange": False,
        "automaticThresholdChange": False,
        "serverMutation": False,
    }


def install_accuracy_control_plane(app_module: Any) -> None:
    """Register the read-only Phase 6.0 scorecard once per Flask worker."""

    flask_app = app_module.app
    if getattr(flask_app, "_phase_600_accuracy_control_plane_installed", False):
        return
    from flask import Blueprint, jsonify, request
    from public_verification import load_tracker_entries

    blueprint = Blueprint("accuracy_control_plane", __name__)

    @blueprint.get("/api/accuracy/control-plane")
    def accuracy_control_plane_api():
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
        payload = build_accuracy_control_plane(
            load_tracker_entries(tracker_path),
            as_of=anchor,
            window_days=window,
        )
        response = jsonify(payload)
        response.headers["Cache-Control"] = "public, max-age=30, stale-while-revalidate=60"
        response.headers["X-Accuracy-Contract"] = ACCURACY_CONTROL_PLANE_VERSION
        return response

    flask_app.register_blueprint(blueprint)
    setattr(flask_app, "_phase_600_accuracy_control_plane_installed", True)
