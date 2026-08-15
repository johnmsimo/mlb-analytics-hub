"""Aggregate-only learning contract for verified My Hub tracker decisions."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


VERIFIED_DECISION_LEARNING_VERSION = "4.72"
VERIFIED_DECISION_SOURCE = "my_hub_verified_decision_draft"
MINIMUM_GRADED_SAMPLE = 10
_GRADED = frozenset({"win", "loss", "push"})


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        return None
    return parsed


def build_verified_decision_learning(
    entries: Iterable[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """Return source-attributed aggregates without returning tracker rows."""

    rows = [
        row
        for row in (entries or ())
        if isinstance(row, Mapping)
        and str(row.get("source") or "") == VERIFIED_DECISION_SOURCE
    ]
    graded = [
        row for row in rows
        if str(row.get("grade") or "").strip().lower() in _GRADED
    ]
    wins = sum(
        1 for row in graded
        if str(row.get("grade") or "").strip().lower() == "win"
    )
    losses = sum(
        1 for row in graded
        if str(row.get("grade") or "").strip().lower() == "loss"
    )
    pushes = sum(
        1 for row in graded
        if str(row.get("grade") or "").strip().lower() == "push"
    )
    decided = wins + losses

    risk = round(sum(
        max(0.0, _number(row.get("stakeDollars")) or 0.0)
        for row in graded
    ), 2)
    profit = round(sum(
        _number(row.get("profitDollars")) or 0.0
        for row in graded
    ), 2)
    units = round(sum(
        _number(row.get("profitUnits")) or 0.0
        for row in graded
    ), 3)
    clv_values = [
        value
        for row in graded
        if (value := _number(row.get("clvEdge"))) is not None
    ]

    graded_count = len(graded)
    sample_ready = graded_count >= MINIMUM_GRADED_SAMPLE
    if not rows:
        state = "no_verified_decisions"
    elif not graded:
        state = "awaiting_outcomes"
    elif sample_ready:
        state = "sample_ready"
    else:
        state = "learning"

    return {
        "version": VERIFIED_DECISION_LEARNING_VERSION,
        "source": VERIFIED_DECISION_SOURCE,
        "state": state,
        "decisionCount": len(rows),
        "pendingCount": len(rows) - graded_count,
        "gradedCount": graded_count,
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "hitRate": round(wins / decided, 4) if decided else None,
        "riskDollars": risk,
        "profitDollars": profit,
        "profitUnits": units,
        "roi": round(profit / risk, 4) if risk > 0 else None,
        "clvCount": len(clv_values),
        "averageClv": (
            round(sum(clv_values) / len(clv_values), 4)
            if clv_values else None
        ),
        "beatCloseRate": (
            round(sum(1 for value in clv_values if value > 0) / len(clv_values), 4)
            if clv_values else None
        ),
        "minimumGradedSample": MINIMUM_GRADED_SAMPLE,
        "sampleReady": sample_ready,
        "aggregateOnly": True,
        "rowsIncluded": False,
        "metricsAreDescriptive": True,
        "failClosed": True,
    }
