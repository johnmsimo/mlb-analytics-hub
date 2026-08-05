import unittest

from learning_engine import analyze_learning


class LearningEngineTests(unittest.TestCase):
    def test_metrics_for_graded_predictions(self):
        result = analyze_learning([
            {'market': 'Player Hits', 'blendedProb': .70, 'grade': 'WIN'},
            {'market': 'Player Hits', 'blendedProb': .60, 'grade': 'LOSS'},
        ])
        self.assertEqual(result['gradedCount'], 2)
        self.assertEqual(result['overall']['wins'], 1)
        self.assertAlmostEqual(result['overall']['winRate'], .5)
        self.assertIsNotNone(result['overall']['brierScore'])
        self.assertIsNotNone(result['overall']['logLoss'])

    def test_pending_and_missing_probabilities_are_skipped(self):
        result = analyze_learning([
            {'blendedProb': .70, 'grade': 'pending'},
            {'grade': 'WIN'},
        ])
        self.assertEqual(result['gradedCount'], 0)
        self.assertEqual(result['skippedCount'], 2)

    def test_groups_by_market_calibration_and_factors(self):
        result = analyze_learning([
            {'market': 'Moneyline', 'blendedProb': .74, 'grade': 'WIN', 'contextScore': 82},
            {'market': 'Pitcher Strikeouts', 'blendedProb': .64, 'grade': 'LOSS', 'contextScore': 45},
        ])
        self.assertIn('Moneyline', result['byMarket'])
        self.assertIn('70-79%', result['calibrationBuckets'])
        self.assertIn('contextScore', result['factorPerformance'])
        self.assertIn('high', result['factorPerformance']['contextScore'])

    def test_learning_is_measurement_only(self):
        result = analyze_learning([])
        self.assertEqual(result['mode'], 'measurement_only')
        self.assertFalse(result['adaptiveWeightsEnabled'])


if __name__ == '__main__':
    unittest.main()
