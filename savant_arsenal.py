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

import csv
import io
import time
import logging
import requests

logger = logging.getLogger(__name__)

# ── In-memory cache  { mlbam_id -> (timestamp, data) } ─────────────────────
_CACHE: dict = {}
_BATTER_CACHE: dict = {}
CACHE_TTL = 3600  # seconds — refresh arsenal data every hour

# Savant arsenal endpoint
_ARSENAL_URL = "https://baseballsavant.mlb.com/player-services/arsenal-stats"
_BATTER_ARSENAL_URL = "https://baseballsavant.mlb.com/leaderboard/pitch-arsenal-stats"

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



def _fetch_batter_pitch_types(mlbam_id: int, year: int) -> list:
    """Fetch one batter's Baseball Savant results split by pitch type."""
    params = {
        "type": "batter",
        "pitchType": "",
        "year": year,
        "team": "",
        "min": 1,
        "csv": "true",
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; MLBAnalyticsHub/1.0)",
        "Accept": "text/csv",
    }
    try:
        resp = requests.get(
            _BATTER_ARSENAL_URL,
            params=params,
            headers=headers,
            timeout=12,
        )
        if resp.status_code != 200:
            logger.warning(
                "Savant batter arsenal HTTP %s for player %s",
                resp.status_code,
                mlbam_id,
            )
            return []
        rows = csv.DictReader(io.StringIO(resp.text))
        result = []
        for row in rows:
            raw_id = (
                row.get("player_id")
                or row.get("playerId")
                or row.get("id")
                or row.get("mlbamid")
            )
            try:
                if int(float(raw_id)) != int(mlbam_id):
                    continue
            except (TypeError, ValueError):
                continue

            def _first(*keys):
                for key in keys:
                    value = row.get(key)
                    if value not in (None, ""):
                        return value
                return None

            def _number(*keys):
                value = _first(*keys)
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return None

            pitch_type = str(
                _first("pitch_type", "pitchType", "pitch_name", "pitch") or ""
            ).strip()
            woba = _number("woba", "wOBA", "estimated_woba", "est_woba")
            pa = _number(
                "pa",
                "plate_appearances",
                "pitches",
                "total_pitches",
                "pitch_count",
            )
            if not pitch_type or woba is None or pa is None or pa <= 0:
                continue
            result.append({
                "pitch_type": pitch_type,
                "woba": round(woba, 4),
                "pa": int(pa),
            })
        return result
    except requests.Timeout:
        logger.warning("[Savant Batter Arsenal] timeout for player %s", mlbam_id)
        return []
    except Exception as exc:
        logger.exception(
            "[Savant Batter Arsenal] error for player %s: %s",
            mlbam_id,
            exc,
        )
        return []


def get_batter_pitch_type_stats(
    mlbam_id: int,
    year: int = 2026,
    force_refresh: bool = False,
) -> list:
    """Return cached current-season batter results split by pitch type."""
    cache_key = (int(mlbam_id), int(year))
    now = time.time()
    if not force_refresh and cache_key in _BATTER_CACHE:
        timestamp, data = _BATTER_CACHE[cache_key]
        if now - timestamp < CACHE_TTL:
            return data
    data = _fetch_batter_pitch_types(int(mlbam_id), int(year))
    if not data and int(year) == 2026:
        data = _fetch_batter_pitch_types(int(mlbam_id), 2025)
    _BATTER_CACHE[cache_key] = (now, data)
    return data


def clear_cache():
    """Clear pitcher and batter pitch-type caches (useful for testing)."""
    _CACHE.clear()
    _BATTER_CACHE.clear()
