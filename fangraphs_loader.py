"""
fangraphs_loader.py
═══════════════════════════════════════════════════════════════════════
Single FanGraphs data layer for MLB Analytics Hub.
Loads CSVs from the data/ folder at startup and caches them.
Falls back to 2025 data when a player has < MIN_IP / MIN_PA in 2026.

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

BAT_PATH       = os.path.join(DATA_DIR, "fg_batting_2026.csv")
PIT_PATH       = os.path.join(DATA_DIR, "fg_pitching_2026.csv")
BAT_PATH_2025  = os.path.join(DATA_DIR, "fg_batting_2025.csv")
PIT_PATH_2025  = os.path.join(DATA_DIR, "fg_pitching_2025.csv")

PROJ_BAT_PATH  = os.path.join(DATA_DIR, "fg_steamer_bat_2026.csv")
PROJ_PIT_PATH  = os.path.join(DATA_DIR, "fg_steamer_pit_2026.csv")
PROJ_BAT_PATH2 = os.path.join(DATA_DIR, "fg_proj_bat_2026.csv")
PROJ_PIT_PATH2 = os.path.join(DATA_DIR, "fg_proj_pit_2026.csv")

# Minimum 2026 sample before we consider the row "real" data
MIN_IP = 5.0   # innings pitched
MIN_PA = 20    # plate appearances

# ── Internal cache (loaded once at first call) ───────────────────────────────
_cache = {}


def _clean_name_html(df: pd.DataFrame, col: str = "Name") -> pd.DataFrame:
    """Strip HTML anchor tags from Name/Team columns FanGraphs returns."""
    if col in df.columns:
        df[col] = df[col].apply(
            lambda x: re.sub(r"<[^>]+>", "", str(x)).strip() if pd.notna(x) else x
        )
    return df


def _try_load(path: str, key: str) -> pd.DataFrame:
    """Load one CSV, clean it, return DataFrame (empty on failure)."""
    try:
        df = pd.read_csv(path, low_memory=False)
        for col in ("Name", "Team", "PlayerName"):
            df = _clean_name_html(df, col)
        if "playerid" in df.columns:
            df["playerid"] = df["playerid"].astype(str).str.strip()
        if "xMLBAMID" in df.columns:
            df["xMLBAMID"] = df["xMLBAMID"].astype(str).str.strip()
        print(f"[FG Loader] {key}: {df.shape[0]} rows, {df.shape[1]} cols")
        return df
    except FileNotFoundError:
        print(f"[FG Loader] WARNING: {path} not found — {key} will be empty")
        return pd.DataFrame()
    except Exception as e:
        print(f"[FG Loader] ERROR loading {key}: {e}")
        return pd.DataFrame()


def _find_proj_path(primary: str, fallback: str) -> str:
    """Return whichever projection path actually exists on disk."""
    if os.path.exists(primary):
        return primary
    if os.path.exists(fallback):
        return fallback
    return primary  # will fail gracefully in _try_load


def _load_all():
    """Load and cache all CSVs. Called once on first access."""
    if _cache:
        return

    _cache["bat"]      = _try_load(BAT_PATH,      "bat_2026")
    _cache["pit"]      = _try_load(PIT_PATH,       "pit_2026")
    _cache["bat_2025"] = _try_load(BAT_PATH_2025,  "bat_2025")
    _cache["pit_2025"] = _try_load(PIT_PATH_2025,  "pit_2025")

    proj_bat = _find_proj_path(PROJ_BAT_PATH, PROJ_BAT_PATH2)
    proj_pit = _find_proj_path(PROJ_PIT_PATH, PROJ_PIT_PATH2)
    _cache["proj_bat"] = _try_load(proj_bat, "proj_bat")
    _cache["proj_pit"] = _try_load(proj_pit, "proj_pit")

    print(f"[FG Loader] All caches ready. Keys: {list(_cache.keys())}")


# ── Name-matching helpers ─────────────────────────────────────────────────────

def _name_col(df: pd.DataFrame) -> str:
    return "PlayerName" if "PlayerName" in df.columns else "Name"


def _find_row(df: pd.DataFrame, name: str = None, player_id: str = None) -> pd.DataFrame:
    """Return matching row(s) from df by id or name (exact then partial)."""
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


def _has_enough_sample(row: pd.Series, ptype: str) -> bool:
    """Return True if 2026 row has enough innings/PA to be reliable."""
    if ptype == "pit":
        ip = 0.0
        for col in ("IP", "ip", "innings_pitched"):
            if col in row.index:
                try:
                    ip = float(row[col])
                    break
                except (ValueError, TypeError):
                    pass
        return ip >= MIN_IP
    else:
        pa = 0
        for col in ("PA", "pa", "plate_appearances"):
            if col in row.index:
                try:
                    pa = int(row[col])
                    break
                except (ValueError, TypeError):
                    pass
        return pa >= MIN_PA


# ── Public accessors ──────────────────────────────────────────────────────────

def get_all_batters() -> pd.DataFrame:
    """Return full 2026 batting stats DataFrame."""
    _load_all()
    return _cache["bat"].copy()


def get_all_pitchers() -> pd.DataFrame:
    """Return full 2026 pitching stats DataFrame."""
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
    Checks 2026 first, then 2025 fallback.
    player_type: 'bat' or 'pit'
    Returns playerid string or None if not found.
    """
    _load_all()
    for key in (("bat" if player_type == "bat" else "pit"),
                ("bat_2025" if player_type == "bat" else "pit_2025")):
        df = _cache.get(key, pd.DataFrame())
        row = _find_row(df, name=name)
        if not row.empty and "playerid" in row.columns:
            return str(row.iloc[0]["playerid"])
    return None


