"""Measurement-first learning analytics for graded MLB predictions."""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Iterable, Mapping


def _num(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _prob(row: Mapping[str, Any]) -> float | None:
    value = row.get('blendedProb', row.get('adjProb', row.get('probability')))
    value = _num(value)
    if value is None:
        return None
    if value > 1:
        value /= 100.0
    return max(0.001, min(0.999, value))


def _outcome(row: Mapping[str, Any]) -> int | None:
    grade = str(row.get('grade') or row.get('result') or row.get('outcome') or '').strip().lower()
    if grade in {'win', 'won', 'w', 'hit', 'correct', '1', 'true'}:
        return 1
    if grade in {'loss', 'lost', 'l', 'miss', 'incorrect', '0', 'false'}:
        return 0
    return None


def _market(row: Mapping[str, Any]) -> str:
    return str(row.get('intelligenceCategory') or row.get('market') or row.get('marketType') or 'unknown').strip() or 'unknown'


def _summary(rows: list[tuple[float, int]]) -> dict[str, Any]:
    if not rows:
        return {'count': 0, 'wins': 0, 'winRate': None, 'averageProbability': None, 'brierScore': None, 'logLoss': None, 'calibrationError': None}
    count = len(rows)
    wins = sum(outcome for _, outcome in rows)
    avg_prob = sum(prob for prob, _ in rows) / count
    win_rate = wins / count
    brier = sum((prob - outcome) ** 2 for prob, outcome in rows) / count
    log_loss = -sum(outcome * math.log(prob) + (1 - outcome) * math.log(1 - prob) for prob, outcome in rows) / count
    return {
        'count': count,
        'wins': wins,
        'winRate': round(win_rate, 4),
        'averageProbability': round(avg_prob, 4),
        'brierScore': round(brier, 5),
        'logLoss': round(log_loss, 5),
        'calibrationError': round(abs(avg_prob - win_rate), 4),
    }


def analyze_learning(entries: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    graded: list[tuple[Mapping[str, Any], float, int]] = []
    skipped = 0
    for source in entries:
        probability, outcome = _prob(source), _outcome(source)
        if probability is None or outcome is None:
            skipped += 1
            continue
        graded.append((source, probability, outcome))

    pairs = [(prob, outcome) for _, prob, outcome in graded]
    by_market: dict[str, list[tuple[float, int]]] = defaultdict(list)
    buckets: dict[str, list[tuple[float, int]]] = defaultdict(list)
    factors = ('confidenceScore', 'contextScore', 'matchupScore', 'simulationScore', 'decisionScore')
    factor_groups: dict[str, dict[str, list[tuple[float, int]]]] = {factor: defaultdict(list) for factor in factors}

    for row, prob, outcome in graded:
        by_market[_market(row)].append((prob, outcome))
        low = int(prob * 10) * 10
        low = min(low, 90)
        buckets[f'{low}-{low + 9}%'].append((prob, outcome))
        for factor in factors:
            value = _num(row.get(factor))
            if value is None:
                continue
            band = 'high' if value >= 70 else 'medium' if value >= 50 else 'low'
            factor_groups[factor][band].append((prob, outcome))

    return {
        'mode': 'measurement_only',
        'adaptiveWeightsEnabled': False,
        'gradedCount': len(graded),
        'skippedCount': skipped,
        'overall': _summary(pairs),
        'calibrationBuckets': {name: _summary(values) for name, values in sorted(buckets.items())},
        'byMarket': {name: _summary(values) for name, values in sorted(by_market.items())},
        'factorPerformance': {
            factor: {band: _summary(values) for band, values in sorted(groups.items())}
            for factor, groups in factor_groups.items() if groups
        },
        'minimumSampleNotice': 'Do not adapt model weights until each evaluated segment has a meaningful sample size.',
    }
