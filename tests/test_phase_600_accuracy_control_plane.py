from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask

from accuracy_control_plane import (
    ACCURACY_CONTROL_PLANE_VERSION,
    CLOSING_BENCHMARK_VERSION,
    build_accuracy_control_plane,
    build_closing_benchmark_receipt,
    closing_benchmark_is_intact,
    install_accuracy_control_plane,
)
from closing_line_integrity import accept_closing_capture
from continuous_learning import build_prediction_receipt
from product_hub import product_hub_bp
from tracker_writer import build_pick_payload


ROOT = Path(__file__).resolve().parents[1]


def verified_row(index: int = 1, *, side: str = "Over") -> dict:
    row = {
        "id": f"prediction-{index}",
        "savedAt": "2026-08-20T15:00:00+00:00",
        "gradedAt": "2026-08-20T23:00:00+00:00",
        "source": "xgb",
        "gamePk": 700000 + index,
        "player": f"Player {index}",
        "marketKey": "batter_hits",
        "line": 0.5,
        "recommendedSide": side,
        "adjProb": 0.8 if side == "Over" else 0.2,
        "openingPrice": -110,
        "openingImplied": 0.5,
        "book": "Book A",
        "grade": "win",
        "closingOverPrice": -130,
        "closingUnderPrice": 110,
        "closingLine": 0.5,
        "closingBook": "Book A",
        "closingSource": "the-odds-api-live",
        "closingCapturedAt": "2026-08-20T18:55:00+00:00",
        "clvEdge": 0.02,
        "oddsLineage": {"version": "4.55", "clvEligible": True},
    }
    row["learningReceipt"] = build_prediction_receipt(row)
    row["closingIntegrity"] = accept_closing_capture(
        opening={"capturedAt": "2026-08-20T15:00:00+00:00"},
        closing={
            "capturedAt": row["closingCapturedAt"],
            "price": -130 if side == "Over" else 110,
            "book": "Book A",
            "source": "the-odds-api-live",
        },
        first_pitch="2026-08-20T19:00:00+00:00",
    )
    row["closingBenchmarkReceipt"] = build_closing_benchmark_receipt(row)
    return row


def test_receipt_is_side_correct_two_way_and_outcome_free() -> None:
    over = verified_row(1, side="Over")
    under = verified_row(2, side="Under")

    assert over["closingBenchmarkReceipt"]["version"] == CLOSING_BENCHMARK_VERSION
    assert over["closingBenchmarkReceipt"]["accepted"] is True
    assert over["closingBenchmarkReceipt"]["snapshot"]["selectedPrice"] == -130
    assert under["closingBenchmarkReceipt"]["snapshot"]["selectedPrice"] == 110
    assert under["closingBenchmarkReceipt"]["snapshot"]["selectedFairProbability"] < 0.5
    assert over["closingBenchmarkReceipt"]["outcomeFieldsIncluded"] is False
    assert over["closingBenchmarkReceipt"]["modelProbabilityIncluded"] is False
    assert closing_benchmark_is_intact(over) is True


def test_one_sided_or_tampered_close_fails_closed() -> None:
    one_sided = verified_row()
    one_sided["closingUnderPrice"] = None
    receipt = build_closing_benchmark_receipt(one_sided)

    assert receipt["accepted"] is False
    assert "closing benchmark lacks a complete two-way price" in receipt["blockers"]

    missing_line = verified_row()
    missing_line.pop("closingLine")
    line_receipt = build_closing_benchmark_receipt(missing_line)
    assert line_receipt["accepted"] is False
    assert "closing line does not match prediction" in line_receipt["blockers"]

    tampered = verified_row()
    tampered["closingOverPrice"] = -150
    assert closing_benchmark_is_intact(tampered) is False


def test_public_rejection_counts_never_echo_row_controlled_text() -> None:
    tampered = verified_row()
    tampered["closingBenchmarkReceipt"]["benchmarkFingerprint"] = "tampered"
    tampered["closingBenchmarkReceipt"]["blockers"] = ["private note must not leak"]

    payload = build_accuracy_control_plane(
        [tampered], as_of=date(2026, 8, 21), window_days=90,
    )

    reasons = payload["coverage"]["rejectedReasonCounts"]
    assert reasons == {"closing_receipt_tampered": 1}
    assert "private note" not in str(reasons)


def test_scorecard_withholds_claim_below_paired_sample_gate() -> None:
    payload = build_accuracy_control_plane(
        [verified_row(index) for index in range(1, 11)],
        as_of=date(2026, 8, 21),
        window_days=90,
    )

    assert payload["version"] == ACCURACY_CONTROL_PLANE_VERSION == "6.0"
    assert payload["state"] == "insufficient_sample"
    assert payload["industryClaimMade"] is False
    assert payload["overall"]["pairedSampleSize"] == 10
    assert payload["overall"]["modelBrier"] < payload["overall"]["closingMarketBrier"]
    assert payload["coverage"]["rawRowsIncluded"] is False
    assert payload["privateTrackerFieldsIncluded"] is False
    assert payload["automaticModelChange"] is False
    assert payload["serverMutation"] is False


