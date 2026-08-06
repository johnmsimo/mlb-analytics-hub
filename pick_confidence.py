"""Build one decision-ready score for dashboard Quick Picks.

``confidenceScore`` predates the intelligence stack and measures projection
reliability.  It is intentionally preserved for API compatibility.  Quick
Picks need a different number: an actionability score that combines the
calibrated probability, price edge, model reliability, context, matchup, and
simulation evidence without presenting any component as win probability.
"""
from __future__ import annotations

from typing import Any, Mapping


def _num(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _probability(pick: Mapping[str, Any]) -> float:
    value = _num(
        pick.get('blendedProb', pick.get('adjProb', pick.get('probability'))),
        0.0,
    ) or 0.0
    return _clamp(value / 100.0 if value > 1 else value, 0.0, 1.0)


def _edge(pick: Mapping[str, Any]) -> float | None:
    value = _num(pick.get('edge'))
    if value is None:
        return None
    return value / 100.0 if abs(value) > 1 else value


def _learning_score(
    pick: Mapping[str, Any], learning: Mapping[str, Any] | None
) -> tuple[float, int]:
    if not learning:
        return 50.0, 0
    markets = learning.get('byMarket') or {}
    keys = (
        pick.get('intelligenceCategory'),
        pick.get('market'),
        pick.get('marketType'),
    )
    summary = next(
        (markets[key] for key in keys if key is not None and key in markets),
        None,
    )
    count = int(_num((summary or {}).get('count'), 0.0) or 0.0)
    error = _num((summary or {}).get('calibrationError'))
    if count < 20 or error is None:
        return 50.0, count
    return _clamp(100.0 - error * 200.0), count


def _tier(score: float) -> tuple[str, str]:
    if score >= 78.0:
        return 'STRONG', 'Strong'
    if score >= 65.0:
        return 'VALUE', 'Value'
    if score >= 50.0:
        return 'LEAN', 'Lean'
    return 'WEAK', 'Weak'


def score_pick_confidence(
    pick: Mapping[str, Any],
    *,
    learning: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return transparent Pick Score fields without mutating ``pick``.

    Pick Score is a ranking/decision score, not a claim that a 70 score means a
    70% win probability.  The calibrated probability is returned separately.
    """
    probability = _probability(pick)
    edge = _edge(pick)
    reliability = _clamp(_num(
        pick.get('modelReliabilityScore', pick.get('confidenceScore')),
        0.0,
    ) or 0.0)
    context = _clamp(_num(pick.get('contextScore'), 50.0) or 50.0)
    matchup = _clamp(_num(pick.get('matchupScore'), 50.0) or 50.0)
    simulation = _clamp(_num(pick.get('simulationScore'), 50.0) or 50.0)
    learning_score, learning_count = _learning_score(pick, learning)

    probability_score = probability * 100.0
    # A fairly priced bet is neutral (50); each 1% of positive edge adds ten
    # points. This makes price value visible without allowing it to dominate.
    edge_score = 0.0 if edge is None else _clamp(50.0 + edge * 1000.0)
    components = {
        'winProbability': (probability_score, 0.30),
        'priceValue': (edge_score, 0.22),
        'modelReliability': (reliability, 0.20),
        'gameContext': (context, 0.08),
        'matchup': (matchup, 0.10),
        'simulation': (simulation, 0.08),
        'learningCalibration': (learning_score, 0.02),
    }
    score = round(sum(value * weight for value, weight in components.values()), 1)
    tier, label = _tier(score)

    labels = {
        'winProbability': f'Calibrated win probability {probability:.1%}',
        'priceValue': (
            'No priced edge is available'
            if edge is None else f'Estimated edge {edge:+.1%}'
        ),
        'modelReliability': f'Model reliability {reliability:.1f}/100',
        'gameContext': f'Game context {context:.1f}/100',
        'matchup': f'Matchup quality {matchup:.1f}/100',
        'simulation': f'Simulation quality {simulation:.1f}/100',
        'learningCalibration': (
            f'Historical calibration {learning_score:.1f}/100 '
            f'over {learning_count} graded picks'
        ),
    }
    ranked = sorted(
        (
            {
                'factor': name,
                'label': labels[name],
                'score': round(value, 1),
                'weight': weight,
                'contribution': round(value * weight, 2),
            }
            for name, (value, weight) in components.items()
            if name != 'learningCalibration' or learning_count >= 20
        ),
        key=lambda item: (-item['contribution'], item['factor']),
    )
    reasons = [
        item['label'] for item in ranked
        if item['score'] >= 55.0
    ][:3]
    risks = []
    if edge is None:
        risks.append('live price is unavailable, so edge cannot be verified')
    elif edge <= 0:
        risks.append('the available price does not offer a positive edge')
    elif edge < .02:
        risks.append('the positive edge is thin and price-sensitive')
    if probability < .55:
        risks.append('calibrated win probability is below the standard play level')
    if reliability < 56.0:
        risks.append('model reliability is below the standard play level')
    if simulation < 50.0:
        risks.append('simulation quality is weak')
    if matchup < 50.0:
        risks.append('matchup quality is unfavorable')

    return {
        'pickScore': score,
        'pickConfidenceScore': score,
        'pickScoreTier': tier,
        'pickScoreLabel': label,
        'modelProbabilityPct': round(probability * 100.0, 1),
        'marketImpliedProbabilityPct': (
            None if edge is None else round((probability - edge) * 100.0, 1)
        ),
        'estimatedEdgePct': None if edge is None else round(edge * 100.0, 1),
        'modelReliabilityScore': reliability,
        'pickScoreBreakdown': {
            name: {
                'score': round(value, 1),
                'weight': weight,
                'contribution': round(value * weight, 2),
            }
            for name, (value, weight) in components.items()
        },
        'pickScoreEvidence': ranked,
        'pickScoreReasons': reasons,
        'pickScoreRisks': risks[:3],
        'pickScoreNarrative': (
            f'{label} Pick Score {score:.1f}/100; '
            f'{probability:.1%} calibrated win probability'
            + ('' if edge is None else f' with {edge:+.1%} estimated edge')
            + '.'
        ),
    }


def enrich_pick_score(
    pick: Mapping[str, Any],
    *,
    learning: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    enriched = dict(pick)
    enriched.update(score_pick_confidence(pick, learning=learning))
    return enriched
