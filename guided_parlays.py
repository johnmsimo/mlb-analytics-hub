"""Phase 5.10 read-only guided-parlay contract.

Guided parlays may use only recommendation legs that already carry the Phase
4.69 evidence receipt and a ready Phase 5.9 multi-book shopping receipt.  The
module compounds those verified single-leg inputs, explains the resulting
risk, and never represents multiplied single-leg prices as a sportsbook parlay
offer or an approved wager.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
import math
from typing import Any


GUIDED_PARLAY_VERSION = "5.10"
RECOMMENDATION_EVIDENCE_VERSION = "4.69"
MULTI_BOOK_SHOPPING_VERSION = "5.9"
MINIMUM_VERIFIED_LEGS = 2
MAXIMUM_GUIDED_LEGS = 4


def _number(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _american_to_decimal(value: Any) -> float | None:
    price = _number(value)
    if price is None or price == 0 or abs(price) < 100:
        return None
    return 1.0 + price / 100.0 if price > 0 else 1.0 + 100.0 / abs(price)


def _decimal_to_american(value: Any) -> int | None:
    decimal = _number(value)
    if decimal is None or decimal <= 1.0:
        return None
    if decimal >= 2.0:
        return round((decimal - 1.0) * 100.0)
    return round(-100.0 / (decimal - 1.0))


def _parse_time(value: Any) -> datetime | None:
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


def _same_number(left: Any, right: Any) -> bool:
    a, b = _number(left), _number(right)
    return a is not None and b is not None and abs(a - b) < 1e-9


def verify_guided_leg(row: Mapping[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    """Return a strict allowlisted leg or aggregate rejection reasons."""
    source = dict(row or {})
    reasons: list[str] = []
    candidate_id = str(source.get("canonicalCandidateId") or "").strip()
    fingerprint = str(source.get("canonicalFingerprint") or "").strip()
    if source.get("actionable") is not True:
        reasons.append("leg is not actionable")
    if str(source.get("actionabilityStage") or "").strip().lower() != "actionable":
        reasons.append("leg has not reached the actionable stage")
    if not candidate_id or not fingerprint:
        reasons.append("leg lacks canonical identity")

    evidence = source.get("evidenceReceipt")
    if not isinstance(evidence, Mapping):
        reasons.append("leg lacks recommendation evidence")
        evidence = {}
    if evidence.get("contractVersion") != RECOMMENDATION_EVIDENCE_VERSION:
        reasons.append("recommendation evidence version is not verified")
    if evidence.get("candidateId") != candidate_id:
        reasons.append("recommendation evidence candidate mismatch")
    if evidence.get("fingerprint") != fingerprint:
        reasons.append("recommendation evidence fingerprint mismatch")
    evidence_price = evidence.get("price")
    if not isinstance(evidence_price, Mapping):
        reasons.append("recommendation evidence lacks price lineage")
        evidence_price = {}
    if evidence_price.get("fresh") is not True:
        reasons.append("recommendation evidence price is not fresh")
    evidence_age = evidence_price.get("ageSeconds")
    if not isinstance(evidence_age, int) or not 0 <= evidence_age <= 900:
        reasons.append("recommendation evidence price age is invalid")
    if not _same_number(evidence_price.get("american"), source.get("canonicalPrice")):
        reasons.append("recommendation evidence price mismatch")
    if str(evidence_price.get("book") or "").strip() != str(
        source.get("canonicalBook") or ""
    ).strip():
        reasons.append("recommendation evidence sportsbook mismatch")

    shopping = source.get("multiBookShopping")
    if not isinstance(shopping, Mapping):
        reasons.append("leg lacks multi-book shopping evidence")
        shopping = {}
    if source.get("multiBookShoppingVersion") != MULTI_BOOK_SHOPPING_VERSION:
        reasons.append("leg multi-book version is not verified")
    if shopping.get("version") != MULTI_BOOK_SHOPPING_VERSION:
        reasons.append("multi-book receipt version is not verified")
    if shopping.get("state") != "ready":
        reasons.append("multi-book consensus is not ready")
    consensus = shopping.get("consensus")
    if not isinstance(consensus, Mapping):
        reasons.append("multi-book consensus is missing")
        consensus = {}
    accepted_count = consensus.get("acceptedBookCount")
    if not isinstance(accepted_count, int) or accepted_count < 2:
        reasons.append("leg lacks two fresh sportsbook quotes")
    price_shopping = shopping.get("priceShopping")
    if not isinstance(price_shopping, Mapping):
        reasons.append("leg lacks best-price evidence")
        price_shopping = {}
    quotes = price_shopping.get("quotes")
    if not isinstance(quotes, list) or len(quotes) < 2:
        reasons.append("leg lacks a verified quote set")
        quotes = []
    for quote in quotes:
        age = quote.get("ageSeconds") if isinstance(quote, Mapping) else None
        if not isinstance(age, int) or not 0 <= age <= 300:
            reasons.append("leg contains a stale or malformed quote")
            break

    probability = _number(
        source.get("canonicalProbability", source.get("modelProb"))
    )
    if probability is None or not 0.0 < probability < 1.0:
        reasons.append("leg lacks a valid model probability")
    best_price = price_shopping.get("bestAvailablePrice")
    best_book = str(price_shopping.get("bestAvailableBook") or "").strip()
    decimal_price = _american_to_decimal(best_price)
    if decimal_price is None or not best_book:
        reasons.append("leg lacks a verified best sportsbook price")
    if not any(
        isinstance(quote, Mapping)
        and str(quote.get("book") or "").strip() == best_book
        and _same_number(quote.get("selectedPrice"), best_price)
        for quote in quotes
    ):
        reasons.append("best price is not present in the verified quote set")
    selection = evidence.get("selection")
    if not isinstance(selection, Mapping):
        reasons.append("recommendation evidence lacks selection identity")
        selection = {}
    market = str(source.get("canonicalMarketKey") or source.get("marketKey") or "").strip()
    side = str(source.get("canonicalSide") or source.get("side") or "").strip()
    line = source.get("line")
    if selection.get("marketKey") != market or str(selection.get("side") or "") != side:
        reasons.append("recommendation evidence selection mismatch")
    if not _same_number(selection.get("line"), line):
        reasons.append("recommendation evidence line mismatch")
    game_pk = source.get("gamePk")
    player = str(source.get("player") or "").strip()
    if game_pk in (None, "") or not player or not market or not side:
        reasons.append("leg lacks complete selection metadata")
    if reasons:
        return None, list(dict.fromkeys(reasons))

    captured_at = _parse_time(price_shopping.get("capturedAt"))
    if captured_at is None:
        return None, ["leg lacks a valid best-price timestamp"]
    expires_at = (
        (captured_at + timedelta(seconds=300)).isoformat()
        if captured_at else None
    )
    return {
        "canonicalCandidateId": candidate_id,
        "canonicalFingerprint": fingerprint,
        "gamePk": game_pk,
        "player": player,
        "playerId": source.get("playerId"),
        "team": source.get("team"),
        "opp": source.get("opp"),
        "marketKey": market,
        "marketLabel": source.get("marketLabel") or market,
        "side": side,
        "line": line,
        "modelProbability": round(float(probability), 6),
        "bestPrice": best_price,
        "bestBook": best_book,
        "quoteCapturedAt": price_shopping.get("capturedAt"),
        "quoteExpiresAt": expires_at,
        "acceptedBookCount": accepted_count,
        "evidenceReceiptVersion": RECOMMENDATION_EVIDENCE_VERSION,
        "multiBookShoppingVersion": MULTI_BOOK_SHOPPING_VERSION,
        "verified": True,
    }, []


def _correlation_assessment(
    legs: list[dict[str, Any]],
    measured_pairs: Iterable[Mapping[str, Any]] | None,
) -> tuple[dict[str, Any], float]:
    warnings: list[dict[str, str]] = []
    same_game_pairs = 0
    factor = 1.0
    measured = list(measured_pairs or [])
    measured_by_key = {
        str(item.get("pairKey") or ""): item
        for item in measured
        if isinstance(item, Mapping) and item.get("verified") is True
    }
    matched_pairs = 0
    for index, left in enumerate(legs):
        for right in legs[index + 1:]:
            if str(left.get("gamePk")) == str(right.get("gamePk")):
                same_game_pairs += 1
                warnings.append({
                    "severity": "high",
                    "type": "same_game",
                    "message": (
                        f"{left['player']} and {right['player']} are in the same game; "
                        "their outcomes may not be independent."
                    ),
                })
                pair_key = "|".join(sorted((
                    f"{left.get('marketKey')}:{str(left.get('side') or '').lower()}",
                    f"{right.get('marketKey')}:{str(right.get('side') or '').lower()}",
                )))
                measured_pair = measured_by_key.get(pair_key)
                pair_factor = _number(
                    measured_pair.get("factor") if measured_pair else None
                )
                if pair_factor is not None and 0.50 <= pair_factor <= 1.50:
                    factor *= pair_factor
                    matched_pairs += 1
            if left.get("playerId") and left.get("playerId") == right.get("playerId"):
                warnings.append({
                    "severity": "critical",
                    "type": "same_player",
                    "message": (
                        f"Multiple legs depend on {left['player']}; overlapping outcomes "
                        "can amplify both upside and failure risk."
                    ),
                })
            elif left.get("marketKey") == right.get("marketKey"):
                warnings.append({
                    "severity": "medium",
                    "type": "shared_market",
                    "message": (
                        f"Two legs use {left['marketLabel']}; shared run environment or "
                        "scoring conditions can move them together."
                    ),
                })
    unresolved = same_game_pairs > matched_pairs
    factor = max(0.50, min(1.50, factor))
    if not warnings:
        warnings.append({
            "severity": "low",
            "type": "independence_assumption",
            "message": (
                "No same-game overlap was detected, but the combined probability still "
                "assumes the legs are independent."
            ),
        })
    return {
        "state": (
            "unresolved" if unresolved
            else "measured" if matched_pairs
            else "clear"
        ),
        "sameGamePairCount": same_game_pairs,
        "measuredPairCount": matched_pairs,
        "unresolvedPairCount": max(0, same_game_pairs - matched_pairs),
        "adjustmentFactor": round(factor, 6),
        "method": "pairwise_adjusted" if matched_pairs else "independence_assumption",
        "warnings": warnings,
    }, factor


def build_guided_parlay(
    rows: Iterable[Mapping[str, Any]],
    *,
    name: str,
    risk_tier: str,
    measured_correlation_pairs: Iterable[Mapping[str, Any]] | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Build a privacy-safe, unapproved guided-parlay receipt."""
    verified: list[dict[str, Any]] = []
    invalid_reasons: list[str] = []
    for row in rows or ():
        leg, reasons = verify_guided_leg(row)
        if leg is None:
            invalid_reasons.extend(reasons)
        else:
            verified.append(leg)
    duplicate_ids = len({leg["canonicalCandidateId"] for leg in verified}) != len(verified)
    if duplicate_ids:
        invalid_reasons.append("duplicate canonical leg")
    if len(verified) < MINIMUM_VERIFIED_LEGS:
        invalid_reasons.append("at least two verified legs are required")
    if len(verified) > MAXIMUM_GUIDED_LEGS:
        invalid_reasons.append("guided parlays are limited to four legs")
    generated = generated_at or datetime.now(timezone.utc)
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=timezone.utc)
    if invalid_reasons:
        return {
            "version": GUIDED_PARLAY_VERSION,
            "state": "unavailable",
            "name": name,
            "riskTier": risk_tier,
            "generatedAt": generated.astimezone(timezone.utc).isoformat(),
            "verifiedLegCount": len(verified),
            "legs": [],
            "correlation": None,
            "combinedRisk": None,
            "referencePrice": None,
            "decision": {
                "status": "withheld",
                "reviewRequired": True,
                "approved": False,
                "trackable": False,
                "reasons": list(dict.fromkeys(invalid_reasons)),
            },
            "readOnly": True,
            "serverMutation": False,
        }

    independent_probability = math.prod(leg["modelProbability"] for leg in verified)
    decimal_price = math.prod(
        _american_to_decimal(leg["bestPrice"]) or 1.0 for leg in verified
    )
    correlation, factor = _correlation_assessment(
        verified, measured_correlation_pairs,
    )
    combined_probability = max(
        0.0001, min(0.9999, independent_probability * factor)
    )
    at_least_one_miss = 1.0 - combined_probability
    expected_value = combined_probability * decimal_price - 1.0
    risk_level = (
        "extreme" if len(verified) >= 4 or combined_probability < 0.12
        else "high" if len(verified) >= 3 or combined_probability < 0.25
        else "elevated"
    )
    unresolved = correlation["state"] == "unresolved"
    reasons = []
    if unresolved:
        reasons.append("same-game correlation lacks a measured adjustment")
    reasons.append("human review is required before tracking")
    return {
        "version": GUIDED_PARLAY_VERSION,
        "state": "review_required" if unresolved else "ready",
        "name": name,
        "riskTier": risk_tier,
        "generatedAt": generated.astimezone(timezone.utc).isoformat(),
        "verifiedLegCount": len(verified),
        "legs": verified,
        "correlation": correlation,
        "combinedRisk": {
            "level": risk_level,
            "independentProbability": round(independent_probability, 6),
            "combinedProbability": round(combined_probability, 6),
            "atLeastOneLegMissProbability": round(at_least_one_miss, 6),
            "expectedValue": round(expected_value, 6),
            "explanations": [
                (
                    f"All {len(verified)} verified legs must win; the current combined "
                    f"estimate is {combined_probability * 100:.1f}%."
                ),
                (
                    f"The estimated chance that at least one leg misses is "
                    f"{at_least_one_miss * 100:.1f}%."
                ),
                (
                    "Reference odds multiply the best current single-leg prices. They are "
                    "not a verified sportsbook parlay offer and may not be jointly available."
                ),
            ],
        },
        "referencePrice": {
            "decimalOdds": round(decimal_price, 3),
            "americanOdds": _decimal_to_american(decimal_price),
            "bookOfferVerified": False,
            "source": "multiplied_best_single_leg_prices",
        },
        "decision": {
            "status": "correlation_review_required" if unresolved else "review_required",
            "reviewRequired": True,
            "approved": False,
            "trackable": not unresolved,
            "reasons": reasons,
        },
        "readOnly": True,
        "serverMutation": False,
    }
