"""
umpire_loader.py — Per-umpire strike-zone features for K prop models.

Data source: umpscorecards.com (free, no key required).

Public API
----------
    fetch_and_save(date_str=None) -> int
        Fetches today's umpire assignments + season-to-date zone stats,
        merges them, and writes data/umpire_zones.json.
        Returns the number of umpire records saved.
        date_str: optional 'YYYY-MM-DD' (defaults to today ET).

    get_umpire_features(umpire_name: str) -> dict
        Returns {'ump_zone_size': float, 'ump_k_boost': float} for the
        named home-plate umpire.  Falls back to league-average defaults
        (0.0 for both) if the umpire is not found in the cache.

    load_cache() -> bool
        Loads data/umpire_zones.json into memory.  Idempotent.

Feature definitions
-------------------
    ump_zone_size  : season z-score of calls_above_average (positive = big zone)
    ump_k_boost    : expected extra Ks per 9 IP vs. league average, derived from
                     the umpire's career strikeout rate delta

Wired into pipeline_scheduler._refresh_best_bets_signals via:
    ("umpire", lambda: __import__("umpire_loader").fetch_and_save(date_str))
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from typing import Dict, Optional
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
_HERE = os.path.dirname(os.path.abspath(__file__))
_CACHE_PATH = os.path.join(_HERE, "data", "umpire_zones.json")

# ── In-memory store ────────────────────────────────────────────────────────────
_lock = threading.RLock()
_ump_index: Dict[str, dict] = {}   # lower(name) -> feature dict
_loaded = False

# ── League-average defaults (used as fallback) ─────────────────────────────────
_DEFAULT_ZONE_SIZE = 0.0    # z-score relative to league
_DEFAULT_K_BOOST   = 0.0    # extra Ks / 9 IP vs. league

# ── umpscorecards.com endpoints ────────────────────────────────────────────────
_UMP_SEASON_URL    = "https://umpscorecards.com/api/umpires"
_UMP_SCHEDULE_URL  = "https://umpscorecards.com/api/games"
_REQUEST_TIMEOUT   = 12


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _safe_float(val, default=0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _fetch_season_stats() -> list:
    """
    Returns a list of dicts, one per umpire, from the umpscorecards season
    summary endpoint.  Each dict includes at minimum:
        name, games, calls_above_average, correct_calls_pct,
        strikeout_rate (if present)
    """
    try:
        resp = requests.get(_UMP_SEASON_URL, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        # API returns a list or a dict with 'umpires' key
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("umpires") or data.get("data") or []
    except Exception as e:
        log.warning(f"[umpire_loader] season stats fetch failed: {e}")
    return []


def _fetch_schedule_assignments(date_str: str) -> Dict[str, str]:
    """
    Returns {game_pk_str: umpire_name} for all HP umpire assignments on date_str.
    Falls back to empty dict on any error.
    """
    try:
        resp = requests.get(
            _UMP_SCHEDULE_URL,
            params={"date": date_str},
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        games = data if isinstance(data, list) else (data.get("games") or [])
        assignments: Dict[str, str] = {}
        for g in games:
            # Field names vary slightly across API versions
            gid = str(g.get("gamePk") or g.get("game_pk") or "")
            ump = (
                g.get("homePlateUmpire")
                or g.get("home_plate_umpire")
                or g.get("umpire")
                or ""
            )
            if gid and ump:
                assignments[gid] = ump
        return assignments
    except Exception as e:
        log.warning(f"[umpire_loader] schedule fetch failed for {date_str}: {e}")
    return {}


def _compute_z_scores(records: list, field: str) -> Dict[str, float]:
    """
    Z-score normalise a numeric field across all umpire records.
    Returns {lower(name): z_score}.
    """
    vals = [_safe_float(r.get(field)) for r in records if r.get(field) is not None]
    if len(vals) < 2:
        return {}
    mu  = sum(vals) / len(vals)
    var = sum((v - mu) ** 2 for v in vals) / len(vals)
    std = var ** 0.5 or 1.0
    result: Dict[str, float] = {}
    for r in records:
        name = (r.get("name") or "").strip()
        if not name:
            continue
        v = r.get(field)
        if v is None:
            continue
        result[name.lower()] = round((_safe_float(v) - mu) / std, 4)
    return result


def _build_k_boost(records: list) -> Dict[str, float]:
    """
    Estimates extra Ks / 9 IP attributable to the umpire.
    Uses 'strikeout_rate' if present, otherwise falls back to
    'calls_above_average' scaled by a league conversion factor.
    """
    # League-average K/9 for reference scaling (2024 season ~8.5)
    LEAGUE_K9 = 8.5
    K_PER_CAA = 0.12   # empirical: each call_above_average ≈ 0.12 extra K / game

    boosts: Dict[str, float] = {}
    for r in records:
        name = (r.get("name") or "").strip()
        if not name:
            continue
        # Prefer explicit strikeout-rate delta if the API exposes it
        sr_delta = r.get("strikeout_rate_above_average")
        if sr_delta is not None:
            k_boost = round(_safe_float(sr_delta) * LEAGUE_K9, 4)
        else:
            caa = _safe_float(r.get("calls_above_average", 0))
            k_boost = round(caa * K_PER_CAA, 4)
        boosts[name.lower()] = k_boost
    return boosts


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def load_cache() -> bool:
    """Load data/umpire_zones.json into memory.  Idempotent."""
    global _loaded, _ump_index
    with _lock:
        if _loaded:
            return bool(_ump_index)
        _loaded = True
        if not os.path.exists(_CACHE_PATH):
            return False
        try:
            with open(_CACHE_PATH) as f:
                raw = json.load(f)
            # raw is {lower_name: {ump_zone_size, ump_k_boost, ...}}
            _ump_index = {k.lower(): v for k, v in raw.items()}
            log.info(f"[umpire_loader] loaded {len(_ump_index)} umpires from cache")
            return True
        except Exception as e:
            log.warning(f"[umpire_loader] cache load failed: {e}")
            return False


def fetch_and_save(date_str: Optional[str] = None) -> int:
    """
    Fetch season umpire stats + today's HP assignments, build feature dicts,
    write data/umpire_zones.json, and refresh the in-memory index.

    Returns the number of umpire records saved (0 on complete failure).
    """
    global _loaded, _ump_index

    if date_str is None:
        date_str = datetime.now(ET).strftime("%Y-%m-%d")

    records = _fetch_season_stats()
    if not records:
        log.warning("[umpire_loader] no season stats returned — skipping save")
        return 0

    zone_z    = _compute_z_scores(records, "calls_above_average")
    k_boosts  = _build_k_boost(records)

    # Build per-umpire feature dict
    index: Dict[str, dict] = {}
    for r in records:
        name = (r.get("name") or "").strip()
        if not name:
            continue
        key = name.lower()
        index[key] = {
            "name":          name,
            "games":         int(r.get("games") or 0),
            "ump_zone_size": zone_z.get(key, _DEFAULT_ZONE_SIZE),
            "ump_k_boost":   k_boosts.get(key, _DEFAULT_K_BOOST),
            # Store raw values for transparency / debugging
            "calls_above_average": _safe_float(r.get("calls_above_average")),
            "correct_calls_pct":   _safe_float(r.get("correct_calls_pct")),
        }

    # Attach today's assignment mapping (game_pk -> umpire name)
    assignments = _fetch_schedule_assignments(date_str)
    meta = {
        "_meta": {
            "date":        date_str,
            "n_umpires":   len(index),
            "n_games":     len(assignments),
            "assignments": assignments,   # game_pk -> umpire name
            "updatedAt":   datetime.utcnow().isoformat() + "Z",
        }
    }

    payload = {**meta, **index}

    os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
    with open(_CACHE_PATH, "w") as f:
        json.dump(payload, f, indent=2)
    log.info(f"[umpire_loader] saved {len(index)} umpires + {len(assignments)} assignments → {_CACHE_PATH}")

    # Refresh in-memory index atomically
    with _lock:
        _ump_index = index
        _loaded = True

    return len(index)


def get_umpire_features(umpire_name: str) -> dict:
    """
    Return {'ump_zone_size': float, 'ump_k_boost': float} for the given
    home-plate umpire name.  Both values are 0.0 (league-average) when the
    umpire is not found.

    Automatically loads the on-disk cache on first call.
    """
    load_cache()
    key = (umpire_name or "").strip().lower()
    entry = _ump_index.get(key)
    if entry:
        return {
            "ump_zone_size": entry.get("ump_zone_size", _DEFAULT_ZONE_SIZE),
            "ump_k_boost":   entry.get("ump_k_boost",   _DEFAULT_K_BOOST),
        }
    # Try last-name match as a loose fallback
    last = key.split()[-1] if key else ""
    if last:
        for k, v in _ump_index.items():
            if k.split()[-1] == last:
                log.debug(f"[umpire_loader] last-name match: '{umpire_name}' → '{v['name']}'")
                return {
                    "ump_zone_size": v.get("ump_zone_size", _DEFAULT_ZONE_SIZE),
                    "ump_k_boost":   v.get("ump_k_boost",   _DEFAULT_K_BOOST),
                }
    return {"ump_zone_size": _DEFAULT_ZONE_SIZE, "ump_k_boost": _DEFAULT_K_BOOST}


def get_game_umpire(game_pk: str | int) -> Optional[str]:
    """
    Return the home-plate umpire name for a given game_pk, or None if not
    found in today's cached assignments.
    """
    load_cache()
    meta = _ump_index.get("_meta") or {}
    # _meta is stored under key '_meta' in the file but filtered from _ump_index
    # Read directly from disk if needed
    try:
        if os.path.exists(_CACHE_PATH):
            with open(_CACHE_PATH) as f:
                raw = json.load(f)
            assignments = raw.get("_meta", {}).get("assignments", {})
            return assignments.get(str(game_pk))
    except Exception:
        pass
    return None


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    n = fetch_and_save(date_arg)
    print(f"Saved {n} umpire records to {_CACHE_PATH}")
