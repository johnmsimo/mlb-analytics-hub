import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from flask import Flask

from accuracy_control_plane import build_closing_benchmark_receipt
from closing_line_integrity import accept_closing_capture
from continuous_learning import build_prediction_receipt
from intelligence_control_plane import (
    INTELLIGENCE_CONTROL_PLANE_VERSION,
    INTELLIGENCE_EVIDENCE_VERSION,
    apply_drift_interventions,
    build_intelligence_control_plane,
    build_intelligence_evidence_receipt,
    install_intelligence_control_plane,
    intelligence_evidence_is_intact,
    verified_correlation_pairs,
)
from product_hub import product_hub_bp
from tracker_writer import write_pick


ROOT = Path(__file__).resolve().parents[1]
ANCHOR = date(2026, 8, 21)


def verified_row(
    index: int,
    *,
    market: str = "batter_hits",
    graded_on: date = date(2026, 8, 20),
    outcome: int = 1,
    model_probability: float = 0.65,
    challenger_probability: float = 0.85,
    edge: float = 0.06,
    game_pk: int | None = None,
) -> dict:
    saved_at = datetime.combine(
        graded_on, datetime.min.time(), tzinfo=timezone.utc,
    ) + timedelta(hours=15)
    graded_at = saved_at + timedelta(hours=8)
    first_pitch = saved_at + timedelta(hours=4)
    closing_at = first_pitch - timedelta(minutes=5)
    line = 0.5 if market == "batter_hits" else 5.5
    row = {
        "id": f"phase615-{market}-{index}",
        "savedAt": saved_at.isoformat(),
        "source": "champion-model",
        "modelVersion": "champion-2026.08",
        "gamePk": game_pk if game_pk is not None else 800000 + index,
        "player": f"Player {index}",
        "marketKey": market,
        "line": line,
        "recommendedSide": "Over",
        "adjProb": model_probability,
        "preCalProb": challenger_probability,
        "componentProbabilities": {
            "logistic": challenger_probability,
            "forest": max(0.01, challenger_probability - 0.01),
        },
        "openingPrice": -110,
        "openingImplied": 0.5,
        "book": "Book A",
        "quoteAgeSeconds": 45,
        "oddsProviderState": "ready",
        "lineupStatus": "confirmed",
        "pitcherHand": "R",
        "parkFactor": 1.0,
        "weatherAdjustment": 0.0,
        "umpireKMult": 1.0,
        "confidenceTier": "A",
        "canonicalEdge": edge,
        "canonicalEv": 0.08,
        "hubRating": 82,
        "gameSimProbability": challenger_probability,
        "gameSimN": 10000,
        "gameSimMean": line + (0.8 if outcome else -0.3),
        "mc_p10": max(0, line - 0.5),
        "mc_p90": line + 1.5,
        "matchupSimulationVersion": "6.4-shadow",
        "matchupSimulationMode": "prospective",
    }
    row["learningReceipt"] = build_prediction_receipt(row)
    row["intelligenceEvidenceReceipt"] = build_intelligence_evidence_receipt(row)
    row.update({
        "grade": "win" if outcome else "loss",
        "gradedAt": graded_at.isoformat(),
        "actual": line + 1 if outcome else max(0, line - 1),
        "closingOverPrice": -110,
        "closingUnderPrice": -110,
        "closingLine": line,
        "closingBook": "Book A",
        "closingSource": "the-odds-api-live",
        "closingCapturedAt": closing_at.isoformat(),
        "closingImplied": 110 / 210,
        "clvEdge": 0.03 if outcome else -0.03,
        "clvEligible": True,
        "oddsLineage": {"version": "4.55", "clvEligible": True},
    })
    row["closingIntegrity"] = accept_closing_capture(
        opening={"capturedAt": saved_at.isoformat()},
        closing={
            "capturedAt": closing_at.isoformat(),
            "price": -110,
            "book": "Book A",
            "source": "the-odds-api-live",
        },
        first_pitch=first_pitch.isoformat(),
    )
    row["closingBenchmarkReceipt"] = build_closing_benchmark_receipt(row)
    return row


