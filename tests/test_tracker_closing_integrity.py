from tracker_writer import build_pick_payload


def _base(**overrides):
    values = {
        "player": "Test Player",
        "market_key": "batter_hits",
        "line": 0.5,
        "adj_prob": 0.62,
        "opening_price": -120,
        "closing_price": -135,
        "opening_implied": 0.52,
        "closing_implied": 0.5745,
        "book": "DraftKings",
        "first_pitch": "2026-08-09T19:00:00Z",
        "opening_captured_at": "2026-08-09T15:00:00Z",
        "closing_captured_at": "2026-08-09T18:55:00Z",
    }
    values.update(overrides)
    return build_pick_payload(**values)


def test_valid_close_is_attached_to_tracker_payload():
    payload = _base()
    assert payload["closingIntegrityAccepted"] is True
    assert payload["closingIntegrity"]["source"] == "odds_api_live"
    assert payload["closingIntegrity"]["fresh"] is True
    assert payload["clvEdge"] == 0.0545


def test_invalid_close_cannot_publish_clv_edge():
    payload = _base(closing_captured_at="2026-08-09T19:10:00Z")
    assert payload["closingIntegrityAccepted"] is False
    assert payload["closingIntegrity"]["reason"] == "outside_first_pitch_window"
    assert payload["clvEdge"] is None


def test_legacy_payload_without_close_metadata_stays_compatible():
    payload = build_pick_payload(
        player="Legacy Player",
        market_key="batter_hits",
        line=0.5,
        adj_prob=0.60,
        opening_implied=0.52,
    )
    assert payload["closingIntegrity"] is None
    assert payload["closingIntegrityAccepted"] is None
    assert payload["clvEdge"] is None


def test_partial_close_is_rejected_with_a_reason():
    payload = _base(closing_captured_at=None)
    assert payload["closingIntegrityAccepted"] is False
    assert payload["closingIntegrity"]["reason"] == "missing_capture_time"
    assert payload["clvEdge"] is None
