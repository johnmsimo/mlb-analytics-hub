import unittest

from confidence_service import (
    confidence_for_pick,
    enrich_pick_confidence,
    score_projection,
)


class ConfidenceServiceTests(unittest.TestCase):
    def test_strong_agreeing_projection_scores_high(self):
        result = score_projection(
            probability=0.68,
            market_implied=0.53,
            interval_low=0.61,
            interval_high=0.73,
            monte_carlo_probability=0.66,
            monte_carlo_std=0.06,
            sample_size=3000,
        )
        self.assertGreaterEqual(result.score, 72.0)
        self.assertIn(result.tier, {"HIGH", "VERY_HIGH"})
        self.assertEqual(set(result.components), {
            "probabilityStrength",
            "marketEdge",
            "intervalStability",
            "modelAgreement",
            "simulationStability",
            "sampleSupport",
        })

    def test_wide_disagreeing_projection_scores_lower(self):
        strong = score_projection(
            probability=0.68,
            market_implied=0.53,
            interval_low=0.61,
            interval_high=0.73,
            monte_carlo_probability=0.66,
            monte_carlo_std=0.06,
            sample_size=3000,
        )
        weak = score_projection(
            probability=0.55,
            market_implied=0.54,
            interval_low=0.35,
            interval_high=0.77,
            monte_carlo_probability=0.43,
            monte_carlo_std=0.20,
            sample_size=200,
        )
        self.assertLess(weak.score, strong.score)
        self.assertIn(weak.tier, {"LOW", "MEDIUM"})

    def test_missing_probability_is_explicitly_low(self):
        result = score_projection(probability=None)
        self.assertEqual(result.score, 0.0)
        self.assertEqual(result.tier, "LOW")
        self.assertIn("No calibrated probability", result.explanation)

    def test_under_pick_uses_under_monte_carlo_probability(self):
        result = confidence_for_pick({
            "recommendedSide": "Under",
            "adjProb": 0.64,
            "marketImplied": 0.51,
            "p_lo": 0.58,
            "p_hi": 0.70,
            "mc_prob_over": 0.35,
            "mc_std": 0.07,
            "mc_n_sims": 2500,
        })
        self.assertGreater(result.components["modelAgreement"], 90.0)

    def test_shared_matchup_simulation_is_confidence_source_for_under(self):
        result = confidence_for_pick({
            "recommendedSide": "Under",
            "adjProb": 0.61,
            "marketImplied": 0.52,
            "sharedSimulationBacked": True,
            "gameSimProbability": 0.40,
            "gameSimPlo": 0.375,
            "gameSimPhi": 0.425,
            "gameSimStd": 0.013,
            "gameSimN": 1500,
            # Deliberately disagreeing legacy candidate simulation; Phase 4.35
            # must ignore it in favor of the shared game trials above.
            "mc_prob_under": 0.30,
            "mc_std": 0.20,
            "mc_n_sims": 100,
        })
        self.assertGreater(result.components["modelAgreement"], 90.0)
        self.assertGreater(result.components["intervalStability"], 80.0)
        self.assertEqual(result.components["sampleSupport"], 75.0)

    def test_enrichment_preserves_original_pick(self):
        pick = {"id": "pick-1", "adjProb": 0.62, "marketImplied": 0.52}
        enriched = enrich_pick_confidence(pick)
        self.assertEqual(enriched["id"], "pick-1")
        self.assertNotIn("confidenceScore", pick)
        self.assertIn("confidenceScore", enriched)
        self.assertIn("confidenceTier", enriched)
        self.assertIn("confidenceExplanation", enriched)
        self.assertIn("modelReliabilityScore", enriched)
        self.assertNotIn("marketEdge", enriched["modelReliabilityComponents"])
        self.assertIn(
            enriched["modelReliabilityTier"],
            {"LOW", "MEDIUM", "HIGH", "VERY_HIGH"},
        )


if __name__ == "__main__":
    unittest.main()
