"""Canonical opening/current/closing odds provenance and CLV denominator rules.

Phase 4.55 keeps price history auditable: every snapshot carries the same
identity fields, current prices expose freshness, and CLV is counted only when
a graded row has a verified opening/closing pair.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any, Iterable, Mapping

ODDS_LINEAGE_VERSION = "4.55"
MINIMUM_CLV_CLAIM_OBSERVATIONS = 500


def _number(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _parse(value: Any) -> datetime | None:
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


def _first(source: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if source.get(key) is not None:
            return source.get(key)
    return None


def normalize_odds_snapshot(
    snapshot: Mapping[str, Any] | None,
    *,
    role: str,
    line: Any = None,
    reference_at: Any = None,
    max_age_seconds: int = 900,
) -> dict[str, Any]:
    source = dict(snapshot or {})
    captured_at = _first(source, "capturedAt", "captured_at", "timestamp")
    captured = _parse(captured_at)
    price = _number(_first(source, "price", "americanOdds", "american_odds"))
    snapshot_line = _number(_first(source, "line", "marketLine"))
    if snapshot_line is None:
        snapshot_line = _number(line)
    implied = _number(_first(
        source, "impliedProbability", "implied", "fairProbability",
    ))
    book = str(_first(source, "book", "bookmaker", "sportsbook") or "").strip()
    snapshot_source = str(
        _first(source, "source", "oddsSource", "provider") or ""
    ).strip()

    missing: list[str] = []
    if price is None or price == 0:
        missing.append("price")
    if snapshot_line is None:
        missing.append("line")
    if not book:
        missing.append("book")
    if captured is None:
        missing.append("capturedAt")
    if not snapshot_source:
        missing.append("source")

    freshness: dict[str, Any] = {
        "status": "timestamped" if captured is not None else "missing",
        "ageSeconds": None,
        "maxAgeSeconds": max(0, int(max_age_seconds)),
    }
    if role == "current":
        reference = _parse(reference_at) or datetime.now(timezone.utc)
        if captured is None:
            freshness["status"] = "missing"
        else:
            age = max(0, int((reference - captured).total_seconds()))
            freshness["ageSeconds"] = age
            freshness["status"] = (
                "fresh" if age <= max(0, int(max_age_seconds)) else "stale"
            )

    return {
        "role": role,
        "price": price,
        "impliedProbability": implied,
        "line": snapshot_line,
        "book": book or None,
        "capturedAt": captured.isoformat() if captured else captured_at,
        "source": snapshot_source or None,
        "freshness": freshness,
        "valid": not missing,
        "missing": missing,
    }


def build_odds_lineage(
    *,
    line: Any = None,
    opening: Mapping[str, Any] | None = None,
    current: Mapping[str, Any] | None = None,
    closing: Mapping[str, Any] | None = None,
    closing_integrity: Mapping[str, Any] | None = None,
    reference_at: Any = None,
    max_age_seconds: int = 900,
) -> dict[str, Any]:
    snapshots = {
        "opening": normalize_odds_snapshot(
            opening, role="opening", line=line, max_age_seconds=max_age_seconds,
        ),
        "current": normalize_odds_snapshot(
            current, role="current", line=line, reference_at=reference_at,
            max_age_seconds=max_age_seconds,
        ),
        "closing": normalize_odds_snapshot(
            closing, role="closing", line=line, max_age_seconds=max_age_seconds,
        ),
    }
    receipt_verified = bool(
        isinstance(closing_integrity, Mapping)
        and closing_integrity.get("accepted") is True
        and closing_integrity.get("fresh") is True
    )
    clv_eligible = bool(
        snapshots["opening"]["valid"]
        and snapshots["closing"]["valid"]
        and receipt_verified
    )
    if clv_eligible:
        status, reason = "verified", None
    elif closing_integrity and closing_integrity.get("accepted") is False:
        status, reason = "rejected", closing_integrity.get("reason")
    elif not snapshots["opening"]["valid"] or not snapshots["closing"]["valid"]:
        status, reason = "incomplete", "opening_or_closing_snapshot_incomplete"
    else:
        status, reason = "unverified", "missing_verified_closing_receipt"

    return {
        "version": ODDS_LINEAGE_VERSION,
        "line": _number(line),
        "snapshots": snapshots,
        "clvEligible": clv_eligible,
        "clvStatus": status,
        "clvReason": reason,
        "currentFreshness": snapshots["current"]["freshness"],
    }


def clv_eligibility(row: Mapping[str, Any]) -> bool:
    lineage = row.get("oddsLineage")
    return bool(
        isinstance(lineage, Mapping)
        and lineage.get("version") == ODDS_LINEAGE_VERSION
        and lineage.get("clvEligible") is True
    )


def _is_graded(row: Mapping[str, Any]) -> bool:
    value = str(
        row.get("grade") or row.get("result") or row.get("outcome") or ""
    ).strip().lower()
    return value in {
        "win", "won", "w", "hit", "correct", "loss", "lost", "l",
        "miss", "incorrect", "push",
    }


def clv_summary(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    values = list(rows)
    graded = [row for row in values if _is_graded(row)]
    eligible = [row for row in values if clv_eligibility(row)]
    clv_graded = [
        row for row in graded
        if clv_eligibility(row) and _number(row.get("clvEdge")) is not None
    ]
    clv_values = [_number(row.get("clvEdge")) for row in clv_graded]
    clv_values = [value for value in clv_values if value is not None]
    beat_close = sum(1 for value in clv_values if value > 0)
    count = len(clv_values)
    return {
        "version": ODDS_LINEAGE_VERSION,
        "gradedCount": len(graded),
        "clvEligibleCount": len(eligible),
        "clvGradedCount": count,
        "clvDenominator": "clvGradedCount",
        "beatCloseCount": beat_close,
        "beatCloseRate": round(beat_close / count, 4) if count else None,
        "averageClv": round(sum(clv_values) / count, 4) if count else None,
        "minimumClaimObservations": MINIMUM_CLV_CLAIM_OBSERVATIONS,
        "claimStatus": (
            "verified"
            if count >= MINIMUM_CLV_CLAIM_OBSERVATIONS
            else "insufficient_sample"
        ),
        "claimEligible": count >= MINIMUM_CLV_CLAIM_OBSERVATIONS,
    }
