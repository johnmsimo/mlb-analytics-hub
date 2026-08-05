"""Turn Monte Carlo outputs into transparent uncertainty and reliability signals."""
from __future__ import annotations
from typing import Any, Iterable, Mapping


def _num(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _probability(pick: Mapping[str, Any]) -> float | None:
    side = str(pick.get('recommendedSide') or 'Over').lower()
    value = _num(pick.get('mc_prob_under') if side == 'under' else pick.get('mc_prob_over'))
    if value is None and side == 'under':
        over = _num(pick.get('mc_prob_over'))
        value = None if over is None else 1.0 - over
    if value is not None and value > 1:
        value /= 100.0
    return value


def score_simulation(pick: Mapping[str, Any]) -> dict[str, Any]:
    probability = _probability(pick)
    std = _num(pick.get('mc_std'))
    sample_size = _num(pick.get('mc_n_sims'))
    low = _num(pick.get('p_lo'))
    high = _num(pick.get('p_hi'))

    probability_score = 50.0 if probability is None else _clamp(50.0 + (probability - .50) * 200.0)
    stability_score = 50.0 if std is None else _clamp(100.0 - max(0.0, std) / .22 * 100.0)
    sample_score = 50.0 if sample_size is None else _clamp(sample_size / 2000.0 * 100.0)
    interval_score = 50.0
    interval_width = None
    if low is not None and high is not None and high >= low:
        interval_width = high - low
        interval_score = _clamp(100.0 - interval_width / .40 * 100.0)

    consistency = round(stability_score * .55 + interval_score * .45, 1)
    volatility = round(100.0 - consistency, 1)
    reliability = round(sample_score * .45 + stability_score * .30 + interval_score * .25, 1)
    score = round(probability_score * .38 + consistency * .32 + reliability * .30, 1)

    tail_risk = 'unknown'
    if interval_width is not None or std is not None:
        risk_value = volatility
        tail_risk = 'high' if risk_value >= 65 else 'moderate' if risk_value >= 40 else 'low'

    evidence = [f"Simulation reliability {reliability:.0f}/100", f"Consistency {consistency:.0f}/100"]
    if probability is not None:
        evidence.insert(0, f"Monte Carlo win probability {probability:.1%}")
    if sample_size is not None:
        evidence.append(f"{int(sample_size):,} simulation trials")

    risks = []
    if volatility >= 65: risks.append('high outcome volatility')
    if reliability < 40: risks.append('weak simulation reliability')
    if sample_size is not None and sample_size < 500: risks.append('small simulation sample')

    return {
        'simulationScore': score,
        'simulationProbability': probability,
        'simulationReliability': reliability,
        'consistencyScore': consistency,
        'volatilityScore': volatility,
        'tailRisk': tail_risk,
        'simulationInterval': {'low': low, 'high': high, 'width': interval_width},
        'simulationEvidence': evidence,
        'simulationRisks': risks,
    }


def enrich_simulations(picks: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    enriched = []
    for source in picks:
        row = dict(source)
        row.update(score_simulation(source))
        enriched.append(row)
    return enriched
