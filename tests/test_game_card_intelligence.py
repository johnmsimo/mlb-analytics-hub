from datetime import timezone
from types import SimpleNamespace

import intelligence_integration
from game_card_intelligence import (
    build_game_card_quick_picks,
    prepare_game_card_candidates,
)
from intelligence_core import classify_pick
from matchup_simulation_intelligence import build_simulation_signal


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
        'intelligenceCandidatePoolComplete': True,
    }
    data.update(build_simulation_signal(
        probability,
        1500,
        mode='linked_test_game_simulation',
        matchup=f'{player} versus opponent',
    ))
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
    assert result['policy']['rankingPriority'] == 'pickScore'
    assert all('modelProbabilityPct' in pick for pick in result['quickPicks'])
    assert all('modelReliabilityScore' in pick for pick in result['quickPicks'])
    assert all(pick['sharedSimulationBacked'] for pick in result['quickPicks'])


def test_moneyline_selection_compares_both_simulated_sides_by_value():
    result = build_game_card_quick_picks([
        row(
            'h2h', player='NYY@BOS', team='NYY', probability=.56,
            implied=.58, recommendedSide='NYY', line=0,
        ),
        row(
            'h2h', player='NYY@BOS', team='BOS', probability=.44,
            implied=.38, recommendedSide='BOS', line=0,
        ),
    ])

    assert result['best']['game_winner']['team'] == 'BOS'
    assert result['best']['game_winner']['recommendedSide'] == 'BOS'
    assert result['best']['game_winner']['estimatedEdgePct'] == 6.0


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
        row('batter_hits', player='Weak', probability=.52, implied=.53),
    ])

    hit = result['best']['hitter_hits']
    assert hit['recommendationGrade'] == 'Pass'
    assert 'probability below threshold' in hit['topRisks']
    assert result['best']['pitcher_strikeouts']['recommendationGrade'] == 'Pass'
    assert result['best']['game_winner']['recommendationGrade'] == 'Pass'
    assert result['passCategoryCount'] == 3
    assert result['quickPicks'] == []
    assert len(result['marketDecisions']) == 3
    assert len(result['unavailableMarkets']) == 3


def test_positive_edge_best_available_candidate_becomes_actionable_lean():
    result = build_game_card_quick_picks([
        row('batter_hits', player='Available Hitter', probability=.54, implied=.53),
        row(
            'pitcher_strikeouts',
            player='Available Starter',
            probability=.46,
            implied=.47,
            line=5.5,
        ),
        row(
            'h2h',
            player='NYY@BOS',
            team='NYY',
            probability=.53,
            implied=.52,
            recommendedSide='NYY',
            line=0,
        ),
    ])

    assert all(
        pick['recommendationGrade'] == 'Lean'
        for pick in result['quickPicks']
    )
    assert all(pick['isActionable'] for pick in result['quickPicks'])
    assert all(
        pick['selectionMode'] == 'best_available'
        for pick in result['quickPicks']
    )
    assert result['eligibleCategoryCount'] == 3
    assert result['qualifiedCategoryCount'] == 0
    assert result['bestAvailableCategoryCount'] == 3
    assert result['passCategoryCount'] == 0
    assert [pick['overallRank'] for pick in result['quickPicks']] == [1, 2, 3]
    assert [pick['pickScore'] for pick in result['quickPicks']] == sorted(
        (pick['pickScore'] for pick in result['quickPicks']), reverse=True
    )
    assert 'Pick Score' in result['best']['hitter_hits']['decisionSummary']
    assert result['best']['hitter_hits']['standardThresholdMisses']


def test_non_positive_edge_remains_a_pass_instead_of_forcing_a_pick():
    result = build_game_card_quick_picks([
        row('batter_hits', player='No Edge', probability=.54, implied=.55),
    ])

    pick = result['best']['hitter_hits']
    assert pick['recommendationGrade'] == 'Pass'
    assert pick['selectionMode'] == 'pass'
    assert pick['isActionable'] is False


