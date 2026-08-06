"""Deterministic confidence scoring for MLB projections and tracker picks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class ConfidenceResult:
    score: float
    tier: str
    label: str
    explanation: str
    components: dict[str, float]

    def as_dict(self) -> dict[str, Any]:
        reliability_components = {
            key: value for key, value in self.components.items()
            if key != "marketEdge"
        }
        reliability_weights = {
            "probabilityStrength": 0.28,
            "intervalStability": 0.18,
            "modelAgreement": 0.14,
            "simulationStability": 0.10,
            "sampleSupport": 0.06,
        }
        weight_total = sum(reliability_weights.values())
        reliability = round(sum(
            reliability_components.get(key, 0.0) * weight
            for key, weight in reliability_weights.items()
        ) / weight_total, 1)
        reliability_tier, reliability_label = _tier(reliability)
        strongest = sorted(
            reliability_components.items(),
            key=lambda item: (-item[1], item[0]),
        )[:2]
        names = {
            "probabilityStrength": "probability strength",
            "intervalStability": "interval stability",
            "modelAgreement": "model agreement",
            "simulationStability": "simulation stability",
            "sampleSupport": "sample support",
        }
        reliability_explanation = (
            f"Driven by {names[strongest[0][0]]} and "
            f"{names[strongest[1][0]]}."
            if len(strongest) >= 2 else "Reliability evidence is unavailable."
        )
        return {
            "confidenceScore": self.score,
            "confidenceTier": self.tier,
            "confidenceLabel": self.label,
            "confidenceExplanation": self.explanation,
            "confidenceComponents": dict(self.components),
            # Explicit aliases stop consumers from mistaking projection
            # reliability for either win probability or final bet quality.
            "modelReliabilityScore": reliability,
            "modelReliabilityTier": reliability_tier,
            "modelReliabilityLabel": reliability_label,
            "modelReliabilityExplanation": reliability_explanation,
            "modelReliabilityComponents": reliability_components,
        }


def _number(value: Any) -> Optional[float]:
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _tier(score: float) -> tuple[str, str]:
    if score >= 85.0:
        return "VERY_HIGH", "Very High"
    if score >= 72.0:
        return "HIGH", "High"
    if score >= 56.0:
        return "MEDIUM", "Medium"
    return "LOW", "Low"


def score_projection(
    *,
    probability: Any,
    market_implied: Any = None,
    interval_low: Any = None,
    interval_high: Any = None,
    monte_carlo_probability: Any = None,
    monte_carlo_std: Any = None,
    sample_size: Any = None,
) -> ConfidenceResult:
    """Return a transparent 0-100 confidence score.

    Confidence rewards calibrated probability separation, positive market edge,
    narrow prediction intervals, XGB/Monte-Carlo agreement, low simulation
    dispersion, and adequate sample support. Missing evidence is neutral rather
    than silently treated as perfect evidence.
    """
    prob = _number(probability)
    if prob is None:
        return ConfidenceResult(
            score=0.0,
            tier="LOW",
            label="Low",
            explanation="No calibrated probability is available.",
            components={
                "probabilityStrength": 0.0,
                "marketEdge": 0.0,
                "intervalStability": 0.0,
                "modelAgreement": 0.0,
                "simulationStability": 0.0,
                "sampleSupport": 0.0,
            },
        )

    prob = _clamp(prob)
    implied = _number(market_implied)
    p_lo = _number(interval_low)
    p_hi = _number(interval_high)
    mc_prob = _number(monte_carlo_probability)
    mc_std = _number(monte_carlo_std)
    n = _number(sample_size)

    probability_strength = _clamp(abs(prob - 0.5) / 0.25)

    market_edge = 0.5
    if implied is not None:
        market_edge = _clamp((prob - _clamp(implied) + 0.02) / 0.14)

    interval_stability = 0.5
    if p_lo is not None and p_hi is not None and p_hi >= p_lo:
        interval_stability = _clamp(1.0 - ((p_hi - p_lo) / 0.40))

    model_agreement = 0.5
    if mc_prob is not None:
        model_agreement = _clamp(1.0 - (abs(prob - _clamp(mc_prob)) / 0.18))

    simulation_stability = 0.5
    if mc_std is not None:
        simulation_stability = _clamp(1.0 - (max(0.0, mc_std) / 0.22))

    sample_support = 0.5
    if n is not None:
        sample_support = _clamp(max(0.0, n) / 2000.0)

    weighted = (
        probability_strength * 0.28
        + market_edge * 0.24
        + interval_stability * 0.18
        + model_agreement * 0.14
        + simulation_stability * 0.10
        + sample_support * 0.06
    )
    score = round(weighted * 100.0, 1)
    tier, label = _tier(score)

    strongest = sorted(
        {
            "probability separation": probability_strength,
            "market edge": market_edge,
            "interval stability": interval_stability,
            "model agreement": model_agreement,
            "simulation stability": simulation_stability,
            "sample support": sample_support,
        }.items(),
        key=lambda item: item[1],
        reverse=True,
    )[:2]
    explanation = f"Driven by {strongest[0][0]} and {strongest[1][0]}."

    return ConfidenceResult(
        score=score,
        tier=tier,
        label=label,
        explanation=explanation,
        components={
            "probabilityStrength": round(probability_strength * 100.0, 1),
            "marketEdge": round(market_edge * 100.0, 1),
            "intervalStability": round(interval_stability * 100.0, 1),
            "modelAgreement": round(model_agreement * 100.0, 1),
            "simulationStability": round(simulation_stability * 100.0, 1),
            "sampleSupport": round(sample_support * 100.0, 1),
        },
    )


def confidence_for_pick(pick: Mapping[str, Any]) -> ConfidenceResult:
    side = str(pick.get("recommendedSide") or "Over").strip().lower()
    shared_probability = pick.get("gameSimProbability")
    shared_backed = (
        pick.get("sharedSimulationBacked") is True
        and shared_probability is not None
    )
    if shared_backed:
        mc_probability = float(shared_probability)
        interval_low = pick.get("gameSimPlo")
        interval_high = pick.get("gameSimPhi")
        if side == "under":
            mc_probability = 1.0 - mc_probability
            if interval_low is not None and interval_high is not None:
                interval_low, interval_high = (
                    1.0 - float(interval_high),
                    1.0 - float(interval_low),
                )
        mc_std = pick.get("gameSimStd")
        sample_size = pick.get("gameSimN")
    else:
        mc_probability = pick.get("mc_prob_over")
        if side == "under":
            mc_probability = pick.get("mc_prob_under")
            if mc_probability is None and pick.get("mc_prob_over") is not None:
                mc_probability = 1.0 - float(pick["mc_prob_over"])
        interval_low = pick.get("p_lo")
        interval_high = pick.get("p_hi")
        mc_std = pick.get("mc_std")
        sample_size = pick.get("mc_n_sims")

    return score_projection(
        probability=(
            pick.get("blendedProb")
            if pick.get("blendedProb") is not None
            else pick.get("adjProb")
        ),
        market_implied=(
            pick.get("marketImplied")
            if pick.get("marketImplied") is not None
            else pick.get("openingImplied")
        ),
        interval_low=interval_low,
        interval_high=interval_high,
        monte_carlo_probability=mc_probability,
        monte_carlo_std=mc_std,
        sample_size=sample_size,
    )


def enrich_pick_confidence(pick: Mapping[str, Any]) -> dict[str, Any]:
    enriched = dict(pick)
    enriched.update(confidence_for_pick(pick).as_dict())
    return enriched
