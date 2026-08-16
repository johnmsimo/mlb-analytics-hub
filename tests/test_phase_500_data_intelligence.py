import json
from pathlib import Path

from data_intelligence import build_data_intelligence_report


ROOT = Path(__file__).resolve().parents[1]


def fixture():
    baseline = {
        "version": "4.86.0",
        "status": "passed",
        "phase5Handoff": {
            "ready": True,
            "promotionContract": {
                "heldOutBrierMustImprove": True,
                "heldOutLoglossMustNotRegress": True,
                "heldOutAucMustNotRegress": True,
                "marketEceMustNotRegress": True,
            },
        },
        "modelBaselines": {
            "rbi": {"featureCount": 2, "trackerMarket": "batter_rbis"},
        },
    }
    features = {"rbi": ["skill", "lineup"]}
    manifest = {
        "version": "5.0.0",
        "sourceContracts": [{
            "id": "source",
            "historicalPath": "history",
            "livePath": "serve",
            "historicallyReconstructable": True,
            "liveServeAvailable": True,
            "freshnessMinutes": 15,
            "leakageMode": "pregame_only",
            "features": ["skill", "lineup"],
        }],
        "candidateSignals": [{
            "id": "context",
            "phase": "5.1",
            "researchPriority": 1,
            "targetModels": ["rbi"],
            "historicalPath": "history",
            "livePath": "serve",
            "modelEligible": True,
            "historicallyReconstructable": True,
            "liveServeAvailable": True,
            "freshnessMinutes": 15,
            "leakageMode": "pregame_only",
        }],
    }
    return baseline, features, manifest


def test_complete_source_governance_admits_phase_51_experiment():
    baseline, features, manifest = fixture()
    report = build_data_intelligence_report(baseline, features, manifest)
    assert report["status"] == "passed"
    assert report["coverage"]["governedServeFeatures"] == 2
    assert report["phase51Admission"]["ready"] is True
    assert report["phase51Admission"]["admittedSignals"] == ["context"]
    assert report["phase51Admission"]["changesProductionProbabilities"] is False


def test_ungoverned_champion_feature_fails_closed():
    baseline, features, manifest = fixture()
    features["rbi"].append("unknown")
    baseline["modelBaselines"]["rbi"]["featureCount"] = 3
    report = build_data_intelligence_report(baseline, features, manifest)
    assert report["status"] == "failed"
    assert any("ungoverned serve features" in error for error in report["errors"])


def test_candidate_missing_history_and_live_path_is_blocked_not_promoted():
    baseline, features, manifest = fixture()
    candidate = manifest["candidateSignals"][0]
    candidate["historicallyReconstructable"] = False
    candidate["liveServeAvailable"] = False
    report = build_data_intelligence_report(baseline, features, manifest)
    item = report["phase51Admission"]["queue"][0]
    assert item["admitted"] is False
    assert item["blockers"] == ["historical_reconstruction", "live_serve_path"]
    assert report["promotionSafety"]["automaticPromotion"] is False


def test_invalid_freshness_or_leakage_policy_fails_current_source_contract():
    baseline, features, manifest = fixture()
    source = manifest["sourceContracts"][0]
    source["freshnessMinutes"] = 0
    source["leakageMode"] = "postgame"
    report = build_data_intelligence_report(baseline, features, manifest)
    assert report["status"] == "failed"
    assert any("current source is not admissible" in error for error in report["errors"])


def test_committed_report_governs_all_production_features():
    report = json.loads((ROOT / "data/data_intelligence_report.json").read_text(encoding="utf-8"))
    assert report["version"] == "5.0.0"
    assert report["status"] == "passed"
    assert report["coverage"]["championModels"] == 14
    assert report["coverage"]["trackerMarkets"] == 5
    assert report["coverage"]["uniqueServeFeatures"] == 50
    assert report["coverage"]["governedServeFeatures"] == 50
    assert report["phase51Admission"]["admittedSignals"] == [
        "rbi_opportunity_context",
        "pitch_mix_contact_matchup",
    ]


def test_quality_and_weekly_regeneration_enforce_data_intelligence():
    quality = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    weekly = (ROOT / ".github/workflows/model-regen.yml").read_text(encoding="utf-8")
    assert "python scripts/data_intelligence.py --check" in quality
    assert "python scripts/data_intelligence.py" in weekly
    assert "data/data_intelligence_report.json" in weekly


def test_roadmap_opens_phase_five_without_changing_live_predictions():
    roadmap = (ROOT / "docs/MLB_ANALYTICS_HUB_ROADMAP.md").read_text(encoding="utf-8")
    docs = (ROOT / "docs/data_intelligence.md").read_text(encoding="utf-8")
    assert "Phase 4.86 is the final 4.x bridge." in roadmap
    assert "### Phase 5.0 — Data intelligence foundation" in roadmap
    assert "does not change production probabilities" in docs
    assert "automatic promotion remains disabled" in docs