def test_intelligence_receipt_is_pre_outcome_and_tamper_evident() -> None:
    row = verified_row(1)
    receipt = row["intelligenceEvidenceReceipt"]

    assert receipt["version"] == INTELLIGENCE_EVIDENCE_VERSION == "6.5.0"
    assert receipt["accepted"] is True
    assert receipt["outcomeFieldsIncluded"] is False
    assert receipt["closingFieldsIncluded"] is False
    assert "grade" not in json.dumps(receipt).lower()
    assert "closingoverprice" not in json.dumps(receipt).lower()
    assert intelligence_evidence_is_intact(row) is True

    row["lineupStatus"] = "projected"
    assert intelligence_evidence_is_intact(row) is False


def test_legacy_prediction_receipt_is_never_backfilled_on_upsert(tmp_path) -> None:
    tracker = tmp_path / "daily_tracker.json"
    legacy = {
        "id": "legacy-row",
        "savedAt": "2026-08-20T15:00:00+00:00",
        "learningReceipt": {"version": "5.4.0"},
    }
    write_pick(legacy, date_str="2026-08-20", tracker_path=str(tracker))
    write_pick(
        {
            **legacy,
            "intelligenceEvidenceReceipt": {
                "version": "6.5.0", "evidenceFingerprint": "late",
            },
        },
        date_str="2026-08-20",
        tracker_path=str(tracker),
    )

    stored = json.loads(tracker.read_text(encoding="utf-8"))
    assert "intelligenceEvidenceReceipt" not in stored["2026-08-20"]["entries"][0]


def test_error_atlas_suppresses_small_cohorts_then_exposes_aggregates() -> None:
    small = build_intelligence_control_plane(
        [verified_row(index) for index in range(1, 30)],
        as_of=ANCHOR,
    )
    ready = build_intelligence_control_plane(
        [verified_row(index) for index in range(1, 31)],
        as_of=ANCHOR,
    )

    assert small["phases"]["errorAtlas"]["state"] == "insufficient_sample"
    assert small["phases"]["errorAtlas"]["cohorts"] == []
    atlas = ready["phases"]["errorAtlas"]
    assert atlas["state"] == "ready"
    assert atlas["cohorts"]
    assert all(row["sampleSize"] >= 30 for row in atlas["cohorts"])
    assert atlas["rawRowsIncluded"] is False


def test_shadow_challenger_requires_temporal_holdout_and_human_review() -> None:
    report = build_intelligence_control_plane(
        [verified_row(index) for index in range(1, 401)],
        as_of=ANCHOR,
    )["phases"]["championChallenger"]
    challenger = next(
        row for row in report["challengers"]
        if row["challenger"] == "pre_calibration"
    )

    assert challenger["state"] == "review_candidate"
    assert challenger["holdoutSampleSize"] == 120
    assert challenger["challengerMinusChampionInterval"]["upper"] < 0
    assert challenger["challengerMinusCloseInterval"]["upper"] < 0
    assert challenger["temporalHoldout"] is True
    assert challenger["humanReviewRequired"] is True
    assert report["automaticPromotion"] is False


def test_drift_suppresses_a_market_without_changing_probability_or_model() -> None:
    baseline = [
        verified_row(
            index,
            graded_on=date(2026, 7, 10),
            outcome=1,
            model_probability=0.8,
        )
        for index in range(1, 101)
    ]
    recent = [
        verified_row(
            1000 + index,
            graded_on=date(2026, 8, 15),
            outcome=0,
            model_probability=0.8,
        )
        for index in range(1, 31)
    ]
    control = build_intelligence_control_plane(baseline + recent, as_of=ANCHOR)
    market = control["phases"]["driftControl"]["markets"]["batter_hits"]

    assert market["state"] == "suppressed"
    assert market["recommendedAction"] == "no_bet"
    assert market["featureDrift"]["maximumTotalVariation"] is not None
    assert market["providerHealth"]["recentReadyRate"] == 1.0
    intervention = apply_drift_interventions(
        [{
            "canonicalCandidateId": "candidate-1",
            "canonicalMarketKey": "batter_hits",
            "canonicalProbability": 0.8,
            "actionable": True,
        }],
        control,
    )
    assert intervention["promoted"] == []
    assert intervention["rejected"][0]["phase6DecisionState"] == "no_bet"
    assert intervention["rejected"][0]["canonicalProbability"] == 0.8
    assert intervention["audit"]["probabilitiesChanged"] is False
    assert intervention["audit"]["modelsChanged"] is False


