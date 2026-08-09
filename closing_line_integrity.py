"""Small, dependency-free rules for auditable closing-line captures."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping


def _parse(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def accept_closing_capture(*, opening: Mapping[str, Any], closing: Mapping[str, Any], first_pitch: Any, now: Any = None, lead_minutes: int = 12, grace_minutes: int = 8) -> dict[str, Any]:
    """Return an auditable receipt; reject stale, self-referential, or incomplete closes."""
    fp = _parse(first_pitch); captured = _parse(closing.get("capturedAt")); opened = _parse(opening.get("capturedAt"))
    reason = None
    if fp is None or captured is None: reason = "missing_capture_time"
    elif captured < fp - timedelta(minutes=max(0, int(lead_minutes))) or captured > fp + timedelta(minutes=max(0, int(grace_minutes))): reason = "outside_first_pitch_window"
    elif not closing.get("price") or not closing.get("book"): reason = "missing_price_provenance"
    elif opened and captured <= opened: reason = "not_newer_than_opening"
    accepted = reason is None
    return {"accepted": accepted, "reason": reason, "capturedAt": closing.get("capturedAt"), "source": closing.get("source") or "odds_api_live", "fresh": accepted}
