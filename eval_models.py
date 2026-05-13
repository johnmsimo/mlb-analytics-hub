"""
eval_models.py — Calibration + CLV evaluation for graded BvP picks.

Reads `data/daily_tracker.json`, filters to graded picks, and computes
per-market reliability and value-vs-the-book metrics. Designed to answer
two questions:

  1. Are our model probabilities CALIBRATED?
     - Brier score, log loss, AUC-ROC
     - Expected Calibration Error (ECE) + 10-bucket reliability table
     - Optional reliability diagram PNG (matplotlib)

  2. Are our picks BEATING THE BOOK?
     - Average CLV (closingImplied - openingImplied)
     - CLV-positive rate
     - Model edge vs close (adjProb - closingImplied)
     - ROI at flat-1-unit stake
     - ROI at fractional-Kelly stake

Usage:
  python eval_models.py                                  # all graded picks, all markets
  python eval_models.py --since 2026-04-01               # filter by date
  python eval_models.py --market batter_hits             # one market
  python eval_models.py --kelly 0.25 --plot --out reports/eval_2026.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional


TRACKER_PATH_DEFAULT = "data/daily_tracker.json"


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────

def american_to_decimal(american) -> Optional[float]:
    """+150 → 2.50, -180 → 1.5556. Returns None when input isn't parseable."""
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
    """Full-Kelly fraction for a binary bet at decimal odds `dec_odds` and
    true win prob `p`. Returns 0 when there's no edge.
    """
    if dec_odds is None or dec_odds <= 1.0 or p is None:
        return 0.0
    b = dec_odds - 1.0
    f = (p * (b + 1) - 1) / b
    return max(0.0, f)


def pick_prob_for_side(adj_prob: float, side: str) -> Optional[float]:
    """`adjProb` is stored as the side's hit probability when `recommendedSide`
    is set; for older rows where it's the over-prob, flip it for Under picks.
    """
    if adj_prob is None:
        return None
    side_norm = (side or "Over").strip().lower()
    if side_norm == "under":
        # If the value is < 0.5 it's already under-prob; otherwise treat as over-prob.
        # In practice _recalc_tracker_entry stores adjProb as the recommended-side prob,
        # so this branch is only for legacy rows.
        return float(adj_prob) if adj_prob < 0.5 else round(1.0 - float(adj_prob), 4)
    return float(adj_prob)


def reliability_bins(probs: List[float], outcomes: List[int],
                     n_bins: int = 10) -> List[dict]:
    """Standard reliability table: bucket probs into n_bins, report bucket
    avg prob and observed hit rate plus N. Used for ECE + diagram.
    """
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
    """ECE = Σ (n_bin / N) × |bucket_p - bucket_hit_rate|."""
    total = sum(1 for p in probs if p is not None)
    if total == 0:
        return 0.0
    bins = reliability_bins(probs, outcomes, n_bins)
    return round(sum(b["n"] / total * abs(b["avg_prob"] - b["hit_rate"]) for b in bins), 4)


def auc_roc(probs: List[float], outcomes: List[int]) -> Optional[float]:
    """Mann-Whitney AUC. Returns None if only one class present (undefined)."""
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


# ────────────────────────────────────────────────────────────────────────────
# Tracker loading + filtering
# ────────────────────────────────────────────────────────────────────────────

def load_tracker(path: str) -> List[dict]:
    """Flatten `{date: {entries: [...]}}` into a single list of pick rows
    with the `date` field guaranteed populated.
    """
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
        p = r.get("adjProb")
        if p is None:
            continue
        if p < min_prob or p > max_prob:               continue
        out.append(r)
    return out


# ────────────────────────────────────────────────────────────────────────────
# Per-market evaluation
# ────────────────────────────────────────────────────────────────────────────

