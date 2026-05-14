"""
fangraphs_loader.py — Single FanGraphs data layer for MLB Analytics Hub.
"""

import os, re
import threading
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

BAT_PATH      = os.path.join(DATA_DIR, "fg_batting_2026.csv")
PIT_PATH      = os.path.join(DATA_DIR, "fg_pitching_2026.csv")
BAT_PATH_2025 = os.path.join(DATA_DIR, "fg_batting_2025.csv")
PIT_PATH_2025 = os.path.join(DATA_DIR, "fg_pitching_2025.csv")
BAT_PATH_2024 = os.path.join(DATA_DIR, "fg_batting_2024.csv")
PIT_PATH_2024 = os.path.join(DATA_DIR, "fg_pitching_2024.csv")
BAT_PATH_2023 = os.path.join(DATA_DIR, "fg_batting_2023.csv")
PIT_PATH_2023 = os.path.join(DATA_DIR, "fg_pitching_2023.csv")
BAT_PATH_2022 = os.path.join(DATA_DIR, "fg_batting_2022.csv")
PIT_PATH_2022 = os.path.join(DATA_DIR, "fg_pitching_2022.csv")
BAT_PATH_2021 = os.path.join(DATA_DIR, "fg_batting_2021.csv")
PIT_PATH_2021 = os.path.join(DATA_DIR, "fg_pitching_2021.csv")

PROJ_BAT_PATH  = os.path.join(DATA_DIR, "fg_steamer_bat_2026.csv")
PROJ_PIT_PATH  = os.path.join(DATA_DIR, "fg_steamer_pit_2026.csv")
PROJ_BAT_PATH2 = os.path.join(DATA_DIR, "fg_proj_bat_2026.csv")
PROJ_PIT_PATH2 = os.path.join(DATA_DIR, "fg_proj_pit_2026.csv")

PROJ_BAT_PATH_2025  = os.path.join(DATA_DIR, "fg_steamer_bat_2025.csv")
PROJ_PIT_PATH_2025  = os.path.join(DATA_DIR, "fg_steamer_pit_2025.csv")
PROJ_BAT_PATH_2024  = os.path.join(DATA_DIR, "fg_steamer_bat_2024.csv")
PROJ_PIT_PATH_2024  = os.path.join(DATA_DIR, "fg_steamer_pit_2024.csv")
PROJ_BAT_PATH_2023  = os.path.join(DATA_DIR, "fg_steamer_bat_2023.csv")
PROJ_PIT_PATH_2023  = os.path.join(DATA_DIR, "fg_steamer_pit_2023.csv")
PROJ_BAT_PATH_2022  = os.path.join(DATA_DIR, "fg_steamer_bat_2022.csv")
PROJ_PIT_PATH_2022  = os.path.join(DATA_DIR, "fg_steamer_pit_2022.csv")
PROJ_BAT_PATH_2021  = os.path.join(DATA_DIR, "fg_steamer_bat_2021.csv")
PROJ_PIT_PATH_2021  = os.path.join(DATA_DIR, "fg_steamer_pit_2021.csv")

# Ordered fallback chains: most recent -> oldest
BAT_SEASON_PATHS = [
    ("bat",      BAT_PATH,      "bat_2026"),
    ("bat_2025", BAT_PATH_2025, "bat_2025"),
    ("bat_2024", BAT_PATH_2024, "bat_2024"),
    ("bat_2023", BAT_PATH_2023, "bat_2023"),
    ("bat_2022", BAT_PATH_2022, "bat_2022"),
    ("bat_2021", BAT_PATH_2021, "bat_2021"),
]
PIT_SEASON_PATHS = [
    ("pit",      PIT_PATH,      "pit_2026"),
    ("pit_2025", PIT_PATH_2025, "pit_2025"),
    ("pit_2024", PIT_PATH_2024, "pit_2024"),
    ("pit_2023", PIT_PATH_2023, "pit_2023"),
    ("pit_2022", PIT_PATH_2022, "pit_2022"),
    ("pit_2021", PIT_PATH_2021, "pit_2021"),
]

