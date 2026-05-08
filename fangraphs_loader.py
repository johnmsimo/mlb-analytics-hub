"""
fangraphs_loader.py
═══════════════════════════════════════════════════════════════════════
Single FanGraphs data layer for MLB Analytics Hub.
Loads all 8 CSVs (2026 + 2025 fallback) at startup and caches them.
All app modules (props, NRFI, HR analytics, deep dive, BvP) import
from here — fallback chain is automatic and app-wide.

FALLBACK CHAIN (applied everywhere automatically):
    1. 2026 season stats        → fg_batting/pitching_2026.csv
    2. Steamer 2026 projections → fg_steamer_bat/pit_2026.csv
    3. 2025 season stats        → fg_batting/pitching_2025.csv
    4. Steamer 2025 projections → fg_steamer_bat/pit_2025.csv

Each lookup result includes a 'data_source' key indicating which
tier was used — the UI can surface this as a badge.

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
import logging
import pandas as pd
from functools import lru_cache

log = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────────────
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

# 2026
BAT_PATH          = os.path.join(DATA_DIR, "fg_batting_2026.csv")
PIT_PATH          = os.path.join(DATA_DIR, "fg_pitching_2026.csv")
PROJ_BAT_PATH     = os.path.join(DATA_DIR, "fg_steamer_bat_2026.csv")
PROJ_PIT_PATH     = os.path.join(DATA_DIR, "fg_steamer_pit_2026.csv")
# 2025 fallback
BAT_PATH_2025     = os.path.join(DATA_DIR, "fg_batting_2025.csv")
PIT_PATH_2025     = os.path.join(DATA_DIR, "fg_pitching_2025.csv")
PROJ_BAT_PATH_2025 = os.path.join(DATA_DIR, "fg_steamer_bat_2025.csv")
PROJ_PIT_PATH_2025 = os.path.join(DATA_DIR, "fg_steamer_pit_2025.csv")

# ── Internal cache ────────────────────────────────────────────────────────────
_cache: dict = {}

# ── Data source labels (returned in every result dict) ────────────────────────
SRC_2026       = "2026 Stats"
SRC_STEAMER26  = "Steamer 2026"
SRC_2025       = "2025 Stats"
SRC_STEAMER25  = "Steamer 2025"
SRC_NONE       = "No Data"


def _clean_name_html(df: pd.DataFrame, col: str = "Name") -> pd.DataFrame:
    """Strip HTML anchor tags FanGraphs sometimes injects into Name columns."""
    if col in df.columns:
        df[col] = df[col].apply(
            lambda x: re.sub(r"<[^>]+>", "", str(x)).strip() if pd.notna(x) else x
        )
    return df


def _load_csv(key: str, path: str) -> None:
    """Load one CSV into _cache[key]. Prints a status line."""
    try:
        df = pd.read_csv(path, low_memory=False)
        for col in ("Name", "Team", "PlayerName"):
            df = _clean_name_html(df, col)
        if "playerid" in df.columns:
            df["playerid"] = df["playerid"].astype(str).str.strip()
        if "xMLBAMID" in df.columns:
            df["xMLBAMID"] = df["xMLBAMID"].astype(str).str.strip()
        _cache[key] = df
        print(f"[FG] {key}: {df.shape[0]} rows, {df.shape[1]} cols")
    except FileNotFoundError:
        print(f"[FG Loader] WARNING: {path} not found — {key} will be empty")
        _cache[key] = pd.DataFrame()
    except Exception as e:
        print(f"[FG Loader] ERROR loading {key}: {e}")
        _cache[key] = pd.DataFrame()


def _load_all() -> None:
    """Load and cache all 8 CSVs once at first access."""
    if _cache:
        return
    _load_csv("bat",          BAT_PATH)
    _load_csv("pit",          PIT_PATH)
    _load_csv("proj_bat",     PROJ_BAT_PATH)
    _load_csv("proj_pit",     PROJ_PIT_PATH)
    _load_csv("bat_2025",     BAT_PATH_2025)
    _load_csv("pit_2025",     PIT_PATH_2025)
    _load_csv("proj_bat_2025", PROJ_BAT_PATH_2025)
    _load_csv("proj_pit_2025", PROJ_PIT_PATH_2025)

    # Summary
    loaded   = [k for k, v in _cache.items() if not v.empty]
    missing  = [k for k, v in _cache.items() if v.empty]
    print(f"[FG] Done — loaded: {loaded}")
    if missing:
        print(f"[FG] Missing (will use fallback): {missing}")


# ── Public DataFrame accessors ────────────────────────────────────────────────

def get_all_batters() -> pd.DataFrame:
    _load_all(); return _cache["bat"].copy()

def get_all_pitchers() -> pd.DataFrame:
    _load_all(); return _cache["pit"].copy()

def get_all_projections_bat() -> pd.DataFrame:
    _load_all(); return _cache["proj_bat"].copy()

def get_all_projections_pit() -> pd.DataFrame:
    _load_all(); return _cache["proj_pit"].copy()

def get_all_batters_2025() -> pd.DataFrame:
    _load_all(); return _cache["bat_2025"].copy()

def get_all_pitchers_2025() -> pd.DataFrame:
    _load_all(); return _cache["pit_2025"].copy()


# ── Internal row finder ───────────────────────────────────────────────────────

def _find_row(df: pd.DataFrame, player_id: str = None, name: str = None) -> pd.Series | None:
    """
    Find a single player row in a DataFrame.
    Tries playerid exact match first, then name exact, then name partial.
    Returns the first matching Series or None.
    """
    if df is None or df.empty:
        return None

    # By ID
    if player_id:
        pid = str(player_id).strip()
        if "playerid" in df.columns:
            match = df[df["playerid"] == pid]
            if not match.empty:
                return match.iloc[0]

    # By name
    if name:
        name_col = "PlayerName" if "PlayerName" in df.columns else (
                   "Name"       if "Name"       in df.columns else None)
        if name_col:
            nl = name.lower().strip()
            # exact
            match = df[df[name_col].str.lower().str.strip() == nl]
            if not match.empty:
                return match.iloc[0]
            # partial
            match = df[df[name_col].str.lower().str.contains(nl, na=False)]
            if not match.empty:
                return match.iloc[0]

    return None


# ── Public ID finder ──────────────────────────────────────────────────────────

def find_player_id(name: str, player_type: str = "bat") -> str | None:
    """
    Fuzzy-find a FanGraphs playerid by name.
    Searches 2026 first, falls back to 2025.
    player_type: 'bat' or 'pit'
    """
    _load_all()
    keys = ("bat", "bat_2025") if player_type == "bat" else ("pit", "pit_2025")
    for key in keys:
        row = _find_row(_cache[key], name=name)
        if row is not None and "playerid" in row:
            return str(row["playerid"])
    return None


# ── Core stat lookups with full fallback chain ────────────────────────────────

def get_pitcher_stats(player_id: str = None, name: str = None) -> dict:
    """
    Get pitching stats with full fallback chain.
    Returned dict always contains 'data_source' key.
    Chain: 2026 stats → Steamer 2026 → 2025 stats → Steamer 2025
    """
    _load_all()

    # Resolve ID if only name given (search 2026 first, then 2025)
    if not player_id and name:
        player_id = find_player_id(name, "pit")

    # 1 — 2026 season stats
    row = _find_row(_cache["pit"], player_id=player_id, name=name)
    if row is not None:
        return {**row.to_dict(), "data_source": SRC_2026}

    # 2 — Steamer 2026 projections
    row = _find_row(_cache["proj_pit"], player_id=player_id, name=name)
    if row is not None:
        return {**row.to_dict(), "data_source": SRC_STEAMER26}

    # 3 — 2025 season stats
    row = _find_row(_cache["pit_2025"], player_id=player_id, name=name)
    if row is not None:
        return {**row.to_dict(), "data_source": SRC_2025}

    # 4 — Steamer 2025 projections
    row = _find_row(_cache["proj_pit_2025"], player_id=player_id, name=name)
    if row is not None:
        return {**row.to_dict(), "data_source": SRC_STEAMER25}

    return {"data_source": SRC_NONE}


def get_batter_stats(player_id: str = None, name: str = None) -> dict:
    """
    Get batting stats with full fallback chain.
    Returned dict always contains 'data_source' key.
    Chain: 2026 stats → Steamer 2026 → 2025 stats → Steamer 2025
    """
    _load_all()

    if not player_id and name:
        player_id = find_player_id(name, "bat")

    # 1 — 2026 season stats
    row = _find_row(_cache["bat"], player_id=player_id, name=name)
    if row is not None:
        return {**row.to_dict(), "data_source": SRC_2026}

    # 2 — Steamer 2026 projections
    row = _find_row(_cache["proj_bat"], player_id=player_id, name=name)
    if row is not None:
        return {**row.to_dict(), "data_source": SRC_STEAMER26}

    # 3 — 2025 season stats
    row = _find_row(_cache["bat_2025"], player_id=player_id, name=name)
    if row is not None:
        return {**row.to_dict(), "data_source": SRC_2025}

    # 4 — Steamer 2025 projections
    row = _find_row(_cache["proj_bat_2025"], player_id=player_id, name=name)
    if row is not None:
        return {**row.to_dict(), "data_source": SRC_STEAMER25}

    return {"data_source": SRC_NONE}


def get_pitcher_projection(player_id: str = None, name: str = None) -> dict:
    """
    Get Steamer projection for a pitcher.
    Tries 2026 projection first, falls back to 2025 projection.
    """
    _load_all()

    if not player_id and name:
        player_id = find_player_id(name, "pit")

    row = _find_row(_cache["proj_pit"], player_id=player_id, name=name)
    if row is not None:
        return {**row.to_dict(), "data_source": SRC_STEAMER26}

    row = _find_row(_cache["proj_pit_2025"], player_id=player_id, name=name)
    if row is not None:
        return {**row.to_dict(), "data_source": SRC_STEAMER25}

    return {"data_source": SRC_NONE}


def get_batter_projection(player_id: str = None, name: str = None) -> dict:
    """
    Get Steamer projection for a batter.
    Tries 2026 projection first, falls back to 2025 projection.
    """
    _load_all()

    if not player_id and name:
        player_id = find_player_id(name, "bat")

    row = _find_row(_cache["proj_bat"], player_id=player_id, name=name)
    if row is not None:
        return {**row.to_dict(), "data_source": SRC_STEAMER26}

    row = _find_row(_cache["proj_bat_2025"], player_id=player_id, name=name)
    if row is not None:
        return {**row.to_dict(), "data_source": SRC_STEAMER25}

    return {"data_source": SRC_NONE}


# ── Feature builders (XGBoost / prop scorer) ──────────────────────────────────

def get_key_batter_features(player_id: str = None, name: str = None) -> dict:
    """
    Returns the most important XGBoost features for a batter.
    Automatically uses fallback chain — works for any active player.
    Includes 'data_source' so callers know which tier was used.
    """
    stats = get_batter_stats(player_id=player_id, name=name)
    if stats.get("data_source") == SRC_NONE:
        return {"data_source": SRC_NONE}

    return {
        "data_source":   stats["data_source"],
        "sv_xba":        stats.get("xAVG",       0.250),
        "sv_xwoba":      stats.get("xwOBA",      0.320),
        "sv_xslg":       stats.get("xSLG",       0.400),
        "sv_ev":         stats.get("EV",          88.0),
        "sv_brl_pct":    stats.get("Barrel%",     4.0),
        "sv_hh_pct":     stats.get("HardHit%",   35.0),
        "sv_ss_pct":     stats.get("SwStr%",      10.0),
        "sv_la":         stats.get("LA",          12.0),
        "sv_k_pct":      stats.get("K%",          22.0),
        "sv_bb_pct":     stats.get("BB%",          8.0),
        "sv_woba":       stats.get("wOBA",        0.320),
        "sv_wrc_plus":   stats.get("wRC+",       100.0),
        "sv_iso":        stats.get("ISO",         0.150),
        "sv_babip":      stats.get("BABIP",       0.300),
        "sv_o_swing":    stats.get("O-Swing%",   30.0),
        "sv_z_contact":  stats.get("Z-Contact%", 85.0),
        "sv_pull_pct":   stats.get("Pull%",       40.0),
        "sv_hard_pct":   stats.get("Hard%",       35.0),
    }


def get_key_pitcher_features(player_id: str = None, name: str = None) -> dict:
    """
    Returns the most important XGBoost features for a pitcher.
    Automatically uses fallback chain — works for IL players, debut pitchers, etc.
    Includes 'data_source' so callers know which tier was used.
    """
    stats = get_pitcher_stats(player_id=player_id, name=name)
    if stats.get("data_source") == SRC_NONE:
        return {"data_source": SRC_NONE}

    return {
        "data_source":  stats["data_source"],
        "opp_xera":     stats.get("xERA",   4.50),
        "opp_era":      stats.get("ERA",    4.50),
        "opp_k_pct":    stats.get("K%",     22.0),
        "opp_bb_pct":   stats.get("BB%",     8.0),
        "opp_whiff":    stats.get("SwStr%", 24.0),
        "opp_fip":      stats.get("FIP",    4.20),
        "opp_xfip":     stats.get("xFIP",   4.20),
        "opp_siera":    stats.get("SIERA",  4.20),
        "opp_hr_fb":    stats.get("HR/FB",  12.0),
        "opp_gb_pct":   stats.get("GB%",    45.0),
    }


# ── Platoon helper ────────────────────────────────────────────────────────────

def get_platoon_stats(batter_name: str, pitcher_hand: str) -> dict:
    """
    Returns batter features with platoon context.
    Uses fallback chain automatically.
    pitcher_hand: 'L' or 'R'
    """
    stats = get_batter_stats(name=batter_name)
    if stats.get("data_source") == SRC_NONE:
        return {"data_source": SRC_NONE}

    batter_hand = str(stats.get("Bats", "R")).strip()
    platoon_adv = (
        (batter_hand == "L" and pitcher_hand == "R") or
        (batter_hand == "R" and pitcher_hand == "L")
    )

    return {
        **get_key_batter_features(name=batter_name),
        "bats_L":      1 if batter_hand == "L" else 0,
        "throws_R":    1 if pitcher_hand == "R" else 0,
        "platoon_adv": 1 if platoon_adv else 0,
    }


# ── Admin ─────────────────────────────────────────────────────────────────────

def reload_data() -> dict:
    """Force reload all CSVs from disk (call after updating data files)."""
    _cache.clear()
    _load_all()
    return {"status": "ok", "rows": {k: len(v) for k, v in _cache.items()}}


def get_data_source(player_name: str, player_type: str = "pit") -> str:
    """
    Quick utility — returns just the data_source string for a player.
    Useful for UI badges: 'data_source' in ['2026 Stats', 'Steamer 2026',
    '2025 Stats', 'Steamer 2025', 'No Data']
    """
    if player_type == "pit":
        return get_pitcher_stats(name=player_name).get("data_source", SRC_NONE)
    return get_batter_stats(name=player_name).get("data_source", SRC_NONE)


# ── Auto-load on import ───────────────────────────────────────────────────────
_load_all()
