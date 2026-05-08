"""
fangraphs_loader.py — Single FanGraphs data layer for MLB Analytics Hub.
"""

import os, re
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

BAT_PATH      = os.path.join(DATA_DIR, "fg_batting_2026.csv")
PIT_PATH      = os.path.join(DATA_DIR, "fg_pitching_2026.csv")
BAT_PATH_2025 = os.path.join(DATA_DIR, "fg_batting_2025.csv")
PIT_PATH_2025 = os.path.join(DATA_DIR, "fg_pitching_2025.csv")
PROJ_BAT_PATH  = os.path.join(DATA_DIR, "fg_steamer_bat_2026.csv")
PROJ_PIT_PATH  = os.path.join(DATA_DIR, "fg_steamer_pit_2026.csv")
PROJ_BAT_PATH2 = os.path.join(DATA_DIR, "fg_proj_bat_2026.csv")
PROJ_PIT_PATH2 = os.path.join(DATA_DIR, "fg_proj_pit_2026.csv")

MIN_IP = 5.0
MIN_PA = 20

_cache: dict = {}


def _clean_name_html(df, col="Name"):
    if col in df.columns:
        df[col] = df[col].apply(
            lambda x: re.sub(r"<[^>]+>", "", str(x)).strip() if pd.notna(x) else x
        )  # FIX: was missing closing ) on .apply(
    return df


def _try_load(path, key):
    try:
        df = pd.read_csv(path, low_memory=False)
        for col in ("Name", "Team", "PlayerName"):
            df = _clean_name_html(df, col)
        if "playerid" in df.columns:
            df["playerid"] = df["playerid"].astype(str).str.strip()
        print(f"[FG Loader] {key}: {df.shape[0]} rows")
        return df
    except FileNotFoundError:
        print(f"[FG Loader] WARNING: {path} not found")
        return pd.DataFrame()
    except Exception as e:
        print(f"[FG Loader] ERROR {key}: {e}")
        return pd.DataFrame()


def _find_proj_path(primary, fallback):
    if os.path.exists(primary):
        return primary
    if os.path.exists(fallback):
        return fallback
    return primary


def _load_all():
    if _cache:
        return
    _cache["bat"]      = _try_load(BAT_PATH,      "bat_2026")
    _cache["pit"]      = _try_load(PIT_PATH,      "pit_2026")
    _cache["bat_2025"] = _try_load(BAT_PATH_2025, "bat_2025")
    _cache["pit_2025"] = _try_load(PIT_PATH_2025, "pit_2025")
    proj_bat = _find_proj_path(PROJ_BAT_PATH, PROJ_BAT_PATH2)
    proj_pit = _find_proj_path(PROJ_PIT_PATH, PROJ_PIT_PATH2)
    _cache["proj_bat"] = _try_load(proj_bat, "proj_bat")
    _cache["proj_pit"] = _try_load(proj_pit, "proj_pit")


def _name_col(df):
    return "PlayerName" if "PlayerName" in df.columns else "Name"


def _find_row(df, name=None, player_id=None):
    if df.empty:
        return pd.DataFrame()
    if player_id:
        if "playerid" in df.columns:
            row = df[df["playerid"] == str(player_id)]
            if not row.empty:
                return row
    if name:
        nc = _name_col(df)
        nl = name.lower().strip()
        row = df[df[nc].str.lower().str.strip() == nl]
        if not row.empty:
            return row
        row = df[df[nc].str.lower().str.contains(nl, na=False)]
        if not row.empty:
            return row
    return pd.DataFrame()


def _has_enough_sample(row, ptype):
    if ptype == "pit":
        for col in ("IP", "ip", "innings_pitched"):
            if col in row.index:
                try:
                    return float(row[col]) >= MIN_IP
                except Exception:
                    pass
        return False
    else:
        for col in ("PA", "pa", "plate_appearances"):
            if col in row.index:
                try:
                    return int(row[col]) >= MIN_PA
                except Exception:
                    pass
        return False


def get_all_batters():
    _load_all()
    return _cache["bat"].copy()


def get_all_pitchers():
    _load_all()
    return _cache["pit"].copy()


def get_all_projections_bat():
    _load_all()
    return _cache["proj_bat"].copy()


def get_all_projections_pit():
    _load_all()
    return _cache["proj_pit"].copy()


def find_player_id(name, player_type="bat"):
    _load_all()
    for key in (
        ("bat" if player_type == "bat" else "pit"),
        ("bat_2025" if player_type == "bat" else "pit_2025"),
    ):
        df = _cache.get(key, pd.DataFrame())
        row = _find_row(df, name=name)
        if not row.empty and "playerid" in row.columns:
            return str(row.iloc[0]["playerid"])
    return None


