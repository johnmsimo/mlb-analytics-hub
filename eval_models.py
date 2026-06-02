"""
eval_models.py — Calibration + CLV + MC convergence evaluation
═══════════════════════════════════════════════════════════════════════════════
Reads `data/daily_tracker.json`, filters to graded picks, computes:

  1. CALIBRATION
     - Brier score, Brier Skill Score (BSS), log loss, AUC-ROC
     - Expected Calibration Error (ECE) + 10-bucket reliability table
     - Optional reliability diagram PNG (matplotlib)

  2. CLV / VALUE  (Step 4)
     - Average CLV (closingImplied - openingImplied)
     - CLV-positive rate
     - Model edge vs closing line (adjProb - closingImplied)
     - ROI at flat-1-unit and fractional-Kelly stake
     - Closing-line value (CLV) used as ground-truth alongside outcomes

  3. MC CONVERGENCE  (Step 4 — new)
     - Head-to-head: mc_prob_over Brier vs adjProb (XGB calibrated) Brier
     - mc_prob_over CLV+ rate vs adjProb CLV+ rate
     - Verdict: which signal adds more value vs closing line
     - MC anchor quality: std dev of mc_p10/p90 spread over graded picks

  4. ROLLING TREND  (--rolling)
     - Brier + CLV broken into 7 / 14 / 30 / 90-day rolling windows
     - Drift alert fired when 14-day Brier degrades > DRIFT_BRIER_THRESHOLD
       vs the 90-day baseline, or CLV-positive rate drops below CLV_POSITIVE_FLOOR

  5. DRIFT ALERTS
     - Written to `data/model_drift_alerts.json` automatically so
       props.html can surface them without a separate script.

Usage:
  python eval_models.py
  python eval_models.py --since 2026-04-01
  python eval_models.py --market batter_hits
  python eval_models.py --rolling --out reports/eval_2026.json
  python eval_models.py --compare-mc --market pitcher_strikeouts
  python eval_models.py --kelly 0.25 --plot --out reports/eval_2026.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple


TRACKER_PATH_DEFAULT = "data/daily_tracker.json"
DRIFT_ALERTS_PATH    = "data/model_drift_alerts.json"

# Drift thresholds
DRIFT_BRIER_THRESHOLD  = 0.04
CLV_POSITIVE_FLOOR     = 0.45
ROLLING_WINDOWS        = [7, 14, 30, 90]

# Tracker field names for Step-3 MC output stored per pick
_MC_PROB_FIELD   = "mc_prob_over"   # P(over line) from mc_simulate()
_MC_P10_FIELD    = "mc_p10"         # 10th pct of Beta draws
_MC_P90_FIELD    = "mc_p90"         # 90th pct of Beta draws
_MC_STD_FIELD    = "mc_std"         # std dev of Beta draws
_CAL_PROB_FIELD  = "adjProb"        # isotonic-calibrated XGB prob (existing)
_BLEND_FIELD     = "blendedProb"    # stacked_calibrator output (existing)


# ─── Math helpers ───────────────────────────────────────────────────────────

def american_to_decimal(american) -> Optional[float]:
    """+150 → 2.50, -180 → 1.5556."""
    try:
        a = float(american)
    except (TypeError, ValueError):
        return None
    if a == 0:
        return None
    if a >= 100:
        return 1.0 + a / 100.0
    if a <= -100:
        return 1.0 + 100.0 / abs(a)
    return None


def kelly_fraction(p: float, dec_odds: float) -> float:
    if dec_odds is None or dec_odds <= 1.0 or p is None:
        return 0.0
    b = dec_odds - 1.0
    f = (p * (b + 1) - 1) / b
    return max(0.0, f)


def pick_prob_for_side(adj_prob: float, side: str) -> Optional[float]:
    if adj_prob is None:
        return None
    side_norm = (side or "Over").strip().lower()
    if side_norm == "under":
        return float(adj_prob) if adj_prob < 0.5 else round(1.0 - float(adj_prob), 4)
    return float(adj_prob)


def reliability_bins(probs: List[float], outcomes: List[int],
                     n_bins: int = 10) -> List[dict]:
    bins = [{"lo": i / n_bins, "hi": (i + 1) / n_bins,
             "n": 0, "sum_p": 0.0, "sum_y": 0} for i in range(n_bins)]
    for p, y in zip(probs, outcomes):
        if p is None:
            continue
        idx = min(n_bins - 1, max(0, int(p * n_bins)))
        bins[idx]["n"] += 1
        bins[idx]["sum_p"] += p
        bins[idx]["sum_y"] += y
    out = []
    for b in bins:
        if b["n"] == 0:
            continue
        out.append({
            "bin":      f"{b['lo']:.2f}-{b['hi']:.2f}",
            "n":        b["n"],
            "avg_prob": round(b["sum_p"] / b["n"], 4),
            "hit_rate": round(b["sum_y"] / b["n"], 4),
            "gap":      round(b["sum_y"] / b["n"] - b["sum_p"] / b["n"], 4),
        })
    return out


def expected_calibration_error(probs, outcomes, n_bins=10) -> float:
    total = sum(1 for p in probs if p is not None)
    if total == 0:
        return 0.0
    bins = reliability_bins(probs, outcomes, n_bins)
    return round(sum(b["n"] / total * abs(b["avg_prob"] - b["hit_rate"]) for b in bins), 4)


def auc_roc(probs: List[float], outcomes: List[int]) -> Optional[float]:
    paired = [(p, y) for p, y in zip(probs, outcomes) if p is not None]
    pos = [p for p, y in paired if y == 1]
    neg = [p for p, y in paired if y == 0]
    if not pos or not neg:
        return None
    wins = ties = 0
    for pp in pos:
        for np_ in neg:
            if pp > np_:    wins += 1
            elif pp == np_: ties += 1
    return round((wins + 0.5 * ties) / (len(pos) * len(neg)), 4)


def brier_score(probs: List[float], outcomes: List[int]) -> Optional[float]:
    paired = [(p, y) for p, y in zip(probs, outcomes) if p is not None]
    if not paired:
        return None
    return round(sum((p - y) ** 2 for p, y in paired) / len(paired), 4)


def brier_skill_score(probs: List[float], outcomes: List[int]) -> Optional[float]:
    bs = brier_score(probs, outcomes)
    if bs is None or not outcomes:
        return None
    base_rate = sum(outcomes) / len(outcomes)
    naive = sum((base_rate - y) ** 2 for y in outcomes) / len(outcomes)
    if naive == 0:
        return None
    return round(1.0 - bs / naive, 4)


def log_loss(probs: List[float], outcomes: List[int],
             eps: float = 1e-6) -> Optional[float]:
    paired = [(p, y) for p, y in zip(probs, outcomes) if p is not None]
    if not paired:
        return None
    s = 0.0
    for p, y in paired:
        p = min(1 - eps, max(eps, p))
        s += -(y * math.log(p) + (1 - y) * math.log(1 - p))
    return round(s / len(paired), 4)


# ─── Tracker loading + filtering ───────────────────────────────────────────────────

def load_tracker(path: str) -> List[dict]:
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    with open(path) as f:
        data = json.load(f)
    rows: List[dict] = []
    if isinstance(data, dict):
        for date_key, day in data.items():
            if not isinstance(day, dict):
                continue
            for r in (day.get("entries") or []):
                r = dict(r)
                r.setdefault("date", date_key)
                rows.append(r)
    elif isinstance(data, list):
        rows = data
    return rows


def filter_picks(rows: List[dict], since: Optional[str], until: Optional[str],
                 market: Optional[str], min_prob: float = 0.0,
                 max_prob: float = 1.0) -> List[dict]:
    out = []
    for r in rows:
        if (r.get("grade") or r.get("status")) not in ("win", "loss", "push", "graded"):
            continue
        if since and (r.get("date") or "") < since:    continue
        if until and (r.get("date") or "") > until:    continue
        if market and r.get("marketKey") != market:    continue
        p = r.get(_CAL_PROB_FIELD)
        if p is None:
            continue
        if p < min_prob or p > max_prob:
            continue
        out.append(r)
    return out


# ─── MC convergence evaluation (Step 4) ───────────────────────────────────────────

def evaluate_mc_vs_xgb(
    picks: List[dict],
    market: str,
) -> dict:
    """
    Head-to-head comparison: mc_prob_over (Step 3 anchored MC) vs
    adjProb (isotonic-calibrated XGB) against:
      a) actual graded outcomes (Brier Score)
      b) closing line implied probability (CLV proxy — market ground truth)

    This answers: "Does the MC simulation layer add accuracy beyond
    the calibrated XGB score alone?"

    A well-anchored MC should have:
      - mc_brier <= xgb_brier   (MC is at least as accurate as raw XGB)
      - mc_clv_edge >= xgb_clv_edge  (MC beats closing line more often)
      - mc_std positively correlated with outcome variance (wide MC = uncertain)

    Returns
    -------
    {
      "market":             str,
      "n":                  int,
      "n_with_mc":          int,    # picks that have mc_prob_over stored
      "xgb_brier":          float,  # Brier using adjProb
      "mc_brier":           float,  # Brier using mc_prob_over
      "brier_delta":        float,  # mc_brier - xgb_brier (negative = MC better)
      "xgb_ece":            float,
      "mc_ece":             float,
      "xgb_clv_edge":       float,  # mean(adjProb - closingImplied)
      "mc_clv_edge":        float,  # mean(mc_prob_over - closingImplied)
      "xgb_clv_pos_rate":   float,
      "mc_clv_pos_rate":    float,
      "avg_mc_std":         float,  # mean of mc_std across picks
      "avg_mc_spread":      float,  # mean of (mc_p90 - mc_p10)
      "mc_verdict":         str,    # "MC_BETTER" / "XGB_BETTER" / "TIED"
      "wide_mc_accuracy":   float,  # hit rate when mc_spread > median spread
      "narrow_mc_accuracy": float,  # hit rate when mc_spread <= median spread
    }
    """
    xgb_probs,  mc_probs   = [], []
    outcomes_xgb, outcomes_mc = [], []
    xgb_close_edges, mc_close_edges = [], []
    mc_stds, mc_spreads = [], []
    paired_spread_outcome = []  # (spread, outcome) for wide vs narrow analysis

    for r in picks:
        grade = r.get("grade") or r.get("status")
        if grade not in ("win", "loss"):
            continue
        y = 1 if grade == "win" else 0
        side = r.get("recommendedSide") or "Over"

        xgb_p = r.get(_CAL_PROB_FIELD) or r.get(_BLEND_FIELD)
        mc_p  = r.get(_MC_PROB_FIELD)

        if xgb_p is not None:
            xgb_p = pick_prob_for_side(float(xgb_p), side)
            xgb_probs.append(xgb_p)
            outcomes_xgb.append(y)

        if mc_p is not None:
            mc_p = float(mc_p)
            mc_probs.append(mc_p)
            outcomes_mc.append(y)

        close_imp = r.get("closingImplied")
        if close_imp is not None:
            ci = float(close_imp)
            if xgb_p is not None:
                xgb_close_edges.append(xgb_p - ci)
            if mc_p is not None:
                mc_close_edges.append(mc_p - ci)

        std = r.get(_MC_STD_FIELD)
        p10 = r.get(_MC_P10_FIELD)
        p90 = r.get(_MC_P90_FIELD)
        if std is not None:
            mc_stds.append(float(std))
        if p10 is not None and p90 is not None:
            spread = float(p90) - float(p10)
            mc_spreads.append(spread)
            paired_spread_outcome.append((spread, y))

    def _avg(xs): return round(sum(xs) / len(xs), 4) if xs else None
    def _clv_pos(xs): return round(sum(1 for x in xs if x > 0) / len(xs), 4) if xs else None

    xgb_brier = brier_score(xgb_probs, outcomes_xgb)
    mc_brier  = brier_score(mc_probs,  outcomes_mc)
    xgb_ece   = expected_calibration_error(xgb_probs, outcomes_xgb)
    mc_ece    = expected_calibration_error(mc_probs,  outcomes_mc)

    brier_delta = None
    mc_verdict  = "INSUFFICIENT_DATA"
    if xgb_brier is not None and mc_brier is not None:
        brier_delta = round(mc_brier - xgb_brier, 4)
        if abs(brier_delta) < 0.002:
            mc_verdict = "TIED"
        elif mc_brier < xgb_brier:
            mc_verdict = "MC_BETTER"
        else:
            mc_verdict = "XGB_BETTER"

    # Wide vs narrow MC spread accuracy
    wide_acc = narrow_acc = None
    if len(paired_spread_outcome) >= 10:
        median_spread = sorted(s for s, _ in paired_spread_outcome)[len(paired_spread_outcome) // 2]
        wide   = [y for s, y in paired_spread_outcome if s >  median_spread]
        narrow = [y for s, y in paired_spread_outcome if s <= median_spread]
        wide_acc   = round(sum(wide)   / len(wide),   4) if wide   else None
        narrow_acc = round(sum(narrow) / len(narrow), 4) if narrow else None

    return {
        "market":             market,
        "n":                  len(outcomes_xgb),
        "n_with_mc":          len(outcomes_mc),
        "xgb_brier":          xgb_brier,
        "mc_brier":           mc_brier,
        "brier_delta":        brier_delta,
        "xgb_ece":            xgb_ece,
        "mc_ece":             mc_ece,
        "xgb_clv_edge":       _avg(xgb_close_edges),
        "mc_clv_edge":        _avg(mc_close_edges),
        "xgb_clv_pos_rate":   _clv_pos(xgb_close_edges),
        "mc_clv_pos_rate":    _clv_pos(mc_close_edges),
        "avg_mc_std":         _avg(mc_stds),
        "avg_mc_spread":      _avg(mc_spreads),
        "mc_verdict":         mc_verdict,
        "wide_mc_accuracy":   wide_acc,
        "narrow_mc_accuracy": narrow_acc,
    }


# ─── Per-market evaluation (single window) ───────────────────────────────────────

def evaluate_market(market: str, picks: List[dict],
                     kelly_frac: float = 0.25) -> dict:
    probs:    List[float] = []
    outcomes: List[int]   = []
    edges_close: List[float] = []
    clv_edges:   List[float] = []
    flat_pl:     List[float] = []
    kelly_pl:    List[float] = []
    n_pushes = 0

    for r in picks:
        side = r.get("recommendedSide") or "Over"
        p = pick_prob_for_side(r.get(_CAL_PROB_FIELD), side)
        if p is None:
            continue
        grade = r.get("grade")
        if grade == "push":
            n_pushes += 1
            continue
        y = 1 if grade == "win" else 0
        probs.append(p)
        outcomes.append(y)

        open_dec  = american_to_decimal(r.get("openingPrice"))
        close_dec = american_to_decimal(r.get("closingPrice"))
        close_imp = r.get("closingImplied")
        open_imp  = r.get("openingImplied")
        clv       = r.get("clvEdge")

        if close_imp is not None:
            edges_close.append(p - float(close_imp))
        if clv is not None:
            clv_edges.append(float(clv))
        elif open_imp is not None and close_imp is not None:
            clv_edges.append(float(close_imp) - float(open_imp))

        if open_dec is not None:
            flat_pl.append((open_dec - 1.0) if y == 1 else -1.0)
            f_full = kelly_fraction(p, open_dec)
            f_use  = max(0.0, kelly_frac * f_full)
            if f_use > 0:
                kelly_pl.append(f_use * (open_dec - 1.0) if y == 1 else -f_use)

    n = len(outcomes)
    summary: Dict[str, object] = {
        "market":         market,
        "n_graded":       n,
        "n_pushes":       n_pushes,
        "win_rate":       round(sum(outcomes) / n, 4) if n else None,
        "avg_model_prob": round(sum(probs) / n, 4)    if n else None,
    }
    if n == 0:
        summary["note"] = "no graded picks in window"
        return summary

    summary["calibration"] = {
        "brier":       brier_score(probs, outcomes),
        "brier_skill": brier_skill_score(probs, outcomes),
        "log_loss":    log_loss(probs, outcomes),
        "auc_roc":     auc_roc(probs, outcomes),
        "ece":         expected_calibration_error(probs, outcomes, 10),
        "bins":        reliability_bins(probs, outcomes, 10),
    }

    def _avg(xs): return round(sum(xs) / len(xs), 4) if xs else None
    summary["clv"] = {
        "n_with_close":       len(edges_close),
        "avg_edge_vs_close":  _avg(edges_close),
        "avg_clv":            _avg(clv_edges),
        "clv_positive_rate":  round(sum(1 for c in clv_edges if c > 0) / len(clv_edges), 4)
                               if clv_edges else None,
    }

    summary["roi"] = {
        "n_with_open_odds": len(flat_pl),
        "flat_unit_pl":     round(sum(flat_pl), 3),
        "flat_unit_roi":    round(sum(flat_pl) / len(flat_pl), 4) if flat_pl else None,
        "kelly_frac":       kelly_frac,
        "kelly_n_bets":     len(kelly_pl),
        "kelly_total_pl":   round(sum(kelly_pl), 4),
    }

    if n < 30:
        summary["warning"] = f"small sample (n={n}) — metrics are noisy"

    return summary


# ─── Rolling window trend + drift detection ─────────────────────────────────────────

def _date_cutoff(days_back: int) -> str:
    return (datetime.utcnow() - timedelta(days=days_back)).strftime("%Y-%m-%d")


def rolling_trend(all_rows: List[dict], market: str,
                  kelly_frac: float = 0.25,
                  windows: List[int] = None) -> dict:
    if windows is None:
        windows = ROLLING_WINDOWS
    result: Dict[str, dict] = {}
    for days in windows:
        since = _date_cutoff(days)
        picks = filter_picks(all_rows, since=since, until=None, market=None)
        picks = [r for r in picks if r.get("marketKey") == market]
        metrics = evaluate_market(f"{market}_{days}d", picks, kelly_frac)
        result[f"{days}d"] = {
            "since":        since,
            "n":            metrics.get("n_graded", 0),
            "win_rate":     metrics.get("win_rate"),
            "brier":        metrics.get("calibration", {}).get("brier")       if metrics.get("calibration") else None,
            "bss":          metrics.get("calibration", {}).get("brier_skill") if metrics.get("calibration") else None,
            "ece":          metrics.get("calibration", {}).get("ece")         if metrics.get("calibration") else None,
            "avg_clv":      metrics.get("clv", {}).get("avg_clv"),
            "clv_pos_rate": metrics.get("clv", {}).get("clv_positive_rate"),
            "flat_roi":     metrics.get("roi", {}).get("flat_unit_roi"),
        }
    return result


def detect_drift(trend: Dict[str, dict], market: str) -> List[dict]:
    alerts = []
    w14 = trend.get("14d", {})
    w90 = trend.get("90d", {})
    if not w14 or not w90:
        return alerts
    if w14.get("n", 0) < 10 or w90.get("n", 0) < 20:
        return alerts

    b14 = w14.get("brier");  b90 = w90.get("brier")
    if b14 is not None and b90 is not None and b14 - b90 > DRIFT_BRIER_THRESHOLD:
        alerts.append({
            "market":    market,
            "type":      "BRIER_DRIFT",
            "severity":  "warn",
            "msg":       f"14d Brier ({b14:.4f}) is {b14-b90:.4f} above 90d baseline ({b90:.4f}). Model may be drifting.",
            "14d_brier": b14,
            "90d_brier": b90,
            "delta":     round(b14 - b90, 4),
        })

    c14 = w14.get("clv_pos_rate")
    if c14 is not None and c14 < CLV_POSITIVE_FLOOR:
        alerts.append({
            "market":           market,
            "type":             "CLV_DECLINE",
            "severity":         "warn",
            "msg":              f"14d CLV+ rate ({c14:.1%}) is below floor ({CLV_POSITIVE_FLOOR:.1%}). Value edge softening.",
            "14d_clv_pos_rate": c14,
            "floor":            CLV_POSITIVE_FLOOR,
        })

    bss14 = w14.get("bss")
    if bss14 is not None and bss14 < 0:
        alerts.append({
            "market":   market,
            "type":     "BSS_NEGATIVE",
            "severity": "critical",
            "msg":      f"14d Brier Skill Score is {bss14:.4f} (negative = worse than naive baseline). Immediate review needed.",
            "14d_bss":  bss14,
        })

    return alerts


def build_rolling_report(all_rows: List[dict], markets: List[str],
                          kelly_frac: float = 0.25) -> dict:
    per_market_trend: Dict[str, dict] = {}
    all_alerts: List[dict] = []
    for market in markets:
        trend = rolling_trend(all_rows, market, kelly_frac)
        per_market_trend[market] = trend
        alerts = detect_drift(trend, market)
        all_alerts.extend(alerts)
    return {
        "generatedAt":   datetime.utcnow().isoformat() + "Z",
        "windows_days":  ROLLING_WINDOWS,
        "per_market":    per_market_trend,
        "alerts":        all_alerts,
        "n_alerts":      len(all_alerts),
        "alert_summary": {
            "critical": sum(1 for a in all_alerts if a.get("severity") == "critical"),
            "warn":     sum(1 for a in all_alerts if a.get("severity") == "warn"),
        }
    }


def write_drift_alerts(rolling_report: dict,
                        alerts_path: str = DRIFT_ALERTS_PATH) -> None:
    slim = {
        "generatedAt":   rolling_report["generatedAt"],
        "n_alerts":      rolling_report["n_alerts"],
        "alert_summary": rolling_report["alert_summary"],
        "alerts":        rolling_report["alerts"],
        "sparklines": {
            market: {
                w: {
                    "n":        d.get("n"),
                    "brier":    d.get("brier"),
                    "bss":      d.get("bss"),
                    "clv_pos":  d.get("clv_pos_rate"),
                    "flat_roi": d.get("flat_roi"),
                }
                for w, d in windows.items()
            }
            for market, windows in rolling_report["per_market"].items()
        }
    }
    os.makedirs(os.path.dirname(os.path.abspath(alerts_path)) or ".", exist_ok=True)
    with open(alerts_path, "w") as f:
        json.dump(slim, f, indent=2)


# ─── Reliability plot ─────────────────────────────────────────────────────────────────

def render_reliability_plot(per_market: dict, out_path: str) -> Optional[str]:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("[plot] matplotlib not installed — skipping diagram", flush=True)
        return None

    markets = [m for m, s in per_market.items() if s.get("calibration")]
    if not markets:
        return None

    cols = min(3, len(markets))
    rows = (len(markets) + cols - 1) // cols
    fig, axes = plt.subplots(rows, cols, figsize=(4.5 * cols, 4.0 * rows), squeeze=False)

    for i, market in enumerate(markets):
        ax = axes[i // cols][i % cols]
        bins = per_market[market]["calibration"]["bins"]
        xs = [b["avg_prob"] for b in bins]
        ys = [b["hit_rate"] for b in bins]
        ns = [b["n"]        for b in bins]
        sizes = [max(20, n * 12) for n in ns]
        ax.plot([0, 1], [0, 1], color="gray", linestyle="--", alpha=0.6)
        ax.scatter(xs, ys, s=sizes, alpha=0.7)
        ax.set_xlim(0, 1); ax.set_ylim(0, 1)
        ax.set_xlabel("model probability")
        ax.set_ylabel("observed hit rate")
        cal   = per_market[market]["calibration"]
        ece   = cal.get("ece");  bss = cal.get("brier_skill")
        n_val = per_market[market]["n_graded"]
        ax.set_title(f"{market}\nECE {ece}  BSS {bss}  N {n_val}")
        ax.grid(True, alpha=0.25)

    for j in range(len(markets), rows * cols):
        axes[j // cols][j % cols].axis("off")

    fig.tight_layout()
    fig.savefig(out_path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    return out_path


# ─── CLI print helpers ─────────────────────────────────────────────────────────────────

def print_human_summary(report: dict) -> None:
    print(f"\n=== Evaluation Report ({report['n_total']:,} graded picks) ===")
    print(f"window: {report['window']['since'] or 'all'} → {report['window']['until'] or 'now'}")
    if report.get("warnings"):
        for w in report["warnings"]:
            print(f"  ⚠ {w}")
    for market, m in report["per_market"].items():
        if m.get("note"):
            print(f"\n[{market}] {m['note']}")
            continue
        cal = m["calibration"]
        clv = m["clv"]
        roi = m["roi"]
        bss = cal.get("brier_skill")
        print(f"\n[{market}] n={m['n_graded']} (pushes {m['n_pushes']}) · win-rate {m['win_rate']:.3f}")
        print(f"  Calibration  Brier {cal['brier']} · BSS {bss} · LogLoss {cal['log_loss']} · AUC {cal['auc_roc']} · ECE {cal['ece']}")
        avg_clv = clv.get("avg_clv");   pos = clv.get("clv_positive_rate")
        edge    = clv.get("avg_edge_vs_close")
        print(f"  CLV          edge-vs-close {edge if edge is not None else '—'} · "
              f"avg-CLV {avg_clv if avg_clv is not None else '—'} · "
              f"CLV+ rate {pos if pos is not None else '—'} ({clv['n_with_close']} with close)")
        roi_v = roi.get("flat_unit_roi");  pl = roi.get("flat_unit_pl")
        kpl   = roi.get("kelly_total_pl")
        print(f"  ROI          flat 1u: {pl:+.2f}u ({roi_v:+.4f}) · "
              f"Kelly {roi['kelly_frac']}: {kpl:+.4f} bankroll units ({roi['kelly_n_bets']} bets)")
        if m.get("warning"):
            print(f"  ⚠ {m['warning']}")


def print_mc_comparison(results: dict) -> None:
    print("\n=== MC vs XGB Head-to-Head ===")
    print(f"  {'MARKET':<28} {'XGB-B':>7} {'MC-B':>7} {'Δ':>7} {'VERDICT':<16} {'XGB-CLV+':>9} {'MC-CLV+':>9} {'AVG-SPREAD':>11}")
    print("  " + "-" * 100)
    for market, r in results.items():
        if r.get("n", 0) == 0:
            continue
        xb  = f"{r['xgb_brier']:.4f}"  if r.get("xgb_brier")  is not None else "  —   "
        mb  = f"{r['mc_brier']:.4f}"   if r.get("mc_brier")   is not None else "  —   "
        d   = f"{r['brier_delta']:+.4f}" if r.get("brier_delta") is not None else "  —   "
        ver = r.get("mc_verdict", "")
        xcp = f"{r['xgb_clv_pos_rate']:.1%}" if r.get("xgb_clv_pos_rate") is not None else "—"
        mcp = f"{r['mc_clv_pos_rate']:.1%}"  if r.get("mc_clv_pos_rate")  is not None else "—"
        sp  = f"{r['avg_mc_spread']:.4f}"     if r.get("avg_mc_spread")    is not None else "—"
        icon = "✅" if ver == "MC_BETTER" else ("❌" if ver == "XGB_BETTER" else "↔")
        print(f"  {market:<28} {xb:>7} {mb:>7} {d:>7} {icon} {ver:<14} {xcp:>9} {mcp:>9} {sp:>11}")
        if r.get("wide_mc_accuracy") is not None:
            print(f"    narrow CI accuracy: {r['narrow_mc_accuracy']:.3f}  vs  wide CI accuracy: {r['wide_mc_accuracy']:.3f}")
    print()


def print_rolling_summary(rolling: dict) -> None:
    print("\n=== Rolling Window Trend ===")
    headers = ["MARKET", "7d-B", "14d-B", "30d-B", "90d-B", "14d-BSS", "14d-CLV+", "14d-ROI", "14d-N"]
    rows_out = []
    for market, windows in rolling["per_market"].items():
        rows_out.append([
            market,
            f"{windows['7d']['brier']}"  if windows["7d"].get("brier")  is not None else "—",
            f"{windows['14d']['brier']}" if windows["14d"].get("brier") is not None else "—",
            f"{windows['30d']['brier']}" if windows["30d"].get("brier") is not None else "—",
            f"{windows['90d']['brier']}" if windows["90d"].get("brier") is not None else "—",
            f"{windows['14d']['bss']}"   if windows["14d"].get("bss")   is not None else "—",
            f"{windows['14d']['clv_pos_rate']:.2%}" if windows["14d"].get("clv_pos_rate") is not None else "—",
            f"{windows['14d']['flat_roi']:+.4f}" if windows["14d"].get("flat_roi") is not None else "—",
            str(windows["14d"].get("n", 0)),
        ])
    col_w = [max(len(h), max((len(r[i]) for r in rows_out), default=0)) for i, h in enumerate(headers)]
    fmt   = "  ".join(f"{{:<{w}}}" for w in col_w)
    print("  " + fmt.format(*headers))
    print("  " + "  ".join("-" * w for w in col_w))
    for row in rows_out:
        print("  " + fmt.format(*row))
    if rolling["alerts"]:
        print(f"\n  🚨 {len(rolling['alerts'])} DRIFT ALERT(S):")
        for a in rolling["alerts"]:
            sev = "🔴 CRITICAL" if a["severity"] == "critical" else "🟡 WARN"
            print(f"    {sev} [{a['market']}] {a['msg']}")
    else:
        print("\n  ✅ No drift alerts detected across all windows.")


# ─── CLI ─────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--tracker",    default=TRACKER_PATH_DEFAULT)
    p.add_argument("--since",      default=None,  help="YYYY-MM-DD")
    p.add_argument("--until",      default=None,  help="YYYY-MM-DD")
    p.add_argument("--market",     default=None,  help="e.g. batter_hits")
    p.add_argument("--min-prob",   type=float, default=0.0)
    p.add_argument("--max-prob",   type=float, default=1.0)
    p.add_argument("--kelly",      type=float, default=0.25)
    p.add_argument("--out",        default=None,  help="Write JSON report here")
    p.add_argument("--plot",       action="store_true", help="Save reliability PNG")
    p.add_argument("--quiet",      action="store_true")
    p.add_argument("--rolling",    action="store_true",
                   help="Compute rolling 7/14/30/90d trend + drift alerts")
    p.add_argument("--compare-mc", action="store_true",
                   help="MC vs XGB head-to-head Brier + CLV comparison (Step 4)")
    p.add_argument("--drift-out",  default=DRIFT_ALERTS_PATH,
                   help=f"Where to write drift alerts JSON (default: {DRIFT_ALERTS_PATH})")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    all_rows = load_tracker(args.tracker)

    # ── Rolling mode ─────────────────────────────────────────────────────────────────
    if args.rolling:
        market_keys = sorted({
            r.get("marketKey") or "unknown"
            for r in all_rows
            if (r.get("grade") or r.get("status")) in ("win", "loss", "push", "graded")
            and r.get(_CAL_PROB_FIELD) is not None
        })
        if args.market:
            market_keys = [k for k in market_keys if k == args.market]
        rolling = build_rolling_report(all_rows, market_keys, args.kelly)
        write_drift_alerts(rolling, args.drift_out)
        if not args.quiet:
            print_rolling_summary(rolling)
        if args.out:
            os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
            with open(args.out, "w") as f:
                json.dump(rolling, f, indent=2)
            print(f"[out] wrote {args.out}", flush=True)
        print(f"[drift] alerts written to {args.drift_out}", flush=True)
        return 0

    # ── MC comparison mode (Step 4) ───────────────────────────────────────────────
    if args.compare_mc:
        market_keys = sorted({
            r.get("marketKey") or "unknown"
            for r in all_rows
            if (r.get("grade") or r.get("status")) in ("win", "loss")
            and r.get(_CAL_PROB_FIELD) is not None
        })
        if args.market:
            market_keys = [k for k in market_keys if k == args.market]
        picks_by_market = defaultdict(list)
        for r in all_rows:
            if (r.get("grade") or r.get("status")) in ("win", "loss"):
                if args.since and (r.get("date") or "") < args.since: continue
                if args.until and (r.get("date") or "") > args.until: continue
                picks_by_market[r.get("marketKey") or "unknown"].append(r)
        mc_results = {
            mk: evaluate_mc_vs_xgb(picks_by_market[mk], mk)
            for mk in market_keys
        }
        if not args.quiet:
            print_mc_comparison(mc_results)
        if args.out:
            os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
            with open(args.out, "w") as f:
                json.dump(mc_results, f, indent=2)
            print(f"[out] wrote {args.out}", flush=True)
        return 0

    # ── Standard (single-window) mode ─────────────────────────────────────────────
    picks = filter_picks(all_rows, args.since, args.until, args.market,
                          args.min_prob, args.max_prob)
    by_market: Dict[str, List[dict]] = defaultdict(list)
    for r in picks:
        by_market[r.get("marketKey") or "unknown"].append(r)
    per_market: Dict[str, dict] = {}
    for market in sorted(by_market):
        per_market[market] = evaluate_market(market, by_market[market], args.kelly)
    report = {
        "generatedAt": datetime.utcnow().isoformat() + "Z",
        "tracker":     os.path.abspath(args.tracker),
        "window":      {"since": args.since, "until": args.until},
        "filter":      {"market": args.market, "minProb": args.min_prob, "maxProb": args.max_prob},
        "n_total":     len(picks),
        "n_markets":   len(per_market),
        "warnings":    [],
        "per_market":  per_market,
    }
    if not picks:
        report["warnings"].append("no graded picks matched filters — nothing to evaluate")
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
        with open(args.out, "w") as f:
            json.dump(report, f, indent=2)
        print(f"[out] wrote {args.out}", flush=True)
    if args.plot:
        plot_path = (os.path.splitext(args.out)[0] + "_reliability.png") if args.out \
                    else "eval_reliability.png"
        path = render_reliability_plot(per_market, plot_path)
        if path:
            print(f"[plot] wrote {path}", flush=True)
    if not args.quiet:
        print_human_summary(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
