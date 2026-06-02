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

  Probability chain  (Step 4 — full chain stored for eval)
    rawProb         float   XGB predict_proba raw output
    adjProb         float   After isotonic calibration  ← eval_models ground truth
    blendedProb     float | null  stacked_calibrator output if available
    p_lo            float   XGB prediction interval lower bound
    p_hi            float   XGB prediction interval upper bound

  Monte Carlo  (Step 4 — NEW)
    mc_prob_over    float   P(outcome > line) from anchored MC
    mc_prob_under   float   1 - mc_prob_over
    mc_mean         float   Mean of Beta draws
    mc_p10          float   10th percentile of Beta draws
    mc_p25          float   25th percentile of Beta draws
    mc_p75          float   75th percentile of Beta draws
    mc_p90          float   90th percentile of Beta draws
    mc_std          float   Std dev of Beta draws  (CI width proxy)
    mc_n_sims       int     Number of MC trials run
    mc_anchored     bool    True = interval came from XGB tree ensemble

  Book / market
    openingPrice    American odds int | null
    closingPrice    American odds int | null
    openingImplied  float | null   Vig-removed implied prob at open
    closingImplied  float | null   Vig-removed implied prob at close
    clvEdge         float | null   closingImplied - openingImplied
    marketImplied   float | null   Alias for openingImplied (UI compat)
    book            str | null
    edge            float   adjProb - openingImplied
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

_HERE        = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR    = os.environ.get("DATA_DIR") or os.path.join(_HERE, "data")
_TRACKER_PATH = os.path.join(_DATA_DIR, "daily_tracker.json")
_lock        = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────────
# Public helpers
# ─────────────────────────────────────────────────────────────────────────────

