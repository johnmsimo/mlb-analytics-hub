"""
tracker_writer.py  —  Centralized daily_tracker.json write layer
═══════════════════════════════════════════════════════════════════════
Single source of truth for every field written to daily_tracker.json.
Previously, pick serialization was scattered across app.py.
Import and call `write_pick()` (or `build_pick_payload()` if you need
the dict before writing) from anywhere in the codebase.

Step 4 addition: mc_prob_over, mc_p10, mc_p90, mc_std are now extracted
from the `mc` sub-dict returned by `xgb_prop_scorer._score_full()` and
stored directly on the tracker entry.  This is what feeds eval_models.py
`--compare-mc` mode and the MC vs XGB head-to-head Brier evaluation.

Usage
─────
    from tracker_writer import write_pick, build_pick_payload

    # After calling xgb_hit_prob_full() / xgb_k_prob_full() / etc.
    full = xgb_hit_prob_full(batter, pitcher)   # → {prob, cal_p, p_lo, p_hi, mc, ...}

    payload = build_pick_payload(
        player      = batter["name"],
        market_key  = "batter_hits",
        line        = 0.5,
        side        = "Over",
        game_pk     = game_pk,
        score_full  = full,             # ← the whole _score_full() dict
        opening_price = opening_american,
        closing_price = closing_american,
        opening_implied = opening_impl,
        closing_implied = closing_impl,
        book        = "DraftKings",
        stake_units = 1.0,
        source      = "xgb",
    )
    write_pick(payload, date_str="2026-06-02")


Tracker schema per entry (full field list)
──────────────────────────────────────────
  Core identity
    id              str     Stable dedup key: f"{date}|{game_pk}|{player}|{market_key}|{line}"
    savedAt         ISO-8601
    gradedAt        ISO-8601 | null
    source          str     "xgb" | "batx" | "stacked" | "dashboard" | ...
    gamePk          int | null
    player          str
    marketKey       str     e.g. "batter_hits", "pitcher_strikeouts"
    line            float
    recommendedSide str     "Over" | "Under"
    sideLabel       str     e.g. "Over 0.5"

  Model outputs
    prob            float   Final probability (calibrated when available)
    rawProb         float   Pre-calibration probability
    calibratedProb float   Isotonic calibrated probability
    marketBlendProb float   Probability after market-prior blend
    pLo / pHi       float   95 % bootstrap confidence interval
    modelSource     str     "xgb" | "baseline" | "batx"
    usedXGB         bool
    featuresUsed    int
    xgbProb         float | null
    batxProb        float | null
    mcProbOver      float | null   Monte-Carlo P(over line)
    mcP10           float | null   MC 10th-percentile outcome
    mcP90           float | null   MC 90th-percentile outcome
    mcStd           float | null   MC outcome standard deviation
    calibrationStatus str
    calibrationSamples int
    consensus       str     "agree" | "mild_split" | "split"
    projectionReason str
    verdict         str     "STRONG BET" | "LEAN" | "PASS" | "FADE"

  Market / edge
    openingPrice    int | null   American odds at pick time
    closingPrice    int | null   Closing American odds
    openingImplied  float | null
    closingImplied  float | null
    clv             float | null   closing_implied − opening_implied
    edge            float | null   model_prob − opening_implied
    book            str
    fairOdds        int | null
    evPct           float | null   Kelly-adjusted EV %
    hubRating       float   0–100 composite score
    hubTier         str     "ELITE" | "STRONG" | "LEAN" | "PASS" | "FADE"

  Staking
    stakeDollars    float | null
    stakeUnits      float | null
    profitDollars   float | null
    profitUnits     float | null

  Result
    grade           str     "pending" | "win" | "loss" | "push"
    sideLabel       str     Human-readable e.g. "Over 0.5"
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from config import settings

_HERE        = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR    = settings.data_dir
_TRACKER_PATH = os.path.join(_DATA_DIR, "daily_tracker.json")
_lock        = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────────
# Public helpers
# ─────────────────────────────────────────────────────────────────────────────

def tracker_path() -> str:
    return _TRACKER_PATH


def load_tracker() -> list:
    """Load the tracker JSON array.  Returns [] on missing/corrupt file."""
    try:
        with open(_TRACKER_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def _atomic_save(entries: list) -> None:
    """Write via temp file + replace so a crash cannot corrupt the tracker."""
    os.makedirs(_DATA_DIR, exist_ok=True)
    tmp = _TRACKER_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _TRACKER_PATH)


def _f(value, default=None) -> Optional[float]:
    """Coerce to finite float; return *default* on failure / NaN / Inf."""
    if value is None:
        return default
    try:
        v = float(value)
        if v != v or v in (float("inf"), float("-inf")):
            return default
        return v
    except (TypeError, ValueError):
        return default


def _i(value, default=None) -> Optional[int]:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _s(value, default="") -> str:
    return str(value).strip() if value is not None else default


def build_pick_payload(
    *,
    player: str,
    market_key: str,
    line: float,
    side: str,
    game_pk=None,
    score_full: Optional[dict] = None,
    projection: Optional[float] = None,
    opening_price=None,
    closing_price=None,
    opening_implied=None,
    closing_implied=None,
    book: str = "",
    stake_dollars=None,
    stake_units=None,
    source: str = "xgb",
    extra: Optional[dict] = None,
) -> Dict[str, Any]:
    """Build one canonical tracker entry from model + market inputs.

    ``score_full`` is the complete dict returned by
    ``xgb_prop_scorer._score_full()`` (or compatible).  All nested MC and
    calibration fields are extracted here so callers never duplicate schema
    knowledge.  ``extra`` is merged last for forward-compatible extensions.
    """
    sf = score_full or {}
    mc = sf.get("mc") or {}

    # Probability precedence: calibrated → market-blended → raw model → none
    calibrated = _f(sf.get("cal_p"))
    market_blend = _f(sf.get("market_blend_p"))
    raw_prob = _f(sf.get("raw_p", sf.get("prob")))
    final_prob = calibrated if calibrated is not None else (
        market_blend if market_blend is not None else raw_prob
    )

    op_impl = _f(opening_implied)
    cp_impl = _f(closing_implied)
    edge = (final_prob - op_impl) if final_prob is not None and op_impl is not None else None
    clv = (cp_impl - op_impl) if cp_impl is not None and op_impl is not None else None

    payload: Dict[str, Any] = {
        "id":              "",          # assigned in write_pick once date is known
        "savedAt":         datetime.now(timezone.utc).isoformat(),
        "gradedAt":        None,
        "source":          _s(source, "xgb"),
        "gamePk":          _i(game_pk),
        "player":          _s(player),
        "marketKey":       _s(market_key),
        "line":            _f(line),
        "recommendedSide": _s(side, "Over").title(),
        "sideLabel":       f"{_s(side, 'Over').title()} {_f(line, 0):g}",

        # Model
        "prob":               final_prob,
        "rawProb":            raw_prob,
        "calibratedProb":     calibrated,
        "marketBlendProb":    market_blend,
        "pLo":                _f(sf.get("p_lo")),
        "pHi":                _f(sf.get("p_hi")),
        "modelSource":        _s(sf.get("model_source", sf.get("source", ""))),
        "usedXGB":            bool(sf.get("used_xgb", False)),
        "featuresUsed":       _i(sf.get("features_used"), 0),
        "xgbProb":            _f(sf.get("xgb_prob")),
        "batxProb":           _f(sf.get("batx_prob")),
        "mcProbOver":         _f(mc.get("prob_over", sf.get("mc_prob_over"))),
        "mcP10":              _f(mc.get("p10", sf.get("mc_p10"))),
        "mcP90":              _f(mc.get("p90", sf.get("mc_p90"))),
        "mcStd":              _f(mc.get("std", sf.get("mc_std"))),
        "calibrationStatus":  _s(sf.get("calibration_status", "uncalibrated")),
        "calibrationSamples": _i(sf.get("calibration_samples"), 0),
        "consensus":          _s(sf.get("consensus", "")),
        "projectionReason":   _s(sf.get("reason", sf.get("projection_reason", ""))),
        "verdict":            _s(sf.get("verdict", "")),
        "projection":         _f(projection, _f(sf.get("projection"))),

        # Market
        "openingPrice":   _i(opening_price),
        "closingPrice":   _i(closing_price),
        "openingImplied": op_impl,
        "closingImplied": cp_impl,
        "clv":            clv,
        "edge":           edge,
        "book":           _s(book),
        "fairOdds":       _i(sf.get("fair_odds")),
        "evPct":          _f(sf.get("ev_pct")),
        "hubRating":      _f(sf.get("hub_rating"), 0.0),
        "hubTier":        _s(sf.get("hub_tier", "PASS")),

        # Staking / result
        "stakeDollars":  _f(stake_dollars),
        "stakeUnits":    _f(stake_units),
        "profitDollars": None,
        "profitUnits":   None,
        "grade":         "pending",
    }

    if extra:
        payload.update(extra)
    return payload


def write_pick(payload: Dict[str, Any], date_str: Optional[str] = None) -> Dict[str, Any]:
    """Append *payload* to the tracker, deduplicating by stable ID.

    Returns the stored entry.  If an entry with the same ID exists it is
    replaced (preserving any existing grade/profit fields when the incoming
    value is still pending), which makes repeated capture idempotent.
    """
    date_str = date_str or datetime.now(timezone.utc).date().isoformat()
    game_pk = payload.get("gamePk") or "na"
    player = payload.get("player", "unknown")
    market = payload.get("marketKey", "unknown")
    line = payload.get("line", "na")
    stable_id = f"{date_str}|{game_pk}|{player}|{market}|{line}"
    payload = dict(payload)
    payload["id"] = stable_id
    payload["date"] = date_str

    with _lock:
        entries = load_tracker()
        idx = next((i for i, e in enumerate(entries) if e.get("id") == stable_id), None)
        if idx is not None:
            old = entries[idx]
            # Do not erase a completed grade when re-capturing the same pick.
            if old.get("grade") not in (None, "", "pending") and payload.get("grade") == "pending":
                for key in ("grade", "gradedAt", "profitDollars", "profitUnits",
                            "closingPrice", "closingImplied", "clv"):
                    if old.get(key) is not None:
                        payload[key] = old[key]
            entries[idx] = payload
        else:
            entries.append(payload)
        _atomic_save(entries)
    return payload


def update_pick(stable_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Patch fields on an existing pick by stable ID.  Returns None if absent."""
    with _lock:
        entries = load_tracker()
        for i, entry in enumerate(entries):
            if entry.get("id") == stable_id:
                entries[i] = {**entry, **updates}
                _atomic_save(entries)
                return entries[i]
    return None