# Steamer projection fallback chains: most recent -> oldest
PROJ_BAT_PATHS = [
    ("proj_bat",      PROJ_BAT_PATH,      PROJ_BAT_PATH2,      "proj_bat_2026"),
    ("proj_bat_2025", PROJ_BAT_PATH_2025, None,                 "proj_bat_2025"),
    ("proj_bat_2024", PROJ_BAT_PATH_2024, None,                 "proj_bat_2024"),
    ("proj_bat_2023", PROJ_BAT_PATH_2023, None,                 "proj_bat_2023"),
    ("proj_bat_2022", PROJ_BAT_PATH_2022, None,                 "proj_bat_2022"),
    ("proj_bat_2021", PROJ_BAT_PATH_2021, None,                 "proj_bat_2021"),
]
PROJ_PIT_PATHS = [
    ("proj_pit",      PROJ_PIT_PATH,      PROJ_PIT_PATH2,      "proj_pit_2026"),
    ("proj_pit_2025", PROJ_PIT_PATH_2025, None,                 "proj_pit_2025"),
    ("proj_pit_2024", PROJ_PIT_PATH_2024, None,                 "proj_pit_2024"),
    ("proj_pit_2023", PROJ_PIT_PATH_2023, None,                 "proj_pit_2023"),
    ("proj_pit_2022", PROJ_PIT_PATH_2022, None,                 "proj_pit_2022"),
    ("proj_pit_2021", PROJ_PIT_PATH_2021, None,                 "proj_pit_2021"),
]

MIN_IP = 5.0
MIN_PA = 20

_cache: dict = {}
_load_lock = threading.Lock()


def _clean_name_html(df, col="Name"):
    if col in df.columns:
        df[col] = df[col].apply(
            lambda x: re.sub(r"<[^>]+>", "", str(x)).strip() if pd.notna(x) else x
        )
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
    if fallback and os.path.exists(fallback):
        return fallback
    return primary


def _load_all():
    # Fast path — no lock needed; dict bool check is atomic in CPython
    if _cache:
        return
    with _load_lock:
        # Double-checked: another thread may have loaded while we waited
        if _cache:
            return
        for cache_key, path, label in BAT_SEASON_PATHS:
            _cache[cache_key] = _try_load(path, label)
        for cache_key, path, label in PIT_SEASON_PATHS:
            _cache[cache_key] = _try_load(path, label)
        for cache_key, primary, fallback, label in PROJ_BAT_PATHS:
            path = _find_proj_path(primary, fallback)
            _cache[cache_key] = _try_load(path, label)
        for cache_key, primary, fallback, label in PROJ_PIT_PATHS:
            path = _find_proj_path(primary, fallback)
            _cache[cache_key] = _try_load(path, label)


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
    season_keys = (
        [k for k, _, _ in BAT_SEASON_PATHS]
        if player_type == "bat"
        else [k for k, _, _ in PIT_SEASON_PATHS]
    )
    for key in season_keys:
        df = _cache.get(key, pd.DataFrame())
        row = _find_row(df, name=name)
        if not row.empty and "playerid" in row.columns:
            return str(row.iloc[0]["playerid"])
    return None


def _get_stats_with_fallback(cache_season_keys, ptype, player_id=None, name=None):
    """Walk through season cache keys newest->oldest, return first row with enough sample."""
    first_row = None
    for key in cache_season_keys:
        df = _cache.get(key, pd.DataFrame())
        row = _find_row(df, player_id=player_id, name=name)
        if row.empty:
            continue
        r = row.iloc[0]
        if first_row is None:
            first_row = r
        if _has_enough_sample(r, ptype):
            # Merge: older season as base, newer season on top (newer wins conflicts)
            if first_row is not r:
                return {**r.to_dict(), **first_row.to_dict()}
            return r.to_dict()
    if first_row is not None:
        return first_row.to_dict()
    return {}


def _get_proj_with_fallback(proj_cache_keys, player_id=None, name=None):
    """Walk projection cache keys newest->oldest, return first match found."""
    for key in proj_cache_keys:
        df = _cache.get(key, pd.DataFrame())
        row = _find_row(df, player_id=player_id, name=name)
        if not row.empty:
            return row.iloc[0].to_dict()
    return {}


def get_batter_stats(player_id=None, name=None):
    _load_all()
    bat_keys = [k for k, _, _ in BAT_SEASON_PATHS]
    return _get_stats_with_fallback(bat_keys, "bat", player_id=player_id, name=name)


