"""Aggregate-only learning contracts for verified My Hub tracker decisions."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


VERIFIED_DECISION_LEARNING_VERSION = "4.72"
VERIFIED_DECISION_MARKET_LEARNING_VERSION = "4.73"
VERIFIED_DECISION_SOURCE = "my_hub_verified_decision_draft"
MINIMUM_GRADED_SAMPLE = 10
SUPPORTED_MARKETS = (
    "batter_hits",
    "batter_total_bases",
    "batter_home_runs",
    "batter_rbis",
    "pitcher_strikeouts",
)
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


def _aggregate(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
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
    }


def _build_market_learning(
    rows: list[Mapping[str, Any]],
) -> dict[str, Any]:
    markets = []
    for market_key in SUPPORTED_MARKETS:
        market_rows = [
            row for row in rows
            if str(row.get("marketKey") or "") == market_key
        ]
        if not market_rows:
            continue
        markets.append({
            "marketKey": market_key,
            **_aggregate(market_rows),
        })

    graded_count = sum(item["gradedCount"] for item in markets)
    if not markets:
        state = "no_market_history"
    elif not graded_count:
        state = "awaiting_outcomes"
    elif any(item["sampleReady"] for item in markets):
        state = "sample_ready"
    else:
        state = "learning"

    return {
        "version": VERIFIED_DECISION_MARKET_LEARNING_VERSION,
        "state": state,
        "supportedMarkets": list(SUPPORTED_MARKETS),
        "marketCount": len(markets),
        "markets": markets,
        "minimumGradedSamplePerMarket": MINIMUM_GRADED_SAMPLE,
        "aggregateOnly": True,
        "trackerRowsIncluded": False,
        "rankingEnabled": False,
        "preferenceMutation": False,
        "recommendation": False,
        "metricsAreDescriptive": True,
        "failClosed": True,
    }


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
    return {
        "version": VERIFIED_DECISION_LEARNING_VERSION,
        "source": VERIFIED_DECISION_SOURCE,
        **_aggregate(rows),
        "aggregateOnly": True,
        "rowsIncluded": False,
        "metricsAreDescriptive": True,
        "failClosed": True,
        "marketLearning": _build_market_learning(rows),
    }
