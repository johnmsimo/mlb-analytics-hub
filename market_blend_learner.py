"""
market_blend_learner.py — learned model-vs-market blend weights.

``logit_blend_prob`` fuses the model probability with the de-vig'd market
probability in log-odds space: ``σ(w·logit(p_model) + (1−w)·logit(p_market))``.
The per-market ``w`` in ``MARKET_MODEL_WEIGHTS`` was hand-tuned from market
-efficiency intuition (props ~0.30-0.45, full-game h2h/totals 0.20). That's a
reasonable prior — but it is a guess, and the optimal mix is *measurable* from
the app's own graded picks, exactly the way Smart-Consensus replaced the
XGB/BATX coverage heuristic with measured per-model Brier.

This module fits, per market, the Brier-optimal ``w`` over graded tracker
history and shrinks it toward the hand-tuned prior by sample size, so:

  • below ``MIN_N`` graded picks the prior is used unchanged (no fabricated
    signal from noise);
  • at moderate samples the weight moves only modestly (``n·ŵ + N0·prior`` over
    ``n + N0`` pseudo-counts);
  • with a large sample the measured optimum dominates.

Feedback-loop safety (same discipline as ``prop_calibration``): the fit inputs
are the persisted PRE-blend model probability (``rawMultProb``) and the market
fair probability recomputed from the persisted opening prices — neither is a
function of the weight being applied, so re-fitting never compounds a prior
correction.

Pure Python, no app imports. Self-tested: ``python market_blend_learner.py``.
"""

from __future__ import annotations

import math
import threading

# Sample-size gates / shrinkage.
MIN_N = 60          # below this: hand-tuned prior unchanged
PRIOR_N = 150       # pseudo-count weight of the prior in the shrinkage
GRID_STEPS = 50     # w grid resolution (0.02)
W_FLOOR, W_CEIL = 0.05, 0.95   # never fully ignore either the model or market


def _logit(p: float) -> float:
    p = min(max(float(p), 1e-6), 1.0 - 1e-6)
    return math.log(p / (1.0 - p))


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    ez = math.exp(z)
    return ez / (1.0 + ez)


class MarketBlendWeight:
    """Learned blend weight for a single market."""

    def __init__(self, market_key: str, prior_w: float):
        self.market_key = market_key
        self.prior_w = float(prior_w)
        self.n = 0
        self.kind = "prior"        # prior | learned
        self.fitted_w = None       # unshrunk grid optimum
        self.weight = float(prior_w)
        self.prior_brier = None    # Brier of the blend at the prior weight
        self.fitted_brier = None   # Brier at the grid optimum
        self.applied_brier = None  # Brier at the shrunk weight actually applied

    def fit(self, rows):
        """rows: iterable of (p_model, p_market_fair, outcome01).

        p_model must be the PRE-blend model probability; p_market_fair the
        de-vig'd market probability for the same side; outcome01 whether that
        side hit.
        """
        clean = []
        for pm, pf, y in rows:
            try:
                pm, pf, y = float(pm), float(pf), float(y)
            except (TypeError, ValueError):
                continue
            if not (0.0 < pm < 1.0) or not (0.0 < pf < 1.0) or y not in (0.0, 1.0):
                continue
            clean.append((_logit(pm), _logit(pf), y))

        self.n = len(clean)
        if self.n < MIN_N:
            self.kind = "prior"
            self.weight = self.prior_w
            return self

        def brier_at(w: float) -> float:
            s = 0.0
            for lm, lf, y in clean:
                p = _sigmoid(w * lm + (1.0 - w) * lf)
                s += (p - y) ** 2
            return s / self.n

        best_w, best_b = None, None
        for i in range(GRID_STEPS + 1):
            w = i / GRID_STEPS
            b = brier_at(w)
            if best_b is None or b < best_b - 1e-12:
                best_w, best_b = w, b

        self.fitted_w = best_w
        self.fitted_brier = best_b
        self.prior_brier = brier_at(self.prior_w)
        shrunk = (self.n * best_w + PRIOR_N * self.prior_w) / (self.n + PRIOR_N)
        self.weight = min(W_CEIL, max(W_FLOOR, shrunk))
        self.applied_brier = brier_at(self.weight)
        self.kind = "learned"
        return self

    def summary(self) -> dict:
        return {
            "market": self.market_key,
            "n": self.n,
            "kind": self.kind,
            "prior_w": round(self.prior_w, 4),
            "fitted_w": round(self.fitted_w, 4) if self.fitted_w is not None else None,
            "weight": round(self.weight, 4),
            "prior_brier": round(self.prior_brier, 5) if self.prior_brier is not None else None,
            "fitted_brier": round(self.fitted_brier, 5) if self.fitted_brier is not None else None,
            "applied_brier": round(self.applied_brier, 5) if self.applied_brier is not None else None,
        }