def evaluate_market(market: str, picks: List[dict],
                     kelly_frac: float = 0.25) -> dict:
    """Calibration + CLV + ROI metrics for a single market."""
    probs:    List[float] = []
    outcomes: List[int]   = []
    edges_close: List[float] = []
    clv_edges:   List[float] = []
    flat_pl:     List[float] = []
    kelly_pl:    List[float] = []
    n_pushes = 0

    for r in picks:
        side = r.get("recommendedSide") or "Over"
        p = pick_prob_for_side(r.get("adjProb"), side)
        if p is None:
            continue
        grade = r.get("grade")
        if grade == "push":
            n_pushes += 1
            continue
        y = 1 if grade == "win" else 0
        probs.append(p)
        outcomes.append(y)

        # Odds-based metrics
        open_dec  = american_to_decimal(r.get("openingPrice"))
        close_dec = american_to_decimal(r.get("closingPrice"))
        close_imp = r.get("closingImplied")
        open_imp  = r.get("openingImplied")
        clv       = r.get("clvEdge")

        # Model edge vs the closing implied probability (positive = model says
        # this side is more likely than the close suggests).
        if close_imp is not None:
            edges_close.append(p - float(close_imp))
        # CLV (line moved toward us by close)
        if clv is not None:
            clv_edges.append(float(clv))
        elif open_imp is not None and close_imp is not None:
            clv_edges.append(float(close_imp) - float(open_imp))

        # ROI at flat 1u stake based on OPENING odds (what we actually bet)
        if open_dec is not None:
            flat_pl.append((open_dec - 1.0) if y == 1 else -1.0)

            # Fractional Kelly using the same odds + model prob
            f_full = kelly_fraction(p, open_dec)
            f_use  = max(0.0, kelly_frac * f_full)
            if f_use > 0:
                kelly_pl.append(f_use * (open_dec - 1.0) if y == 1 else -f_use)

    n = len(outcomes)
    summary: Dict[str, object] = {
        "market":       market,
        "n_graded":     n,
        "n_pushes":     n_pushes,
        "win_rate":     round(sum(outcomes) / n, 4) if n else None,
        "avg_model_prob": round(sum(probs) / n, 4) if n else None,
    }
    if n == 0:
        summary["note"] = "no graded picks in window"
        return summary

    # Calibration block
    summary["calibration"] = {
        "brier":    brier_score(probs, outcomes),
        "log_loss": log_loss(probs, outcomes),
        "auc_roc":  auc_roc(probs, outcomes),
        "ece":      expected_calibration_error(probs, outcomes, 10),
        "bins":     reliability_bins(probs, outcomes, 10),
    }

    # CLV / value-vs-book block
    def _avg(xs): return round(sum(xs) / len(xs), 4) if xs else None
    summary["clv"] = {
        "n_with_close":    len(edges_close),
        "avg_edge_vs_close": _avg(edges_close),         # model-prob - close-implied
        "avg_clv":         _avg(clv_edges),             # close-implied - open-implied
        "clv_positive_rate": round(sum(1 for c in clv_edges if c > 0) / len(clv_edges), 4)
                              if clv_edges else None,
    }

    # ROI block
    summary["roi"] = {
        "n_with_open_odds": len(flat_pl),
        "flat_unit_pl":     round(sum(flat_pl), 3),
        "flat_unit_roi":    round(sum(flat_pl) / len(flat_pl), 4) if flat_pl else None,
        "kelly_frac":       kelly_frac,
        "kelly_n_bets":     len(kelly_pl),
        "kelly_total_pl":   round(sum(kelly_pl), 4),
    }

    # Sample-size warning
    if n < 30:
        summary["warning"] = f"small sample (n={n}) — metrics are noisy"

    return summary


def render_reliability_plot(per_market: dict, out_path: str) -> Optional[str]:
    """Save a multi-panel reliability diagram. Returns path or None on failure."""
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
        ax.plot([0, 1], [0, 1], color="gray", linestyle="--", alpha=0.6, label="perfect")
        ax.scatter(xs, ys, s=sizes, alpha=0.7, label=f"buckets (n shown by size)")
        ax.set_xlim(0, 1); ax.set_ylim(0, 1)
        ax.set_xlabel("model probability")
        ax.set_ylabel("observed hit rate")
        ece = per_market[market]["calibration"]["ece"]
        n   = per_market[market]["n_graded"]
        ax.set_title(f"{market}\nECE {ece} · N {n}")
        ax.grid(True, alpha=0.25)

    # Hide any unused axes
    for j in range(len(markets), rows * cols):
        axes[j // cols][j % cols].axis("off")

    fig.tight_layout()
    fig.savefig(out_path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    return out_path


# ────────────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--tracker", default=TRACKER_PATH_DEFAULT,
                   help=f"Tracker JSON path (default: {TRACKER_PATH_DEFAULT})")
    p.add_argument("--since",  default=None,
                   help="Only include picks on/after this date (YYYY-MM-DD)")
    p.add_argument("--until",  default=None,
                   help="Only include picks on/before this date (YYYY-MM-DD)")
    p.add_argument("--market", default=None,
                   help="Filter to one marketKey (e.g. batter_hits)")
    p.add_argument("--min-prob", type=float, default=0.0,
                   help="Filter picks with model prob < this (default 0)")
    p.add_argument("--max-prob", type=float, default=1.0,
                   help="Filter picks with model prob > this (default 1)")
    p.add_argument("--kelly",  type=float, default=0.25,
                   help="Fractional Kelly multiplier (default 0.25)")
    p.add_argument("--out",    default=None,
                   help="Write JSON report to this path (default: stdout only)")
    p.add_argument("--plot",   action="store_true",
                   help="Save reliability diagram PNG next to --out (or in CWD)")
    p.add_argument("--quiet",  action="store_true",
                   help="Suppress per-market human-readable summary")
    return p.parse_args()


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
        print(f"\n[{market}] n={m['n_graded']} (pushes {m['n_pushes']}) · win-rate {m['win_rate']:.3f} · model-prob μ {m['avg_model_prob']:.3f}")
        print(f"  Calibration  Brier {cal['brier']} · LogLoss {cal['log_loss']} · "
              f"AUC {cal['auc_roc']} · ECE {cal['ece']}")
        avg_clv = clv.get("avg_clv");   pos = clv.get("clv_positive_rate")
        edge    = clv.get("avg_edge_vs_close")
        print(f"  CLV          edge-vs-close {edge if edge is not None else '—'} · "
              f"avg-CLV {avg_clv if avg_clv is not None else '—'} · "
              f"CLV+ rate {pos if pos is not None else '—'} ({clv['n_with_close']} with close)")
        roi_v = roi.get("flat_unit_roi");  pl = roi.get("flat_unit_pl")
        kpl   = roi.get("kelly_total_pl")
        print(f"  ROI          flat 1u: {pl:+.2f}u total ({roi_v:+.4f} ROI) · "
              f"Kelly {roi['kelly_frac']}: {kpl:+.4f} bankroll units across {roi['kelly_n_bets']} bets")
        if m.get("warning"):
            print(f"  ⚠ {m['warning']}")


def main() -> int:
    args = parse_args()
    rows = load_tracker(args.tracker)
    picks = filter_picks(rows, args.since, args.until, args.market,
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
    # Top-level warnings
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
