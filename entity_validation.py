"""Fail-closed entity and source-data validation for recommendation candidates."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Any, Mapping


ENTITY_VALIDATION_VERSION = "4.56"

_ALLOWED_HANDS = {
    "L": "L", "LEFT": "L", "LEFT-HANDED": "L",
    "R": "R", "RIGHT": "R", "RIGHT-HANDED": "R",
    "S": "S", "SWITCH": "S", "SWITCH-HITTER": "S",
}
_ALLOWED_LINEUP_STATES = {"confirmed", "projected"}
_INVALID_LINEUP_STATES = {
    "inactive", "out", "scratched", "unknown", "missing", "unavailable",
}
_INVALID_ASSET_STATES = {
    "failed", "missing", "invalid", "blocked", "unavailable", "404", "error",
}
_STALE_PITCHER_STATES = {"stale", "old", "outdated", "unconfirmed", "unknown"}

_MARKET_ALIASES = {
    "batterhits": "batter_hits",
    "batter_hits": "batter_hits",
    "batter_rbi": "batter_rbis",
    "batter_rbis": "batter_rbis",
    "player_hits": "batter_hits",
    "player hits": "batter_hits",
    "hits": "batter_hits",
    "player_home_runs": "batter_home_runs",
    "player_total_bases": "batter_total_bases",
    "player_rbi": "batter_rbis",
    "player_runs": "batter_runs_scored",
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
_TEAM_KEYS = (
    "team", "teamAbbreviation", "team_abbr", "teamCode", "team_code",
)
_PLAYER_TEAM_KEYS = (
    "playerTeam", "player_team", "playerTeamAbbreviation",
    "player_team_abbreviation", "playerTeamCode", "player_team_code",
)
_TEAM_ID_KEYS = ("teamId", "team_id")
_PLAYER_TEAM_ID_KEYS = ("playerTeamId", "player_team_id")
_HAND_KEYS = (
    "handedness", "playerHand", "player_hand", "playerHandedness",
    "player_handedness", "batterHand", "batter_hand", "batterHandedness",
    "pitcherHand", "pitcher_hand", "pitcherHandedness", "throws",
)
_LINEUP_KEYS = ("lineupStatus", "lineup_status", "lineupSource")
_ASSET_KEYS = (
    "logoUrl", "logoURL", "teamLogo", "teamLogoUrl", "team_logo_url",
    "playerImage", "playerImageUrl", "assetUrl", "asset_url",
)
_ASSET_STATUS_KEYS = ("assetStatus", "logoStatus", "imageStatus", "asset_status")
_MARKET_KEYS = (
    "marketKey", "market_key", "marketType", "market_type", "propType",
    "prop_type", "market", "stat",
)
_LINE_KEYS = (
    "line", "propLine", "prop_line", "marketLine", "market_line",
    "totalLine", "total_line", "strikeoutLine", "strikeout_line",
)

# These bounds apply only to explicitly named single-game or rate fields. They
# do not constrain cumulative season totals whose scale is not known here.
_STAT_BOUNDS = {
    "hits": (0.0, 5.0),
    "homeRuns": (0.0, 4.0),
    "home_runs": (0.0, 4.0),
    "rbi": (0.0, 10.0),
    "rbis": (0.0, 10.0),
    "runs": (0.0, 10.0),
    "runsScored": (0.0, 10.0),
    "stolenBases": (0.0, 5.0),
    "stolen_bases": (0.0, 5.0),
    "strikeouts": (0.0, 20.0),
    "outsRecorded": (0.0, 27.0),
    "outs_recorded": (0.0, 27.0),
    "earnedRuns": (0.0, 20.0),
    "earned_runs": (0.0, 20.0),
    "hitsAllowed": (0.0, 30.0),
    "hits_allowed": (0.0, 30.0),
    "walks": (0.0, 15.0),
    "totalBases": (0.0, 20.0),
    "total_bases": (0.0, 20.0),
    "battingAverage": (0.0, 1.0),
    "batting_average": (0.0, 1.0),
    "onBasePercentage": (0.0, 1.0),
    "on_base_percentage": (0.0, 1.0),
    "sluggingPercentage": (0.0, 4.0),
    "slugging_percentage": (0.0, 4.0),
    "era": (0.0, 100.0),
    "whip": (0.0, 20.0),
}


@dataclass(frozen=True)
class EntityValidationPolicy:
    maximum_probable_pitcher_age_seconds: int = 43_200


def _first(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None and value != "":
            return value
    return None


def _number(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            parsed = datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
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


def _utc_now(now: datetime | None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _normalized_team(value: Any) -> str:
    return "".join(char for char in str(value or "").upper() if char.isalnum())


def _normalized_market(value: Any) -> str:
    text = " ".join(str(value or "").strip().lower().split())
    key = _MARKET_ALIASES.get(text, text)
    return key.replace("-", "_").replace(" ", "_")


def _normalized_hand(value: Any) -> str | None:
    text = str(value or "").strip().upper().replace(" ", "-")
    return _ALLOWED_HANDS.get(text)


def _add_reason(reasons: list[str], reason: str) -> None:
    if reason not in reasons:
        reasons.append(reason)


def _valid_asset(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return (
        (text.startswith("https://") or text.startswith("http://")
         or text.startswith("/"))
        and text not in {"#", "/"}
    )


def validate_entity_data(
    source: Mapping[str, Any],
    *,
    market_key: str = "",
    role: str = "",
    now: datetime | None = None,
    policy: EntityValidationPolicy | None = None,
) -> dict[str, Any]:
    """Return an auditable source-data verdict without guessing missing fields."""
    policy = policy or EntityValidationPolicy()
    checked_at = _utc_now(now)
    row = dict(source)
    reasons: list[str] = []
    checks: dict[str, str] = {}

    expected_team = _first(row, *_TEAM_KEYS)
    player_team = _first(row, *_PLAYER_TEAM_KEYS)
    if expected_team is not None and player_team is not None:
        if _normalized_team(expected_team) != _normalized_team(player_team):
            _add_reason(reasons, "player/team identity mismatch")
            checks["identity"] = "rejected"
        else:
            checks["identity"] = "valid"
    else:
        checks["identity"] = "not_provided"

    expected_team_id = _first(row, *_TEAM_ID_KEYS)
    player_team_id = _first(row, *_PLAYER_TEAM_ID_KEYS)
    if expected_team_id is not None and player_team_id is not None:
        if str(expected_team_id).strip() != str(player_team_id).strip():
            _add_reason(reasons, "player/team identity mismatch")
            checks["identity"] = "rejected"

    lineup_value = _first(row, *_LINEUP_KEYS)
    if role in {"batter", "pitcher"} and lineup_value is not None:
        lineup = str(lineup_value).strip().lower().replace("_", "-")
        if lineup not in _ALLOWED_LINEUP_STATES or lineup in _INVALID_LINEUP_STATES:
            _add_reason(reasons, "invalid lineup status")
            checks["lineup"] = "rejected"
        else:
            checks["lineup"] = "valid"
    else:
        checks["lineup"] = "not_provided"

    for field, bounds in _STAT_BOUNDS.items():
        if field not in row or row[field] in (None, ""):
            continue
        value = _number(row[field])
        if value is None or value < bounds[0] or value > bounds[1]:
            _add_reason(reasons, f"suspicious or impossible stat: {field}")
            checks[f"stat:{field}"] = "rejected"
        else:
            checks[f"stat:{field}"] = "valid"

    hand_values = []
    for key in _HAND_KEYS:
        if key in row and row[key] not in (None, ""):
            normalized = _normalized_hand(row[key])
            if normalized is None:
                _add_reason(reasons, "invalid handedness")
                checks["handedness"] = "rejected"
            else:
                hand_values.append(normalized)
    if hand_values and len(set(hand_values)) > 1:
        _add_reason(reasons, "conflicting handedness evidence")
        checks["handedness"] = "rejected"
    elif hand_values:
        checks["handedness"] = "valid"
    else:
        checks["handedness"] = "not_provided"

    probable_stale = _first(
        row, "probablePitcherStale", "probable_pitcher_stale",
        "pitcherStale", "pitcher_stale",
    )
    probable_status = _first(
        row, "probablePitcherStatus", "probable_pitcher_status",
    )
    if probable_stale is True or str(probable_status or "").strip().lower() in _STALE_PITCHER_STATES:
        _add_reason(reasons, "probable pitcher is stale")
        checks["probablePitcher"] = "rejected"
    else:
        updated_at = _parse_datetime(_first(
            row, "probablePitcherUpdatedAt", "probable_pitcher_updated_at",
            "pitcherUpdatedAt", "pitcher_updated_at",
        ))
        starts_at = _parse_datetime(_first(
            row, "gameStartIso", "gameDate", "startTime", "start_time",
        ))
        if updated_at is not None and starts_at is not None:
            age = max(0.0, (checked_at - updated_at).total_seconds())
            if age > policy.maximum_probable_pitcher_age_seconds and starts_at > checked_at:
                _add_reason(reasons, "probable pitcher is stale")
                checks["probablePitcher"] = "rejected"
            else:
                checks["probablePitcher"] = "valid"
        else:
            checks["probablePitcher"] = "not_provided"

    asset_values = [
        row[key] for key in _ASSET_KEYS
        if key in row and row[key] not in (None, "")
    ]
    asset_status = _first(row, *_ASSET_STATUS_KEYS)
    required = row.get("assetRequired") is True or row.get("asset_required") is True
    if (
        (asset_status is not None
         and str(asset_status).strip().lower() in _INVALID_ASSET_STATES)
        or any(not _valid_asset(value) for value in asset_values)
        or (required and not asset_values)
    ):
        _add_reason(reasons, "missing or invalid asset")
        checks["assets"] = "rejected"
    elif asset_values or required:
        checks["assets"] = "valid" if asset_values else "not_provided"
    else:
        checks["assets"] = "not_provided"

    market_values = [
        row[key] for key in _MARKET_KEYS
        if key in row and row[key] not in (None, "")
    ]
    normalized_markets = {_normalized_market(value) for value in market_values}
    if market_key:
        normalized_markets.add(_normalized_market(market_key))
    if len(normalized_markets) > 1:
        _add_reason(reasons, "inconsistent market names")
        checks["market"] = "rejected"
    else:
        checks["market"] = "valid" if normalized_markets else "not_provided"

    line_values = []
    for key in _LINE_KEYS:
        if key in row and row[key] not in (None, ""):
            line_values.append((key, _number(row[key])))
    numeric_lines = [value for _, value in line_values]
    if any(value is None for _, value in line_values):
        _add_reason(reasons, "inconsistent market lines")
        checks["line"] = "rejected"
    elif numeric_lines and max(numeric_lines) - min(numeric_lines) > 1e-9:
        _add_reason(reasons, "inconsistent market lines")
        checks["line"] = "rejected"
    else:
        checks["line"] = "valid" if numeric_lines else "not_provided"

    return {
        "version": ENTITY_VALIDATION_VERSION,
        "checkedAt": checked_at.isoformat(),
        "status": "valid" if not reasons else "rejected",
        "valid": not reasons,
        "reasons": reasons,
        "checks": checks,
    }