def build_pick_payload(
    *,
    player:           str,
    market_key:       str,
    line:             float,
    side:             str            = "Over",
    game_pk:          Optional[int]  = None,
    score_full:       Optional[dict] = None,   # full dict from _score_full() / xgb_*_prob_full()
    # Allow callers to pass individual probs if they don't have score_full
    adj_prob:         Optional[float] = None,
    raw_prob:         Optional[float] = None,
    blended_prob:     Optional[float] = None,
    p_lo:             Optional[float] = None,
    p_hi:             Optional[float] = None,
    # Book / market data
    opening_price:    Optional[int]   = None,
    closing_price:    Optional[int]   = None,
    opening_implied:  Optional[float] = None,
    closing_implied:  Optional[float] = None,
    book:             Optional[str]   = None,
    # Staking
    stake_units:      Optional[float] = None,
    stake_dollars:    Optional[float] = None,
    # Metadata
    source:           str             = "xgb",
    date_str:         Optional[str]   = None,   # YYYY-MM-DD; defaults to today UTC
    extra:            Optional[dict]  = None,   # any additional fields to merge
) -> dict:
    """
    Build a fully-populated tracker entry dict.

    Pass `score_full` as the raw return value of any `xgb_*_prob_full()`
    call and all probability-chain + MC fields will be extracted automatically.
    Alternatively, pass individual fields if you are constructing a pick
    from a non-XGB source.
    """
    today = date_str or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # ── Extract probability chain from score_full ───────────────────────────
    sf = score_full or {}
    mc = sf.get("mc") or {}

    _adj_prob     = adj_prob     if adj_prob     is not None else sf.get("cal_p") or sf.get("prob")
    _raw_prob     = raw_prob     if raw_prob     is not None else sf.get("raw_p")
    _blended_prob = blended_prob if blended_prob is not None else sf.get("blendedProb")
    _p_lo         = p_lo         if p_lo         is not None else sf.get("p_lo")
    _p_hi         = p_hi         if p_hi         is not None else sf.get("p_hi")

    # ── Derived market fields ───────────────────────────────────────────────
    _edge     = None
    if _adj_prob is not None and opening_implied is not None:
        _edge = round(float(_adj_prob) - float(opening_implied), 4)

    _clv_edge = None
    if closing_implied is not None and opening_implied is not None:
        _clv_edge = round(float(closing_implied) - float(opening_implied), 4)

    _hub_rating = _compute_hub_rating(_adj_prob, _edge)
    _hub_tier   = _compute_hub_tier(_hub_rating)
    _ev_pct     = _compute_ev_pct(_adj_prob, opening_price)

    side_label = f"{side} {line}"

    # ── Dedup / stable ID ───────────────────────────────────────────────────
    gp_str = str(game_pk or "")
    entry_id = "|".join([
        today, gp_str, player,
        market_key, str(line)
    ])

    payload: Dict[str, Any] = {
        # Identity
        "id":             entry_id,
        "savedAt":        datetime.now(timezone.utc).isoformat(),
        "gradedAt":       None,
        "source":         source,
        "gamePk":         game_pk,
        "player":         player,
        "marketKey":      market_key,
        "line":           line,
        "recommendedSide": side,
        "sideLabel":      side_label,

        # Probability chain
        "rawProb":        round(_raw_prob, 4)     if _raw_prob     is not None else None,
        "adjProb":        round(_adj_prob, 4)     if _adj_prob     is not None else None,
        "blendedProb":    round(_blended_prob, 4) if _blended_prob is not None else None,
        "p_lo":           _p_lo,
        "p_hi":           _p_hi,

        # ── Monte Carlo fields (Step 4) ───────────────────────────────────
        "mc_prob_over":   mc.get("mc_prob_over"),
        "mc_prob_under":  mc.get("mc_prob_under"),
        "mc_mean":        mc.get("mc_mean"),
        "mc_p10":         mc.get("mc_p10"),
        "mc_p25":         mc.get("mc_p25"),
        "mc_p75":         mc.get("mc_p75"),
        "mc_p90":         mc.get("mc_p90"),
        "mc_std":         mc.get("mc_std"),
        "mc_n_sims":      mc.get("n_sims"),
        "mc_anchored":    mc.get("anchored"),
        # ─────────────────────────────────────────────────────────────────

        # Book / market
        "openingPrice":   opening_price,
        "closingPrice":   closing_price,
        "openingImplied": round(opening_implied, 4) if opening_implied is not None else None,
        "closingImplied": round(closing_implied, 4) if closing_implied is not None else None,
        "marketImplied":  round(opening_implied, 4) if opening_implied is not None else None,
        "clvEdge":        _clv_edge,
        "book":           book,
        "edge":           _edge,
        "evPct":          _ev_pct,
        "hubRating":      _hub_rating,
        "hubTier":        _hub_tier,

        # Staking
        "stakeUnits":     stake_units,
        "stakeDollars":   stake_dollars,
        "profitUnits":    None,
        "profitDollars":  None,

        # Result
        "grade":          "pending",
    }

    # Merge any extra fields last (caller overrides)
    if extra:
        payload.update(extra)

    return payload


def write_pick(
    payload: dict,
    date_str: Optional[str]  = None,
    tracker_path: str        = _TRACKER_PATH,
) -> None:
    """
    Thread-safe upsert of `payload` into `data/daily_tracker.json`.

    Deduplication key: payload["id"].
    If a matching entry already exists for the date, it is updated in-place
    (preserving grade / grading timestamps if already set).
    """
    today = date_str or payload.get("savedAt", "")[:10] or \
            datetime.now(timezone.utc).strftime("%Y-%m-%d")
    entry_id = payload.get("id") or ""

    os.makedirs(os.path.dirname(os.path.abspath(tracker_path)), exist_ok=True)

    with _lock:
        # Load
        tracker: dict = {}
        if os.path.exists(tracker_path):
            try:
                with open(tracker_path, "r", encoding="utf-8") as f:
                    tracker = json.load(f)
            except (json.JSONDecodeError, OSError):
                tracker = {}

        # Ensure day bucket exists
        if today not in tracker:
            tracker[today] = {
                "capturedAt":        datetime.now(timezone.utc).isoformat(),
                "gradedAt":          None,
                "closingCapturedAt": None,
                "entries":           [],
            }

        entries: list = tracker[today].get("entries") or []

        # Upsert by id
        existing_idx = None
        if entry_id:
            for i, e in enumerate(entries):
                if e.get("id") == entry_id:
                    existing_idx = i
                    break

        if existing_idx is not None:
            existing = entries[existing_idx]
            # Preserve grading data if already set; update everything else
            merged = {**payload}
            for preserve_key in ("grade", "gradedAt", "profitUnits", "profitDollars"):
                if existing.get(preserve_key) not in (None, "pending"):
                    merged[preserve_key] = existing[preserve_key]
            entries[existing_idx] = merged
        else:
            entries.append(payload)

        tracker[today]["entries"] = entries

        # Write
        tmp = tracker_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(tracker, f, indent=2, default=str)
        os.replace(tmp, tracker_path)


