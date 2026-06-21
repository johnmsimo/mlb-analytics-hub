"""
prop_calibration.py — per-market probability recalibration layer.

The raw model probabilities surfaced as ``adjProb`` are systematically
miscalibrated on several markets (see ``/api/calibration/markets``): totals,
total_bases, h2h and f5_totals run over-confident (predicted rate well above
the realized rate), while ``batter_hits`` barely beats its base rate. Left
uncorrected this produces head-line "edges" of 30-40 points that are pure
miscalibration, not value.

This module fits a per-market correction from the app's OWN graded tracker
history — the same (model probability, win/loss) pairs the calibration readout
already trusts — and applies it to model probabilities BEFORE edge / EV / hub
are computed, so what the UI shows reflects realized accuracy rather than the
model's optimism.

Two correction tiers, chosen automatically by sample size:

  1. **Isotonic regression** (``>= ISO_MIN_N`` graded picks for the market and
     scikit-learn available) — a full monotonic recalibration curve mapping
     predicted -> realized probability.
  2. **Log-odds recentre** (``>= SHRINK_MIN_N`` picks) — shifts the market's
     mean predicted probability onto its realized base rate, killing the
     systematic over/under bias without needing a large sample, then shrinks
     lightly toward that base rate to damp residual over-dispersion.

Below ``SHRINK_MIN_N`` the mapping is the identity (too little signal to act
on). The module has NO dependency on app internals: the caller supplies
``{market_key: [(prob, outcome01), ...]}`` and everything degrades to identity
when there is no history.

Feedback-loop note: callers MUST fit on the pre-calibration probability (the
value before ``calibrate`` is applied) and persist it separately (``preCalProb``)
so re-fitting never compounds a previous correction.
"""

from __future__ import annotations

import math
import threading

# Sample-size gates.
SHRINK_MIN_N = 20    # below this: identity (live Brier is too noisy to trust)
ISO_MIN_N = 80       # at/above this (with sklearn): full isotonic curve

# A light shrink toward the realized base rate applied on top of the log-odds
# recentre, to damp residual over-dispersion. 0 = none, 1 = collapse to base.
SHRINK_TO_BASE = 0.15

try:  # scikit-learn is already a transitive dep (CalibratedClassifierCV etc.)
    from sklearn.isotonic import IsotonicRegression
    _HAVE_SK = True
except Exception:  # pragma: no cover - defensive
    IsotonicRegression = None  # type: ignore
    _HAVE_SK = False


def _logit(p: float) -> float:
    p = min(max(float(p), 1e-6), 1.0 - 1e-6)
    return math.log(p / (1.0 - p))


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    ez = math.exp(z)
    return ez / (1.0 + ez)


class MarketCalibrator:
    """Calibration for a single market, fit from (prob, outcome) pairs."""

    def __init__(self, market_key: str):
        self.market_key = market_key
        self.n = 0
        self.kind = "identity"          # identity | isotonic | logodds_shift
        self._iso = None
        self.mean_pred = None
        self.mean_actual = None
        self.base_rate = None
        self.shift = 0.0                # log-odds shift for the fallback
        self.brier = None
        self.baserate_brier = None

    # -- fitting ---------------------------------------------------------------
    def fit(self, pairs):
        clean = []
        for p, y in pairs:
            try:
                p = float(p)
                y = float(y)
            except (TypeError, ValueError):
                continue
            if not (0.0 <= p <= 1.0) or y not in (0.0, 1.0):
                continue
            clean.append((p, y))

        self.n = len(clean)
        if self.n < SHRINK_MIN_N:
            self.kind = "identity"
            return self

        xs = [p for p, _ in clean]
        ys = [y for _, y in clean]
        self.mean_pred = sum(xs) / self.n
        self.mean_actual = sum(ys) / self.n
        self.base_rate = self.mean_actual
        self.brier = sum((p - y) ** 2 for p, y in clean) / self.n
        self.baserate_brier = self.mean_actual * (1.0 - self.mean_actual)

        if _HAVE_SK and self.n >= ISO_MIN_N:
            try:
                iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
                iso.fit(xs, ys)
                self._iso = iso
                self.kind = "isotonic"
                return self
            except Exception:
                self._iso = None  # fall through to the log-odds recentre

        # Fallback: recentre predictions so their mean lands on the realized
        # base rate (removes the systematic over/under bias).
        self.shift = _logit(self.mean_actual) - _logit(self.mean_pred)
        self.kind = "logodds_shift"
        return self

    # -- applying --------------------------------------------------------------
    def apply(self, prob):
        if prob is None:
            return prob
        try:
            p = float(prob)
        except (TypeError, ValueError):
            return prob
        if self.kind == "identity":
            return p

        if self.kind == "isotonic" and self._iso is not None:
            try:
                out = float(self._iso.predict([min(max(p, 0.0), 1.0)])[0])
                return min(max(out, 0.0), 1.0)
            except Exception:
                return p

        if self.kind == "logodds_shift":
            out = _sigmoid(_logit(p) + self.shift)
            if self.base_rate is not None and SHRINK_TO_BASE > 0.0:
                out = (1.0 - SHRINK_TO_BASE) * out + SHRINK_TO_BASE * self.base_rate
            return min(max(out, 0.0), 1.0)

        return p

    def status(self) -> str:
        """Health flag mirroring /api/calibration/markets semantics."""
        if self.n < SHRINK_MIN_N:
            return "warming_up"
        if self.brier is not None and self.baserate_brier is not None:
            if self.brier >= self.baserate_brier:
                return "no_edge"
        return "calibrated"

    def summary(self) -> dict:
        return {
            "market": self.market_key,
            "n": self.n,
            "kind": self.kind,
            "mean_pred": round(self.mean_pred, 4) if self.mean_pred is not None else None,
            "mean_actual": round(self.mean_actual, 4) if self.mean_actual is not None else None,
            "shift": round(self.shift, 4),
            "status": self.status(),
        }


class PropCalibrator:
    """Container holding one MarketCalibrator per market."""

    def __init__(self):
        self._markets = {}
        self._lock = threading.Lock()

    @classmethod
    def fit_from_entries(cls, by_market: dict):
        inst = cls()
        for mk, pairs in (by_market or {}).items():
            inst._markets[mk] = MarketCalibrator(mk).fit(pairs)
        return inst

    def calibrate(self, market_key, prob):
        cal = self._markets.get(market_key)
        if cal is None:
            return prob
        return cal.apply(prob)

    def status(self, market_key) -> str:
        cal = self._markets.get(market_key)
        return cal.status() if cal is not None else "warming_up"

    def summary(self) -> dict:
        return {mk: c.summary() for mk, c in sorted(self._markets.items())}
