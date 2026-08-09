"""Canonical stages for MLB recommendation actionability."""
from __future__ import annotations

from collections import Counter
import math
from typing import Any, Iterable, Mapping


ACTIONABILITY_VERSION = "4.52"
ACTIONABILITY_STAGES = (
    "Research", "Projected", "Priced", "Validated", "Actionable", "Graded",
)
_INVALID_BOOK_NAMES = {
    "model", "n/a", "na", "none", "projection", "research", "sim",
    "simulation", "unknown", "unpriced",
}
_GRADED_VALUES = {
    "win", "won", "w", "loss", "lost", "l", "push", "void",
    "hit", "miss", "correct", "incorrect",
}


def _number(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _first(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None and value != "":
            return value
    return None


def _has_model_probability(row: Mapping[str, Any]) -> bool:
    value = _number(_first(
        row, "canonicalProbability", "blendedProb", "adjProb",
        "probability", "winProb", "modelProbability",
    ))
    if value is None:
        return False
    value = value / 100.0 if value > 1.0 else value
    return math.isfinite(value) and 0.0 < value < 1.0


def _has_real_price(row: Mapping[str, Any]) -> bool:
    value = _number(_first(
        row, "canonicalPrice", "bestAvailablePrice", "marketPrice",
        "bestOverPrice", "best_over_price", "bestUnderPrice",
        "best_under_price", "price",
    ))
    return value is not None and math.isfinite(value) and value != 0 and abs(value) >= 100


def _has_real_book(row: Mapping[str, Any]) -> bool:
    value = _first(
        row, "canonicalBook", "bestAvailableBook", "bestBook",
        "bestOverBook", "best_over_book", "bestUnderBook",
        "best_under_book", "bookmaker", "book",
    )
    value = str(value or "").strip().lower()
    return bool(value) and value not in _INVALID_BOOK_NAMES


def _has_positive_edge(row: Mapping[str, Any]) -> bool:
    value = _number(_first(
        row, "canonicalEdge", "edge", "estimatedEdgePct", "edgePct",
    ))
    if value is None or not math.isfinite(value):
        return False
    value = value / 100.0 if abs(value) > 1.0 else value
    return value > 0.0


def _market_gate_passed(row: Mapping[str, Any]) -> bool:
    return (
        row.get("marketGatePromoted") is True
        and str(row.get("marketGateStatus") or "").lower() == "promoted"
        and str(row.get("marketSideGateStatus") or "").lower() == "promoted"
    )


def _is_graded(row: Mapping[str, Any]) -> bool:
    value = str(
        row.get("grade") or row.get("result") or row.get("outcome") or ""
    ).strip().lower()
    return value in _GRADED_VALUES


def evaluate_actionability(
    source: Mapping[str, Any],
    *,
    require_market_validation: bool = False,
) -> dict[str, Any]:
    """Return an auditable stage and a fail-closed actionable boolean."""
    row = dict(source)
    reasons = list(dict.fromkeys(
        str(reason)
        for reason in (
            row.get("actionabilityReasons")
            or row.get("promotionReasons")
            or row.get("marketGateReasons")
            or row.get("integrityReasons")
            or []
        )
        if str(reason).strip()
    ))
    has_model = _has_model_probability(row)
    has_price = _has_real_price(row)
    has_book = _has_real_book(row)
    gate_present = (
        require_market_validation
        or "marketGatePromoted" in row
        or "promotionStatus" in row
    )
    if _is_graded(row):
        stage, actionable = "Graded", False
        reasons.append("candidate is already graded")
    elif not has_model:
        stage, actionable = "Research", False
        reasons.append("missing valid model probability")
    elif not has_price or not has_book:
        stage, actionable = "Projected", False
        if not has_price:
            reasons.append("missing sportsbook price")
        if not has_book:
            reasons.append("missing real sportsbook")
    elif gate_present and not _market_gate_passed(row):
        stage, actionable = "Priced", False
        reasons.append("market validation has not promoted this market side")
    elif row.get("actionable") is not True:
        stage, actionable = "Validated", False
        reasons.append("candidate integrity or downstream evidence gate rejected row")
    elif not _has_positive_edge(row):
        stage, actionable = "Validated", False
        reasons.append("no positive edge after validation")
    else:
        stage, actionable = "Actionable", True
    reasons = list(dict.fromkeys(reasons))
    return {
        "actionabilityVersion": ACTIONABILITY_VERSION,
        "actionabilityStage": stage,
        "actionable": actionable,
        "actionabilityReasons": reasons,
        "actionability": {
            "version": ACTIONABILITY_VERSION,
            "stage": stage,
            "actionable": actionable,
            "reasons": reasons,
        },
    }


def filter_actionable(
    sources: Iterable[Mapping[str, Any]],
    *,
    require_market_validation: bool = True,
) -> dict[str, Any]:
    """Return only Actionable rows plus stage and rejection audits."""
    actionable, rejected = [], []
    stage_counts, reason_counts = Counter(), Counter()
    for source in sources:
        decision = evaluate_actionability(
            source, require_market_validation=require_market_validation,
        )
        row = dict(source)
        row.update(decision)
        stage_counts[decision["actionabilityStage"]] += 1
        if decision["actionable"]:
            actionable.append(row)
        else:
            rejected.append(row)
            reason_counts.update(decision["actionabilityReasons"])
    return {
        "version": ACTIONABILITY_VERSION,
        "actionable": actionable,
        "rejected": rejected,
        "audit": {
            "version": ACTIONABILITY_VERSION,
            "sourceCount": len(actionable) + len(rejected),
            "actionableCount": len(actionable),
            "rejectedCount": len(rejected),
            "stageCounts": dict(sorted(stage_counts.items())),
            "rejectionReasons": dict(sorted(reason_counts.items())),
        },
    }
