"""Canonical eligibility contract for every surfaced MLB betting candidate.

Projection and research rows may still exist without passing this contract, but
only candidates marked ``actionable`` may be promoted as picks, value plays, or
parlay legs.  The module has no Flask or application imports so every producer
and consumer can share the exact same decision boundary.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import re
from typing import Any, Iterable, Mapping

from entity_validation import (
    ENTITY_VALIDATION_VERSION,
    validate_entity_data,
)


INTEGRITY_VERSION = "4.37"

BATTER_MARKETS = {
    "batter_hits",
    "batter_total_bases",
    "batter_home_runs",
    "batter_rbis",
    "batter_runs_scored",
    "batter_hits_runs_rbis",
    "batter_stolen_bases",
}
PITCHER_MARKETS = {
    "pitcher_strikeouts",
    "pitcher_outs_recorded",
    "pitcher_earned_runs",
    "pitcher_hits_allowed",
    "pitcher_walks",
}
TEAM_MARKETS = {
    "h2h", "totals", "f5_h2h", "f5_totals", "nrfi", "yrfi",
}
SUPPORTED_MARKETS = BATTER_MARKETS | PITCHER_MARKETS | TEAM_MARKETS
SIMULATION_REQUIRED_MARKETS = {"batter_hits", "pitcher_strikeouts", "h2h"}

_MARKET_ALIASES = {
    "batterhits": "batter_hits",
    "batter_rbi": "batter_rbis",
    "player_hits": "batter_hits",
    "player hits": "batter_hits",
    "hits": "batter_hits",
    "player_home_runs": "batter_home_runs",
    "player_total_bases": "batter_total_bases",
    "player_rbi": "batter_rbis",
    "player_runs": "batter_runs_scored",
    "player_hits_runs_rbis": "batter_hits_runs_rbis",
    "pitcher strikeouts": "pitcher_strikeouts",
    "strikeouts": "pitcher_strikeouts",
    "pitcher ks": "pitcher_strikeouts",
    "pitcher_strikeouts": "pitcher_strikeouts",
    "hitter_hits": "batter_hits",
    "game_winner": "h2h",
    "game_moneyline": "h2h",
    "moneyline": "h2h",
    "game winner": "h2h",
    "game_total": "totals",
    "total": "totals",
}
_PITCHER_POSITIONS = {"P", "SP", "RP", "CP"}
_BATTER_POSITIONS = {
    "C", "1B", "2B", "3B", "SS", "LF", "CF", "RF", "OF", "DH",
    "UT", "UTIL",
}
_VALID_LINEUP_STATES = {"confirmed", "projected"}
_INVALID_BOOK_NAMES = {
    "model", "n/a", "na", "none", "projection", "research", "sim",
    "simulation", "unknown", "unpriced",
}
_NON_ACTIONABLE_GAME_TOKENS = {
    "final", "completed", "game over", "live", "in progress", "delayed",
    "postponed", "cancelled", "canceled", "suspended",
}
_UPCOMING_GAME_TOKENS = {"preview", "scheduled", "pre-game", "pregame"}


@dataclass(frozen=True)
class CandidateIntegrityPolicy:
    maximum_odds_age_seconds: int = 900
    minimum_simulation_trials: int = 750
    minimum_edge: float = 0.0


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


def _utc_now(now: datetime | None = None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _utc_now(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _utc_now(parsed)


def american_implied_probability(price: Any) -> float | None:
    value = _number(price)
    if value is None or value == 0:
        return None
    return (
        100.0 / (value + 100.0)
        if value > 0
        else -value / (-value + 100.0)
    )


def canonical_market_key(row: Mapping[str, Any]) -> str:
    raw = _first(
        row, "canonicalMarketKey", "marketKey", "market_key", "market",
        "marketType", "propType", "stat", "intelligenceCategory", "category",
    )
    key = re.sub(r"\s+", " ", str(raw or "").strip().lower())
    if key in _MARKET_ALIASES:
        return _MARKET_ALIASES[key]
    return key.replace("-", "_").replace(" ", "_")


def market_role(market_key: str) -> str:
    if market_key in BATTER_MARKETS:
        return "batter"
    if market_key in PITCHER_MARKETS:
        return "pitcher"
    if market_key in TEAM_MARKETS:
        return "team"
    return "unknown"


def _probability(row: Mapping[str, Any]) -> float | None:
    value = _number(_first(
        row, "blendedProb", "adjProb", "probability", "winProb", "prob",
    ))
    if value is None:
        return None
    value = value / 100.0 if value > 1.0 else value
    return value if 0.0 < value < 1.0 else None


def _selected_price_and_book(
    row: Mapping[str, Any], side: str,
) -> tuple[float | None, str | None, float | None]:
    is_under = side.lower() == "under"
    if is_under:
        selected_price = _first(
            row, "bestAvailablePrice", "marketPrice", "bestUnderPrice",
            "best_under_price", "price", "underPrice",
        )
        selected_book = _first(
            row, "bestAvailableBook", "bookmaker", "bestUnderBook",
            "best_under_book", "book", "underBook",
        )
        opposite_price = _first(row, "bestOverPrice", "best_over_price")
    else:
        selected_price = _first(
            row, "bestAvailablePrice", "marketPrice", "bestOverPrice",
            "best_over_price", "bestPrice", "price", "overPrice",
        )
        selected_book = _first(
            row, "bestAvailableBook", "bookmaker", "bestOverBook",
            "best_over_book", "bestBook", "book", "overBook",
        )
        opposite_price = _first(
            row, "bestUnderPrice", "best_under_price", "underPrice",
        )
    return _number(selected_price), (
        str(selected_book).strip() if selected_book is not None else None
    ), _number(opposite_price)


def _candidate_identity(row: Mapping[str, Any], market_key: str, side: str) -> str:
    identity = "|".join(str(value or "").strip().lower() for value in (
        row.get("gamePk"),
        row.get("playerId") or row.get("team") or row.get("player"),
        market_key,
        row.get("line"),
        side,
    ))
    return "candidate:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _game_is_upcoming(
    status: str, abstract_state: str, starts_at: datetime | None, now: datetime,
) -> bool:
    state = f"{status} {abstract_state}".strip().lower()
    if any(token in state for token in _NON_ACTIONABLE_GAME_TOKENS):
        return False
    if starts_at is not None and starts_at <= now:
        return False
    return (
        any(token in state for token in _UPCOMING_GAME_TOKENS)
        or (not state and starts_at is not None and starts_at > now)
    )


def evaluate_candidate(
    source: Mapping[str, Any],
    *,
    now: datetime | None = None,
    policy: CandidateIntegrityPolicy | None = None,
) -> dict[str, Any]:
    """Return a normalized copy with one auditable actionable verdict."""
    policy = policy or CandidateIntegrityPolicy()
    checked_at = _utc_now(now)
    row = dict(source)
    reasons: list[str] = []

    market_key = canonical_market_key(row)
    role = market_role(market_key)
    side = str(_first(row, "recommendedSide", "side", "sideLabel") or "Over").strip()
    row["canonicalMarketKey"] = market_key
    row["playerRole"] = str(row.get("playerRole") or role).strip().lower()
    row["canonicalSide"] = side
    row["canonicalCandidateId"] = _candidate_identity(row, market_key, side)

    entity_validation = validate_entity_data(
        row, market_key=market_key, role=role, now=checked_at,
    )
    row["entityValidation"] = entity_validation
    row["entityValidationVersion"] = ENTITY_VALIDATION_VERSION
    reasons.extend(entity_validation["reasons"])

    if market_key not in SUPPORTED_MARKETS:
        reasons.append("unsupported market")
    if row["playerRole"] != role:
        reasons.append("player role does not match market")

    position = str(_first(row, "playerPosition", "position", "pos") or "").strip().upper()
    row["playerPosition"] = position or None
    if role == "batter" and position not in _BATTER_POSITIONS:
        reasons.append("invalid batter position")
    elif role == "pitcher" and position not in _PITCHER_POSITIONS:
        reasons.append("invalid pitcher position")

    status = str(_first(row, "gameStatus", "game_status", "scheduleStatus") or "").strip()
    abstract_state = str(_first(
        row, "gameAbstractState", "abstractGameState", "gameState",
    ) or "").strip()
    starts_at = _parse_datetime(_first(
        row, "gameStartIso", "gameDate", "startTime", "start_time",
    ))
    row["gameStatus"] = status or abstract_state or None
    row["gameStartIso"] = starts_at.isoformat() if starts_at else None
    if starts_at is None:
        reasons.append("missing game start time")
    if not _game_is_upcoming(status, abstract_state, starts_at, checked_at):
        reasons.append("game is not upcoming")

    lineup_status = str(_first(
        row, "lineupStatus", "lineup_status", "lineupSource",
    ) or ("not_applicable" if role == "team" else "missing")).strip().lower()
    row["lineupStatus"] = lineup_status
    if role in {"batter", "pitcher"} and lineup_status not in _VALID_LINEUP_STATES:
        reasons.append("lineup is neither confirmed nor projected")

    probability = _probability(row)
    row["canonicalProbability"] = (
        round(probability, 6) if probability is not None else None
    )
    if probability is None:
        reasons.append("missing valid model probability")

    line = _number(row.get("line"))
    if line is None:
        reasons.append("missing sportsbook line")
    else:
        row["line"] = line

    price, book, opposite_price = _selected_price_and_book(row, side)
    row["canonicalPrice"] = price
    row["canonicalBook"] = book
    row["oppositePrice"] = opposite_price
    if price is None or price == 0 or abs(price) < 100:
        reasons.append("missing sportsbook price")
    if not book or book.lower() in _INVALID_BOOK_NAMES:
        reasons.append("missing real sportsbook")
    if opposite_price is None or opposite_price == 0 or abs(opposite_price) < 100:
        reasons.append("missing opposite-side price for de-vigging")

    quoted_implied = american_implied_probability(price)
    opposite_implied = american_implied_probability(opposite_price)
    fair_probability = None
    if quoted_implied is not None and opposite_implied is not None:
        total_implied = quoted_implied + opposite_implied
        if total_implied > 0:
            fair_probability = quoted_implied / total_implied
    edge = (
        probability - fair_probability
        if probability is not None and fair_probability is not None
        else None
    )
    row["quotedMarketImplied"] = (
        round(quoted_implied, 6) if quoted_implied is not None else None
    )
    row["marketFairProbability"] = (
        round(fair_probability, 6) if fair_probability is not None else None
    )
    row["quotedEdge"] = row.get("edge")
    row["canonicalEdge"] = round(edge, 6) if edge is not None else None
    # Missing price evidence already has a precise rejection reason above.
    # Do not also describe the same row as a negative-edge candidate: without
    # a real two-sided quote there is no de-vigged edge to evaluate.
    if edge is not None and edge <= policy.minimum_edge:
        reasons.append("no positive edge after de-vigging")
    elif edge is not None:
        row["edge"] = round(edge, 6)

    odds_at = _parse_datetime(_first(
        row, "oddsUpdatedAt", "oddsObservedAt", "marketUpdatedAt", "savedAt",
        "timestamp",
    ))
    row["oddsUpdatedAt"] = odds_at.isoformat() if odds_at else None
    odds_age = (
        max(0.0, (checked_at - odds_at).total_seconds()) if odds_at else None
    )
    row["oddsAgeSeconds"] = round(odds_age, 1) if odds_age is not None else None
    if odds_at is None:
        reasons.append("missing odds freshness timestamp")
    elif odds_age is not None and odds_age > policy.maximum_odds_age_seconds:
        reasons.append("sportsbook price is stale")

    model_version = str(_first(
        row, "modelVersion", "candidateModelVersion", "modelArtifactVersion",
    ) or "").strip()
    row["modelVersion"] = model_version or None
    if not model_version:
        reasons.append("missing model version")

    if market_key in SIMULATION_REQUIRED_MARKETS:
        simulation_version = str(_first(
            row, "matchupSimulationVersion", "simulationVersion",
        ) or "").strip()
        trials = _number(_first(row, "gameSimN", "simulationTrials", "mc_n_sims"))
        row["simulationVersion"] = simulation_version or None
        row["simulationTrials"] = int(trials or 0)
        if not simulation_version:
            reasons.append("missing simulation version")
        if trials is None or trials < policy.minimum_simulation_trials:
            reasons.append("insufficient simulation coverage")

    grade = str(row.get("grade") or "pending").strip().lower()
    if grade not in {"", "pending", "open"}:
        reasons.append("candidate is already graded")

    row["integrityVersion"] = INTEGRITY_VERSION
    row["integrityCheckedAt"] = checked_at.isoformat()
    row["integrityReasons"] = list(dict.fromkeys(reasons))
    row["integrityStatus"] = "eligible" if not reasons else "rejected"
    row["actionable"] = not reasons
    return row


def _dedupe_rank(row: Mapping[str, Any]) -> tuple[float, float, float]:
    price = _number(row.get("canonicalPrice")) or -10000.0
    edge = _number(row.get("canonicalEdge")) or -1.0
    timestamp = _parse_datetime(row.get("oddsUpdatedAt"))
    return (
        edge,
        price,
        timestamp.timestamp() if timestamp is not None else 0.0,
    )


def evaluate_candidates(
    sources: Iterable[Mapping[str, Any]],
    *,
    now: datetime | None = None,
    policy: CandidateIntegrityPolicy | None = None,
) -> dict[str, Any]:
    """Evaluate and de-duplicate a candidate collection with audit metadata."""
    evaluated = [
        evaluate_candidate(source, now=now, policy=policy) for source in sources
    ]
    by_identity: dict[str, dict[str, Any]] = {}
    duplicate_count = 0
    for row in evaluated:
        identity = row["canonicalCandidateId"]
        current = by_identity.get(identity)
        if current is None:
            by_identity[identity] = row
            continue
        duplicate_count += 1
        if _dedupe_rank(row) > _dedupe_rank(current):
            by_identity[identity] = row

    rows = list(by_identity.values())
    eligible = [row for row in rows if row.get("actionable") is True]
    rejected = [row for row in rows if row.get("actionable") is not True]
    reason_counts = Counter(
        reason for row in rejected for reason in row.get("integrityReasons", [])
    )
    # Reasons remain fully auditable and may overlap, while this primary
    # distribution gives product surfaces a mutually exclusive candidate
    # count. It therefore always sums to rejectedCount.
    primary_reason_counts = Counter(
        (row.get("integrityReasons") or ["other validation gate"])[0]
        for row in rejected
    )
    return {
        "version": INTEGRITY_VERSION,
        "eligible": eligible,
        "rejected": rejected,
        "audit": {
            "version": INTEGRITY_VERSION,
            "sourceCount": len(evaluated),
            "uniqueCount": len(rows),
            "eligibleCount": len(eligible),
            "rejectedCount": len(rejected),
            "duplicateCount": duplicate_count,
            "rejectionReasons": dict(sorted(reason_counts.items())),
            "primaryRejectionReasons": dict(
                sorted(primary_reason_counts.items())
            ),
            "entityValidationVersion": ENTITY_VALIDATION_VERSION,
            "entityRejectedCount": sum(
                row.get("entityValidation", {}).get("valid") is False
                for row in rows
            ),
            "entityRejectionReasons": dict(sorted(Counter(
                reason
                for row in rows
                for reason in row.get("entityValidation", {}).get("reasons", [])
            ).items())),
        },
    }
