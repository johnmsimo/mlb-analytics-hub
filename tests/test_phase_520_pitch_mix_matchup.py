import json
from pathlib import Path

import pandas as pd
import pytest

from pitch_mix_matchup import (
    NEUTRAL_PITCH_MIX_EDGE,
    PITCH_MIX_CONTACT_FEATURE,
    add_historical_pitch_mix_contact,
    aggregate_game_pitch_inputs,
    pitch_family,
    resolve_live_pitch_mix_contact,
    weighted_contact_edge,
)


ROOT = Path(__file__).resolve().parents[1]


def test_pitch_types_collapse_to_stable_families():
    assert pitch_family("FF") == "fastball"
    assert pitch_family("ST") == "breaking"
    assert pitch_family("CH") == "offspeed"
    assert pitch_family("PO") is None


def test_weighted_edge_uses_pitcher_mix_and_shrunk_batter_contact():
    pitcher = [
        {"pitch_type": "FF", "usage_pct": 70},
        {"pitch_type": "SL", "usage_pct": 20},
        {"pitch_type": "CH", "usage_pct": 10},
    ]
    batter = [
        {"pitch_type": "FF", "pa": 40, "woba": 0.410},
        {"pitch_type": "SL", "pa": 30, "woba": 0.250},
        {"pitch_type": "CH", "pa": 20, "woba": 0.300},
    ]
    fastball_heavy = weighted_contact_edge(pitcher, batter)
    breaking_heavy = weighted_contact_edge(
        [
            {"pitch_type": "FF", "usage_pct": 10},
            {"pitch_type": "SL", "usage_pct": 80},
            {"pitch_type": "CH", "usage_pct": 10},
        ],
        batter,
    )
    assert fastball_heavy > 0
    assert breaking_heavy < fastball_heavy
    assert -0.2 <= fastball_heavy <= 0.2


def test_weighted_edge_fails_neutral_without_minimum_evidence():
    assert weighted_contact_edge([], []) == NEUTRAL_PITCH_MIX_EDGE
    assert weighted_contact_edge(
        [{"pitch_type": "FF", "usage_pct": 100}],
        [{"pitch_type": "FF", "pa": 5, "woba": 0.600}],
    ) == NEUTRAL_PITCH_MIX_EDGE


def test_live_resolution_preserves_explicit_evidence_without_fetch():
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("loader should not run")

    value = resolve_live_pitch_mix_contact(
        {"pitchMixContactEdge": 0.044},
        {},
        pitcher_loader=forbidden,
        batter_loader=forbidden,
    )
    assert value == pytest.approx(0.044)
    assert calls == []


def test_live_resolution_uses_injected_savant_contract():
    value = resolve_live_pitch_mix_contact(
        {"mlbamid": 1},
        {"mlbamid": 2},
        year=2026,
        pitcher_loader=lambda player_id, year: [
            {"pitch_type": "FF", "usage_pct": 80},
            {"pitch_type": "SL", "usage_pct": 20},
        ],
        batter_loader=lambda player_id, year: [
            {"pitch_type": "FF", "pa": 50, "woba": 0.420},
            {"pitch_type": "SL", "pa": 30, "woba": 0.250},
        ],
    )
    assert value > 0


def test_savant_batter_pitch_type_loader_parses_csv(monkeypatch):
    import savant_arsenal

    class Response:
        status_code = 200
        text = (
            "player_id,pitch_type,woba,pa\n"
            "1,FF,0.401,42\n"
            "1,SL,0.255,31\n"
            "2,FF,0.300,50\n"
        )

    monkeypatch.setattr(savant_arsenal.requests, "get", lambda *args, **kwargs: Response())
    rows = savant_arsenal._fetch_batter_pitch_types(1, 2026)
    assert rows == [
        {"pitch_type": "FF", "woba": 0.401, "pa": 42},
        {"pitch_type": "SL", "woba": 0.255, "pa": 31},
    ]


def test_game_aggregation_creates_pitcher_mix_and_batter_contact_sums():
    sc = pd.DataFrame([
        {"game_pk": 10, "pitcher": 90, "batter": 1, "pitch_type": "FF", "events": None},
        {"game_pk": 10, "pitcher": 90, "batter": 1, "pitch_type": "FF", "events": "single", "woba_value": 0.88},
        {"game_pk": 10, "pitcher": 90, "batter": 1, "pitch_type": "SL", "events": "field_out", "woba_value": 0.0},
    ])
    pa = sc[sc["events"].notna()].copy()
    batter_games = pd.DataFrame([
        {"game_pk": 10, "batter": 1, "opp_starter": 90},
    ])
    result = aggregate_game_pitch_inputs(sc, pa, batter_games).iloc[0]
    assert result["pm_fastball_pitches"] == 2
    assert result["pm_breaking_pitches"] == 1
    assert result["pc_fastball_pa"] == 1
    assert result["pc_fastball_woba_sum"] == pytest.approx(0.88)
    assert result["pc_breaking_pa"] == 1


