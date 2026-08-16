"""Leakage-safe RBI opportunity context and frozen-champion comparison.

The feature is the mean pregame OBP of the three lineup slots immediately
preceding a batter. Historical values use only season-to-date events that
occurred before the target game. Live values use the confirmed lineup and
current season stats. Missing predecessors fall back to league-average OBP.
"""

from __future__ import annotations

from copy import deepcopy
import math
from typing import Any, Callable, Mapping


RBI_OPPORTUNITY_VERSION = "5.1.0"
RBI_TRAFFIC_FEATURE = "rbi_traffic_obp"
LEAGUE_OBP = 0.320
PRECEDING_LINEUP_SLOTS = 3


def _finite_obp(value: Any, fallback: float = LEAGUE_OBP) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        return fallback
    return number


def preceding_slots(slot: Any, count: int = PRECEDING_LINEUP_SLOTS) -> tuple[int, ...]:
    """Return cyclic lineup slots immediately before the target, nearest first."""
    try:
        value = int(slot)
    except (TypeError, ValueError):
        return ()
    if not 1 <= value <= 9 or count < 1:
        return ()
    return tuple(((value - offset - 1) % 9) + 1 for offset in range(1, count + 1))


def attach_live_rbi_opportunity(
    lineups: Mapping[Any, Mapping[str, Any]],
    stats_lookup: Callable[[int], Mapping[str, Any]],
    *,
    league_obp: float = LEAGUE_OBP,
) -> dict[Any, dict[str, Any]]:
    """Attach serve-time RBI traffic context to parsed confirmed lineups.

    stats_lookup must return current season stats known before today's game.
    It is injected so callers can use their existing cache and tests never need
    network access.
    """
    fallback = _finite_obp(league_obp)
    enriched = deepcopy(dict(lineups))
    stats_by_id: dict[int, Mapping[str, Any]] = {}
    for raw_id in enriched:
        try:
            player_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        try:
            stats_by_id[player_id] = stats_lookup(player_id) or {}
        except Exception:
            stats_by_id[player_id] = {}

    groups: dict[tuple[Any, Any], dict[int, int]] = {}
    for raw_id, entry in enriched.items():
        if not isinstance(entry, dict):
            continue
        try:
            player_id = int(raw_id)
            slot = int(entry.get("batting_order") or 0)
        except (TypeError, ValueError):
            continue
        if not 1 <= slot <= 9:
            continue
        key = (entry.get("game_pk"), entry.get("team"))
        groups.setdefault(key, {}).setdefault(slot, player_id)

    for entry in enriched.values():
        if not isinstance(entry, dict):
            continue
        slots = preceding_slots(entry.get("batting_order"))
        slot_map = groups.get((entry.get("game_pk"), entry.get("team")), {})
        values: list[float] = []
        observed = 0
        for slot in slots:
            predecessor_id = slot_map.get(slot)
            raw_obp = None
            if predecessor_id is not None:
                raw_obp = stats_by_id.get(predecessor_id, {}).get("obp")
            value = _finite_obp(raw_obp, fallback)
            values.append(value)
            try:
                observed_value = float(raw_obp)
                observed += int(math.isfinite(observed_value) and 0.0 <= observed_value <= 1.0)
            except (TypeError, ValueError):
                pass
        entry[RBI_TRAFFIC_FEATURE] = round(
            sum(values) / len(values) if values else fallback,
            4,
        )
        entry["rbi_traffic_observed_slots"] = observed
    return enriched