def test_thin_but_positive_edge_is_an_actionable_price_sensitive_lean():
    result = build_game_card_quick_picks([
        row('batter_hits', player='Thin Edge', probability=.58, implied=.579),
    ])

    pick = result['best']['hitter_hits']
    assert pick['recommendationGrade'] == 'Lean'
    assert pick['selectionMode'] == 'best_available'
    assert pick['isActionable'] is True
    assert pick['estimatedEdgePct'] == .1
    assert 'price-sensitive' in pick['pickScoreRisks'][0]


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
        row('h2h', player='NYY@BOS', team='BOS', probability=.36, implied=.46, recommendedSide='BOS', line=0),
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
    monkeypatch.setattr(intelligence_integration, '_read_cached_payload', lambda *_args: None)
    monkeypatch.setattr(intelligence_integration, '_write_cached_payload', lambda *_args: None)
    scheduled = []
    monkeypatch.setattr(
        intelligence_integration,
        '_schedule_game_card_refresh',
        lambda *_args: scheduled.append(True) or {'status': 'queued'},
    )

    intelligence_integration.install_intelligence_api(app_module)
    payload = fake_app.view_functions['api_intelligence_game_card'](7)

    assert payload['success'] is True
    assert payload['quickPicksVersion'] == '4.35.1'
    assert payload['pickConfidenceVersion'] == '4.34'
    assert payload['matchupSimulationVersion'] == '4.35'
    assert payload['recommendationSource'] == 'shared_game_matchup_simulation'
    assert payload['simulationReady'] is True
    assert payload['simulationAudit']['simulationCoveragePct'] == 100.0
    assert payload['computing'] is True
    assert scheduled == [True]
    assert {pick['intelligenceCategory'] for pick in payload['marketDecisions']} == {
        'hitter_hits', 'pitcher_strikeouts', 'game_winner',
    }
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

    generation_calls = []

    def fail_generation(*_args, **_kwargs):
        generation_calls.append(True)
        raise RuntimeError('upstream unavailable')

    captured = row(
        'batter_hits', player='Captured Hitter', probability=.69, implied=.56
    )
    captured['sharedSimulationBacked'] = False
    captured['intelligenceCandidatePoolComplete'] = False
    rows = [captured]
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
    monkeypatch.setattr(intelligence_integration, '_read_cached_payload', lambda *_args: None)
    scheduled = []
    monkeypatch.setattr(
        intelligence_integration,
        '_schedule_game_card_refresh',
        lambda *_args: scheduled.append(True) or {'status': 'queued'},
    )

    intelligence_integration.install_intelligence_api(app_module)
    payload = fake_app.view_functions['api_intelligence_game_card'](7)

    assert payload['success'] is True
    assert payload['sourceCount'] == 1
    assert payload['generatedSourceCount'] == 0
    assert payload['simulationReady'] is False
    assert payload['recommendationSource'] == 'simulation_refresh_pending'
    assert payload['quickPicks'] == []
    assert payload['best']['hitter_hits']['recommendationGrade'] == 'Pass'
    assert payload['computing'] is True
    assert generation_calls == []
    assert scheduled == [True]