def grade_pick(
    stable_id: str,
    grade: str,
    *,
    closing_price=None,
    closing_implied=None,
    profit_dollars=None,
    profit_units=None,
) -> Optional[Dict[str, Any]]:
    """Grade a pick win/loss/push and stamp CLV/profit fields."""
    if grade not in ("win", "loss", "push"):
        raise ValueError("grade must be win, loss, or push")
    entries = load_tracker()
    existing = next((e for e in entries if e.get("id") == stable_id), None)
    if existing is None:
        return None
    op_impl = _f(existing.get("openingImplied"))
    cp_impl = _f(closing_implied)
    updates = {
        "grade":          grade,
        "gradedAt":       datetime.now(timezone.utc).isoformat(),
        "closingPrice":   _i(closing_price, existing.get("closingPrice")),
        "closingImplied": cp_impl if cp_impl is not None else existing.get("closingImplied"),
        "clv":            (cp_impl - op_impl) if cp_impl is not None and op_impl is not None else existing.get("clv"),
        "profitDollars":  _f(profit_dollars),
        "profitUnits":    _f(profit_units),
    }
    return update_pick(stable_id, updates)


def write_many(payloads: List[Dict[str, Any]], date_str: Optional[str] = None) -> List[Dict[str, Any]]:
    """Write a batch of picks.  Returns the stored entries."""
    return [write_pick(p, date_str=date_str) for p in payloads]