def get_pitcher_stats(player_id=None, name=None):
    _load_all()
    pit_keys = [k for k, _, _ in PIT_SEASON_PATHS]
    return _get_stats_with_fallback(pit_keys, "pit", player_id=player_id, name=name)


def get_batter_projection(player_id=None, name=None):
    _load_all()
    proj_keys = [k for k, _, _, _ in PROJ_BAT_PATHS]
    return _get_proj_with_fallback(proj_keys, player_id=player_id, name=name)


def get_pitcher_projection(player_id=None, name=None):
    _load_all()
    proj_keys = [k for k, _, _, _ in PROJ_PIT_PATHS]
    return _get_proj_with_fallback(proj_keys, player_id=player_id, name=name)


def get_key_batter_features(player_id=None, name=None):
    stats = get_batter_stats(player_id=player_id, name=name)
    if not stats:
        return {}
    return {
        "sv_xba":       stats.get("xAVG",       0.250),
        "sv_xwoba":     stats.get("xwOBA",       0.320),
        "sv_xslg":      stats.get("xSLG",        0.400),
        "sv_ev":        stats.get("EV",           88.0),
        "sv_brl_pct":   stats.get("Barrel%",      4.0),
        "sv_hh_pct":    stats.get("HardHit%",     35.0),
        "sv_ss_pct":    stats.get("SwStr%",       10.0),
        "sv_la":        stats.get("LA",           12.0),
        "sv_k_pct":     stats.get("K%",           22.0),
        "sv_bb_pct":    stats.get("BB%",            8.0),
        "sv_woba":      stats.get("wOBA",          0.320),
        "sv_wrc_plus":  stats.get("wRC+",         100.0),
        "sv_iso":       stats.get("ISO",           0.150),
        "sv_babip":     stats.get("BABIP",         0.300),
        "sv_o_swing":   stats.get("O-Swing%",      30.0),
        "sv_z_contact": stats.get("Z-Contact%",    85.0),
        "sv_o_contact": stats.get("O-Contact%",    65.0),
        "sv_f_strike":  stats.get("F-Strike%",     60.0),
        "sv_pull_pct":  stats.get("Pull%",         40.0),
        "sv_cent_pct":  stats.get("Cent%",         35.0),
        "sv_oppo_pct":  stats.get("Oppo%",         25.0),
        "sv_gb_pct":    stats.get("GB%",           45.0),
        "sv_fb_pct":    stats.get("FB%",           35.0),
        "sv_ld_pct":    stats.get("LD%",           20.0),
        "sv_sprint":    stats.get("Sprint Speed",  27.0),
        "sv_pa":        stats.get("PA",             0),
    }


def get_key_pitcher_features(player_id=None, name=None):
    stats = get_pitcher_stats(player_id=player_id, name=name)
    if not stats:
        return {}
    return {
        "sv_era":       stats.get("ERA",        4.00),
        "sv_fip":       stats.get("FIP",        4.00),
        "sv_xfip":      stats.get("xFIP",       4.00),
        "sv_k_pct":     stats.get("K%",         20.0),
        "sv_bb_pct":    stats.get("BB%",          8.0),
        "sv_hr9":       stats.get("HR/9",         1.2),
        "sv_lob_pct":   stats.get("LOB%",        72.0),
        "sv_gb_pct":    stats.get("GB%",         45.0),
        "sv_fb_pct":    stats.get("FB%",         35.0),
        "sv_ld_pct":    stats.get("LD%",         20.0),
        "sv_babip":     stats.get("BABIP",        0.300),
        "sv_woba":      stats.get("wOBA",         0.320),
        "sv_xwoba":     stats.get("xwOBA",        0.320),
        "sv_ip":        stats.get("IP",            0),
        "sv_swstr_pct": stats.get("SwStr%",       10.0),
        "sv_f_strike":  stats.get("F-Strike%",    60.0),
        "sv_o_swing":   stats.get("O-Swing%",     30.0),
        "sv_z_contact": stats.get("Z-Contact%",   85.0),
        "sv_velocity":  stats.get("vFB",           93.0),
        "sv_spin":      stats.get("Spin Rate",   2200.0),
    }