def test_market_leading_state_requires_brier_and_clv_confidence() -> None:
    payload = build_accuracy_control_plane(
        [verified_row(index) for index in range(1, 501)],
        as_of=date(2026, 8, 21),
        window_days=90,
    )

    assert payload["state"] == "market_leading"
    assert payload["industryClaimMade"] is True
    assert payload["overall"]["pairedSampleSize"] == 500
    assert payload["overall"]["brierEvidence"] == "model_better"
    assert payload["overall"]["pairedBrierDeltaInterval"]["upper"] < 0
    assert payload["overall"]["beatCloseRate"] == 1.0
    assert payload["overall"]["beatCloseInterval"]["lower"] > 0.5
    assert payload["byMarket"]["batter_hits"]["claimEligible"] is True


def test_api_is_read_only_and_validates_query_contract(tmp_path) -> None:
    tracker = tmp_path / "daily_tracker.json"
    tracker.write_text('{"2026-08-20": {"entries": []}}', encoding="utf-8")
    flask_app = Flask(__name__)
    app_module = SimpleNamespace(app=flask_app, TRACKER_STORE=str(tracker))
    install_accuracy_control_plane(app_module)
    client = flask_app.test_client()

    response = client.get("/api/accuracy/control-plane?date=2026-08-21&window=90")
    invalid = client.get("/api/accuracy/control-plane?window=nope")
    mutation = client.post("/api/accuracy/control-plane", json={})

    assert response.status_code == 200
    assert response.headers["X-Accuracy-Contract"] == "6.0"
    assert response.get_json()["state"] == "insufficient_sample"
    assert invalid.status_code == 400
    assert mutation.status_code == 405


def test_tracker_writer_selects_under_close_and_fair_probability() -> None:
    payload = build_pick_payload(
        player="Under Pitcher",
        market_key="pitcher_strikeouts",
        line=6.5,
        side="Under",
        adj_prob=0.42,
        opening_price=-105,
        opening_implied=0.49,
        closing_over_price=-125,
        closing_under_price=105,
        book="Book A",
        first_pitch="2026-08-20T19:00:00+00:00",
        opening_captured_at="2026-08-20T15:00:00+00:00",
        closing_captured_at="2026-08-20T18:55:00+00:00",
        closing_source="the-odds-api-live",
        closing_book="Book A",
        date_str="2026-08-20",
    )

    receipt = payload["closingBenchmarkReceipt"]
    assert receipt["accepted"] is True
    assert receipt["snapshot"]["side"] == "under"
    assert receipt["snapshot"]["selectedPrice"] == 105
    assert payload["closingPrice"] == 105
    assert payload["closingFairProbability"] == receipt["snapshot"]["selectedFairProbability"]
    assert payload["closingImplied"] == pytest.approx(100 / 205)
    assert payload["closingImplied"] < 0.5


def test_product_journey_exposes_read_only_phase_600_contract() -> None:
    flask_app = Flask(__name__)
    flask_app.register_blueprint(product_hub_bp)

    contract = flask_app.test_client().get("/api/product/journey").get_json()[
        "accuracyControlPlane"
    ]

    assert contract["version"] == "6.0"
    assert contract["sourceEndpoint"] == "/api/accuracy/control-plane?window=90"
    assert contract["minimumPairedSample"] == 500
    assert contract["industryClaimDefaultsToFalse"] is True
    assert contract["automaticModelChange"] is False
    assert contract["serverMutation"] is False
    assert contract["failClosed"] is True


def test_public_scorecard_and_wsgi_install_phase_600_once() -> None:
    html = (ROOT / "public_verification.html").read_text(encoding="utf-8")
    script = (ROOT / "static" / "public-verification.js").read_text(encoding="utf-8")
    css = (ROOT / "static" / "public-verification.css").read_text(encoding="utf-8")
    wsgi = (ROOT / "wsgi.py").read_text(encoding="utf-8")

    assert 'id="accuracyControlPlane"' in html
    assert 'id="accuracyGateState"' in html
    assert 'id="accuracyClaim"' in html
    assert "/api/accuracy/control-plane?window=" in script
    assert "payload.version !== '6.0'" in script
    assert "renderAccuracyUnavailable" in script
    assert ".accuracy-plane" in css
    assert "min-height:44px" in css
    assert wsgi.count("install_accuracy_control_plane(app_module)") == 1


def test_tracker_close_path_captures_both_sides_and_selected_fair_close() -> None:
    source = (ROOT / "app.py").read_text(encoding="utf-8")

    assert "row['closingOverPrice'] = closing_over" in source
    assert "row['closingUnderPrice'] = closing_under" in source
    assert "row['closingBenchmarkReceipt'] = benchmark" in source
    assert "snapshot.get('selectedFairProbability')" in source
    assert "build_odds_lineage(" in source
    assert "build_prediction_receipt(prepared)" in source
