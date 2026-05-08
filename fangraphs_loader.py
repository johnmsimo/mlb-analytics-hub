"""
fangraphs_loader.py
═══════════════════════════════════════════════════════════════════════
Single FanGraphs data layer for MLB Analytics Hub.
Loads all 4 CSVs from the data/ folder at startup and caches them.
All app modules (props, NRFI, HR analytics, deep dive, BvP) should
import from here instead of reading CSV files directly.

USAGE:
    from fangraphs_loader import (
        get_batter_stats,
        get_pitcher_stats,
        get_batter_projection,
        get_pitcher_projection,
        get_all_batters,
        get_all_pitchers,
        find_player_id,
        get_platoon_stats,
    )
═══════════════════════════════════════════════════════════════════════
"""

import os
import re
import pandas as pd
from functools import lru_cache

# ── Paths ────────────────────────────────────────────────────────────────────
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

BAT_PATH     = os.path.join(DATA_DIR, "fg_batting_2026.csv")
PIT_PATH     = os.path.join(DATA_DIR, "fg_pitching_2026.csv")
PROJ_BAT_PATH = os.path.join(DATA_DIR, "fg_steamer_bat_2026.csv")
PROJ_PIT_PATH = os.path.join(DATA_DIR, "fg_steamer_pit_2026.csv")

# ── Internal cache (loaded once at first call) ────────────────────────────────
_cache = {}

def _clean_name_html(df: pd.DataFrame, col: str = "Name") -> pd.DataFrame:
    """Strip HTML anchor tags from Name/Team columns FanGraphs returns."""
    if col in df.columns:
        df[col] = df[col].apply(
            lambda x: re.sub(r"<[^>]+>", "", str(x)).strip() if pd.notna(x) else x
        )
    return df

def _load_all():
    """Load and cache all 4 CSVs. Called once on first access."""
    if _cache:
        return

    for key, path in [
        ("bat",      BAT_PATH),
        ("pit",      PIT_PATH),
        ("proj_bat", PROJ_BAT_PATH),
        ("proj_pit", PROJ_PIT_PATH),
    ]:
        try:
            df = pd.read_csv(path, low_memory=False)
            # Clean HTML from name/team columns
            for col in ("Name", "Team", "PlayerName"):
                df = _clean_name_html(df, col)
            # Normalise playerid to string for consistent merging
            if "playerid" in df.columns:
                df["playerid"] = df["playerid"].astype(str).str.strip()
            if "xMLBAMID" in df.columns:
                df["xMLBAMID"] = df["xMLBAMID"].astype(str).str.strip()
            _cache[key] = df
            print(f"[FG Loader] {key}: {df.shape[0]} rows, {df.shape[1]} cols")
        except FileNotFoundError:
            print(f"[FG Loader] WARNING: {path} not found — {key} will be empty")
            _cache[key] = pd.DataFrame()
        except Exception as e:
            print(f"[FG Loader] ERROR loading {key}: {e}")
            _cache[key] = pd.DataFrame()

# ── Public accessors ──────────────────────────────────────────────────────────

def get_all_batters() -> pd.DataFrame:
    """Return full batting stats DataFrame (475 columns)."""
    _load_all()
    return _cache["bat"].copy()

def get_all_pitchers() -> pd.DataFrame:
    """Return full pitching stats DataFrame."""
    _load_all()
    return _cache["pit"].copy()

def get_all_projections_bat() -> pd.DataFrame:
    """Return full Steamer batting projections DataFrame."""
    _load_all()
    return _cache["proj_bat"].copy()

def get_all_projections_pit() -> pd.DataFrame:
    """Return full Steamer pitching projections DataFrame."""
    _load_all()
    return _cache["proj_pit"].copy()


def find_player_id(name: str, player_type: str = "bat") -> str | None:
    """
    Fuzzy-find a FanGraphs playerid by player name.
    player_type: 'bat' or 'pit'
    Returns playerid string or None if not found.
    """
    _load_all()
    key = "bat" if player_type == "bat" else "pit"
    df = _cache[key]
    if df.empty:
        return None

    name_col = "PlayerName" if "PlayerName" in df.columns else "Name"
    name_lower = name.lower().strip()

    # Exact match first
    match = df[df[name_col].str.lower().str.strip() == name_lower]
    if not match.empty:
        return str(match.iloc[0]["playerid"])

    # Partial match fallback
    match = df[df[name_col].str.lower().str.contains(name_lower, na=False)]
    if not match.empty:
        return str(match.iloc[0]["playerid"])

    return None


def get_batter_stats(player_id: str = None, name: str = None) -> dict:
    """
    Get full batting stats for one player.
    Pass either playerid (string) or name (string).
    Returns a dict of all stat columns, or {} if not found.
    """
    _load_all()
    df = _cache["bat"]
    if df.empty:
        return {}

    if player_id:
        row = df[df["playerid"] == str(player_id)]
    elif name:
        pid = find_player_id(name, "bat")
        row = df[df["playerid"] == pid] if pid else pd.DataFrame()
    else:
        return {}

    if row.empty:
        return {}
    return row.iloc[0].to_dict()


def get_pitcher_stats(player_id: str = None, name: str = None) -> dict:
    """
    Get full pitching stats for one player.
    Pass either playerid (string) or name (string).
    Returns a dict of all stat columns, or {} if not found.
    """
    _load_all()
    df = _cache["pit"]
    if df.empty:
        return {}

    if player_id:
        row = df[df["playerid"] == str(player_id)]
    elif name:
        pid = find_player_id(name, "pit")
        row = df[df["playerid"] == pid] if pid else pd.DataFrame()
    else:
        return {}

    if row.empty:
        return {}
    return row.iloc[0].to_dict()