def test_game_card_api_rebuilds_an_incomplete_tracker_candidate_pool(monkeypatch):
    class FakeApp:
        def __init__(self):
            self.view_functions = {}

        def route(self, _path, methods=None):
            del methods

            def register(function):
                self.view_functions[function.__name__] = function
                return function

            return register

    captured = row('batter_hits', player='Partial Hitter')
    captured['intelligenceCandidatePoolComplete'] = False
    generated = [
        row('batter_hits', player='Full Pool Hitter', probability=.70),
        row('pitcher_strikeouts', player='Full Pool Starter', probability=.62, line=5.5),
        row('h2h', player='NYY@BOS', team='NYY', probability=.61, implied=.53, recommendedSide='NYY', line=0),
        row('h2h', player='NYY@BOS', team='BOS', probability=.39, implied=.47, recommendedSide='BOS', line=0),
    ]
    calls = []
    fake_app = FakeApp()
    app_module = SimpleNamespace(
        app=fake_app,
        request=SimpleNamespace(args={'date': '2026-08-06', 'refresh': '1'}),
        jsonify=lambda payload: payload,
        ET=timezone.utc,
        logging=SimpleNamespace(warning=lambda *_args, **_kwargs: None),
        _tracker_today_payload=lambda _date: {
            'date': '2026-08-06', 'entries': [captured]
        },
        fetch_schedule=lambda _date: [{'gamePk': 7}],
        _get_adjustments=lambda: {},
        _build_tracker_rows_for_game=lambda *_args, **_kwargs: (
            calls.append(True) or generated
        ),
    )
    monkeypatch.setattr(intelligence_integration, 'enrich_context', lambda values: values)
    monkeypatch.setattr(intelligence_integration, 'enrich_matchups', lambda values: values)
    monkeypatch.setattr(intelligence_integration, 'enrich_simulations', lambda values: values)
    monkeypatch.setattr(intelligence_integration, 'analyze_learning', lambda _values: {})
    monkeypatch.setattr(intelligence_integration, '_read_cached_payload', lambda *_args: None)
    scheduled = []
    monkeypatch.setattr(
        intelligence_integration,
        '_schedule_game_card_refresh',
        lambda *_args: scheduled.append(True) or {'status': 'queued'},
    )

    intelligence_integration.install_intelligence_api(app_module)
    payload = fake_app.view_functions['api_intelligence_game_card'](7)

    # The HTTP request only enqueues the full build and returns immediately.
    assert calls == []
    assert scheduled == [True]
    assert payload['generatedSourceCount'] == 0
    assert payload['simulationReady'] is False
    assert payload['computing'] is True

    # The same heavy builder still produces the complete simulation snapshot
    # when executed by the serialized background worker.
    rebuilt = intelligence_integration._generate_game_card_payload(
        app_module, 7, '2026-08-06'
    )
    assert calls == [True]
    assert rebuilt['generatedSourceCount'] == 4
    assert rebuilt['best']['hitter_hits']['player'] == 'Full Pool Hitter'
    assert rebuilt['simulationReady'] is True
    assert rebuilt['simulationAudit']['simulationCoveragePct'] == 100.0


def test_game_card_api_serves_stale_snapshot_while_refreshing(monkeypatch):
    class FakeApp:
        def __init__(self):
            self.view_functions = {}

        def route(self, _path, methods=None):
            del methods

            def register(function):
                self.view_functions[function.__name__] = function
                return function

            return register

    cached_payload = {
        'success': True,
        'gamePk': 7,
        'quickPicks': [{'player': 'Cached Hitter'}],
        'simulationReady': True,
    }
    fake_app = FakeApp()
    app_module = SimpleNamespace(
        app=fake_app,
        request=SimpleNamespace(args={'date': '2026-08-06', 'refresh': '1'}),
        jsonify=lambda payload: payload,
        ET=timezone.utc,
    )
    monkeypatch.setattr(
        intelligence_integration,
        '_read_cached_payload',
        lambda *_args: {'timestamp': 1, 'payload': cached_payload},
    )
    scheduled = []
    monkeypatch.setattr(
        intelligence_integration,
        '_schedule_game_card_refresh',
        lambda *_args: scheduled.append(True) or {'status': 'queued'},
    )

    intelligence_integration.install_intelligence_api(app_module)
    payload = fake_app.view_functions['api_intelligence_game_card'](7)

    assert payload['quickPicks'][0]['player'] == 'Cached Hitter'
    assert payload['stale'] is True
    assert payload['computing'] is True
    assert payload['retryAfterSeconds'] == 4
    assert scheduled == [True]
