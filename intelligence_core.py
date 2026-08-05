"""Select the strongest MLB predictions and abstain when evidence is weak."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

@dataclass(frozen=True)
class DecisionPolicy:
    minimum_probability: float = 0.55
    minimum_confidence: float = 56.0
    minimum_edge: float = 0.02
    maximum_card_size: int = 5

def _num(value: Any, default: float = 0.0) -> float:
    try:
        return default if value is None or isinstance(value, bool) else float(value)
    except (TypeError, ValueError):
        return default

def _prob(p: Mapping[str, Any]) -> float:
    value = p.get('blendedProb', p.get('adjProb', p.get('probability')))
    value = _num(value)
    return value / 100.0 if value > 1 else value

def _edge(p: Mapping[str, Any]) -> float:
    value = _num(p.get('edge'))
    return value / 100.0 if abs(value) > 1 else value

def classify_pick(p: Mapping[str, Any]) -> str:
    text = ' '.join(str(p.get(k) or '') for k in ('market','marketType','stat','propType','betType','category')).lower()
    if 'strikeout' in text or text.strip() in {'k','ks','pitcher k'}:
        return 'pitcher_strikeouts'
    if 'moneyline' in text or 'game winner' in text or 'to win' in text:
        return 'game_winner'
    if 'hit' in text and 'allowed' not in text and 'hard hit' not in text:
        return 'hitter_hits'
    return 'other'

def decision_score(p: Mapping[str, Any]) -> float:
    probability = max(0.0, min(1.0, _prob(p)))
    confidence = max(0.0, min(100.0, _num(p.get('confidenceScore')))) / 100.0
    edge = max(0.0, min(0.15, _edge(p))) / 0.15
    context = max(0.0, min(100.0, _num(p.get('contextScore'), 50.0))) / 100.0
    matchup = max(0.0, min(100.0, _num(p.get('matchupScore'), 50.0))) / 100.0
    simulation = max(0.0, min(100.0, _num(p.get('simulationScore'), 50.0))) / 100.0
    rating = max(0.0, min(100.0, _num(p.get('hubRating')))) / 100.0
    agreement = _num((p.get('confidenceComponents') or {}).get('modelAgreement'), 50.0) / 100.0
    return round(min(1.0, probability*.25 + confidence*.22 + edge*.14 + context*.09 + matchup*.12 + simulation*.12 + rating*.03 + agreement*.03)*100, 1)

def build_recommendations(picks: Iterable[Mapping[str, Any]], policy: DecisionPolicy | None = None) -> dict[str, Any]:
    policy = policy or DecisionPolicy()
    eligible, rejected = [], []
    for source in picks:
        p = dict(source)
        category = classify_pick(p)
        reasons = []
        if _prob(p) < policy.minimum_probability: reasons.append('probability below threshold')
        if _num(p.get('confidenceScore')) < policy.minimum_confidence: reasons.append('confidence below threshold')
        if _edge(p) < policy.minimum_edge: reasons.append('edge below threshold')
        if str(p.get('grade') or 'pending').lower() not in {'pending','open',''}: reasons.append('already graded')
        if category == 'other': reasons.append('unsupported market category')
        if reasons:
            rejected.append({'id': p.get('id'), 'category': category, 'reasons': reasons})
            continue
        p['intelligenceCategory'] = category
        p['decisionScore'] = decision_score(p)
        p['whyThisPick'] = [
            f"Model win probability {_prob(p):.1%}",
            f"Estimated edge {_edge(p):.1%}",
            f"Confidence {_num(p.get('confidenceScore')):.1f}/100",
            f"Game context {_num(p.get('contextScore'), 50.0):.1f}/100",
            f"Baseball matchup {_num(p.get('matchupScore'), 50.0):.1f}/100",
            f"Simulation quality {_num(p.get('simulationScore'), 50.0):.1f}/100",
        ]
        p['whyThisPick'].extend(p.get('contextEvidence') or [])
        p['whyThisPick'].extend(p.get('matchupAdvantages') or [])
        p['whyThisPick'].extend(p.get('simulationEvidence') or [])
        if p.get('confidenceExplanation'): p['whyThisPick'].append(p['confidenceExplanation'])
        risks = list(p.get('contextRisks') or []) + list(p.get('matchupRisks') or []) + list(p.get('simulationRisks') or [])
        if risks: p['riskFactors'] = risks
        eligible.append(p)
    eligible.sort(key=lambda p: (-p['decisionScore'], -_prob(p), -_edge(p)))
    categories = ('hitter_hits','pitcher_strikeouts','game_winner')
    best = {c: next((p for p in eligible if p['intelligenceCategory']==c), None) for c in categories}
    card, seen = [], set()
    for c in categories:
        if best[c] is not None:
            card.append(best[c]); seen.add(best[c].get('id'))
    for p in eligible:
        if len(card) >= policy.maximum_card_size: break
        if p.get('id') not in seen: card.append(p); seen.add(p.get('id'))
    return {'policy': {'minimumProbability': policy.minimum_probability, 'minimumConfidence': policy.minimum_confidence, 'minimumEdge': policy.minimum_edge, 'maximumCardSize': policy.maximum_card_size}, 'best': best, 'card': card, 'abstentions': {c:'No play met all intelligence thresholds.' for c in categories if best[c] is None}, 'eligibleCount': len(eligible), 'rejectedCount': len(rejected), 'rejected': rejected}
