"""Normalize baseball matchup evidence into transparent prediction scores."""
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


def _first(pick: Mapping[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = _num(pick.get(key))
        if value is not None:
            return value
    return None


def score_matchup(pick: Mapping[str, Any]) -> dict[str, Any]:
    """Score platoon, pitch-fit, contact, strikeout, form, and bullpen transition.

    Inputs may be pre-normalized 0-100 scores under any supported alias. Missing
    evidence remains neutral. Raw warning flags apply conservative penalties.
    """
    aliases = {
        'platoon': ('platoonScore', 'platoonAdvantageScore', 'handednessFitScore'),
        'pitchProfile': ('pitchProfileFit', 'pitchTypeFitScore', 'arsenalMatchupScore'),
        'contactProfile': ('contactProfileFit', 'contactMatchupScore', 'qualityOfContactScore'),
        'strikeoutProfile': ('strikeoutMatchupScore', 'whiffMatchupScore', 'kProfileFit'),
        'recentForm': ('recentFormScore', 'velocityTrendScore', 'pitchMixTrendScore'),
        'bullpenTransition': ('bullpenTransitionScore', 'reliefMatchupScore'),
    }
    components = {
        name: _clamp(_first(pick, keys) if _first(pick, keys) is not None else 50.0)
        for name, keys in aliases.items()
    }

    if pick.get('platoonAdvantage') is True:
        components['platoon'] = max(components['platoon'], 70.0)
    elif pick.get('platoonDisadvantage') is True:
        components['platoon'] = min(components['platoon'], 30.0)

    if pick.get('velocityDecline') is True or pick.get('pitcherVelocityDown') is True:
        components['recentForm'] = min(components['recentForm'], 35.0)
    if pick.get('pitchMixUnstable') is True:
        components['recentForm'] = min(components['recentForm'], 40.0)
    if pick.get('smallSampleBvp') is True:
        components['pitchProfile'] = min(components['pitchProfile'], 50.0)
    if pick.get('poorBullpenTransition') is True:
        components['bullpenTransition'] = min(components['bullpenTransition'], 35.0)

    weights = {
        'platoon': .16,
        'pitchProfile': .24,
        'contactProfile': .20,
        'strikeoutProfile': .20,
        'recentForm': .12,
        'bullpenTransition': .08,
    }
    score = round(sum(components[name] * weight for name, weight in weights.items()), 1)
    advantages = [
        f"{name.replace('Profile', ' profile').replace('Transition', ' transition').title()} {value:.0f}/100"
        for name, value in components.items() if value >= 65.0
    ]
    risks = [label for label, condition in (
        ('platoon disadvantage', components['platoon'] < 40),
        ('poor pitch-type fit', components['pitchProfile'] < 40),
        ('weak contact matchup', components['contactProfile'] < 40),
        ('unfavorable strikeout profile', components['strikeoutProfile'] < 40),
        ('negative recent pitcher/form trend', components['recentForm'] < 40),
        ('poor bullpen transition', components['bullpenTransition'] < 40),
    ) if condition]
    return {
        'matchupScore': score,
        'matchupComponents': components,
        'matchupAdvantages': advantages,
        'matchupRisks': risks,
        'pitchProfileFit': components['pitchProfile'],
        'contactProfileFit': components['contactProfile'],
    }


def enrich_matchups(picks: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    enriched = []
    for source in picks:
        row = dict(source)
        row.update(score_matchup(source))
        enriched.append(row)
    return enriched
