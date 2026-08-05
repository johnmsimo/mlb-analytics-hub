"""Normalize game context into a transparent score for prediction decisions."""
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


def score_context(pick: Mapping[str, Any]) -> dict[str, Any]:
    """Score weather, park, lineup, bullpen, rest, and umpire evidence.

    Existing normalized scores are preferred. Raw danger flags apply conservative
    penalties. Missing evidence is neutral rather than treated as favorable.
    """
    components: dict[str, float] = {}
    aliases = {
        'weather': ('weatherScore', 'weather_score'),
        'park': ('parkScore', 'parkFactorScore', 'park_factor_score'),
        'lineup': ('lineupScore', 'lineupConfirmationScore'),
        'bullpen': ('bullpenScore', 'bullpenAvailabilityScore'),
        'rest': ('restScore', 'travelRestScore'),
        'umpire': ('umpireScore', 'umpireFitScore'),
    }
    for name, keys in aliases.items():
        value = next((_num(pick.get(key)) for key in keys if _num(pick.get(key)) is not None), None)
        components[name] = _clamp(value if value is not None else 50.0)

    if pick.get('lineupConfirmed') is True:
        components['lineup'] = max(components['lineup'], 70.0)
    elif pick.get('lineupConfirmed') is False:
        components['lineup'] = min(components['lineup'], 30.0)

    if pick.get('roofClosed') is True:
        components['weather'] = max(components['weather'], 55.0)
    if pick.get('weatherRisk') is True or pick.get('rainRisk') is True:
        components['weather'] = min(components['weather'], 25.0)
    if pick.get('bullpenFatigued') is True:
        components['bullpen'] = min(components['bullpen'], 35.0)
    if pick.get('travelDisadvantage') is True:
        components['rest'] = min(components['rest'], 35.0)

    weights = {'weather': .18, 'park': .18, 'lineup': .22, 'bullpen': .18, 'rest': .12, 'umpire': .12}
    score = round(sum(components[key] * weights[key] for key in weights), 1)
    evidence = [f"{name.title()} context {value:.0f}/100" for name, value in components.items() if value != 50.0]
    risks = [label for label, condition in (
        ('weather uncertainty', components['weather'] < 40),
        ('unconfirmed or weak lineup context', components['lineup'] < 40),
        ('bullpen disadvantage', components['bullpen'] < 40),
        ('travel/rest disadvantage', components['rest'] < 40),
    ) if condition]
    return {'contextScore': score, 'contextComponents': components, 'contextEvidence': evidence, 'contextRisks': risks}


def enrich_context(picks: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    enriched = []
    for source in picks:
        row = dict(source)
        row.update(score_context(source))
        enriched.append(row)
    return enriched
