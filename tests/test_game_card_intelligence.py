import unittest

from game_card_intelligence import (
    build_game_card_quick_picks,
    prepare_game_card_candidates,
)
from intelligence_core import classify_pick


def row(market, *, player='Player', probability=.66, implied=.55, confidence_hint=None, **extra):
    data = {
        'id': f'{market}:{player}',
        'gamePk': 7,
        'player': player,
        'playerId': player,
        'team': extra.pop('team', 'NYY'),
        'marketKey': market,
        'line': extra.pop('line', 0.5),
        'recommendedSide': extra.pop('recommendedSide', 'Over'),
        'adjProb': probability,
        'marketImplied': implied,
        'edge': probability - implied,
        'bestOverPrice': -120,
        'bestOverBook': 'Book A',
        'bestUnderPrice': 110,
        'bestUnderBook': 'Book B',
        'mc_prob_over': probability,
        'mc_prob_under': 1 - probability,
        'mc_std': .04,
        'mc_n_sims': 2500,
        'p_lo': probability - .04,
        'p_hi': probability + .04,
        'hubRating': 82,
        'grade': 'pending',
    }
    data.update(extra)
    if confidence_hint is not None:
        data['confidenceScore'] = confidence_hint
    return data


class GameCardIntelligenceTests(unittest.TestCase):
    def test_classifies_tracker_market_key_aliases(self):
        self.assertEqual(classify_pick({'marketKey': 'batter_hits'}), 'hitter_hits')
        self.assertEqual(classify_pick({'marketKey': 'pitcher_strikeouts'}), 'pitcher_strikeouts')
        self.assertEqual(classify_pick({'marketKey': 'h2h'}), 'game_winner')

    def test_creates_independent_over_and_under_strikeout_candidates(self):
        candidates = prepare_game_card_candidates([
            row('pitcher_strikeouts', player='Starter', probability=.43, implied=.52, line=5.5)
        ])
        self.assertEqual(len(candidates), 2)
        over = next(item for item in candidates if item['recommendedSide'] == 'Over')
        under = next(item for item in candidates if item['recommendedSide'] == 'Under')
        self.assertAlmostEqual(over['adjProb'], .43)
        self.assertAlmostEqual(under['adjProb'], .57)
        self.assertEqual(under['bestAvailablePrice'], 110)
        self.assertEqual(under['bestAvailableBook'], 'Book B')
        self.assertNotEqual(over['id'], under['id'])

    def test_returns_one_pick_for_each_required_game_card_category(self):
        result = build_game_card_quick_picks([
            row('batter_hits', player='Hitter A', probability=.69, implied=.56),
            row('pitcher_strikeouts', player='Starter A', probability=.67, implied=.54, line=5.5),
            row('h2h', player='NYY@BOS', team='NYY', probability=.64, implied=.54, recommendedSide='NYY', line=0),
        ])
        self.assertEqual(
            [pick['intelligenceCategory'] for pick in result['quickPicks']],
            ['hitter_hits', 'pitcher_strikeouts', 'game_winner'],
        )
        self.assertTrue(all('decisionSummary' in pick for pick in result['quickPicks']))
        self.assertEqual(result['best']['game_winner']['recommendedSide'], 'NYY')
        self.assertEqual(result['policy']['rankingPriority'], 'confidenceScore')

    def test_selects_under_when_pitcher_k_under_is_the_value_side(self):
        result = build_game_card_quick_picks([
            row('batter_hits', player='Hitter', probability=.69, implied=.56),
            row('pitcher_strikeouts', player='Starter', probability=.43, implied=.52, line=5.5),
            row('h2h', player='NYY@BOS', team='NYY', probability=.64, implied=.54, recommendedSide='NYY', line=0),
        ])
        pitcher = result['best']['pitcher_strikeouts']
        self.assertEqual(pitcher['recommendedSide'], 'Under')
        self.assertAlmostEqual(pitcher['adjProb'], .57)
        self.assertEqual(pitcher['recommendationGrade'] in {'Strong Play', 'Value Play', 'Lean'}, True)

    def test_selects_highest_confidence_hit_not_first_input(self):
        result = build_game_card_quick_picks([
            row('batter_hits', player='Lower', probability=.60, implied=.55, mc_std=.12, p_lo=.45, p_hi=.75),
            row('batter_hits', player='Higher', probability=.70, implied=.56, mc_std=.03, p_lo=.67, p_hi=.73),
            row('pitcher_strikeouts', player='Starter', probability=.66, implied=.54, line=5.5),
            row('h2h', player='NYY@BOS', team='NYY', probability=.64, implied=.54, recommendedSide='NYY', line=0),
        ])
        self.assertEqual(result['best']['hitter_hits']['player'], 'Higher')

    def test_weak_category_is_explicit_pass(self):
        result = build_game_card_quick_picks([
            row('batter_hits', player='Weak', probability=.52, implied=.51),
        ])
        hit = result['best']['hitter_hits']
        self.assertEqual(hit['recommendationGrade'], 'Pass')
        self.assertIn('probability below threshold', hit['topRisks'])
        self.assertEqual(result['best']['pitcher_strikeouts']['recommendationGrade'], 'Pass')
        self.assertEqual(result['best']['game_winner']['recommendationGrade'], 'Pass')


if __name__ == '__main__':
    unittest.main()