def get_batter_stats(player_id=None, name=None):
    _load_all()
    row26 = _find_row(_cache["bat"], player_id=player_id, name=name)
    if not row26.empty:
        r = row26.iloc[0]
        if _has_enough_sample(r, "bat"):
            return r.to_dict()
        row25 = _find_row(_cache["bat_2025"], player_id=player_id, name=name)
        if not row25.empty:
            return {**row25.iloc[0].to_dict(), **r.to_dict()}
        return r.to_dict()
    row25 = _find_row(_cache["bat_2025"], player_id=player_id, name=name)
    if not row25.empty:
        return row25.iloc[0].to_dict()
    return {}


def get_pitcher_stats(player_id=None, name=None):
    _load_all()
    row26 = _find_row(_cache["pit"], player_id=player_id, name=name)
    if not row26.empty:
        r = row26.iloc[0]
        if _has_enough_sample(r, "pit"):
            return r.to_dict()
        row25 = _find_row(_cache["pit_2025"], player_id=player_id, name=name)
        if not row25.empty:
            return {**row25.iloc[0].to_dict(), **r.to_dict()}
        return r.to_dict()
    row25 = _find_row(_cache["pit_2025"], player_id=player_id, name=name)
    if not row25.empty:
        return row25.iloc[0].to_dict()
    return {}


def get_batter_projection(player_id=None, name=None):
    _load_all()
    df = _cache["proj_bat"]
    if df.empty:
        return {}
    row = _find_row(df, player_id=player_id, name=name)
    return row.iloc[0].to_dict() if not row.empty else {}


def get_pitcher_projection(player_id=None, name=None):
    _load_all()
    df = _cache["proj_pit"]
    if df.empty:
        return {}
    row = _find_row(df, player_id=player_id, name=name)
    return row.iloc[0].to_dict() if not row.empty else {}


def get_key_batter_features(player_id=None, name=None):
    stats = get_batter_stats(player_id=player_id, name=name)
    if not stats:
        return {}
    return {
        "sv_xba":       stats.get("xAVG",       0.250),
        "sv_xwoba":     stats.get("xwOBA",       0.320),
        "sv_xslg":      stats.get("xSLG",        0.400),
        "sv_ev":        stats.get("EV",           88.0),
        "sv_brl_pct":   stats.get("Barrel%",      4.0),  # FIX: 'Barrels' -> 'Barrel%'
        "sv_hh_pct":    stats.get("HardHit%",     35.0),
        "sv_ss_pct":    stats.get("SwStr%",       10.0),
        "sv_la":        stats.get("LA",           12.0),
        "sv_k_pct":     stats.get("K%",           22.0),
        "sv_bb_pct":    stats.get("BB%",           8.0),
        "sv_woba":      stats.get("wOBA",          0.320),
        "sv_wrc_plus":  stats.get("wRC+",         100.0),
        "sv_iso":       stats.get("ISO",           0.150),
        "sv_babip":     stats.get("BABIP",         0.300),
        "sv_o_swing":   stats.get("O-Swing%",      30.0),
        "sv_z_contact": stats.get("Z-Contact%",    85.0),
        "sv_pull_pct":  stats.get("Pull%",         40.0),
        "sv_hard_pct":  stats.get("Hard%",         35.0),
    }  # FIX: was missing closing }


def get_key_pitcher_features(player_id=None, name=None):
    stats = get_pitcher_stats(player_id=player_id, name=name)
    if not stats:
        return {}
    return {
        "opp_xera":   stats.get("xERA",   4.50),
        "opp_era":    stats.get("ERA",     4.50),
        "opp_k_pct":  stats.get("K%",     22.0),
        "opp_bb_pct": stats.get("BB%",      8.0),
        "opp_whiff":  stats.get("SwStr%",  24.0),
        "opp_fip":    stats.get("FIP",     4.20),
        "opp_xfip":   stats.get("xFIP",    4.20),
        "opp_siera":  stats.get("SIERA",   4.20),
        "opp_hr_fb":  stats.get("HR/FB",   12.0),
        "opp_gb_pct": stats.get("GB%",     45.0),
    }  # FIX: was missing closing }


def get_platoon_stats(batter_name, pitcher_hand):
    stats = get_batter_stats(name=batter_name)
    if not stats:
        return {}
    batter_hand = str(stats.get("Bats", "R")).strip()
    platoon_advantage = (
        (batter_hand == "L" and pitcher_hand == "R") or
        (batter_hand == "R" and pitcher_hand == "L")
    )  # FIX: was missing closing ) on tuple
    return {
        **get_key_batter_features(name=batter_name),
        "bats_L":      1 if batter_hand == "L" else 0,
        "throws_R":    1 if pitcher_hand == "R" else 0,
        "platoon_adv": 1 if platoon_advantage else 0,
    }  # FIX: was missing closing }


def reload_data():
    _cache.clear()
    _load_all()
    return {"status": "ok", "rows": {k: len(v) for k, v in _cache.items()}}


_load_all()
