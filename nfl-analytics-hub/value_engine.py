"""
value_engine.py — single source of truth for the betting math.

Pure-Python (no numpy / sklearn) so it imports cleanly under the app's
`try/except ImportError` shim convention and never blocks boot. Everything in
here is deterministic and unit-testable; `python value_engine.py` runs a small
self-test.

What lives here
---------------
- Odds conversions:        american <-> decimal <-> implied probability
- De-vig (remove the book's overround) two ways:
    * multiplicative / proportional  (fast, the industry default)
    * power method (Buchdahl)          (favorite-longshot aware)
- Expected value of a wager given a *true* probability and a price
- Kelly Criterion staking, with fractional-Kelly + bankroll caps

The app already had this math scattered across app.py (`_kelly_fraction`,
`_american_to_implied`, inline EV). This module consolidates it so the Value
Bets page, the `/api/v1/edges` endpoint, and the tracker all agree on one
implementation.
"""
from __future__ import annotations

from typing import Optional, Tuple, Dict


# ─── Odds conversions ────────────────────────────────────────────────────────

def american_to_decimal(price) -> Optional[float]:
    """American odds -> decimal odds (e.g. -110 -> 1.909, +150 -> 2.50)."""
    try:
        odds = float(price)
    except (TypeError, ValueError):
        return None
    if odds == 0:
        return None
    if odds > 0:
        return 1.0 + odds / 100.0
    return 1.0 + 100.0 / abs(odds)


def decimal_to_american(dec) -> Optional[int]:
    try:
        d = float(dec)
    except (TypeError, ValueError):
        return None
    if d <= 1.0:
        return None
    if d >= 2.0:
        return int(round((d - 1.0) * 100.0))
    return int(round(-100.0 / (d - 1.0)))


def american_to_implied(price) -> Optional[float]:
    """American odds -> implied probability (vig included)."""
    dec = american_to_decimal(price)
    if dec is None:
        return None
    return 1.0 / dec


def implied_to_american(prob) -> Optional[int]:
    try:
        p = float(prob)
    except (TypeError, ValueError):
        return None
    if not (0.0 < p < 1.0):
        return None
    return decimal_to_american(1.0 / p)


# ─── De-vig (remove the overround) ───────────────────────────────────────────

def devig_two_way(over_price, under_price,
                  method: str = "multiplicative") -> Optional[Tuple[float, float]]:
    """Return the no-vig (fair) probabilities (p_over, p_under) from a two-way
    market quoted in American odds.

    method="multiplicative": p_i = q_i / (q_over + q_under)   — proportional.
    method="power": find n with q_over**n + q_under**n == 1, fair p_i = q_i**n
        (Buchdahl power method — shrinks the favorite less than proportional,
        which better matches realized win rates on lopsided lines).
    """
    q_over = american_to_implied(over_price)
    q_under = american_to_implied(under_price)
    if q_over is None or q_under is None:
        return None
    total = q_over + q_under
    if total <= 0:
        return None

    if method == "power":
        return _devig_power((q_over, q_under))

    # multiplicative / proportional default
    return q_over / total, q_under / total


def _devig_power(quotes, tol: float = 1e-9, max_iter: int = 100):
    """Solve sum(q_i ** n) == 1 for n via bisection, return q_i ** n."""
    qs = [q for q in quotes if q and q > 0]
    if len(qs) < 2:
        return None
    if abs(sum(qs) - 1.0) < tol:           # already fair
        return tuple(qs)

    def s(n):
        return sum(q ** n for q in qs)

    # sum(q) > 1 (overround) -> need n > 1 to push it down to 1.
    lo, hi = 1.0, 1.0
    # expand hi until s(hi) <= 1
    for _ in range(max_iter):
        if s(hi) <= 1.0:
            break
        hi *= 2.0
    else:
        return None
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        val = s(mid)
        if abs(val - 1.0) < tol:
            break
        if val > 1.0:
            lo = mid
        else:
            hi = mid
    n = 0.5 * (lo + hi)
    return tuple(q ** n for q in qs)


def fair_prob(over_price, under_price=None,
              method: str = "multiplicative") -> Optional[float]:
    """Fair (no-vig) probability of the OVER side.

    If only one price is supplied we cannot remove vig, so we return the raw
    vig-included implied probability (callers treat that as a conservative
    fallback — it slightly over-states the true probability)."""
    if under_price is None:
        return american_to_implied(over_price)
    devigged = devig_two_way(over_price, under_price, method=method)
    if devigged is None:
        return american_to_implied(over_price)
    return devigged[0]


# ─── Expected value ──────────────────────────────────────────────────────────

