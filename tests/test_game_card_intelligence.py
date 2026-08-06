from datetime import timezone
from types import SimpleNamespace

import intelligence_integration
from game_card_intelligence import (
    build_game_card_quick_picks,
    prepare_game_card_candidates,
)
from intelligence_core import classify_pick


def row(
    market,
    *,
    player='Player',
    probability=.66,
    implied=.55,
    **extra,
):
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
    return data


def test_classifies_tracker_market_key_aliases():
    assert classify_pick({'marketKey': 'batter_hits'}) == 'hitter_hits'
    assert classify_pick({'marketKey': 'pitcher_strikeouts'}) == 'pitcher_strikeouts'
    assert classify_pick({'marketKey': 'h2h'}) == 'game_winner'


def test_creates_independently_scored_over_and_under_strikeout_candidates():
    candidates = prepare_game_card_candidates([
        row(
            'pitcher_strikeouts',
            player='Starter',
            probability=.43,
            implied=.52,
            line=5.5,
        )
    ])

    over = next(item for item in candidates if item['recommendedSide'] == 'Over')
    under = next(item for item in candidates if item['recommendedSide'] == 'Under')

    assert over['adjProb'] == .43
    assert under['adjProb'] == .57
    assert under['bestAvailablePrice'] == 110
    assert under['bestAvailableBook'] == 'Book B'
    assert under['p_lo'] == .53
    assert under['p_hi'] == .61
    assert under['confidenceComponents']['modelAgreement'] == 100.0
    assert over['id'] != under['id']


def test_returns_one_explained_decision_for_each_required_category():
    result = build_game_card_quick_picks([
        row('batter_hits', player='Hitter A', probability=.69, implied=.56),
        row(
            'pitcher_strikeouts',
            player='Starter A',
            probability=.67,
            implied=.54,
            line=5.5,
        ),
        row(
            'h2h',
            player='NYY@BOS',
            team='NYY',
            probability=.64,
            implied=.54,
            recommendedSide='NYY',
            line=0,
        ),
    ])

    assert [pick['intelligenceCategory'] for pick in result['quickPicks']] == [
        'hitter_hits',
        'pitcher_strikeouts',
        'game_winner',
    ]
    assert all('decisionSummary' in pick for pick in result['quickPicks'])
    assert result['best']['game_winner']['recommendedSide'] == 'NYY'
    assert result['policy']['rankingPriority'] == 'confidenceScore'


def test_selects_highest_confidence_hit_instead_of_first_input():
    result = build_game_card_quick_picks([
        row(
            'batter_hits',
            player='Lower',
            probability=.60,
            implied=.55,
            mc_std=.12,
            p_lo=.45,
            p_hi=.75,
        ),
        row(
            'batter_hits',
            player='Higher',
            probability=.70,
            implied=.56,
            mc_std=.03,
            p_lo=.67,
            p_hi=.73,
        ),
        row('pitcher_strikeouts', player='Starter', probability=.66, implied=.54, line=5.5),
        row('h2h', player='NYY@BOS', team='NYY', probability=.64, implied=.54, recommendedSide='NYY', line=0),
    ])

    assert result['best']['hitter_hits']['player'] == 'Higher'


def test_chooses_strikeout_under_when_it_has_the_stronger_qualified_case():
    result = build_game_card_quick_picks([
        row(
            'pitcher_strikeouts',
            player='Starter',
            probability=.41,
            implied=.52,
            line=6.5,
            bestUnderPrice=-105,
        ),
    ])

    strikeouts = result['best']['pitcher_strikeouts']
    assert strikeouts['recommendedSide'] == 'Under'
    assert strikeouts['adjProb'] == .59
    assert strikeouts['recommendationGrade'] != 'Pass'


