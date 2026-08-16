import json
from pathlib import Path

from accuracy_baseline import build_accuracy_baseline


ROOT = Path(__file__).resolve().parents[1]


def fixture():
    from model_operations import FEATURE_ALIASES, MARKET_CONTRACTS

    metrics = {"models": {}}
    regen = {}
    features = {}
    champions = {"version": "4.86.0", "frozen_from_main_sha": "a" * 40, "models": {}}
    blobs = {}
    calibration = {"markets": {}}
    for key, contract in MARKET_CONTRACTS.items():
        metrics["models"][key] = {
            "tracker_market": contract["tracker_market"],
            "test_auc": 0.70,
            "train_auc": 0.74,
            "test_brier": 0.20,
            "baserate_brier": 0.24,
            "test_logloss": 0.58,
            "n_train": 1000,
            "n_test": 300,
        }
        regen[key] = {
            "target": contract["target"],
            "line": contract["line"],
            "train_seasons": [2021, 2022, 2023, 2024],
            "test_season": 2025,
            "calibration": "isotonic_cv4",
            "model_type": "CalibratedClassifierCV(XGBClassifier)",
        }
        values = ["feature_a", "feature_b"]
        for alias in FEATURE_ALIASES[key]:
            features[alias] = values
        champions["models"][key] = {
            "artifact_ref": f"models/{key}.pkl",
            "git_blob_sha": "b" * 40,
        }
        blobs[key] = "b" * 40
    for market in {item["tracker_market"] for item in MARKET_CONTRACTS.values()}:
        calibration["markets"][market] = [[0.6, 1], [0.4, 0]] * 250
    return metrics, regen, features, champions, calibration, blobs


def test_complete_temporal_baseline_is_ready_for_phase_five():
    metrics, regen, features, champions, calibration, blobs = fixture()
    report = build_accuracy_baseline(
        metrics,
        regen,
        features,
        champions,
        calibration,
        actual_artifact_blobs=blobs,
        source_blobs={"metrics": "c" * 40},
    )
    assert report["status"] == "passed"
    assert report["coverage"] == {
        "expectedModels": 14,
        "baselineModels": 14,
        "expectedTrackerMarkets": 5,
        "calibrationMarkets": 5,
    }
    assert report["phase5Handoff"]["ready"] is True
    assert len(report["weaknessRanking"]) == 14


def test_non_temporal_split_and_artifact_mismatch_fail_closed():
    metrics, regen, features, champions, calibration, blobs = fixture()
    regen["hits"]["test_season"] = 2024
    blobs["hits"] = "d" * 40
    report = build_accuracy_baseline(
        metrics,
        regen,
        features,
        champions,
        calibration,
        actual_artifact_blobs=blobs,
        source_blobs={},
    )
    assert report["status"] == "failed"
    assert report["phase5Handoff"]["ready"] is False
    assert any("strictly temporal" in error for error in report["errors"])
    assert any("artifact identity" in error for error in report["errors"])


def test_target_leakage_and_serve_alias_mismatch_fail_closed():
    metrics, regen, features, champions, calibration, blobs = fixture()
    features["hits"] = ["feature_a", "hit_over_0.5"]
    report = build_accuracy_baseline(
        metrics,
        regen,
        features,
        champions,
        calibration,
        actual_artifact_blobs=blobs,
        source_blobs={},
    )
    assert report["status"] == "failed"
    assert any("target leakage" in error for error in report["errors"])
    assert any("serve feature alias" in error for error in report["errors"])


def test_calibration_sample_is_bounded_and_machine_readable():
    metrics, regen, features, champions, calibration, blobs = fixture()
    report = build_accuracy_baseline(
        metrics,
        regen,
        features,
        champions,
        calibration,
        actual_artifact_blobs=blobs,
        source_blobs={},
    )
    market = report["marketBaselines"]["batter_hits"]["currentSeasonCalibration"]
    assert market["sampleSize"] == 500
    assert market["sampleReady"] is True
    assert 0 <= market["ece"] <= 1


def test_clv_and_roi_cannot_promote_a_model():
    metrics, regen, features, champions, calibration, blobs = fixture()
    report = build_accuracy_baseline(
        metrics,
        regen,
        features,
        champions,
        calibration,
        actual_artifact_blobs=blobs,
        source_blobs={},
    )
    assert report["observationalMetrics"]["clv"]["minimumValidObservations"] == 500
    assert report["observationalMetrics"]["clv"]["promotionMetric"] is False
    assert report["observationalMetrics"]["roi"]["promotionMetric"] is False
    assert report["phase5Handoff"]["promotionContract"]["automaticPromotion"] is False


def test_committed_baseline_is_complete_and_phase_five_ready():
    report = json.loads((ROOT / "data" / "accuracy_baseline.json").read_text(encoding="utf-8"))
    assert report["version"] == "4.86.0"
    assert report["status"] == "passed"
    assert report["coverage"] == {
        "baselineModels": 14,
        "calibrationMarkets": 5,
        "expectedModels": 14,
        "expectedTrackerMarkets": 5,
    }
    assert report["phase5Handoff"]["ready"] is True
    assert report["phase5Handoff"]["priorityModels"][:2] == ["rbi_1.5", "rbi"]
    assert report["errors"] == []


def test_champions_are_frozen_to_the_deployed_phase_485_merge():
    manifest = json.loads((ROOT / "data" / "champion_manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == "4.86.0"
    assert manifest["frozen_from_main_sha"] == "f43e007c5e09893d01f17e896753ca532de8f9ae"
    assert len(manifest["models"]) == 14
    for record in manifest["models"].values():
        assert record["artifact_ref"].startswith("models/")
        assert record["artifact_ref"].endswith(".pkl")
        assert len(record["git_blob_sha"]) == 40


def test_quality_and_weekly_workflows_enforce_the_baseline():
    quality = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
    weekly = (ROOT / ".github" / "workflows" / "model-regen.yml").read_text(encoding="utf-8")
    assert "python scripts/accuracy_baseline.py --check" in quality
    assert "python scripts/accuracy_baseline.py --refresh-champions" in weekly
    assert "--source-sha \"$GITHUB_SHA\"" in weekly
    assert "data/champion_manifest.json" in weekly
    assert "data/accuracy_baseline.json" in weekly


def test_phase_486_closes_four_x_and_hands_off_to_phase_five():
    roadmap = (ROOT / "docs" / "MLB_ANALYTICS_HUB_ROADMAP.md").read_text(encoding="utf-8")
    handoff = (ROOT / "docs" / "accuracy_intelligence_handoff.md").read_text(encoding="utf-8")
    html = (ROOT / "product_hub.html").read_text(encoding="utf-8")
    assert "Phase 4.86 is the active phase." in roadmap
    assert "### Phase 4.86 — Accuracy baseline and reliability closeout" in roadmap
    assert "Phase 5 — Accuracy and Intelligence" in handoff
    assert "FEATURE 4.86" in html


def test_phase_five_promotion_contract_stays_review_gated():
    report = json.loads((ROOT / "data" / "accuracy_baseline.json").read_text(encoding="utf-8"))
    contract = report["phase5Handoff"]["promotionContract"]
    assert contract["heldOutBrierMustImprove"] is True
    assert contract["heldOutLoglossMustNotRegress"] is True
    assert contract["heldOutAucMustNotRegress"] is True
    assert contract["marketEceMustNotRegress"] is True
    assert contract["automaticPromotion"] is False