def expected_value(prob, price) -> Optional[float]:
    """EV per $1 staked, as a fraction. prob is your *true* win probability,
    price the American odds you'd take. EV = p*(dec-1) - (1-p) = p*dec - 1.

    +0.05 means +5% EV (a $100 bet returns $5 in expectation)."""
    dec = american_to_decimal(price)
    if dec is None:
        return None
    try:
        p = float(prob)
    except (TypeError, ValueError):
        return None
    return p * dec - 1.0


# ─── Kelly staking ───────────────────────────────────────────────────────────

def kelly_fraction(prob, price) -> float:
    """Full-Kelly fraction of bankroll for a single wager. Returns 0 when the
    bet is -EV (Kelly never recommends a negative stake)."""
    dec = american_to_decimal(price)
    if dec is None:
        return 0.0
    try:
        p = float(prob)
    except (TypeError, ValueError):
        return 0.0
    b = dec - 1.0
    if b <= 0:
        return 0.0
    q = 1.0 - p
    frac = (b * p - q) / b
    return max(0.0, frac)


def kelly_stake(prob, price, *,
                bankroll: float = 1000.0,
                fraction: float = 0.25,
                max_pct: float = 0.05,
                unit_pct: float = 0.01) -> Dict[str, float]:
    """Fractional-Kelly stake for a wager, capped at `max_pct` of bankroll.

    fraction=0.25 -> Quarter-Kelly (the default the roadmap asked for): lower
    variance than full Kelly with most of the growth. Returns dollars, % of
    bankroll, and units (1 unit = `unit_pct` of bankroll)."""
    full = kelly_fraction(prob, price)
    sized_pct = min(max(0.0, full * fraction), max_pct)
    unit_dollars = bankroll * unit_pct
    stake_dollars = bankroll * sized_pct
    units = stake_dollars / unit_dollars if unit_dollars > 0 else 0.0
    return {
        "full_kelly_pct": round(full, 4),
        "fraction": fraction,
        "stake_pct": round(sized_pct, 4),
        "stake_dollars": round(stake_dollars, 2),
        "stake_units": round(units, 2),
    }


def edge_grade(edge) -> Optional[str]:
    """Letter grade from edge (probability points). Mirrors the app's existing
    `_edge_letter_grade` so the engine and the routes agree."""
    if edge is None:
        return None
    try:
        e = float(edge)
    except (TypeError, ValueError):
        return None
    if e >= 0.10:  return "A+"
    if e >= 0.075: return "A"
    if e >= 0.055: return "B+"
    if e >= 0.04:  return "B"
    if e >= 0.025: return "C+"
    if e >= 0.015: return "C"
    return "D"


# ─── Self-test ───────────────────────────────────────────────────────────────

def _selftest() -> None:
    # conversions
    assert abs(american_to_decimal(-110) - 1.90909) < 1e-4
    assert abs(american_to_decimal(+150) - 2.5) < 1e-9
    assert abs(american_to_implied(-110) - 0.52381) < 1e-4
    assert decimal_to_american(2.5) == 150
    assert decimal_to_american(1.5) == -200

    # two-way devig: -110/-110 is a perfectly symmetric 50/50 after vig removal
    p_over, p_under = devig_two_way(-110, -110, method="multiplicative")
    assert abs(p_over - 0.5) < 1e-6 and abs(p_under - 0.5) < 1e-6
    pp_over, pp_under = devig_two_way(-110, -110, method="power")
    assert abs(pp_over - 0.5) < 1e-6
    # devig always sums to 1
    a, b = devig_two_way(-200, +160, method="multiplicative")
    assert abs((a + b) - 1.0) < 1e-9
    a, b = devig_two_way(-200, +160, method="power")
    assert abs((a + b) - 1.0) < 1e-6

    # EV: a 55% shot at -110 is clearly +EV; fair 52.38% is ~break-even
    assert expected_value(0.55, -110) > 0.04
    assert abs(expected_value(0.52381, -110)) < 1e-3
    assert expected_value(0.40, -110) < 0           # -EV

    # Kelly: -EV -> 0 stake; +EV -> positive, fractional < full
    assert kelly_fraction(0.40, -110) == 0.0
    full = kelly_fraction(0.60, -110)
    assert full > 0
    qk = kelly_stake(0.60, -110, bankroll=1000, fraction=0.25, max_pct=0.05)
    assert 0 < qk["stake_pct"] <= 0.05
    assert qk["stake_pct"] <= full           # capped/fractional never exceeds full
    print("value_engine self-test OK")


if __name__ == "__main__":
    _selftest()