def get_batter_stats(player_id: str = None, name: str = None) -> dict:
    """
    Get full batting stats for one player.
    Returns 2026 data if PA >= MIN_PA, otherwise falls back to 2025.
    """
    _load_all()
    row26 = _find_row(_cache["bat"], player_id=player_id, name=name)
    if not row26.empty:
        r = row26.iloc[0]
        if _has_enough_sample(r, "bat"):
            return r.to_dict()
        # 2026 exists but small sample — merge 2025 as base, overlay 2026
        row25 = _find_row(_cache["bat_2025"], player_id=player_id, name=name)
        if not row25.empty:
            merged = {**row25.iloc[0].to_dict(), **r.to_dict()}
            print(f"[FG Loader] batter '{name or player_id}' using 2025+2026 merged (PA<{MIN_PA})")
            return merged
        return r.to_dict()

    # No 2026 row at all — fall back fully to 2025
    row25 = _find_row(_cache["bat_2025"], player_id=player_id, name=name)
    if not row25.empty:
        print(f"[FG Loader] batter '{name or player_id}' using 2025 fallback (no 2026 row)")
        return row25.iloc[0].to_dict()

    return {}


def get_pitcher_stats(player_id: str = None, name: str = None) -> dict:
    """
    Get full pitching stats for one player.
    Returns 2026 data if IP >= MIN_IP, otherwise falls back to 2025.
    Handles returning pitchers (Lodolo, post-TJ, etc.) gracefully.
    """
    _load_all()
    row26 = _find_row(_cache["pit"], player_id=player_id, name=name)
    if not row26.empty:
        r = row26.iloc[0]
        if _has_enough_sample(r, "pit"):
            return r.to_dict()
        # Small 2026 sample — use 2025 as base, overlay 2026 current-season fields
        row25 = _find_row(_cache["pit_2025"], player_id=player_id, name=name)
        if not row25.empty:
            merged = {**row25.iloc[0].to_dict(), **r.to_dict()}
            print(f"[FG Loader] pitcher '{name or player_id}' using 2025+2026 merged (IP<{MIN_IP})")
            return merged
        return r.to_dict()

    # No 2026 row at all — fall back fully to 2025
    row25 = _find_row(_cache["pit_2025"], player_id=player_id, name=name)
    if not row25.empty:
        print(f"[FG Loader] pitcher '{name or player_id}' using 2025 fallback (no 2026 row)")
        return row25.iloc[0].to_dict()

    return {}


def get_batter_projection(player_id: str = None, name: str = None) -> dict:
    """
    Get Steamer projection for one batter.
    Returns a dict of projected stats, or {} if not found.
    """
    _load_all()
    df = _cache["proj_bat"]
    if df.empty:
        return {}
    row = _find_row(df, player_id=player_id, name=name)
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
    row = _find_row(df, player_id=player_id, name=name)
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
