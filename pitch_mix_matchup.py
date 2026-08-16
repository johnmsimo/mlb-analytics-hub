"""Phase 5.2 pitch-mix/contact matchup challenger features.

The historical feature is reconstructed from pitches strictly before the
target game.  The live feature uses the current pitcher arsenal and the
batter's current pitch-type results from the same Baseball Savant contract.
The neutral fallback is zero: no evidence means no matchup adjustment.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
import math
from typing import Any


PITCH_MIX_MATCHUP_VERSION = "5.2.0"
PITCH_MIX_CONTACT_FEATURE = "pitch_mix_contact_edge"
NEUTRAL_PITCH_MIX_EDGE = 0.0
LEAGUE_WOBA = 0.320
CONTACT_PRIOR_PA = 25.0
MIN_BATTER_PA = 15.0
MIN_PITCHER_PITCHES = 50.0
PITCH_FAMILIES = ("fastball", "breaking", "offspeed")

_FAMILY_BY_TYPE = {
    "FF": "fastball", "SI": "fastball", "FC": "fastball",
    "4-SEAM FASTBALL": "fastball", "FOUR-SEAM FASTBALL": "fastball",
    "SINKER": "fastball", "CUTTER": "fastball",
    "SL": "breaking", "ST": "breaking", "CU": "breaking",
    "KC": "breaking", "SV": "breaking", "CS": "breaking",
    "SLIDER": "breaking", "SWEEPER": "breaking", "CURVEBALL": "breaking",
    "KNUCKLE CURVE": "breaking", "SLURVE": "breaking", "SLOW CURVE": "breaking",
    "CH": "offspeed", "FS": "offspeed", "FO": "offspeed",
    "SC": "offspeed", "EP": "offspeed", "KN": "offspeed",
    "CHANGEUP": "offspeed", "SPLITTER": "offspeed", "FORKBALL": "offspeed",
    "SCREWBALL": "offspeed", "EEPHUS": "offspeed", "KNUCKLEBALL": "offspeed",
}
_EVENT_WOBA = {
    "walk": 0.69,
    "intent_walk": 0.69,
    "hit_by_pitch": 0.72,
    "single": 0.88,
    "double": 1.25,
    "triple": 1.58,
    "home_run": 2.00,
}


def pitch_family(pitch_type: Any) -> str | None:
    """Map a Statcast pitch type to one stable matchup family."""
    return _FAMILY_BY_TYPE.get(str(pitch_type or "").strip().upper())


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _row_value(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def weighted_contact_edge(
    pitcher_arsenal: Iterable[Mapping[str, Any]],
    batter_pitch_results: Iterable[Mapping[str, Any]],
    *,
    league_woba: float = LEAGUE_WOBA,
) -> float:
    """Return the arsenal-weighted batter contact edge on the wOBA scale."""
    usage = {family: 0.0 for family in PITCH_FAMILIES}
    for row in pitcher_arsenal or ():
        family = pitch_family(_row_value(row, "pitch_type", "pitchType", "pitch_name"))
        value = _finite(_row_value(row, "usage_pct", "pitch_usage", "usage_percent", "usage"))
        if family and value is not None and value > 0:
            usage[family] += value / 100.0 if value > 1.0 else value
    usage_total = sum(usage.values())
    if usage_total <= 0:
        return NEUTRAL_PITCH_MIX_EDGE
    usage = {family: value / usage_total for family, value in usage.items()}

    pa = {family: 0.0 for family in PITCH_FAMILIES}
    woba_sum = {family: 0.0 for family in PITCH_FAMILIES}
    for row in batter_pitch_results or ():
        family = pitch_family(_row_value(row, "pitch_type", "pitchType", "pitch_name"))
        woba = _finite(_row_value(row, "woba", "wOBA", "estimated_woba"))
        attempts = _finite(_row_value(row, "pa", "plate_appearances", "pitches", "total_pitches"))
        if family and woba is not None and attempts is not None and attempts > 0:
            pa[family] += attempts
            woba_sum[family] += woba * attempts
    total_pa = sum(pa.values())
    if total_pa < MIN_BATTER_PA:
        return NEUTRAL_PITCH_MIX_EDGE

    anchor = float(league_woba)
    overall = (sum(woba_sum.values()) + CONTACT_PRIOR_PA * anchor) / (
        total_pa + CONTACT_PRIOR_PA
    )
    expected = 0.0
    for family in PITCH_FAMILIES:
        family_woba = (woba_sum[family] + CONTACT_PRIOR_PA * anchor) / (
            pa[family] + CONTACT_PRIOR_PA
        )
        expected += usage[family] * family_woba
    return round(max(-0.200, min(0.200, expected - overall)), 6)


def resolve_live_pitch_mix_contact(
    batter: Mapping[str, Any],
    pitcher: Mapping[str, Any],
    *,
    year: int | None = None,
    pitcher_loader: Callable[..., list] | None = None,
    batter_loader: Callable[..., list] | None = None,
) -> float:
    """Resolve the live edge, preserving explicit caller evidence when present."""
    explicit = _finite(_row_value(
        batter,
        "pitchMixContactEdge",
        PITCH_MIX_CONTACT_FEATURE,
    ))
    if explicit is not None:
        return max(-0.200, min(0.200, explicit))
    batter_id = _row_value(batter, "mlbamid", "xMLBAMID", "player_id")
    pitcher_id = _row_value(pitcher, "mlbamid", "xMLBAMID", "player_id", "id")
    try:
        batter_id = int(batter_id)
        pitcher_id = int(pitcher_id)
    except (TypeError, ValueError):
        return NEUTRAL_PITCH_MIX_EDGE
    if year is None:
        year = datetime.utcnow().year
    if pitcher_loader is None or batter_loader is None:
        try:
            from savant_arsenal import get_arsenal_stats, get_batter_pitch_type_stats
        except Exception:
            return NEUTRAL_PITCH_MIX_EDGE
        pitcher_loader = pitcher_loader or get_arsenal_stats
        batter_loader = batter_loader or get_batter_pitch_type_stats
    try:
        arsenal = pitcher_loader(pitcher_id, year=year)
        contact = batter_loader(batter_id, year=year)
    except Exception:
        return NEUTRAL_PITCH_MIX_EDGE
    return weighted_contact_edge(arsenal, contact)


def aggregate_game_pitch_inputs(sc, pa, batter_games):
    """Attach current-game pitch-family sufficient statistics to batter games."""
    import pandas as pd

    out = batter_games.copy()
    pitch_cols = [f"pm_{family}_pitches" for family in PITCH_FAMILIES]
    pa_cols = [f"pc_{family}_pa" for family in PITCH_FAMILIES]
    sum_cols = [f"pc_{family}_woba_sum" for family in PITCH_FAMILIES]
    for column in [*pitch_cols, *pa_cols, *sum_cols]:
        out[column] = 0.0
    if out.empty or "pitch_type" not in sc.columns:
        return out

    pitches = sc[["game_pk", "pitcher", "pitch_type"]].copy()
    pitches["_family"] = pitches["pitch_type"].map(pitch_family)
    pitches = pitches.dropna(subset=["_family"])
    if not pitches.empty:
        mix = (
            pitches.groupby(["game_pk", "pitcher", "_family"])
            .size()
            .unstack(fill_value=0)
            .reindex(columns=PITCH_FAMILIES, fill_value=0)
            .reset_index()
        )
        mix = mix.rename(columns={
            family: f"pm_{family}_pitches" for family in PITCH_FAMILIES
        })
        out = out.drop(columns=pitch_cols).merge(
            mix,
            left_on=["game_pk", "opp_starter"],
            right_on=["game_pk", "pitcher"],
            how="left",
        ).drop(columns=["pitcher"], errors="ignore")

    contacts = pa[["game_pk", "batter", "pitch_type", "events"]].copy()
    contacts["_family"] = contacts["pitch_type"].map(pitch_family)
    if "woba_value" in pa.columns:
        contacts["_woba"] = pd.to_numeric(pa["woba_value"], errors="coerce")
    else:
        contacts["_woba"] = pd.NA
    contacts["_woba"] = contacts["_woba"].fillna(
        contacts["events"].astype(str).map(_EVENT_WOBA).fillna(0.0)
    )
    contacts = contacts.dropna(subset=["_family"])
    if not contacts.empty:
        contact = (
            contacts.groupby(["game_pk", "batter", "_family"])
            .agg(pa=("_woba", "size"), woba_sum=("_woba", "sum"))
            .reset_index()
        )
        pa_wide = (
            contact.pivot(index=["game_pk", "batter"], columns="_family", values="pa")
            .reindex(columns=PITCH_FAMILIES, fill_value=0)
            .fillna(0)
            .rename(columns={family: f"pc_{family}_pa" for family in PITCH_FAMILIES})
        )
        sum_wide = (
            contact.pivot(index=["game_pk", "batter"], columns="_family", values="woba_sum")
            .reindex(columns=PITCH_FAMILIES, fill_value=0)
            .fillna(0)
            .rename(columns={
                family: f"pc_{family}_woba_sum" for family in PITCH_FAMILIES
            })
        )
        contact_wide = pa_wide.join(sum_wide).reset_index()
        out = out.drop(columns=[*pa_cols, *sum_cols]).merge(
            contact_wide,
            on=["game_pk", "batter"],
            how="left",
        )
    for column in [*pitch_cols, *pa_cols, *sum_cols]:
        out[column] = pd.to_numeric(out.get(column), errors="coerce").fillna(0.0)
    return out


def add_historical_pitch_mix_contact(
    frame,
    *,
    league_woba: float = LEAGUE_WOBA,
):
    """Append a strictly pregame pitch-mix/contact edge to batter-game rows."""
    import pandas as pd

    out = frame.copy()
    if out.empty:
        out[PITCH_MIX_CONTACT_FEATURE] = []
        out["pitch_mix_batter_pa"] = []
        out["pitch_mix_pitcher_pitches"] = []
        return out
    required = {"season", "game_pk", "game_date", "batter", "opp_starter"}
    missing = sorted(required - set(out.columns))
    if missing:
        raise ValueError(f"missing pitch-mix historical columns: {', '.join(missing)}")
    out["_original_order"] = range(len(out))
    out = out.sort_values(["season", "game_date", "game_pk", "batter"]).copy()
    raw_columns = []
    for family in PITCH_FAMILIES:
        for prefix in ("pm", "pc"):
            suffixes = ("pitches",) if prefix == "pm" else ("pa", "woba_sum")
            for suffix in suffixes:
                column = f"{prefix}_{family}_{suffix}"
                raw_columns.append(column)
                out[column] = pd.to_numeric(out.get(column, 0.0), errors="coerce").fillna(0.0)

    batter_group = out.groupby(["season", "batter"], sort=False)
    for family in PITCH_FAMILIES:
        for suffix in ("pa", "woba_sum"):
            source = f"pc_{family}_{suffix}"
            target = f"_pregame_{family}_{suffix}"
            out[target] = batter_group[source].cumsum() - out[source]

    starter_games = (
        out[["season", "game_pk", "game_date", "opp_starter", *[
            f"pm_{family}_pitches" for family in PITCH_FAMILIES
        ]]]
        .drop_duplicates(["season", "game_pk", "opp_starter"])
        .sort_values(["season", "opp_starter", "game_date", "game_pk"])
    )
    starter_group = starter_games.groupby(["season", "opp_starter"], sort=False)
    merge_columns = ["season", "game_pk", "opp_starter"]
    for family in PITCH_FAMILIES:
        source = f"pm_{family}_pitches"
        target = f"_pregame_{family}_pitches"
        starter_games[target] = starter_group[source].cumsum() - starter_games[source]
        merge_columns.append(target)
    out = out.merge(
        starter_games[merge_columns],
        on=["season", "game_pk", "opp_starter"],
        how="left",
    )

    batter_pa = sum(out[f"_pregame_{family}_pa"] for family in PITCH_FAMILIES)
    pitcher_pitches = sum(
        out[f"_pregame_{family}_pitches"].fillna(0.0) for family in PITCH_FAMILIES
    )
    overall_sum = sum(
        out[f"_pregame_{family}_woba_sum"] for family in PITCH_FAMILIES
    )
    overall = (overall_sum + CONTACT_PRIOR_PA * float(league_woba)) / (
        batter_pa + CONTACT_PRIOR_PA
    )
    expected = 0.0
    for family in PITCH_FAMILIES:
        family_contact = (
            out[f"_pregame_{family}_woba_sum"] + CONTACT_PRIOR_PA * float(league_woba)
        ) / (out[f"_pregame_{family}_pa"] + CONTACT_PRIOR_PA)
        family_usage = out[f"_pregame_{family}_pitches"].fillna(0.0) / pitcher_pitches.where(
            pitcher_pitches > 0, 1.0
        )
        expected = expected + family_usage * family_contact
    valid = (batter_pa >= MIN_BATTER_PA) & (pitcher_pitches >= MIN_PITCHER_PITCHES)
    out[PITCH_MIX_CONTACT_FEATURE] = (
        (expected - overall)
        .where(valid, NEUTRAL_PITCH_MIX_EDGE)
        .fillna(NEUTRAL_PITCH_MIX_EDGE)
        .clip(-0.200, 0.200)
    )
    out["pitch_mix_batter_pa"] = batter_pa
    out["pitch_mix_pitcher_pitches"] = pitcher_pitches
    internal = [column for column in out.columns if column.startswith("_pregame_")]
    return (
        out.sort_values("_original_order")
        .drop(columns=["_original_order", *internal])
        .reset_index(drop=True)
    )
