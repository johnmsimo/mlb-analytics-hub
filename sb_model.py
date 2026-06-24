"""sb_model.py — lightweight stolen-base probability model.

There is no Statcast-trained XGB artifact for stolen bases (the prop board's
hit/HR/TB/RBI models don't cover SB), so this module supplies a small,
self-contained, *calibrated-by-construction* estimator for the probability a
batter records at least one stolen base in a game. It mirrors the spirit of
`value_engine.py`: pure-Python, no heavy deps, self-tested (`python sb_model.py`).

Model
-----
The per-PA stolen-base rate already embeds how often a runner reaches base AND
chooses to run, so the season SB count over season PA is the natural base
signal. Expected SB in a game is that rate scaled by the batter's expected plate
appearances, then nudged by context:

    rate_pa   = season_sb / season_pa
    lambda_sb = rate_pa * expected_pa * hand_mult * speed_mult
    P(>=1 SB) = 1 - exp(-lambda_sb)          # Poisson tail

Context multipliers (each a small, defensible tweak, not fabricated skill):
  * hand_mult  — LHP hold runners better (they face first base), so SB attempts
                 drop ~12% vs a lefty. RHP neutral.
  * speed_mult — when Statcast sprint speed is available, faster-than-league
                 runners convert/attempt more. Centered at the 27 ft/s league
                 average and clamped so a missing/odd value can't blow up.

Outputs a dict with the probability, the Poisson lambda, a tier, and a short
human-readable reason so the UI can explain the number.
"""
from __future__ import annotations

import math
from typing import Optional

LEAGUE_SPRINT_SPEED = 27.0   # ft/s, roughly MLB average
_DEFAULT_EXPECTED_PA = 4.2   # league-average PA per game


def _num(v, default: float = 0.0) -> float:
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (TypeError, ValueError):
        return default


def sb_rate_per_pa(season_sb, season_pa) -> float:
    sb = max(0.0, _num(season_sb))
    pa = _num(season_pa)
    if pa <= 0:
        return 0.0
    return sb / pa


def _hand_mult(opp_hand: Optional[str]) -> float:
    return 0.88 if str(opp_hand or "R").upper().startswith("L") else 1.0


def _speed_mult(sprint_speed: Optional[float]) -> float:
    if sprint_speed is None:
        return 1.0
    spd = _num(sprint_speed, LEAGUE_SPRINT_SPEED)
    if spd <= 0:
        return 1.0
    # ~6% per ft/s above/below league average, clamped to a sane band.
    return max(0.65, min(1.45, 1.0 + (spd - LEAGUE_SPRINT_SPEED) * 0.06))


def expected_sb(season_sb, season_pa, expected_pa: float = _DEFAULT_EXPECTED_PA,
                opp_hand: Optional[str] = "R",
                sprint_speed: Optional[float] = None) -> float:
    rate = sb_rate_per_pa(season_sb, season_pa)
    epa = _num(expected_pa, _DEFAULT_EXPECTED_PA) or _DEFAULT_EXPECTED_PA
    lam = rate * epa * _hand_mult(opp_hand) * _speed_mult(sprint_speed)
    return max(0.0, lam)


def _tier(prob: float) -> str:
    if prob >= 0.30:
        return "ELITE"
    if prob >= 0.18:
        return "STRONG"
    if prob >= 0.08:
        return "LIVE"
    return "LOW"


def sb_probability(season_sb, season_pa, expected_pa: float = _DEFAULT_EXPECTED_PA,
                   opp_hand: Optional[str] = "R",
                   sprint_speed: Optional[float] = None,
                   obp: Optional[float] = None) -> dict:
    """Probability of >=1 stolen base this game, with context.

    `obp` is accepted for the reason string only — the season SB/PA rate already
    accounts for on-base opportunity, so it is not double-counted in lambda.
    """
    lam = expected_sb(season_sb, season_pa, expected_pa, opp_hand, sprint_speed)
    prob = 1.0 - math.exp(-lam)
    prob = max(0.004, min(0.85, prob))

    sb = int(round(max(0.0, _num(season_sb))))
    reasons = []
    if sb <= 0:
        reasons.append("no steals on the season")
    else:
        reasons.append(f"{sb} SB on the season")
    if str(opp_hand or "R").upper().startswith("L"):
        reasons.append("LHP holds runners (−12%)")
    if sprint_speed is not None and _num(sprint_speed) > 0:
        spd = _num(sprint_speed)
        if spd >= 28.5:
            reasons.append(f"plus speed ({spd:.1f} ft/s)")
        elif spd <= 26.0:
            reasons.append(f"below-avg speed ({spd:.1f} ft/s)")

    return {
        "prob": round(prob, 4),
        "lambda": round(lam, 4),
        "seasonSb": sb,
        "tier": _tier(prob),
        "reason": "; ".join(reasons),
    }


if __name__ == "__main__":
    # Self-test: monotonicity, bounds, and a few sanity anchors.
    fast = sb_probability(40, 600, opp_hand="R")          # burner
    mod  = sb_probability(12, 550, opp_hand="R")           # occasional
    none = sb_probability(0, 500, opp_hand="R")            # clogger
    assert fast["prob"] > mod["prob"] > none["prob"], (fast, mod, none)
    assert 0.0 < none["prob"] < 0.05, none
    assert fast["prob"] < 0.85, fast
    # LHP suppresses vs the same runner.
    assert sb_probability(40, 600, opp_hand="L")["prob"] < fast["prob"]
    # Sprint speed lifts probability.
    assert (sb_probability(20, 600, sprint_speed=29.5)["prob"]
            > sb_probability(20, 600, sprint_speed=25.0)["prob"])
    # Zero PA must not divide-by-zero.
    assert sb_probability(5, 0)["prob"] >= 0.004
    print("sb_model self-test OK")
    for label, r in (("burner", fast), ("moderate", mod), ("none", none)):
        print(f"  {label:9s} p={r['prob']:.3f} tier={r['tier']:6s} ({r['reason']})")
