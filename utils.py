"""Shared small utility helpers used across MLB Analytics Hub modules."""

from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def _safe_num(v, default=0.0):
    try:
        if v in (None, "", "-.--", ".---", "N/A"):
            return default
        return float(v)
    except Exception:
        return default


def _safe_float(v, default=0.0):
    return float(_safe_num(v, default))


def _safe_int(v, default=0):
    try:
        return int(float(_safe_num(v, default)))
    except Exception:
        return default


def _normalize_date_str(value, fallback=None):
    """Return a YYYY-MM-DD string from loose date/datetime inputs."""
    fb = (fallback or datetime.now(ET).strftime("%Y-%m-%d")).strip()
    raw = str(value or "").strip()
    if not raw:
        return fb

    token = raw.split("T", 1)[0].strip().replace("/", "-")
    try:
        return datetime.strptime(token, "%Y-%m-%d").strftime("%Y-%m-%d")
    except Exception:
        return fb


__all__ = ["ET", "_normalize_date_str", "_safe_float", "_safe_int", "_safe_num"]
