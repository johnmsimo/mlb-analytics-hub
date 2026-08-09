from closing_line_integrity import accept_closing_capture


def test_accepts_fresh_live_close_inside_pitch_window():
    result = accept_closing_capture(opening={"capturedAt": "2026-08-09T15:00:00Z"}, closing={"capturedAt": "2026-08-09T18:52:00Z", "price": -115, "book": "book", "source": "odds_api_live"}, first_pitch="2026-08-09T19:00:00Z")
    assert result["accepted"] is True
    assert result["source"] == "odds_api_live"


def test_rejects_snapshot_close_that_is_not_newer_than_opening():
    result = accept_closing_capture(opening={"capturedAt": "2026-08-09T19:00:00Z"}, closing={"capturedAt": "2026-08-09T19:00:00Z", "price": -115, "book": "book"}, first_pitch="2026-08-09T19:05:00Z")
    assert result == {"accepted": False, "reason": "not_newer_than_opening", "capturedAt": "2026-08-09T19:00:00Z", "source": "odds_api_live", "fresh": False}
