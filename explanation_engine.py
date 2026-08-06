"""Turn MLB intelligence outputs into ranked, explainable recommendations."""
from __future__ import annotations

from typing import Any, Mapping


_GRADE_ACTIONS = {
    'Strong Play': 'Prioritize this play at a disciplined stake.',
    'Value Play': 'Consider this value play at a standard or smaller stake.',
    'Lean': 'Treat this as a lean; wait for confirmation or a better number.',
    'Pass': 'Pass; wait for stronger evidence.',
}


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return default if value is None or isinstance(value, bool) else float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _probability(pick: Mapping[str, Any]) -> float:
    value = _num(pick.get('blendedProb', pick.get('adjProb', pick.get('probability'))))
    return _clamp(value / 100.0 if value > 1 else value, 0.0, 1.0)


def _edge(pick: Mapping[str, Any]) -> float:
    value = _num(pick.get('edge'))
    return value / 100.0 if abs(value) > 1 else value


def _label(name: str) -> str:
    pieces = []
    for char in name:
        if char.isupper() and pieces:
            pieces.append(' ')
        pieces.append(char.lower())
    return ''.join(pieces)


def _learning_market_summary(
    pick: Mapping[str, Any], learning: Mapping[str, Any] | None
) -> Mapping[str, Any] | None:
    if not learning:
        return None
    markets = learning.get('byMarket') or {}
    keys = (
        pick.get('intelligenceCategory'),
        pick.get('market'),
        pick.get('marketType'),
    )
    for key in keys:
        if key is not None and key in markets:
            return markets[key]
    return None


def _rank_evidence(
    pick: Mapping[str, Any], learning: Mapping[str, Any] | None
) -> list[dict[str, Any]]:
    probability = _probability(pick)
    edge = _edge(pick)
    inputs = [
        ('projection', 'modelProbability', probability * 100.0, probability * 25.0,
         f'Model win probability {probability:.1%}'),
        ('projection', 'marketEdge', _clamp(edge / .15 * 100.0),
         _clamp(edge / .15 * 100.0) * .14, f'Estimated market edge {edge:.1%}'),
        ('confidence', 'confidenceScore', _clamp(_num(pick.get('confidenceScore'))),
         _clamp(_num(pick.get('confidenceScore'))) * .22,
         f"Confidence {_clamp(_num(pick.get('confidenceScore'))):.1f}/100"),
        ('context', 'contextScore', _clamp(_num(pick.get('contextScore'), 50.0)),
         _clamp(_num(pick.get('contextScore'), 50.0)) * .09,
         f"Game context {_clamp(_num(pick.get('contextScore'), 50.0)):.1f}/100"),
        ('matchup', 'matchupScore', _clamp(_num(pick.get('matchupScore'), 50.0)),
         _clamp(_num(pick.get('matchupScore'), 50.0)) * .12,
         f"Baseball matchup {_clamp(_num(pick.get('matchupScore'), 50.0)):.1f}/100"),
        ('simulation', 'simulationScore', _clamp(_num(pick.get('simulationScore'), 50.0)),
         _clamp(_num(pick.get('simulationScore'), 50.0)) * .12,
         f"Simulation quality {_clamp(_num(pick.get('simulationScore'), 50.0)):.1f}/100"),
    ]

    market_summary = _learning_market_summary(pick, learning)
    if market_summary and _num(market_summary.get('count')) > 0:
        count = int(_num(market_summary.get('count')))
        calibration_error = _num(market_summary.get('calibrationError'), 1.0)
        learning_score = _clamp((1.0 - calibration_error) * 100.0)
        win_rate = _num(market_summary.get('winRate'))
        inputs.append((
            'learning', 'marketCalibration', learning_score, learning_score * .02,
            f'Learning history: {count} graded, {win_rate:.1%} win rate, '
            f'{calibration_error:.1%} calibration error',
        ))

    ranked = sorted(
        (
            {
                'engine': engine,
                'factor': factor,
                'label': label,
                'score': round(score, 1),
                'contribution': round(contribution, 2),
            }
            for engine, factor, score, contribution, label in inputs
            if score >= 55.0
        ),
        key=lambda item: (-item['contribution'], item['factor']),
    )[:5]
    for index, evidence in enumerate(ranked, 1):
        evidence['rank'] = index
    return ranked


