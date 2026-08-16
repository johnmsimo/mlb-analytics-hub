"""Phase 5.3 fail-closed sportsbook consensus and decision intelligence.

Consensus prices are decision evidence only. They never enter model training,
change a champion probability, place a wager, or bypass human review.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from statistics import median
from typing import Any

from value_engine import devig_two_way, expected_value, kelly_fraction


DECISION_INTELLIGENCE_VERSION = "5.3.0"
SUPPORTED_DECISION_MARKETS = {
    "batter_hits",
    "batter_rbis",
    "batter_total_bases",
}
_INVALID_BOOKS = {
    "", "model", "n/a", "na", "none", "projection", "research",
    "simulation", "unknown", "unpriced",
}


@dataclass(frozen=True)
class DecisionPolicy:
    minimum_books: int = 2
    maximum_quote_age_seconds: int = 300
    maximum_consensus_spread: float = 0.08
    kelly_fraction: float = 0.25
    maximum_stake_pct: float = 0.01
    unit_pct: float = 0.01


_MARKET_THRESHOLDS = {
    "batter_hits": {"minimumEdge": 0.025, "minimumExpectedValue": 0.030},
    "batter_rbis": {"minimumEdge": 0.035, "minimumExpectedValue": 0.040},
    "batter_total_bases": {
        "minimumEdge": 0.030,
        "minimumExpectedValue": 0.035,
    },
}


def _number(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _probability(value: Any) -> float | None:
    number = _number(value)
    if number is None:
        return None
    number = number / 100.0 if number > 1.0 else number
    return number if 0.0 < number < 1.0 else None


def _parse_time(value: Any) -> datetime | None:
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


def _utc(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _first(source: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = source.get(key)
        if value is not None and value != "":
            return value
    return None


def _side(value: Any) -> str | None:
    side = str(value or "").strip().lower()
    if side.startswith("over"):
        return "over"
    if side.startswith("under"):
        return "under"
    return None


def _market(source: Mapping[str, Any]) -> str:
    raw = _first(source, "canonicalMarketKey", "marketKey", "market_key", "market")
    return str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")


def _american_price(value: Any) -> float | None:
    price = _number(value)
    if price is None or price == 0 or abs(price) < 100:
        return None
    return price


def _normalize_quote(
    source: Mapping[str, Any],
    *,
    side: str,
    line: float,
    now: datetime,
    policy: DecisionPolicy,
) -> tuple[dict[str, Any] | None, list[str]]:
    quote = dict(source)
    reasons: list[str] = []
    book = str(_first(quote, "book", "bookmaker", "sportsbook") or "").strip()
    source_name = str(_first(quote, "source", "provider", "oddsSource") or "").strip()
    captured = _parse_time(_first(
        quote, "capturedAt", "captured_at", "oddsUpdatedAt", "timestamp",
    ))
    quote_line = _number(_first(quote, "line", "marketLine"))
    over_price = _american_price(_first(
        quote, "overPrice", "over_price", "bestOverPrice",
    ))
    under_price = _american_price(_first(
        quote, "underPrice", "under_price", "bestUnderPrice",
    ))
    if book.lower() in _INVALID_BOOKS:
        reasons.append("missing real sportsbook")
    if not source_name:
        reasons.append("missing odds source")
    if captured is None:
        reasons.append("missing quote timestamp")
        age = None
    else:
        age = max(0, int((now - captured).total_seconds()))
        if age > policy.maximum_quote_age_seconds:
            reasons.append("sportsbook quote is stale")
    if quote_line is None or abs(quote_line - line) > 1e-9:
        reasons.append("quote line does not match candidate")
    if over_price is None or under_price is None:
        reasons.append("quote lacks a complete two-way price")
    devigged = (
        devig_two_way(over_price, under_price, method="power")
        if over_price is not None and under_price is not None
        else None
    )
    if devigged is None:
        reasons.append("quote cannot be de-vigged")
    if reasons:
        return None, list(dict.fromkeys(reasons))
    fair_probability = devigged[0] if side == "over" else devigged[1]
    selected_price = over_price if side == "over" else under_price
    return {
        "book": book,
        "source": source_name,
        "capturedAt": captured.isoformat(),
        "ageSeconds": age,
        "line": quote_line,
        "overPrice": over_price,
        "underPrice": under_price,
        "selectedPrice": selected_price,
        "fairProbability": round(float(fair_probability), 6),
    }, []


def market_thresholds(market: str) -> dict[str, float] | None:
    threshold = _MARKET_THRESHOLDS.get(str(market or "").strip().lower())
    return dict(threshold) if threshold else None


def evaluate_decision(
    candidate: Mapping[str, Any],
    quotes: Iterable[Mapping[str, Any]],
    *,
    now: datetime | None = None,
    policy: DecisionPolicy | None = None,
) -> dict[str, Any]:
    """Build a consensus decision receipt that remains review-only."""
    policy = policy or DecisionPolicy()
    checked_at = _utc(now)
    row = dict(candidate)
    market = _market(row)
    side = _side(_first(row, "canonicalSide", "recommendedSide", "side"))
    line = _number(row.get("line"))
    model_probability = _probability(_first(
        row, "canonicalProbability", "blendedProb", "adjProb",
        "probability", "winProb", "modelProbability",
    ))
    reasons: list[str] = []
    if market not in SUPPORTED_DECISION_MARKETS:
        reasons.append("market has no Phase 5.3 decision threshold")
    if side is None:
        reasons.append("candidate side must be over or under")
    if line is None:
        reasons.append("missing candidate line")
    if model_probability is None:
        reasons.append("missing valid model probability")
    if row.get("marketGatePromoted") is not True:
        reasons.append("market validation has not promoted this market")
    if str(row.get("marketGateStatus") or "").lower() != "promoted":
        reasons.append("market gate is not promoted")
    if str(row.get("marketSideGateStatus") or "").lower() != "promoted":
        reasons.append("market side gate is not promoted")

    normalized: list[dict[str, Any]] = []
    rejected_quotes: list[dict[str, Any]] = []
    if side is not None and line is not None:
        for index, quote in enumerate(quotes or ()):
            value, quote_reasons = _normalize_quote(
                quote,
                side=side,
                line=line,
                now=checked_at,
                policy=policy,
            )
            if value is not None:
                normalized.append(value)
            else:
                rejected_quotes.append({
                    "index": index,
                    "book": str(_first(quote, "book", "bookmaker", "sportsbook") or ""),
                    "reasons": quote_reasons,
                })

    by_book: dict[str, dict[str, Any]] = {}
    duplicate_books = 0
    for quote in sorted(normalized, key=lambda value: value["capturedAt"]):
        key = quote["book"].strip().lower()
        if key in by_book:
            duplicate_books += 1
        by_book[key] = quote
    accepted = list(by_book.values())
    if len(accepted) < max(2, int(policy.minimum_books)):
        reasons.append(
            f"consensus requires at least {max(2, int(policy.minimum_books))} fresh books"
        )

    fair_values = [quote["fairProbability"] for quote in accepted]
    consensus = float(median(fair_values)) if fair_values else None
    spread = (
        max(fair_values) - min(fair_values)
        if len(fair_values) >= 2 else None
    )
    if spread is not None and spread > policy.maximum_consensus_spread:
        reasons.append("sportsbook consensus dispersion exceeds limit")
    best = max(accepted, key=lambda quote: quote["selectedPrice"]) if accepted else None
    thresholds = market_thresholds(market)
    edge = (
        model_probability - consensus
        if model_probability is not None and consensus is not None
        else None
    )
    ev = (
        expected_value(model_probability, best["selectedPrice"])
        if model_probability is not None and best is not None
        else None
    )
    if thresholds is not None:
        if edge is None or edge < thresholds["minimumEdge"]:
            reasons.append("model edge is inside the no-bet zone")
        if ev is None or ev < thresholds["minimumExpectedValue"]:
            reasons.append("expected value is inside the no-bet zone")

    reasons = list(dict.fromkeys(reasons))
    qualified = not reasons
    full_kelly = (
        kelly_fraction(model_probability, best["selectedPrice"])
        if qualified and model_probability is not None and best is not None
        else 0.0
    )
    stake_pct = min(
        max(0.0, full_kelly * policy.kelly_fraction),
        policy.maximum_stake_pct,
    ) if qualified else 0.0
    stake_units = (
        stake_pct / policy.unit_pct if policy.unit_pct > 0 else 0.0
    )
    bankroll = _number(row.get("bankroll"))
    stake_dollars = (
        round(bankroll * stake_pct, 2)
        if bankroll is not None and bankroll > 0 and qualified else None
    )

    fingerprint_payload = {
        "candidate": {
            "id": row.get("canonicalCandidateId"),
            "market": market,
            "side": side,
            "line": line,
            "modelProbability": model_probability,
        },
        "quotes": sorted(accepted, key=lambda value: value["book"].lower()),
        "version": DECISION_INTELLIGENCE_VERSION,
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "decisionIntelligenceVersion": DECISION_INTELLIGENCE_VERSION,
        "decisionStatus": "qualified" if qualified else "no_bet",
        "decisionQualified": qualified,
        "decisionReviewRequired": True,
        "decisionApproved": False,
        "actionable": False,
        "decisionReasons": reasons,
        "decisionCheckedAt": checked_at.isoformat(),
        "decisionFingerprint": fingerprint,
        "marketKey": market,
        "side": side,
        "line": line,
        "modelProbability": (
            round(model_probability, 6) if model_probability is not None else None
        ),
        "thresholds": thresholds,
        "consensus": {
            "requiredBooks": max(2, int(policy.minimum_books)),
            "acceptedBookCount": len(accepted),
            "duplicateBookCount": duplicate_books,
            "rejectedQuoteCount": len(rejected_quotes),
            "fairProbability": round(consensus, 6) if consensus is not None else None,
            "spread": round(spread, 6) if spread is not None else None,
            "maximumSpread": policy.maximum_consensus_spread,
            "quotes": sorted(accepted, key=lambda value: value["book"].lower()),
            "rejectedQuotes": rejected_quotes,
        },
        "priceShopping": {
            "bestAvailableBook": best["book"] if best else None,
            "bestAvailablePrice": best["selectedPrice"] if best else None,
            "capturedAt": best["capturedAt"] if best else None,
        },
        "modelEdge": round(edge, 6) if edge is not None else None,
        "expectedValue": round(ev, 6) if ev is not None else None,
        "stakePreview": {
            "eligibleAfterReview": qualified,
            "fullKellyPct": round(full_kelly, 6),
            "kellyFraction": policy.kelly_fraction,
            "maximumStakePct": policy.maximum_stake_pct,
            "stakePct": round(stake_pct, 6),
            "stakeUnits": round(stake_units, 3),
            "stakeDollars": stake_dollars,
        },
    }


def evaluate_decisions(
    candidates: Iterable[Mapping[str, Any]],
    quotes_by_candidate: Mapping[str, Iterable[Mapping[str, Any]]],
    *,
    now: datetime | None = None,
    policy: DecisionPolicy | None = None,
) -> dict[str, Any]:
    """Evaluate a batch and expose no-bet reasons without auto-approval."""
    decisions = []
    for candidate in candidates:
        key = str(candidate.get("canonicalCandidateId") or "")
        decisions.append(evaluate_decision(
            candidate,
            quotes_by_candidate.get(key, ()),
            now=now,
            policy=policy,
        ))
    reason_counts = Counter(
        reason
        for decision in decisions
        if not decision["decisionQualified"]
        for reason in decision["decisionReasons"]
    )
    return {
        "version": DECISION_INTELLIGENCE_VERSION,
        "decisions": decisions,
        "audit": {
            "candidateCount": len(decisions),
            "qualifiedForReviewCount": sum(
                decision["decisionQualified"] for decision in decisions
            ),
            "noBetCount": sum(
                not decision["decisionQualified"] for decision in decisions
            ),
            "approvedCount": 0,
            "actionableCount": 0,
            "noBetReasons": dict(sorted(reason_counts.items())),
        },
    }
