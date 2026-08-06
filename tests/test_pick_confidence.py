from pick_confidence import enrich_pick_score, score_pick_confidence


def pick(**overrides):
    row = {
        'intelligenceCategory': 'pitcher_strikeouts',
        'adjProb': .59,
        'marketImplied': .53,
        'edge': .06,
        'confidenceScore': 68,
        'contextScore': 62,
        'matchupScore': 70,
        'simulationScore': 82,
    }
    row.update(overrides)
    return row


def test_pick_score_separates_probability_reliability_and_edge():
    result = score_pick_confidence(pick())

    assert result['modelProbabilityPct'] == 59.0
    assert result['estimatedEdgePct'] == 6.0
    assert result['modelReliabilityScore'] == 68.0
    assert result['pickScore'] != result['modelProbabilityPct']
    assert result['pickScore'] != result['modelReliabilityScore']
    assert set(result['pickScoreBreakdown']) == {
        'winProbability', 'priceValue', 'modelReliability', 'gameContext',
        'matchup', 'simulation', 'learningCalibration',
    }


def test_stronger_probability_edge_and_reliability_raise_pick_score():
    weak = score_pick_confidence(pick(
        adjProb=.53, marketImplied=.52, edge=.01, confidenceScore=40,
    ))
    strong = score_pick_confidence(pick(
        adjProb=.66, marketImplied=.54, edge=.12, confidenceScore=84,
    ))

    assert strong['pickScore'] > weak['pickScore']
    assert strong['pickScoreTier'] in {'VALUE', 'STRONG'}


def test_missing_price_is_a_visible_risk_not_fake_edge():
    result = score_pick_confidence(pick(edge=None, marketImplied=None))

    assert result['estimatedEdgePct'] is None
    assert result['marketImpliedProbabilityPct'] is None
    assert result['pickScoreBreakdown']['priceValue']['score'] == 0.0
    assert 'live price is unavailable' in result['pickScoreRisks'][0]


def test_learning_only_contributes_with_meaningful_sample():
    learning = {
        'byMarket': {
            'pitcher_strikeouts': {
                'count': 30,
                'calibrationError': .04,
            }
        }
    }
    result = score_pick_confidence(pick(), learning=learning)

    assert result['pickScoreBreakdown']['learningCalibration']['score'] == 92.0
    assert any(
        item['factor'] == 'learningCalibration'
        for item in result['pickScoreEvidence']
    )


def test_enrichment_does_not_mutate_source():
    source = pick()
    result = enrich_pick_score(source)

    assert 'pickScore' not in source
    assert 'pickScore' in result