def _rank_risks(
    pick: Mapping[str, Any], rejection_reasons: list[str] | None
) -> list[str]:
    risks: dict[str, float] = {}

    for reason in rejection_reasons or []:
        risks[str(reason)] = 100.0

    engine_risks = (
        ('contextRisks', _num(pick.get('contextScore'), 50.0)),
        ('matchupRisks', _num(pick.get('matchupScore'), 50.0)),
        ('simulationRisks', _num(pick.get('simulationScore'), 50.0)),
    )
    for field, score in engine_risks:
        for risk in pick.get(field) or []:
            text = str(risk)
            severity = 90.0 if any(word in text.lower() for word in ('high ', 'weak ', 'unfavorable')) else 75.0
            risks[text] = max(risks.get(text, 0.0), severity, 100.0 - score)

    if not rejection_reasons:
        automatic = (
            ('model probability is near the minimum threshold',
             55.0 - _probability(pick) * 100.0),
            ('confidence evidence is weak', 56.0 - _num(pick.get('confidenceScore'))),
            ('game context is unfavorable', 50.0 - _num(pick.get('contextScore'), 50.0)),
            ('baseball matchup is unfavorable', 50.0 - _num(pick.get('matchupScore'), 50.0)),
            ('simulation quality is weak', 50.0 - _num(pick.get('simulationScore'), 50.0)),
        )
        for label, deficit in automatic:
            if deficit > 0:
                risks[label] = max(risks.get(label, 0.0), 50.0 + deficit)

    return [label for label, _ in sorted(risks.items(), key=lambda item: (-item[1], item[0]))[:3]]


def _confidence_narrative(pick: Mapping[str, Any]) -> str:
    score = _clamp(_num(pick.get('confidenceScore')))
    label = str(pick.get('confidenceLabel') or pick.get('confidenceTier') or (
        'High' if score >= 72 else 'Medium' if score >= 56 else 'Low'
    )).replace('_', ' ').title()
    components = pick.get('confidenceComponents') or {}
    ranked = sorted(
        ((_label(str(name)), _num(value)) for name, value in components.items()),
        key=lambda item: (-item[1], item[0]),
    )
    if not ranked:
        return f'{label} confidence ({score:.1f}/100); detailed confidence components are unavailable.'
    strongest = ' and '.join(name for name, _ in ranked[:2])
    weakest = ranked[-1][0]
    return f'{label} confidence ({score:.1f}/100), led by {strongest}; the weakest input is {weakest}.'


def _grade(
    pick: Mapping[str, Any], top_risks: list[str], rejection_reasons: list[str] | None
) -> str:
    if rejection_reasons:
        return 'Pass'
    score = _num(pick.get('decisionScore'))
    if score >= 75.0 and _probability(pick) >= .62 and _num(pick.get('confidenceScore')) >= 72.0 and not top_risks:
        return 'Strong Play'
    if score >= 65.0 and _edge(pick) >= .05:
        return 'Value Play'
    return 'Lean'


def explain_recommendation(
    pick: Mapping[str, Any],
    *,
    learning: Mapping[str, Any] | None = None,
    rejection_reasons: list[str] | None = None,
) -> dict[str, Any]:
    """Return a copy enriched with concise, ranked explanation fields."""
    row = dict(pick)
    evidence = _rank_evidence(row, learning)
    top_reasons = [item['label'] for item in evidence[:3]]
    top_risks = _rank_risks(row, rejection_reasons)
    grade = _grade(row, top_risks, rejection_reasons)

    if grade == 'Pass':
        summary = f"Pass because {top_risks[0] if top_risks else 'the evidence is insufficient'}."
    elif top_reasons:
        summary = f"{grade} led by {top_reasons[0].lower()}."
        if top_risks:
            summary += f" Main risk: {top_risks[0]}."
    else:
        summary = f'{grade}; no strong supporting factor is available.'

    row.update({
        'recommendationGrade': grade,
        'decisionSummary': summary,
        'topReasons': top_reasons,
        'topRisks': top_risks,
        'recommendedAction': _GRADE_ACTIONS[grade],
        'confidenceNarrative': _confidence_narrative(row),
        'supportingEvidence': evidence,
    })
    return row


def explain_decisions(
    decisions: Mapping[str, Any], learning: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Explain every surfaced recommendation and convert rejections to passes."""
    explained = dict(decisions)
    explained['card'] = [
        explain_recommendation(pick, learning=learning)
        for pick in decisions.get('card') or []
    ]
    explained['best'] = {
        category: None if pick is None else explain_recommendation(pick, learning=learning)
        for category, pick in (decisions.get('best') or {}).items()
    }
    passes = [
        explain_recommendation(
            pick,
            learning=learning,
            rejection_reasons=list(pick.get('reasons') or []),
        )
        for pick in decisions.get('rejected') or []
    ]
    explained['rejected'] = passes
    explained['passes'] = passes
    return explained
