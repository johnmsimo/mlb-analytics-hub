from model_operations import ModelRegistry, compare_challenger, evaluate_candidate


FEATURES = ["x", "y"]
FEATURE_MAP = {"k_4.5": FEATURES, "k_over_4.5": FEATURES}


def metadata(**overrides):
    value = {
        "target": "k_over_4.5",
        "line": 4.5,
        "tracker_market": "pitcher_strikeouts",
        "test_auc": 0.71,
        "test_brier": 0.21,
        "baserate_brier": 0.24,
        "n_test": 400,
        "calibration": "isotonic_cv4",
        "model_type": "CalibratedClassifierCV(XGBClassifier)",
    }
    value.update(overrides)
    return value


def test_candidate_requires_all_four_production_gates():
    result = evaluate_candidate("k_4.5", metadata(), candidate_features=FEATURES, serve_feature_map=FEATURE_MAP)
    assert result.passed is True
    assert {gate.name for gate in result.gates} == {
        "held_out", "calibration", "serve_parity", "market_validation"
    }


def test_feature_mismatch_blocks_candidate():
    result = evaluate_candidate("k_4.5", metadata(), candidate_features=["x"], serve_feature_map=FEATURE_MAP)
    assert result.passed is False
    gate = next(item for item in result.gates if item.name == "serve_parity")
    assert gate.passed is False


def test_missing_calibration_and_market_contract_fail_closed():
    result = evaluate_candidate(
        "k_4.5", metadata(calibration=None, model_type=None, target="wrong"),
        candidate_features=FEATURES, serve_feature_map=FEATURE_MAP,
    )
    assert result.passed is False
    assert {gate.name for gate in result.gates if not gate.passed} == {"calibration", "market_validation"}


def test_challenger_must_improve_both_metrics():
    assert compare_challenger(metadata(test_brier=0.20), metadata(test_brier=0.19))["decision"] == "promote"
    assert compare_challenger(metadata(test_auc=0.72), metadata(test_auc=0.70))["decision"] == "hold"


def test_registry_rejects_blocked_models_and_can_rollback():
    registry = ModelRegistry()
    evaluation = evaluate_candidate("k_4.5", metadata(), candidate_features=FEATURES, serve_feature_map=FEATURE_MAP)
    registry.promote(evaluation, "models/k_4.5-v2.pkl")
    registry.champions["k_4.5"] = {"artifact_ref": "models/k_4.5-v1.pkl"}
    registry.promote(evaluation, "models/k_4.5-v3.pkl")
    assert registry.rollback("k_4.5")["artifact_ref"] == "models/k_4.5-v1.pkl"