def test_simulation_and_same_game_correlation_require_verified_samples() -> None:
    rows = []
    for game in range(1, 51):
        batter = verified_row(
            game * 2, game_pk=900000 + game, challenger_probability=0.95,
        )
        pitcher = verified_row(
            game * 2 + 1,
            market="pitcher_strikeouts",
            game_pk=900000 + game,
            challenger_probability=0.95,
        )
        if game <= 10:
            batter["actual"] = batter["line"] + 3
            pitcher["actual"] = pitcher["line"] + 3
        rows.extend((batter, pitcher))
    phase = build_intelligence_control_plane(rows, as_of=ANCHOR)["phases"][
        "simulationCalibration"
    ]
    pairs = verified_correlation_pairs({"phases": {"simulationCalibration": phase}})

    assert phase["simulation"]["sampleSize"] == 100
    assert phase["simulation"]["state"] == "verified"
    assert len(pairs) == 1
    assert pairs[0]["sampleSize"] == 50
    assert pairs[0]["verified"] is True
    assert pairs[0]["factor"] == 1.5
    assert phase["unverifiedCorrelationTrackable"] is False


def test_policy_lab_is_temporal_review_only_and_never_changes_staking() -> None:
    rows = [
        verified_row(
            index,
            outcome=0 if index % 4 == 0 else 1,
            edge=0.08 if index % 4 == 0 else 0.01,
        )
        for index in range(1, 401)
    ]
    policy = build_intelligence_control_plane(rows, as_of=ANCHOR)["phases"]["policyLab"]

    assert policy["proposals"][0]["temporalHoldout"] is True
    assert "proposedMinimumExpectedValue" in policy["proposals"][0]
    assert policy["proposals"][0]["humanReviewRequired"] is True
    assert "prospectively tracked decisions" in policy["proposals"][0]["selectionBiasWarning"]
    assert policy["automaticThresholdChange"] is False
    assert policy["automaticStakingChange"] is False
    assert policy["humanReviewRequired"] is True


def test_api_is_read_only_and_validates_window_and_date(tmp_path) -> None:
    tracker = tmp_path / "daily_tracker.json"
    tracker.write_text('{"2026-08-20": {"entries": []}}', encoding="utf-8")
    flask_app = Flask(__name__)
    install_intelligence_control_plane(
        SimpleNamespace(app=flask_app, TRACKER_STORE=str(tracker))
    )
    client = flask_app.test_client()

    response = client.get("/api/accuracy/intelligence?date=2026-08-21&window=120")
    invalid_window = client.get("/api/accuracy/intelligence?window=nope")
    invalid_date = client.get("/api/accuracy/intelligence?date=08-21-2026")
    mutation = client.post("/api/accuracy/intelligence", json={})

    assert response.status_code == 200
    assert response.headers["X-Intelligence-Contract"] == "6.5"
    assert response.get_json()["state"] == "insufficient_sample"
    assert invalid_window.status_code == 400
    assert invalid_date.status_code == 400
    assert mutation.status_code == 405


def test_product_and_public_surfaces_expose_phase_615_contract() -> None:
    flask_app = Flask(__name__)
    flask_app.register_blueprint(product_hub_bp)
    contract = flask_app.test_client().get("/api/product/journey").get_json()[
        "accuracyIntelligenceProgram"
    ]
    html = (ROOT / "public_verification.html").read_text(encoding="utf-8")
    script = (ROOT / "static" / "public-verification.js").read_text(encoding="utf-8")
    css = (ROOT / "static" / "public-verification.css").read_text(encoding="utf-8")
    wsgi = (ROOT / "wsgi.py").read_text(encoding="utf-8")

    assert contract["version"] == INTELLIGENCE_CONTROL_PLANE_VERSION == "6.5"
    assert contract["phaseVersions"]["errorAtlas"] == "6.1"
    assert contract["phaseVersions"]["policyLab"] == "6.5"
    assert contract["automaticModelPromotion"] is False
    assert contract["automaticThresholdChange"] is False
    assert 'id="intelligenceProgram"' in html
    assert "/api/accuracy/intelligence?window=" in script
    assert "payload.version !== '6.5'" in script
    assert ".intelligence-program" in css
    assert wsgi.count("install_intelligence_control_plane(app_module)") == 1
