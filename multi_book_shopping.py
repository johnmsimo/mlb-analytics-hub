"""Phase 5.9 read-only presentation contract for multi-book shopping.

The Phase 5.3 decision engine remains the source of truth for quote admission,
consensus, expected value, and no-bet decisions.  This module exposes a strict,
bankroll-free subset for recommendation surfaces and never changes candidate
actionability, ranking, model probability, or promotion state.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from decision_intelligence import evaluate_decision


MULTI_BOOK_SHOPPING_VERSION = "5.9"
SOURCE_DECISION_VERSION = "5.3.0"
_PROVIDER_STATES = {
    "ready", "computing", "partial", "stale", "failed", "unavailable",
}


def _provider_health(value: Mapping[str, Any] | None) -> dict[str, Any]:
    source = dict(value or {})
    state = str(source.get("state") or "unavailable").strip().lower()
    if state not in _PROVIDER_STATES:
        state = "unavailable"
    return {
        "provider": str(source.get("provider") or "The Odds API"),
        "state": state,
        "configured": bool(source.get("configured")),
        "capturedAt": source.get("capturedAt"),
        "eventCount": int(source.get("eventCount") or 0),
        "fetchedEventCount": int(source.get("fetchedEventCount") or 0),
        "degradedEventCount": int(source.get("degradedEventCount") or 0),
        "message": str(source.get("message") or ""),
    }


def _shopping_state(
    *,
    accepted_books: int,
    required_books: int,
    provider_state: str,
    rejected_quotes: int,
) -> str:
    if accepted_books >= required_books:
        if provider_state == "ready" and rejected_quotes == 0:
            return "ready"
        return "partial"
    if accepted_books > 0:
        return "partial"
    if provider_state == "stale":
        return "stale"
    if provider_state in {"computing", "partial"}:
        return provider_state
    return "unavailable"


def build_multi_book_shopping(
    candidate: Mapping[str, Any],
    quotes: Iterable[Mapping[str, Any]],
    *,
    provider_health: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return the fail-closed Phase 5.9 card payload.

    Rejected quote contents are not returned.  Only their aggregate count and
    the Phase 5.3 decision reasons are exposed, so a degraded provider cannot
    leak malformed upstream rows into recommendation responses.
    """
    decision = evaluate_decision(candidate, quotes, now=now)
    provider = _provider_health(provider_health)
    consensus = decision.get("consensus") or {}
    price = decision.get("priceShopping") or {}
    accepted_quotes = []
    for quote in consensus.get("quotes") or []:
        accepted_quotes.append({
            "book": quote.get("book"),
            "source": quote.get("source"),
            "capturedAt": quote.get("capturedAt"),
            "ageSeconds": quote.get("ageSeconds"),
            "line": quote.get("line"),
            "overPrice": quote.get("overPrice"),
            "underPrice": quote.get("underPrice"),
            "selectedPrice": quote.get("selectedPrice"),
            "fairProbability": quote.get("fairProbability"),
        })
    accepted_quotes.sort(
        key=lambda quote: (
            -(float(quote.get("selectedPrice") or -10000)),
            str(quote.get("book") or "").lower(),
        )
    )
    accepted_count = int(consensus.get("acceptedBookCount") or 0)
    required_count = max(2, int(consensus.get("requiredBooks") or 2))
    rejected_count = int(consensus.get("rejectedQuoteCount") or 0)
    state = _shopping_state(
        accepted_books=accepted_count,
        required_books=required_count,
        provider_state=provider["state"],
        rejected_quotes=rejected_count,
    )
    return {
        "version": MULTI_BOOK_SHOPPING_VERSION,
        "sourceDecisionVersion": decision.get("decisionIntelligenceVersion"),
        "state": state,
        "reviewRequired": True,
        "changesRecommendation": False,
        "providerHealth": provider,
        "consensus": {
            "requiredBooks": required_count,
            "acceptedBookCount": accepted_count,
            "rejectedQuoteCount": rejected_count,
            "fairProbability": (
                consensus.get("fairProbability")
                if accepted_count >= required_count else None
            ),
            "spread": (
                consensus.get("spread")
                if accepted_count >= required_count else None
            ),
            "maximumSpread": consensus.get("maximumSpread"),
        },
        "priceShopping": {
            "bestAvailableBook": price.get("bestAvailableBook"),
            "bestAvailablePrice": price.get("bestAvailablePrice"),
            "capturedAt": price.get("capturedAt"),
            "quotes": accepted_quotes,
        },
        "decision": {
            "status": decision.get("decisionStatus"),
            "qualifiedForReview": bool(decision.get("decisionQualified")),
            "approved": False,
            "reasons": list(decision.get("decisionReasons") or []),
            "modelEdge": (
                decision.get("modelEdge")
                if accepted_count >= required_count else None
            ),
            "expectedValue": (
                decision.get("expectedValue")
                if accepted_count >= required_count else None
            ),
            "thresholds": decision.get("thresholds"),
            "checkedAt": decision.get("decisionCheckedAt"),
            "fingerprint": decision.get("decisionFingerprint"),
        },
    }