def test_weak_or_missing_category_is_an_explicit_pass():
    result = build_game_card_quick_picks([
        row('batter_hits', player='Weak', probability=.52, implied=.51),
    ])

    hit = result['best']['hitter_hits']
    assert hit['recommendationGrade'] == 'Pass'
    assert 'probability below threshold' in hit['topRisks']
    assert result['best']['pitcher_strikeouts']['recommendationGrade'] == 'Pass'
    assert result['best']['game_winner']['recommendationGrade'] == 'Pass'
    assert result['passCategoryCount'] == 3


def test_game_card_api_returns_the_three_ranked_decisions(monkeypatch):
    class FakeApp:
        def __init__(self):
            self.view_functions = {}

        def route(self, _path, methods=None):
            del methods

            def register(function):
                self.view_functions[function.__name__] = function
                return function

            return register

    rows = [
        row('batter_hits', player='Hitter', probability=.69, implied=.56),
        row('pitcher_strikeouts', player='Starter', probability=.42, implied=.54, line=6.5),
        row('h2h', player='NYY@BOS', team='NYY', probability=.64, implied=.54, recommendedSide='NYY', line=0),
    ]
    fake_app = FakeApp()
    app_module = SimpleNamespace(
        app=fake_app,
        request=SimpleNamespace(args={'date': '2026-08-06', 'refresh': '1'}),
        jsonify=lambda payload: payload,
        ET=timezone.utc,
        _tracker_today_payload=lambda _date: {'date': '2026-08-06', 'entries': rows},
    )
    monkeypatch.setattr(intelligence_integration, 'enrich_context', lambda values: values)
    monkeypatch.setattr(intelligence_integration, 'enrich_matchups', lambda values: values)
    monkeypatch.setattr(intelligence_integration, 'enrich_simulations', lambda values: values)
    monkeypatch.setattr(intelligence_integration, 'analyze_learning', lambda _values: {})

    intelligence_integration.install_intelligence_api(app_module)
    payload = fake_app.view_functions['api_intelligence_game_card'](7)

    assert payload['success'] is True
    assert payload['quickPicksVersion'] == '4.33'
    assert [pick['intelligenceCategory'] for pick in payload['quickPicks']] == [
        'hitter_hits',
        'pitcher_strikeouts',
        'game_winner',
    ]
    assert payload['best']['pitcher_strikeouts']['recommendedSide'] == 'Under'


def test_game_card_api_falls_back_when_live_generation_fails(monkeypatch):
    class FakeApp:
        def __init__(self):
            self.view_functions = {}

        def route(self, _path, methods=None):
            del methods

            def register(function):
                self.view_functions[function.__name__] = function
                return function

            return register

    def fail_generation(*_args, **_kwargs):
        raise RuntimeError('upstream unavailable')

    rows = [row('batter_hits', player='Captured Hitter', probability=.69, implied=.56)]
    fake_app = FakeApp()
    app_module = SimpleNamespace(
        app=fake_app,
        request=SimpleNamespace(args={'date': '2026-08-06', 'refresh': '1'}),
        jsonify=lambda payload: payload,
        ET=timezone.utc,
        logging=SimpleNamespace(warning=lambda *_args, **_kwargs: None),
        _tracker_today_payload=lambda _date: {'date': '2026-08-06', 'entries': rows},
        fetch_schedule=lambda _date: [],
        _get_adjustments=lambda: {},
        _build_tracker_rows_for_game=fail_generation,
    )
    monkeypatch.setattr(intelligence_integration, 'enrich_context', lambda values: values)
    monkeypatch.setattr(intelligence_integration, 'enrich_matchups', lambda values: values)
    monkeypatch.setattr(intelligence_integration, 'enrich_simulations', lambda values: values)
    monkeypatch.setattr(intelligence_integration, 'analyze_learning', lambda _values: {})

    intelligence_integration.install_intelligence_api(app_module)
    payload = fake_app.view_functions['api_intelligence_game_card'](7)

    assert payload['success'] is True
    assert payload['sourceCount'] == 1
    assert payload['generatedSourceCount'] == 0
    assert payload['best']['hitter_hits']['player'] == 'Captured Hitter'
