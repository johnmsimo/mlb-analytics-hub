"""
savant_arsenal.py
═══════════════════════════════════════════════════════════════════════
Fetches per-pitch-type arsenal stats (wOBA, Whiff%, usage, velo, spin)
from Baseball Savant for a given pitcher MLBAM ID.

Caches results in memory for the lifetime of the process to avoid
hammering Savant on every request.

USAGE (from app.py or any route):
    from savant_arsenal import get_arsenal_stats
    data = get_arsenal_stats(mlbam_id=592789)   # e.g. Gerrit Cole

Returns a list of dicts, one per pitch type:
    [
        {
            "pitch_type":  "FF",
            "pitch_name":  "4-Seam Fastball",
            "usage_pct":   54.2,
            "velo":        96.8,
            "spin_rate":   2312,
            "whiff_pct":   22.1,
            "woba":        0.301,
            "xwoba":       0.288,
            "put_away":    18.4,
            "hard_hit":    41.2,
        },
        ...
    ]
Returns [] on any failure (network error, bad ID, no data).
═══════════════════════════════════════════════════════════════════════
"""

import time
import logging
import requests

logger = logging.getLogger(__name__)

# ── In-memory cache  { mlbam_id -> (timestamp, data) } ─────────────────────
_CACHE: dict = {}
CACHE_TTL = 3600  # seconds — refresh arsenal data every hour

# Savant arsenal endpoint
_ARSENAL_URL = "https://baseballsavant.mlb.com/player-services/arsenal-stats"

# Friendly pitch-type name map
_PITCH_NAMES = {
    "FF": "4-Seam Fastball",
    "SI": "Sinker",
    "FC": "Cutter",
    "SL": "Slider",
    "ST": "Sweeper",
    "CU": "Curveball",
    "KC": "Knuckle Curve",
    "CH": "Changeup",
    "FS": "Splitter",
    "FO": "Forkball",
    "KN": "Knuckleball",
    "SC": "Screwball",
    "EP": "Eephus",
    "CS": "Slow Curve",
    "SV": "Slurve",
    "PO": "Pitch Out",
}


def _fetch_from_savant(mlbam_id: int, year: int = 2026) -> list:
    """
    Hit the Savant arsenal endpoint and return parsed pitch list.
    Returns [] on any failure.
    """
    params = {
        "playerId": mlbam_id,
        "year": year,
        "type": "pitcher",
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; MLBAnalyticsHub/1.0)",
        "Accept": "application/json",
    }
    try:
        resp = requests.get(_ARSENAL_URL, params=params, headers=headers, timeout=8)
        if resp.status_code != 200:
            logger.warning("Savant arsenal HTTP %s for player %s", resp.status_code, mlbam_id)
            return []

        raw = resp.json()

        # Savant returns either a list directly or {"arsenal": [...]}
        if isinstance(raw, list):
            pitches = raw
        elif isinstance(raw, dict):
            pitches = raw.get("arsenal") or raw.get("pitches") or raw.get("data") or []
        else:
            logger.warning("Savant arsenal unexpected response type for %s", mlbam_id)
            return []

        result = []
        for p in pitches:
            pt = str(p.get("pitch_type") or p.get("pitchType") or "").upper()
            if not pt:
                continue

            def _flt(key, alt_keys=(), default=None):
                for k in (key, *alt_keys):
                    v = p.get(k)
                    if v is not None:
                        try:
                            return round(float(v), 3)
                        except (ValueError, TypeError):
                            pass
                return default

            result.append({
                "pitch_type": pt,
                "pitch_name": _PITCH_NAMES.get(pt, pt),
                "usage_pct":  _flt("pitch_usage", ("usage_percent", "pitchUsage"), 0.0),
                "velo":       _flt("avg_speed", ("release_speed", "velocity"), None),
                "spin_rate":  _flt("avg_spin", ("release_spin_rate", "spin"), None),
                "whiff_pct":  _flt("whiff_percent", ("whiff_pct", "swinging_strike_percent"), None),
                "woba":       _flt("woba", ("wOBA",), None),
                "xwoba":      _flt("est_woba", ("xwOBA", "xwoba"), None),
                "put_away":   _flt("putaway_percent", ("put_away",), None),
                "hard_hit":   _flt("hard_hit_percent", ("hard_hit",), None),
            })

        # Sort by usage descending so primary pitch is first
        result.sort(key=lambda x: x["usage_pct"] or 0, reverse=True)
        logger.info("[Savant Arsenal] player %s → %d pitches", mlbam_id, len(result))
        return result

    except requests.Timeout:
        logger.warning("[Savant Arsenal] timeout for player %s", mlbam_id)
        return []
    except Exception as e:
        logger.exception("[Savant Arsenal] error for player %s: %s", mlbam_id, e)
        return []


def get_arsenal_stats(mlbam_id: int, year: int = 2026, force_refresh: bool = False) -> list:
    """
    Public accessor. Returns cached result if fresh, otherwise fetches.

    Args:
        mlbam_id:      MLB MLBAM player ID (integer)
        year:          Season year (default 2026)
        force_refresh: Bypass cache and re-fetch from Savant

    Returns:
        List of pitch dicts, sorted by usage descending.
        Returns [] if player not found or Savant unavailable.
    """
    cache_key = (mlbam_id, year)
    now = time.time()

    if not force_refresh and cache_key in _CACHE:
        ts, data = _CACHE[cache_key]
        if now - ts < CACHE_TTL:
            return data

    data = _fetch_from_savant(mlbam_id, year)

    # If 2026 returns empty (season too early / no data yet) fall back to 2025
    if not data and year == 2026:
        logger.info("[Savant Arsenal] no 2026 data for %s, falling back to 2025", mlbam_id)
        data = _fetch_from_savant(mlbam_id, 2025)

    _CACHE[cache_key] = (now, data)
    return data


def clear_cache():
    """Clear the in-memory arsenal cache (useful for testing)."""
    _CACHE.clear()
