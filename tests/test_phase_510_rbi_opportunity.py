import json
from pathlib import Path

import pandas as pd

from rbi_opportunity import (
    RBI_TRAFFIC_FEATURE,
    add_historical_rbi_opportunity,
    attach_live_rbi_opportunity,
    compare_frozen_champion,
    preceding_slots,
)


ROOT = Path(__file__).resolve().parents[1]


def test_preceding_slots_are_cyclic_and_nearest_first():
    assert preceding_slots(4) == (3, 2, 1)
    assert preceding_slots(1) == (9, 8, 7)
    assert preceding_slots(0) == ()


def test_live_context_uses_three_preceding_confirmed_slots():
    lineups = {
        player_id: {
            "game_pk": 1,
            "team": "Example",
            "batting_order": player_id,
            "lineup_confirmed": 1,
        }
        for player_id in range(1, 10)
    }
    obp = {1: 0.310, 2: 0.320, 3: 0.330}
    enriched = attach_live_rbi_opportunity(
        lineups,
        lambda player_id: {"obp": obp.get(player_id, 0.300)},
    )
    assert enriched[4][RBI_TRAFFIC_FEATURE] == 0.320
    assert enriched[4]["rbi_traffic_observed_slots"] == 3


def _historical_frame():
    rows = []
    first_game = {
        1: (2, 4),
        2: (2, 5),
        3: (1, 4),
        4: (0, 4),
    }
    for game_pk, game_date in ((10, "2025-04-01"), (11, "2025-04-02")):
        for slot in range(1, 5):
            on_base, denom = first_game[slot] if game_pk == 10 else (0, 4)
            rows.append({
                "season": 2025,
                "batter": slot,
                "game_pk": game_pk,
                "game_date": game_date,
                "inning_topbot": "Top",
                "batting_order": slot,
                "on_base": on_base,
                "obp_denom": denom,
                "rbi": 99 if game_pk == 11 else 0,
            })
    return pd.DataFrame(rows)


def test_historical_context_is_pregame_and_target_isolated():
    frame = _historical_frame()
    result = add_historical_rbi_opportunity(frame)
    second_game_cleanup = result[(result.game_pk == 11) & (result.batting_order == 4)].iloc[0]
    assert second_game_cleanup[RBI_TRAFFIC_FEATURE] == 0.3833

    changed = frame.copy()
    changed.loc[changed.game_pk == 11, ["on_base", "obp_denom", "rbi"]] = [4, 4, 0]
    changed_result = add_historical_rbi_opportunity(changed)
    changed_cleanup = changed_result[
        (changed_result.game_pk == 11) & (changed_result.batting_order == 4)
    ].iloc[0]
    assert changed_cleanup[RBI_TRAFFIC_FEATURE] == second_game_cleanup[RBI_TRAFFIC_FEATURE]


def test_missing_predecessors_fall_back_without_future_information():
    frame = _historical_frame()
    frame = frame[~((frame.game_pk == 11) & (frame.batting_order == 2))]
    result = add_historical_rbi_opportunity(frame)
    cleanup = result[(result.game_pk == 11) & (result.batting_order == 4)].iloc[0]
    assert cleanup[RBI_TRAFFIC_FEATURE] == round((0.25 + 0.320 + 0.5) / 3, 4)
    assert cleanup["rbi_traffic_observed_slots"] == 2


def _champion():
    return {
        "heldOut": {"brier": 0.100, "auc": 0.620, "logloss": 0.340},
        "split": {"testSeason": 2025, "nTest": 1000},
    }


def test_challenger_must_clear_every_metric_gate_but_cannot_promote():
    report = compare_frozen_champion(
        "rbi_1.5",
        _champion(),
        {
            "test_brier": 0.099,
            "test_auc": 0.621,
            "test_logloss": 0.339,
            "test_season": 2025,
            "n_test": 1000,
        },
    )
    assert report["status"] == "metric_gates_passed"
    assert report["shadowEligible"] is True
    assert report["promotionEligible"] is False
    assert report["automaticPromotion"] is False
    assert report["gates"]["marketEcePendingShadowCalibration"] is True


def test_brier_gain_with_auc_regression_is_held():
    report = compare_frozen_champion(
        "rbi",
        _champion(),
        {
            "test_brier": 0.099,
            "test_auc": 0.619,
            "test_logloss": 0.339,
            "test_season": 2025,
            "n_test": 1000,
        },
    )
    assert report["status"] == "held"
    assert report["shadowEligible"] is False
    assert report["gates"]["heldOutAucDoesNotRegress"] is False


def test_experiment_contract_is_shadow_only_and_targets_weakest_rbi_lines():
    contract = json.loads(
        (ROOT / "data/rbi_opportunity_experiment.json").read_text(encoding="utf-8")
    )
    assert contract["version"] == "5.1.0"
    assert contract["targetModels"] == ["rbi_1.5", "rbi"]
    assert contract["feature"]["name"] == RBI_TRAFFIC_FEATURE
    assert contract["safety"]["writesChampionArtifacts"] is False
    assert contract["safety"]["changesProductionProbabilities"] is False
    assert contract["safety"]["automaticPromotion"] is False
    assert contract["safety"]["shadowCalibrationRequiredBeforePromotion"] is True


def test_challenger_feature_is_not_in_committed_champions():
    from regenerate_models import RBI_CHALLENGER_FEATURES, RBI_FEATURES

    production = json.loads(
        (ROOT / "models/xgb_feature_cols.json").read_text(encoding="utf-8")
    )
    assert RBI_CHALLENGER_FEATURES == [*RBI_FEATURES, RBI_TRAFFIC_FEATURE]
    assert RBI_TRAFFIC_FEATURE not in production["rbi"]
    assert RBI_TRAFFIC_FEATURE not in production["rbi_1.5"]


def test_ci_and_manual_workflow_enforce_frozen_experiment_boundary():
    quality = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    workflow = (
        ROOT / ".github/workflows/rbi-opportunity-experiment.yml"
    ).read_text(encoding="utf-8")
    assert "python scripts/rbi_opportunity_experiment.py --check-contract" in quality
    assert "workflow_dispatch" in workflow
    assert "python scripts/accuracy_baseline.py --check" in workflow
    assert "python scripts/data_intelligence.py --check" in workflow
    assert "python scripts/rbi_opportunity_experiment.py --run" in workflow
    assert "actions/upload-artifact@v4" in workflow


def test_phase_51_is_documented_without_claiming_model_lift():
    roadmap = (ROOT / "docs/MLB_ANALYTICS_HUB_ROADMAP.md").read_text(encoding="utf-8")
    docs = (ROOT / "docs/rbi_opportunity_experiment.md").read_text(encoding="utf-8")
    assert "### Phase 5.1 — RBI opportunity challenger lane" in roadmap
    assert "does not change production probabilities" in docs
    assert "Automatic promotion remains disabled" in docs
