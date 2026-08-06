"""Shared game-simulation signals for decision-ready MLB recommendations.

This module is intentionally independent from Flask and the large application
module.  It standardizes the probabilities produced by one game simulation so
hitter hits, pitcher strikeouts, and moneyline candidates can be compared and
audited as outcomes of the same simulated matchup.
"""
from __future__ import annotations

import math
import statistics
from typing import Any, Iterable, Mapping, Sequence


SIMULATION_VERSION = "4.35"


def _number(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or isinstance(value, bool):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp_probability(value: Any) -> float:
    return max(0.0, min(1.0, _number(value)))


def exact_over_probability(values: Iterable[Any], line: Any) -> float:
    """Return the empirical probability of an outcome finishing over ``line``."""
    samples = [_number(value) for value in values]
    if not samples:
        return 0.0
    threshold = _number(line)
    return round(sum(value > threshold for value in samples) / len(samples), 4)


def wilson_interval(probability: Any, sample_size: Any) -> tuple[float, float]:
    """Return a stable 95% Wilson interval for a simulated event probability."""
    probability = _clamp_probability(probability)
    n = max(0, int(_number(sample_size)))
    if n <= 0:
        return 0.0, 1.0
    z = 1.959963984540054
    denominator = 1.0 + (z * z / n)
    center = (probability + (z * z / (2.0 * n))) / denominator
    margin = (
        z
        * math.sqrt(
            (probability * (1.0 - probability) / n)
            + (z * z / (4.0 * n * n))
        )
        / denominator
    )
    return round(max(0.0, center - margin), 4), round(
        min(1.0, center + margin), 4
    )


def build_simulation_signal(
    probability: Any,
    sample_size: Any,
    *,
    mode: str,
    matchup: str,
    outcome_mean: Any = None,
    evidence: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Build the shared simulation contract attached to every candidate.

    ``probability`` always describes the row's base/Over outcome.  Consumers
    invert it for an Under candidate, which preserves one auditable source for
    both sides of a sportsbook market.
    """
    probability = _clamp_probability(probability)
    n_sims = max(0, int(_number(sample_size)))
    low, high = wilson_interval(probability, n_sims)
    standard_error = (
        math.sqrt(probability * (1.0 - probability) / n_sims)
        if n_sims else None
    )
    details = [
        f"Shared matchup simulation {probability:.1%}",
        f"{n_sims:,} linked game trials",
        matchup,
    ]
    details.extend(str(item) for item in (evidence or []) if item)
    return {
        "sharedSimulationBacked": n_sims > 0,
        "matchupSimulationVersion": SIMULATION_VERSION,
        "matchupSimulationMode": mode,
        "matchupSimulationSource": matchup,
        "gameSimProbability": round(probability, 4),
        "gameSimN": n_sims,
        "gameSimStd": (
            None if standard_error is None else round(standard_error, 5)
        ),
        "gameSimPlo": low,
        "gameSimPhi": high,
        "gameSimMean": (
            None if outcome_mean is None else round(_number(outcome_mean), 3)
        ),
        "gameSimulationEvidence": list(dict.fromkeys(details)),
    }


def summarize_game_outcomes(
    away_runs: Sequence[Any], home_runs: Sequence[Any]
) -> dict[str, Any]:
    """Summarize linked score trials into two-way moneyline probabilities.

    Nine-inning ties are divided equally between the teams.  This is the
    neutral extra-innings assumption and keeps the two moneyline probabilities
    complementary instead of silently discarding tied trials.
    """
    pairs = [
        (_number(away), _number(home))
        for away, home in zip(away_runs, home_runs)
    ]
    if not pairs:
        return {
            "nSims": 0,
            "awayWinProbability": 0.5,
            "homeWinProbability": 0.5,
            "tieProbability": 0.0,
            "awayMeanRuns": 0.0,
            "homeMeanRuns": 0.0,
            "meanTotalRuns": 0.0,
        }
    away_wins = sum(away > home for away, home in pairs)
    home_wins = sum(home > away for away, home in pairs)
    ties = len(pairs) - away_wins - home_wins
    n_sims = len(pairs)
    away_probability = (away_wins + 0.5 * ties) / n_sims
    home_probability = 1.0 - away_probability
    away_values = [away for away, _ in pairs]
    home_values = [home for _, home in pairs]
    return {
        "nSims": n_sims,
        "awayWinProbability": round(away_probability, 4),
        "homeWinProbability": round(home_probability, 4),
        "tieProbability": round(ties / n_sims, 4),
        "awayMeanRuns": round(statistics.mean(away_values), 3),
        "homeMeanRuns": round(statistics.mean(home_values), 3),
        "meanTotalRuns": round(
            statistics.mean(
                away + home for away, home in pairs
            ),
            3,
        ),
    }


def simulation_audit(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Return compact coverage metadata for API/UI observability."""
    candidates = list(rows)
    backed = [row for row in candidates if row.get("sharedSimulationBacked")]
    versions = sorted({
        str(row.get("matchupSimulationVersion"))
        for row in backed if row.get("matchupSimulationVersion")
    })
    trials = [int(_number(row.get("gameSimN"))) for row in backed]
    return {
        "candidateCount": len(candidates),
        "simulationBackedCount": len(backed),
        "simulationCoveragePct": round(
            (len(backed) / len(candidates) * 100.0) if candidates else 0.0,
            1,
        ),
        "minimumLinkedTrials": min(trials) if trials else 0,
        "versions": versions,
    }
