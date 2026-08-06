import unittest

from simulation_engine import enrich_simulations, score_simulation


class SimulationEngineTests(unittest.TestCase):
    def test_missing_simulation_data_is_neutral(self):
        result = score_simulation({'id': 'p1'})
        self.assertEqual(result['simulationScore'], 50.0)
        self.assertEqual(result['tailRisk'], 'unknown')
        self.assertEqual(result['simulationRisks'], [])

    def test_stable_large_simulation_scores_well(self):
        result = score_simulation({
            'mc_prob_over': .68,
            'mc_std': .05,
            'mc_n_sims': 5000,
            'p_lo': .62,
            'p_hi': .73,
        })
        self.assertGreater(result['simulationScore'], 75)
        self.assertGreater(result['consistencyScore'], 65)
        self.assertEqual(result['tailRisk'], 'low')

    def test_wide_unstable_simulation_exposes_risk(self):
        result = score_simulation({
            'mc_prob_over': .57,
            'mc_std': .20,
            'mc_n_sims': 250,
            'p_lo': .34,
            'p_hi': .78,
        })
        self.assertGreater(result['volatilityScore'], 65)
        self.assertEqual(result['tailRisk'], 'high')
        self.assertIn('high outcome volatility', result['simulationRisks'])
        self.assertIn('small simulation sample', result['simulationRisks'])

    def test_under_side_uses_under_probability_and_does_not_mutate(self):
        original = {'id': 'p1', 'recommendedSide': 'Under', 'mc_prob_over': .34}
        enriched = enrich_simulations([original])
        self.assertAlmostEqual(enriched[0]['simulationProbability'], .66)
        self.assertNotIn('simulationScore', original)

    def test_shared_game_simulation_overrides_disconnected_candidate_mc(self):
        result = score_simulation({
            'recommendedSide': 'Over',
            'sharedSimulationBacked': True,
            'gameSimProbability': .64,
            'gameSimStd': .012,
            'gameSimN': 1500,
            'gameSimPlo': .616,
            'gameSimPhi': .664,
            'matchupSimulationSource': 'Hitter versus opposing starter',
            'gameSimulationEvidence': ['linked plate-appearance trials'],
            'mc_prob_over': .51,
            'mc_n_sims': 100,
        })

        self.assertAlmostEqual(result['simulationProbability'], .64)
        self.assertTrue(result['sharedSimulationBacked'])
        self.assertEqual(
            result['simulationSource'], 'Hitter versus opposing starter'
        )
        self.assertIn('linked plate-appearance trials', result['simulationEvidence'])

    def test_shared_game_simulation_inverts_probability_and_interval_for_under(self):
        result = score_simulation({
            'recommendedSide': 'Under',
            'sharedSimulationBacked': True,
            'gameSimProbability': .42,
            'gameSimStd': .013,
            'gameSimN': 1500,
            'gameSimPlo': .395,
            'gameSimPhi': .445,
        })

        self.assertAlmostEqual(result['simulationProbability'], .58)
        self.assertAlmostEqual(result['simulationInterval']['low'], .555)
        self.assertAlmostEqual(result['simulationInterval']['high'], .605)


if __name__ == '__main__':
    unittest.main()