def get_batter_projection(player_id: str = None, name: str = None) -> dict:
    """
    Get Steamer projection for one batter.
    Returns a dict of projected stats, or {} if not found.
    """
    _load_all()
    df = _cache["proj_bat"]
    if df.empty:
        return {}

    id_col = "playerid" if "playerid" in df.columns else (
        "minpos" if "minpos" in df.columns else None
    )

    if player_id and id_col:
        row = df[df[id_col].astype(str) == str(player_id)]
    elif name:
        name_col = "PlayerName" if "PlayerName" in df.columns else "Name"
        if name_col in df.columns:
            row = df[df[name_col].str.lower().str.strip() == name.lower().strip()]
        else:
            return {}
    else:
        return {}

    if row.empty:
        return {}
    return row.iloc[0].to_dict()


def get_pitcher_projection(player_id: str = None, name: str = None) -> dict:
    """
    Get Steamer projection for one pitcher.
    Returns a dict of projected stats, or {} if not found.
    """
    _load_all()
    df = _cache["proj_pit"]
    if df.empty:
        return {}

    id_col = "playerid" if "playerid" in df.columns else None

    if player_id and id_col:
        row = df[df[id_col].astype(str) == str(player_id)]
    elif name:
        name_col = "PlayerName" if "PlayerName" in df.columns else "Name"
        if name_col in df.columns:
            row = df[df[name_col].str.lower().str.strip() == name.lower().strip()]
        else:
            return {}
    else:
        return {}

    if row.empty:
        return {}
    return row.iloc[0].to_dict()


def get_key_batter_features(player_id: str = None, name: str = None) -> dict:
    """
    Returns only the most important features for XGBoost model scoring.
    Matches the HITS_FEATURES list in xgb_prop_scorer.py.
    """
    stats = get_batter_stats(player_id=player_id, name=name)
    if not stats:
        return {}

    return {
        "sv_xba":       stats.get("xAVG", 0.250),
        "sv_xwoba":     stats.get("xwOBA", 0.320),
        "sv_xslg":      stats.get("xSLG", 0.400),
        "sv_ev":        stats.get("EV", 88.0),
        "sv_brl_pct":   stats.get("Barrel%", 4.0),
        "sv_hh_pct":    stats.get("HardHit%", 35.0),
        "sv_ss_pct":    stats.get("SwStr%", 10.0),
        "sv_la":        stats.get("LA", 12.0),
        "sv_k_pct":     stats.get("K%", 22.0),
        "sv_bb_pct":    stats.get("BB%", 8.0),
        "sv_woba":      stats.get("wOBA", 0.320),
        "sv_wrc_plus":  stats.get("wRC+", 100.0),
        "sv_iso":       stats.get("ISO", 0.150),
        "sv_babip":     stats.get("BABIP", 0.300),
        "sv_o_swing":   stats.get("O-Swing%", 30.0),
        "sv_z_contact": stats.get("Z-Contact%", 85.0),
        "sv_pull_pct":  stats.get("Pull%", 40.0),
        "sv_hard_pct":  stats.get("Hard%", 35.0),
    }


def get_key_pitcher_features(player_id: str = None, name: str = None) -> dict:
    """
    Returns only the most important features for XGBoost K model scoring.
    Matches the K_FEATURES list in xgb_prop_scorer.py.
    """
    stats = get_pitcher_stats(player_id=player_id, name=name)
    if not stats:
        return {}

    return {
        "opp_xera":    stats.get("xERA", 4.50),
        "opp_era":     stats.get("ERA", 4.50),
        "opp_k_pct":   stats.get("K%", 22.0),
        "opp_bb_pct":  stats.get("BB%", 8.0),
        "opp_whiff":   stats.get("SwStr%", 24.0),
        "opp_fip":     stats.get("FIP", 4.20),
        "opp_xfip":    stats.get("xFIP", 4.20),
        "opp_siera":   stats.get("SIERA", 4.20),
        "opp_hr_fb":   stats.get("HR/FB", 12.0),
        "opp_gb_pct":  stats.get("GB%", 45.0),
    }


def get_platoon_stats(batter_name: str, pitcher_hand: str) -> dict:
    """
    Returns batter stats filtered by pitcher handedness context.
    pitcher_hand: 'L' or 'R'
    Uses season stats as proxy (full splits require separate FG splits pull).
    """
    stats = get_batter_stats(name=batter_name)
    if not stats:
        return {}

    batter_hand = str(stats.get("Bats", "R")).strip()
    platoon_advantage = (
        (batter_hand == "L" and pitcher_hand == "R") or
        (batter_hand == "R" and pitcher_hand == "L")
    )

    return {
        **get_key_batter_features(name=batter_name),
        "bats_L":      1 if batter_hand == "L" else 0,
        "throws_R":    1 if pitcher_hand == "R" else 0,
        "platoon_adv": 1 if platoon_advantage else 0,
    }


def reload_data():
    """Force reload all CSVs from disk (call after updating data files)."""
    _cache.clear()
    _load_all()
    return {"status": "ok", "rows": {k: len(v) for k, v in _cache.items()}}


# ── Auto-load on import ───────────────────────────────────────────────────────
_load_all()