def add_historical_rbi_opportunity(frame, *, league_obp: float = LEAGUE_OBP):
    """Return a frame with strictly pregame, season-to-date RBI traffic OBP.

    Required columns are season, batter, game_pk, game_date,
    inning_topbot, batting_order, on_base and obp_denom.
    Neither RBI outcomes nor any other target column is read.
    """
    import numpy as np
    import pandas as pd

    required = {
        "season", "batter", "game_pk", "game_date", "inning_topbot",
        "batting_order", "on_base", "obp_denom",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError("missing RBI opportunity columns: " + ", ".join(missing))
    fallback = _finite_obp(league_obp)
    out = frame.copy()
    out["_original_order"] = np.arange(len(out))
    out["batting_order"] = pd.to_numeric(out["batting_order"], errors="coerce")
    out["on_base"] = pd.to_numeric(out["on_base"], errors="coerce").fillna(0.0)
    out["obp_denom"] = pd.to_numeric(out["obp_denom"], errors="coerce").fillna(0.0)
    out = out.sort_values(["season", "batter", "game_date", "game_pk", "_original_order"])

    player_groups = out.groupby(["season", "batter"], sort=False)
    prior_on_base = player_groups["on_base"].cumsum() - out["on_base"]
    prior_denom = player_groups["obp_denom"].cumsum() - out["obp_denom"]
    out["_pregame_obp_observed"] = prior_denom > 0
    out["_pregame_obp"] = prior_on_base / prior_denom.where(prior_denom > 0)
    out["_pregame_obp"] = out["_pregame_obp"].replace([np.inf, -np.inf], np.nan)
    out["_pregame_obp"] = out["_pregame_obp"].fillna(fallback).clip(0.0, 1.0)

    side_keys = ["game_pk", "inning_topbot"]
    slot_obp = (
        out.dropna(subset=["batting_order"])
        .assign(batting_order=lambda value: value["batting_order"].astype(int))
        .query("1 <= batting_order <= 9")
        .groupby(side_keys + ["batting_order"], as_index=False)
        .agg(
            _pregame_obp=("_pregame_obp", "mean"),
            _pregame_obp_observed=("_pregame_obp_observed", "max"),
        )
    )
    for offset in range(1, PRECEDING_LINEUP_SLOTS + 1):
        lookup = slot_obp.copy()
        lookup["batting_order"] = ((lookup["batting_order"] + offset - 1) % 9) + 1
        column = f"_traffic_{offset}"
        observed_column = f"_traffic_observed_{offset}"
        lookup = lookup.rename(columns={
            "_pregame_obp": column,
            "_pregame_obp_observed": observed_column,
        })
        out = out.merge(lookup, on=side_keys + ["batting_order"], how="left")

    traffic_columns = [f"_traffic_{offset}" for offset in range(1, PRECEDING_LINEUP_SLOTS + 1)]
    observed_columns = [
        f"_traffic_observed_{offset}"
        for offset in range(1, PRECEDING_LINEUP_SLOTS + 1)
    ]
    out[RBI_TRAFFIC_FEATURE] = out[traffic_columns].fillna(fallback).mean(axis=1).round(4)
    out["rbi_traffic_observed_slots"] = out[observed_columns].eq(True).sum(axis=1)
    return (
        out.sort_values("_original_order")
        .drop(columns=[
            "_original_order",
            "_pregame_obp",
            "_pregame_obp_observed",
            *traffic_columns,
            *observed_columns,
        ])
        .reset_index(drop=True)
    )


def compare_frozen_champion(
    model_key: str,
    champion: Mapping[str, Any],
    challenger: Mapping[str, Any],
) -> dict[str, Any]:
    """Evaluate a challenger against the Phase 4.86 frozen holdout contract."""
    held_out = champion.get("heldOut") if isinstance(champion.get("heldOut"), Mapping) else champion
    errors: list[str] = []
    required = ("brier", "auc", "logloss")
    champion_values: dict[str, float] = {}
    challenger_values: dict[str, float] = {}
    challenger_aliases = {"brier": "test_brier", "auc": "test_auc", "logloss": "test_logloss"}
    for metric in required:
        try:
            champion_values[metric] = float(held_out[metric])
            challenger_values[metric] = float(challenger[challenger_aliases[metric]])
        except (KeyError, TypeError, ValueError):
            errors.append(f"missing finite {metric} metric")
    champion_split = champion.get("split", {})
    try:
        same_season = int(challenger.get("test_season")) == int(champion_split.get("testSeason"))
        same_cohort = int(challenger.get("n_test")) == int(champion_split.get("nTest"))
    except (TypeError, ValueError):
        same_season = same_cohort = False
    if not same_season:
        errors.append("challenger test season differs from frozen champion")
    if not same_cohort:
        errors.append("challenger held-out cohort differs from frozen champion")

    gates = {
        "heldOutBrierImproves": False,
        "heldOutAucDoesNotRegress": False,
        "heldOutLoglossDoesNotRegress": False,
        "sameTestSeason": same_season,
        "sameHeldOutCohort": same_cohort,
        "marketEcePendingShadowCalibration": True,
    }
    if len(champion_values) == 3 and len(challenger_values) == 3:
        gates["heldOutBrierImproves"] = challenger_values["brier"] < champion_values["brier"]
        gates["heldOutAucDoesNotRegress"] = challenger_values["auc"] >= champion_values["auc"]
        gates["heldOutLoglossDoesNotRegress"] = challenger_values["logloss"] <= champion_values["logloss"]
    metric_gates = all(
        value for key, value in gates.items()
        if key != "marketEcePendingShadowCalibration"
    )
    return {
        "modelKey": model_key,
        "status": "metric_gates_passed" if metric_gates and not errors else "held",
        "errors": errors,
        "champion": champion_values,
        "challenger": challenger_values,
        "brierImprovement": (
            round(champion_values["brier"] - challenger_values["brier"], 6)
            if "brier" in champion_values and "brier" in challenger_values else None
        ),
        "gates": gates,
        "shadowEligible": metric_gates and not errors,
        "promotionEligible": False,
        "automaticPromotion": False,
    }
