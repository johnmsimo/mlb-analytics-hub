"""Build one confidence-first decision for each dashboard game-card market."""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from confidence_service import enrich_pick_confidence
from explanation_engine import explain_recommendation
from intelligence_core import (
    DecisionPolicy,
    build_recommendations,
    classify_pick,
    decision_score,
)


CATEGORY_ORDER = ('hitter_hits', 'pitcher_strikeouts', 'game_winner')
CATEGORY_LABELS = {
    'hitter_hits': 'Hitter Hits',
    'pitcher_strikeouts': 'Pitcher Strikeouts',
    'game_winner': 'Moneyline',
}

# The global intelligence policy remains the standard for a qualified play. A
# game card can also surface its best priced positive-edge side as a clearly
# labeled Lean. This keeps Quick Props useful without presenting a weak or
# negative-edge candidate as a normal wager.
BEST_AVAILABLE_MINIMUM_PROBABILITY = 0.50
BEST_AVAILABLE_MINIMUM_CONFIDENCE = 25.0
BEST_AVAILABLE_MINIMUM_EDGE = 0.005


def _num(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _probability(row: Mapping[str, Any]) -> float:
    value = _num(
        row.get('blendedProb', row.get('adjProb', row.get('probability'))),
        0.0,
    ) or 0.0
    return max(0.0, min(1.0, value / 100.0 if value > 1 else value))


def _american_implied(price: Any) -> float | None:
    value = _num(price)
    if value is None or value == 0:
        return None
    return 100.0 / (value + 100.0) if value > 0 else -value / (-value + 100.0)


def _first(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if row.get(key) is not None:
            return row.get(key)
    return None


def _candidate_id(row: Mapping[str, Any], side: str) -> str:
    return ':'.join(str(value) for value in (
        row.get('gamePk') or 'game',
        row.get('playerId') or row.get('team') or row.get('player') or 'pick',
        row.get('marketKey') or row.get('market') or 'market',
        row.get('line') or 0,
        side.lower(),
    ))


def _under_interval(row: Mapping[str, Any]) -> tuple[float | None, float | None]:
    over_low = _num(row.get('p_lo'))
    over_high = _num(row.get('p_hi'))
    if over_low is None or over_high is None:
        return None, None
    return max(0.0, 1.0 - over_high), min(1.0, 1.0 - over_low)


def _as_side(row: Mapping[str, Any], side: str) -> dict[str, Any]:
    """Normalize a tracker row to the probability, price, and edge of one side."""
    pick = dict(row)
    over_probability = _probability(row)
    is_under = side.lower() == 'under'

    if is_under:
        probability = 1.0 - over_probability
        price = _first(row, 'bestUnderPrice', 'best_under_price')
        book = _first(row, 'bestUnderBook', 'best_under_book')
        implied = _american_implied(price)
        if implied is None:
            over_implied = _num(_first(row, 'marketImplied', 'openingImplied'))
            implied = None if over_implied is None else max(0.0, 1.0 - over_implied)
        p_lo, p_hi = _under_interval(row)
        pick.update({
            'recommendedSide': 'Under',
            'sideLabel': 'Under',
            'adjProb': round(probability, 4),
            'marketPrice': price,
            'bestAvailablePrice': price,
            'bestAvailableBook': book,
            'bookmaker': book,
            'marketImplied': round(implied, 4) if implied is not None else None,
            'openingPrice': price,
            'openingImplied': round(implied, 4) if implied is not None else None,
            'edge': round(probability - implied, 4) if implied is not None else None,
            'p_lo': round(p_lo, 4) if p_lo is not None else None,
            'p_hi': round(p_hi, 4) if p_hi is not None else None,
        })
    else:
        probability = over_probability
        normalized_side = side if classify_pick(row) == 'game_winner' else 'Over'
        price = _first(
            row, 'bestOverPrice', 'best_over_price',
            'bestAvailablePrice', 'marketPrice',
        )
        book = _first(
            row, 'bestOverBook', 'best_over_book',
            'bestAvailableBook', 'bookmaker',
        )
        implied = _num(_first(row, 'marketImplied', 'openingImplied'))
        if implied is None:
            implied = _american_implied(price)
        pick.update({
            'recommendedSide': normalized_side,
            'sideLabel': normalized_side,
            'adjProb': round(probability, 4),
            'marketPrice': price,
            'bestAvailablePrice': price,
            'bestAvailableBook': book,
            'bookmaker': book,
            'marketImplied': round(implied, 4) if implied is not None else None,
            'openingPrice': price,
            'openingImplied': round(implied, 4) if implied is not None else None,
            'edge': (
                round(probability - implied, 4)
                if implied is not None else pick.get('edge')
            ),
        })

    pick['id'] = _candidate_id(pick, pick['recommendedSide'])
    pick['grade'] = pick.get('grade') or 'pending'
    pick['market'] = pick.get('market') or pick.get('marketKey')
    return enrich_pick_confidence(pick)


def prepare_game_card_candidates(
    rows: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Keep supported markets and create a separately scored Under K candidate."""
    candidates: list[dict[str, Any]] = []
    for source in rows:
        category = classify_pick(source)
        if category not in CATEGORY_ORDER:
            continue
        row = dict(source)
        row['intelligenceCategory'] = category
        if category == 'pitcher_strikeouts':
            candidates.append(_as_side(row, 'Over'))
            candidates.append(_as_side(row, 'Under'))
        else:
            side = str(row.get('recommendedSide') or 'Over')
            candidates.append(_as_side(row, side))
    return candidates


def _rank_key(row: Mapping[str, Any]) -> tuple[float, float, float, float]:
    return (
        _num(row.get('confidenceScore'), 0.0) or 0.0,
        _num(row.get('decisionScore'), 0.0) or 0.0,
        _probability(row),
        _num(row.get('edge'), -1.0) or -1.0,
    )


def _edge(row: Mapping[str, Any]) -> float:
    value = _num(row.get('edge'), -1.0)
    if value is None:
        return -1.0
    return value / 100.0 if abs(value) > 1 else value


def _has_price(row: Mapping[str, Any]) -> bool:
    return _first(
        row,
        'bestAvailablePrice', 'marketPrice',
        'bestOverPrice', 'best_over_price',
        'bestUnderPrice', 'best_under_price',
    ) is not None


def _is_best_available_candidate(row: Mapping[str, Any]) -> bool:
    """Return whether a rejected candidate is still an honest actionable Lean."""
    grade = str(row.get('grade') or 'pending').strip().lower()
    return (
        _has_price(row)
        and _probability(row) >= BEST_AVAILABLE_MINIMUM_PROBABILITY
        and (_num(row.get('confidenceScore'), 0.0) or 0.0)
        >= BEST_AVAILABLE_MINIMUM_CONFIDENCE
        and _edge(row) >= BEST_AVAILABLE_MINIMUM_EDGE
        and grade in {'pending', 'open', ''}
        and classify_pick(row) in CATEGORY_ORDER
    )


def _standard_risk(reason: str) -> str:
    return {
        'probability below threshold': (
            'model probability is below the standard play threshold'
        ),
        'confidence below threshold': (
            'confidence is below the standard play threshold'
        ),
        'edge below threshold': 'edge is below the standard play threshold',
    }.get(reason, reason)


def _explain_best_available(
    candidate: Mapping[str, Any],
    reasons: list[str],
    *,
    learning: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Explain a positive-edge fallback as a Lean, never as a full-strength play."""
    row = dict(candidate)
    row['decisionScore'] = decision_score(row)
    explained = explain_recommendation(row, learning=learning)
    cautions = [_standard_risk(reason) for reason in reasons]
    explained['topRisks'] = list(dict.fromkeys(
        cautions + list(explained.get('topRisks') or [])
    ))[:3]
    lead = (explained.get('topReasons') or [
        'the strongest available positive-edge side'
    ])[0].lower()
    caution = explained['topRisks'][0]
    explained.update({
        'recommendationGrade': 'Lean',
        'decisionSummary': (
            f'Best available Lean led by {lead}. Main caution: {caution}.'
        ),
        'recommendedAction': (
            'Best available side; use a smaller stake and confirm the price '
            'before betting.'
        ),
        'selectionMode': 'best_available',
        'meetsStandardThresholds': False,
        'isActionable': True,
        'standardThresholdMisses': list(reasons),
    })
    return explained


def select_game_card_quick_picks(
    candidates: Iterable[Mapping[str, Any]],
    *,
    learning: Mapping[str, Any] | None = None,
    policy: DecisionPolicy | None = None,
) -> dict[str, Any]:
    """Return the highest-confidence eligible pick—or an explicit Pass—per market."""
    policy = policy or DecisionPolicy(maximum_card_size=3)
    grouped: dict[str, list[dict[str, Any]]] = {
        category: [] for category in CATEGORY_ORDER
    }
    for candidate in candidates:
        row = dict(candidate)
        category = classify_pick(row)
        if category in grouped:
            row['intelligenceCategory'] = category
            grouped[category].append(row)

    selections: list[dict[str, Any]] = []
    for category in CATEGORY_ORDER:
        eligible: list[dict[str, Any]] = []
        rejected: list[tuple[dict[str, Any], list[str]]] = []
        for candidate in grouped[category]:
            decision = build_recommendations([candidate], policy=policy)
            if decision['card']:
                eligible.append(decision['card'][0])
            else:
                reasons = list(
                    (decision.get('rejected') or [{}])[0].get('reasons') or []
                )
                rejected.append((candidate, reasons))

        if eligible:
            chosen = max(eligible, key=_rank_key)
            explained = explain_recommendation(chosen, learning=learning)
            explained.update({
                'selectionMode': 'qualified',
                'meetsStandardThresholds': True,
                'isActionable': True,
                'standardThresholdMisses': [],
            })
        elif rejected:
            best_available = [
                pair for pair in rejected
                if _is_best_available_candidate(pair[0])
            ]
            if best_available:
                chosen, reasons = max(
                    best_available,
                    key=lambda pair: _rank_key(pair[0]),
                )
                explained = _explain_best_available(
                    chosen,
                    reasons,
                    learning=learning,
                )
            else:
                chosen, reasons = max(
                    rejected,
                    key=lambda pair: _rank_key(pair[0]),
                )
                explained = explain_recommendation(
                    chosen,
                    learning=learning,
                    rejection_reasons=(
                        reasons or ['the evidence is insufficient']
                    ),
                )
                explained.update({
                    'selectionMode': 'pass',
                    'meetsStandardThresholds': False,
                    'isActionable': False,
                    'standardThresholdMisses': list(reasons),
                })
        else:
            explained = explain_recommendation({
                'id': f'no-candidate:{category}',
                'market': CATEGORY_LABELS[category],
                'intelligenceCategory': category,
                'confidenceScore': 0.0,
                'adjProb': 0.0,
                'edge': 0.0,
                'grade': 'pending',
            }, learning=learning, rejection_reasons=[
                'no market candidate is available',
            ])
            explained.update({
                'selectionMode': 'pass',
                'meetsStandardThresholds': False,
                'isActionable': False,
                'standardThresholdMisses': [
                    'no market candidate is available',
                ],
            })

        explained['intelligenceCategory'] = category
        explained['categoryLabel'] = CATEGORY_LABELS[category]
        explained['rankWithinCategory'] = 1
        selections.append(explained)

    return {
        'quickPicks': selections,
        'best': {pick['intelligenceCategory']: pick for pick in selections},
        'eligibleCategoryCount': sum(
            pick['recommendationGrade'] != 'Pass' for pick in selections
        ),
        'qualifiedCategoryCount': sum(
            pick.get('selectionMode') == 'qualified' for pick in selections
        ),
        'bestAvailableCategoryCount': sum(
            pick.get('selectionMode') == 'best_available'
            for pick in selections
        ),
        'passCategoryCount': sum(
            pick['recommendationGrade'] == 'Pass' for pick in selections
        ),
        'policy': {
            'minimumProbability': policy.minimum_probability,
            'minimumConfidence': policy.minimum_confidence,
            'minimumEdge': policy.minimum_edge,
            'bestAvailableMinimumProbability': (
                BEST_AVAILABLE_MINIMUM_PROBABILITY
            ),
            'bestAvailableMinimumConfidence': (
                BEST_AVAILABLE_MINIMUM_CONFIDENCE
            ),
            'bestAvailableMinimumEdge': BEST_AVAILABLE_MINIMUM_EDGE,
            'passRule': (
                'no priced candidate clears the positive-edge viability floor'
            ),
            'rankingPriority': 'confidenceScore',
        },
    }


def build_game_card_quick_picks(
    rows: Iterable[Mapping[str, Any]],
    *,
    learning: Mapping[str, Any] | None = None,
    policy: DecisionPolicy | None = None,
) -> dict[str, Any]:
    """Convenience boundary for raw tracker-style rows."""
    return select_game_card_quick_picks(
        prepare_game_card_candidates(rows),
        learning=learning,
        policy=policy,
    )