def _historical_frame():
    rows = []
    for game, date, target in [
        (1, "2025-04-01", 0),
        (2, "2025-04-05", 1),
        (3, "2025-04-09", 0),
    ]:
        rows.append({
            "season": 2025,
            "game_pk": game,
            "game_date": date,
            "batter": 7,
            "opp_starter": 90,
            "hit_over_1.5": target,
            "pc_fastball_pa": 10,
            "pc_fastball_woba_sum": 4.2,
            "pc_breaking_pa": 4,
            "pc_breaking_woba_sum": 0.8,
            "pc_offspeed_pa": 2,
            "pc_offspeed_woba_sum": 0.6,
            "pm_fastball_pitches": 40,
            "pm_breaking_pitches": 8,
            "pm_offspeed_pitches": 2,
        })
    return pd.DataFrame(rows)


def test_historical_feature_is_strictly_pregame_and_neutral_until_observed():
    result = add_historical_pitch_mix_contact(_historical_frame())
    assert result.loc[0, PITCH_MIX_CONTACT_FEATURE] == 0
    assert result.loc[0, "pitch_mix_batter_pa"] == 0
    assert result.loc[1, "pitch_mix_batter_pa"] == 16
    assert result.loc[1, "pitch_mix_pitcher_pitches"] == 50
    assert result.loc[1, PITCH_MIX_CONTACT_FEATURE] != 0


def test_target_mutation_cannot_change_historical_feature():
    source = _historical_frame()
    original = add_historical_pitch_mix_contact(source)[PITCH_MIX_CONTACT_FEATURE]
    source["hit_over_1.5"] = 1 - source["hit_over_1.5"]
    mutated = add_historical_pitch_mix_contact(source)[PITCH_MIX_CONTACT_FEATURE]
    pd.testing.assert_series_equal(original, mutated)


def test_phase_52_contract_is_shadow_only_and_frozen_ordered():
    contract = json.loads(
        (ROOT / "data/pitch_mix_matchup_experiment.json").read_text(encoding="utf-8")
    )
    assert contract["targetModels"] == [
        "hits_1.5", "tb_2.5", "tb_3.5", "hits", "tb", "hr"
    ]
    assert contract["historicalContract"]["targetColumnsRead"] is False
    assert contract["safety"] == {
        "writesChampionArtifacts": False,
        "changesProductionProbabilities": False,
        "automaticPromotion": False,
        "reviewRequired": True,
    }


def test_challenger_feature_is_absent_from_every_champion():
    from regenerate_models import MARKETS, PITCH_MIX_CHALLENGER_FEATURES

    production = json.loads(
        (ROOT / "models/xgb_feature_cols.json").read_text(encoding="utf-8")
    )
    targets = ["hits_1.5", "tb_2.5", "tb_3.5", "hits", "tb", "hr"]
    for model_key in targets:
        assert PITCH_MIX_CHALLENGER_FEATURES[model_key] == [
            *MARKETS[model_key]["features"],
            PITCH_MIX_CONTACT_FEATURE,
        ]
        assert PITCH_MIX_CONTACT_FEATURE not in production.get(model_key, [])


def test_ci_and_manual_workflow_enforce_phase_52_contract():
    deploy = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    manual = (
        ROOT / ".github/workflows/pitch-mix-matchup-experiment.yml"
    ).read_text(encoding="utf-8")
    assert "python scripts/pitch_mix_matchup_experiment.py --check-contract" in deploy
    assert "workflow_dispatch" in manual
    assert "actions/upload-artifact@v4" in manual
    assert "python scripts/pitch_mix_matchup_experiment.py --run" in manual


def test_phase_52_is_documented_without_claiming_lift():
    roadmap = (
        ROOT / "docs/MLB_ANALYTICS_HUB_ROADMAP.md"
    ).read_text(encoding="utf-8")
    docs = (
        ROOT / "docs/pitch_mix_matchup_experiment.md"
    ).read_text(encoding="utf-8")
    assert "### Phase 5.2 — Pitch-mix/contact challenger lane" in roadmap
    assert "does not change production probabilities" in docs
    assert "Promotion remains" in docs