def patch_pick_mc(
    entry_id: str,
    mc: dict,
    date_str: Optional[str] = None,
    tracker_path: str       = _TRACKER_PATH,
) -> bool:
    """
    Patch MC fields onto an existing tracker entry by id.
    Useful if MC is computed async after the initial pick write.

    Returns True if the entry was found and patched, False otherwise.
    """
    today = date_str or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    mc_patch = {
        "mc_prob_over":  mc.get("mc_prob_over"),
        "mc_prob_under": mc.get("mc_prob_under"),
        "mc_mean":       mc.get("mc_mean"),
        "mc_p10":        mc.get("mc_p10"),
        "mc_p25":        mc.get("mc_p25"),
        "mc_p75":        mc.get("mc_p75"),
        "mc_p90":        mc.get("mc_p90"),
        "mc_std":        mc.get("mc_std"),
        "mc_n_sims":     mc.get("n_sims"),
        "mc_anchored":   mc.get("anchored"),
    }
    with _lock:
        if not os.path.exists(tracker_path):
            return False
        try:
            with open(tracker_path, "r", encoding="utf-8") as f:
                tracker = json.load(f)
        except (json.JSONDecodeError, OSError):
            return False

        found = False
        day = tracker.get(today, {})
        for entry in (day.get("entries") or []):
            if entry.get("id") == entry_id:
                entry.update(mc_patch)
                found = True
                break

        if found:
            tmp = tracker_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(tracker, f, indent=2, default=str)
            os.replace(tmp, tracker_path)

        return found


# ─────────────────────────────────────────────────────────────────────────────
# Derived-field helpers  (mirrors app.py logic, kept in sync here)
# ─────────────────────────────────────────────────────────────────────────────

def _american_to_decimal(american: Optional[int]) -> Optional[float]:
    if american is None:
        return None
    try:
        a = float(american)
    except (TypeError, ValueError):
        return None
    if a >= 100:
        return 1.0 + a / 100.0
    if a <= -100:
        return 1.0 + 100.0 / abs(a)
    return None


def _compute_ev_pct(
    adj_prob:      Optional[float],
    opening_price: Optional[int],
) -> Optional[float]:
    """Kelly-weighted EV% = p * (dec_odds - 1) - (1 - p)."""
    if adj_prob is None or opening_price is None:
        return None
    dec = _american_to_decimal(opening_price)
    if dec is None or dec <= 1.0:
        return None
    ev = float(adj_prob) * (dec - 1.0) - (1.0 - float(adj_prob))
    return round(ev, 4)


def _compute_hub_rating(
    adj_prob: Optional[float],
    edge:     Optional[float],
) -> float:
    """0–100 composite score matching tracker.html hubRating() JS."""
    prob_base  = float(adj_prob or 0) * 60.0
    edge_bonus = min(float(edge or 0) * 100.0, 20.0)
    edge_bonus = max(edge_bonus, 0.0)
    return round(min(100.0, prob_base + edge_bonus), 1)


# Tier thresholds matching tier_calibrator defaults
_DEFAULT_TIERS = {
    "ELITE":  75.0,
    "STRONG": 60.0,
    "LEAN":   50.0,
    "PASS":   40.0,
}


def _compute_hub_tier(hub_rating: float) -> str:
    if hub_rating >= _DEFAULT_TIERS["ELITE"]:
        return "ELITE"
    if hub_rating >= _DEFAULT_TIERS["STRONG"]:
        return "STRONG"
    if hub_rating >= _DEFAULT_TIERS["LEAN"]:
        return "LEAN"
    if hub_rating >= _DEFAULT_TIERS["PASS"]:
        return "PASS"
    return "FADE"
