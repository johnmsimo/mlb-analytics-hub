import unittest
from intelligence_core import build_recommendations, classify_pick


def pick(pid, market, probability=.64, confidence=76, edge=.08, rating=80):
    return {'id': pid, 'market': market, 'blendedProb': probability, 'confidenceScore': confidence, 'edge': edge, 'hubRating': rating, 'grade': 'pending'}


class IntelligenceCoreTests(unittest.TestCase):
    def test_classifies_first_intelligence_markets(self):
        self.assertEqual(classify_pick(pick('h','Player Hits')), 'hitter_hits')
        self.assertEqual(classify_pick(pick('k','Pitcher Strikeouts')), 'pitcher_strikeouts')
        self.assertEqual(classify_pick(pick('m','Moneyline')), 'game_winner')

    def test_selects_best_in_each_category_and_diverse_card(self):
        result = build_recommendations([
            pick('h1','Player Hits',.62,72,.06), pick('h2','Player Hits',.69,84,.10),
            pick('k1','Pitcher Strikeouts',.66,80,.09), pick('m1','Moneyline',.61,73,.05),
        ])
        self.assertEqual(result['best']['hitter_hits']['id'], 'h2')
        self.assertEqual(result['best']['pitcher_strikeouts']['id'], 'k1')
        self.assertEqual(result['best']['game_winner']['id'], 'm1')
        self.assertEqual({row['intelligenceCategory'] for row in result['card'][:3]}, {'hitter_hits','pitcher_strikeouts','game_winner'})

    def test_abstains_instead_of_forcing_weak_play(self):
        result = build_recommendations([pick('weak','Player Hits',.51,40,.01)])
        self.assertIsNone(result['best']['hitter_hits'])
        self.assertIn('hitter_hits', result['abstentions'])
        self.assertEqual(result['eligibleCount'], 0)
        self.assertGreater(result['rejectedCount'], 0)

    def test_does_not_mutate_input(self):
        original = pick('h','Player Hits')
        build_recommendations([original])
        self.assertNotIn('decisionScore', original)


if __name__ == '__main__':
    unittest.main()
