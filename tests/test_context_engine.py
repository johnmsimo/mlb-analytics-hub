import unittest

from context_engine import enrich_context, score_context


class ContextEngineTests(unittest.TestCase):
    def test_missing_context_is_neutral(self):
        result = score_context({'id': 'p1'})
        self.assertEqual(result['contextScore'], 50.0)
        self.assertEqual(result['contextRisks'], [])

    def test_confirmed_lineup_and_positive_context_raise_score(self):
        result = score_context({
            'lineupConfirmed': True,
            'weatherScore': 75,
            'parkFactorScore': 70,
            'bullpenAvailabilityScore': 68,
            'restScore': 65,
            'umpireFitScore': 60,
        })
        self.assertGreater(result['contextScore'], 65)
        self.assertIn('Lineup context 70/100', result['contextEvidence'])

    def test_risk_flags_reduce_context_and_are_explained(self):
        result = score_context({
            'weatherRisk': True,
            'lineupConfirmed': False,
            'bullpenFatigued': True,
            'travelDisadvantage': True,
        })
        self.assertLess(result['contextScore'], 40)
        self.assertIn('weather uncertainty', result['contextRisks'])
        self.assertIn('bullpen disadvantage', result['contextRisks'])

    def test_enrichment_does_not_mutate_input(self):
        original = {'id': 'p1', 'weatherScore': 70}
        enriched = enrich_context([original])
        self.assertNotIn('contextScore', original)
        self.assertEqual(enriched[0]['id'], 'p1')
        self.assertIn('contextComponents', enriched[0])


if __name__ == '__main__':
    unittest.main()
