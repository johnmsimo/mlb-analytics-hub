import unittest

from matchup_engine import enrich_matchups, score_matchup


class MatchupEngineTests(unittest.TestCase):
    def test_missing_matchup_evidence_is_neutral(self):
        result = score_matchup({'id': 'p1'})
        self.assertEqual(result['matchupScore'], 50.0)
        self.assertEqual(result['matchupAdvantages'], [])
        self.assertEqual(result['matchupRisks'], [])

    def test_strong_pitch_and_contact_fit_raise_score(self):
        result = score_matchup({
            'platoonAdvantage': True,
            'pitchTypeFitScore': 82,
            'contactMatchupScore': 78,
            'strikeoutMatchupScore': 72,
            'recentFormScore': 68,
            'bullpenTransitionScore': 65,
        })
        self.assertGreater(result['matchupScore'], 70)
        self.assertEqual(result['pitchProfileFit'], 82)
        self.assertTrue(result['matchupAdvantages'])

    def test_warning_flags_create_conservative_risks(self):
        result = score_matchup({
            'platoonDisadvantage': True,
            'velocityDecline': True,
            'poorBullpenTransition': True,
            'pitchTypeFitScore': 30,
        })
        self.assertLess(result['matchupScore'], 45)
        self.assertIn('platoon disadvantage', result['matchupRisks'])
        self.assertIn('poor pitch-type fit', result['matchupRisks'])
        self.assertIn('poor bullpen transition', result['matchupRisks'])

    def test_enrichment_does_not_mutate_input(self):
        original = {'id': 'p1', 'platoonScore': 70}
        enriched = enrich_matchups([original])
        self.assertNotIn('matchupScore', original)
        self.assertEqual(enriched[0]['id'], 'p1')
        self.assertIn('matchupComponents', enriched[0])


if __name__ == '__main__':
    unittest.main()
