import unittest
from datetime import datetime, timezone

from explanation_engine import explain_decisions, explain_recommendation
from intelligence_core import build_recommendations


def strong_pick():
    return {
        'id': 'strong-1',
        'market': 'Player Hits',
        'intelligenceCategory': 'hitter_hits',
        'blendedProb': .70,
        'edge': .11,
        'confidenceScore': 86,
        'confidenceLabel': 'Very High',
        'confidenceComponents': {
            'modelAgreement': 92,
            'intervalStability': 88,
            'sampleSupport': 80,
        },
        'contextScore': 80,
        'matchupScore': 84,
        'simulationScore': 82,
        'decisionScore': 81,
        'grade': 'pending',
    }


class ExplanationEngineTests(unittest.TestCase):
    def test_generates_complete_explanation(self):
        result = explain_recommendation(strong_pick())
        expected = {
            'recommendationGrade', 'decisionSummary', 'topReasons', 'topRisks',
            'recommendedAction', 'confidenceNarrative', 'supportingEvidence',
        }
        self.assertTrue(expected.issubset(result))
        self.assertEqual(result['recommendationGrade'], 'Strong Play')
        self.assertIn('Very High confidence', result['confidenceNarrative'])

    def test_ranks_only_highest_contributing_evidence(self):
        learning = {
            'byMarket': {
                'Player Hits': {
                    'count': 40,
                    'winRate': .65,
                    'calibrationError': .03,
                },
            },
        }
        result = explain_recommendation(strong_pick(), learning=learning)
        evidence = result['supportingEvidence']
        self.assertLessEqual(len(evidence), 5)
        self.assertEqual([row['rank'] for row in evidence], list(range(1, len(evidence) + 1)))
        self.assertEqual(
            [row['contribution'] for row in evidence],
            sorted((row['contribution'] for row in evidence), reverse=True),
        )
        self.assertEqual(result['topReasons'], [row['label'] for row in evidence[:3]])

    def test_consumes_measurement_only_learning_output(self):
        pick = strong_pick()
        pick.update({'contextScore': 50, 'matchupScore': 50, 'simulationScore': 50})
        learning = {
            'mode': 'measurement_only',
            'adaptiveWeightsEnabled': False,
            'byMarket': {
                'Player Hits': {
                    'count': 25,
                    'winRate': .64,
                    'calibrationError': .04,
                },
            },
        }
        result = explain_recommendation(pick, learning=learning)
        learning_evidence = [row for row in result['supportingEvidence'] if row['engine'] == 'learning']
        self.assertEqual(len(learning_evidence), 1)
        self.assertIn('25 graded', learning_evidence[0]['label'])

    def test_classifies_value_play_and_lean(self):
        value = strong_pick()
        value.update({'decisionScore': 69, 'edge': .07})
        lean = strong_pick()
        lean.update({'decisionScore': 63, 'edge': .03})
        self.assertEqual(explain_recommendation(value)['recommendationGrade'], 'Value Play')
        self.assertEqual(explain_recommendation(lean)['recommendationGrade'], 'Lean')

    def test_surfaces_high_impact_risks_separately(self):
        pick = strong_pick()
        pick.update({
            'decisionScore': 69,
            'contextScore': 32,
            'contextRisks': ['weather uncertainty', 'unconfirmed lineup'],
            'simulationScore': 35,
            'simulationRisks': ['high outcome volatility'],
        })
        result = explain_recommendation(pick)
        self.assertIn('high outcome volatility', result['topRisks'])
        self.assertNotIn('high outcome volatility', result['topReasons'])
        self.assertFalse(any(row['label'] == 'high outcome volatility' for row in result['supportingEvidence']))

    def test_rejected_candidate_becomes_explicit_pass(self):
        decisions = build_recommendations([{
            'id': 'weak-1',
            'market': 'Player Hits',
            'blendedProb': .51,
            'confidenceScore': 40,
            'edge': .01,
            'grade': 'pending',
            'gamePk': 7,
            'player': 'Weak Hitter',
            'playerId': 77,
            'line': 0.5,
            'recommendedSide': 'Over',
            'bestAvailablePrice': -110,
            'bestAvailableBook': 'Book A',
            'bestOverPrice': -110,
            'bestUnderPrice': -105,
            'gameStatus': 'Scheduled',
            'gameAbstractState': 'Preview',
            'gameStartIso': '2099-08-06T23:10:00+00:00',
            'lineupStatus': 'confirmed',
            'playerRole': 'batter',
            'playerPosition': 'CF',
            'modelVersion': 'test-model-4.37',
            'matchupSimulationVersion': '4.35',
            'gameSimN': 1500,
            'oddsUpdatedAt': datetime.now(timezone.utc).isoformat(),
        }])
        result = explain_decisions(decisions)
        self.assertEqual(result['rejected'][0]['recommendationGrade'], 'Pass')
        self.assertEqual(result['passes'][0]['id'], 'weak-1')
        self.assertIn('probability below threshold', result['passes'][0]['topRisks'])
        self.assertTrue(result['passes'][0]['recommendedAction'].startswith('Pass'))

    def test_explanation_does_not_mutate_input(self):
        pick = strong_pick()
        explain_recommendation(pick)
        self.assertNotIn('recommendationGrade', pick)


if __name__ == '__main__':
    unittest.main()