class BlendWeights:
    """Container holding one MarketBlendWeight per market."""

    def __init__(self):
        self._markets = {}
        self._lock = threading.Lock()

    @classmethod
    def fit_from_entries(cls, by_market: dict, priors: dict, default_prior: float = 0.50):
        """by_market: {market_key: [(p_model, p_market_fair, outcome01), ...]}
        priors: the hand-tuned MARKET_MODEL_WEIGHTS table."""
        inst = cls()
        for mk, rows in (by_market or {}).items():
            prior = float((priors or {}).get(mk, default_prior))
            inst._markets[mk] = MarketBlendWeight(mk, prior).fit(rows)
        return inst

    def weight(self, market_key, prior_w):
        """The weight to use for this market; falls back to the caller's prior
        for markets with no fitted entry (identity behaviour)."""
        w = self._markets.get(market_key)
        return w.weight if w is not None else prior_w

    def summary(self) -> dict:
        return {mk: w.summary() for mk, w in sorted(self._markets.items())}


# ── Self-test ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import random

    random.seed(7)

    def synth(n, true_w, noise_model=1.4, noise_market=0.4):
        """Outcomes drawn from a latent prob; model and market observe it with
        different noise so the optimal blend weight is recoverable."""
        rows = []
        for _ in range(n):
            z = random.gauss(0.0, 1.0)             # latent log-odds
            lm = z + random.gauss(0.0, noise_model)  # noisy model read
            lf = z + random.gauss(0.0, noise_market) # sharper market read
            y = 1.0 if random.random() < _sigmoid(z) else 0.0
            rows.append((_sigmoid(lm), _sigmoid(lf), y))
        return rows

    # 1) Small sample → prior unchanged.
    w = MarketBlendWeight("batter_hits", 0.35).fit(synth(MIN_N - 1, 0.2))
    assert w.kind == "prior" and w.weight == 0.35, w.summary()

    # 2) Market much sharper than model → learned weight moves DOWN from prior.
    w = MarketBlendWeight("h2h", 0.50).fit(synth(4000, None))
    assert w.kind == "learned" and w.weight < 0.50, w.summary()
    assert w.applied_brier <= w.prior_brier + 1e-9, w.summary()

    # 3) Model sharper than market → learned weight moves UP.
    rows = synth(4000, None, noise_model=0.3, noise_market=1.5)
    w = MarketBlendWeight("batter_rbis", 0.40).fit(rows)
    assert w.weight > 0.40, w.summary()

    # 4) Shrinkage: moderate sample lands between prior and grid optimum.
    rows = synth(200, None, noise_model=0.3, noise_market=1.5)
    w = MarketBlendWeight("x", 0.30).fit(rows)
    assert min(0.30, w.fitted_w) - 1e-9 <= w.weight <= max(0.30, w.fitted_w) + 1e-9, w.summary()

    # 5) Container fallback for unfitted markets.
    bw = BlendWeights.fit_from_entries({"batter_hits": synth(500, None)}, {"batter_hits": 0.35})
    assert bw.weight("nrfi", 0.35) == 0.35
    assert 0.05 <= bw.weight("batter_hits", 0.35) <= 0.95

    # 6) Garbage rows are ignored, never raise.
    w = MarketBlendWeight("junk", 0.4).fit([(None, 0.5, 1), ("x", 0.5, 0), (0.5, 0.5, 2)])
    assert w.kind == "prior" and w.weight == 0.4

    print("market_blend_learner: all self-tests passed.")
