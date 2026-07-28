"""
tier_calibrator.py — Backtest-driven Best Bets tier thresholds.

Replaces the hardcoded ELITE / STRONG / LEAN edge cutoffs in dashboard.html
(e.g. HITS over 0.5 is ELITE when edge > 0.35) with empirically-derived
thresholds that target a real hit-rate per tier.

Inputs:  data/daily_tracker.json — historical picks with grade ∈ {win,loss,push}
Outputs: data/tier_thresholds.json — {market_key: {ELITE: x, STRONG: y, LEAN: z}}

Targets:
  • ELITE  → 62% hit rate
  • STRONG → 55% hit rate
  • LEAN   → 52% hit rate (anything above is a pick)

Cold-start: when there isn't enough historical data, ships sane defaults
that mirror the current hardcoded values so the UI keeps working.
"""

import json
import os
from collections import defaultdict
from datetime import datetime

from config import settings

DATA_DIR = settings.data_dir
TRACKER_PATH = os.path.join(DATA_DIR, "daily_tracker.json")
OUT_PATH = os.path.join(DATA_DIR, "tier_thresholds.json")

TARGETS = {"ELITE": 0.62, "STRONG": 0.55, "LEAN": 0.52}
MIN_SAMPLES_PER_BUCKET = 30
MIN_TOTAL_SAMPLES = 80

DEFAULTS = {
    "HITS_0.5":  {"ELITE": 0.35, "STRONG": 0.20, "LEAN": 0.05},
    "HITS_1.5":  {"ELITE": 0.20, "STRONG": 0.10, "LEAN": 0.05},
    "TB_1.5":    {"ELITE": 0.50, "STRONG": 0.25, "LEAN": 0.10},
    "TB_2.5":    {"ELITE": 0.30, "STRONG": 0.15, "LEAN": 0.05},
    "HR_0.5":    {"ELITE": 0.20, "STRONG": 0.10, "LEAN": 0.05},
    "RBI_0.5":   {"ELITE": 0.30, "STRONG": 0.15, "LEAN": 0.05},
    "K":         {"ELITE": 1.50, "STRONG": 0.75, "LEAN": 0.15},
    "MONEYLINE": {"ELITE": 0.10, "STRONG": 0.06, "LEAN": 0.02},
    "SPREAD":    {"ELITE": 0.10, "STRONG": 0.06, "LEAN": 0.02},
    "TOTALS":    {"ELITE": 0.10, "STRONG": 0.06, "LEAN": 0.02},
}


def _market_key(entry):
    """Map a tracker entry to a market key we calibrate against."""
    mk = (entry.get("marketKey") or "").lower()
    line = entry.get("line")
    if mk == "batter_hits":
        return f"HITS_{line}" if line in (0.5, 1.5) else None
    if mk == "batter_total_bases":
        return f"TB_{line}" if line in (1.5, 2.5) else None
    if mk == "batter_home_runs":
        return "HR_0.5"
    if mk == "batter_rbis":
        return "RBI_0.5"
    if mk == "pitcher_strikeouts":
        return "K"
    if mk in ("h2h", "moneyline"):
        return "MONEYLINE"
    if mk in ("spreads", "spread", "run_line", "runline"):
        return "SPREAD"
    if mk in ("totals", "total"):
        return "TOTALS"
    return None


def _grade_to_outcome(grade):
    g = (grade or "").lower()
    if g == "win":
        return 1
    if g == "loss":
        return 0
    return None  # push / pending → skip


def _load_tracker():
    try:
        with open(TRACKER_PATH, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _collect_picks(tracker):
    """Yield (market_key, edge, outcome) tuples for all graded picks."""
    if not isinstance(tracker, dict):
        return
    for date_str, day in tracker.items():
        if not isinstance(day, dict):
            continue
        for entry in day.get("entries", []) or []:
            if not isinstance(entry, dict):
                continue
            outcome = _grade_to_outcome(entry.get("grade"))
            if outcome is None:
                continue
            mkey = _market_key(entry)
            if not mkey:
                continue
            edge = entry.get("edge")
            if edge is None:
                # Synthesize edge from modelProb − impliedProb when available
                mp = entry.get("modelProb"); ip = entry.get("impliedProb")
                if mp is not None and ip is not None:
                    try:
                        edge = float(mp) - float(ip)
                    except (TypeError, ValueError):
                        edge = None
            if edge is None:
                continue
            try:
                edge = abs(float(edge))
            except (TypeError, ValueError):
                continue
            yield mkey, edge, outcome


def _solve_threshold(rows, target_rate):
    """Find the smallest edge threshold s.t. hit-rate at edge >= t meets target.

    rows: list of (edge, outcome). Returns float threshold or None.
    """
    if not rows:
        return None
    rows = sorted(rows, key=lambda r: -r[0])  # high edge first
    cum_n = 0
    cum_hits = 0
    best = None
    for edge, outcome in rows:
        cum_n += 1
        cum_hits += outcome
        if cum_n < MIN_SAMPLES_PER_BUCKET:
            continue
        rate = cum_hits / cum_n
        if rate >= target_rate:
            best = edge
        else:
            # Once we dip below target, the threshold is the previous edge
            if best is not None:
                return best
    return best


def calibrate(tracker=None, write=True):
    """Run the calibration and optionally write data/tier_thresholds.json."""
    tracker = tracker if tracker is not None else _load_tracker()
    by_market = defaultdict(list)
    for mkey, edge, outcome in _collect_picks(tracker):
        by_market[mkey].append((edge, outcome))

    result = {}
    meta = {}
    for mkey, default in DEFAULTS.items():
        rows = by_market.get(mkey, [])
        meta[mkey] = {"n": len(rows)}
        if len(rows) < MIN_TOTAL_SAMPLES:
            result[mkey] = dict(default)
            meta[mkey]["source"] = "default"
            continue
        tiers = {}
        for tier_name, target in TARGETS.items():
            thr = _solve_threshold(rows, target)
            if thr is None:
                thr = default[tier_name]
            tiers[tier_name] = round(float(thr), 4)
        # Enforce monotonicity: ELITE >= STRONG >= LEAN
        tiers["STRONG"] = max(tiers["STRONG"], tiers["LEAN"])
        tiers["ELITE"]  = max(tiers["ELITE"],  tiers["STRONG"])
        result[mkey] = tiers
        meta[mkey]["source"] = "calibrated"

    payload = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "thresholds":   result,
        "meta":         meta,
    }

    if write:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(OUT_PATH, "w") as f:
            json.dump(payload, f, indent=2)

    return payload


def load_thresholds():
    """Return the persisted thresholds, falling back to defaults."""
    try:
        with open(OUT_PATH, "r") as f:
            payload = json.load(f)
        if isinstance(payload, dict) and "thresholds" in payload:
            return payload
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {
        "generated_at": None,
        "thresholds":   dict(DEFAULTS),
        "meta":         {k: {"n": 0, "source": "default"} for k in DEFAULTS},
    }


if __name__ == "__main__":
    payload = calibrate()
    n_calibrated = sum(1 for m in payload["meta"].values() if m.get("source") == "calibrated")
    n_default = sum(1 for m in payload["meta"].values() if m.get("source") == "default")
    print(f"[tier_calibrator] wrote {OUT_PATH}  calibrated={n_calibrated} default={n_default}")
