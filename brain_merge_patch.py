# =============================================================================
# BRAIN UPLOAD → LIVE STAT CACHE MERGE  (paste this block into app.py)
#
# WHERE TO PASTE:
#   • This entire block: right after the existing fg_batter / fg_pitcher /
#     sv_batter / sv_pitcher functions (replace those 4 functions with the
#     patched versions at the bottom of this file).
#
# TWO ADDITIONAL WIRING LINES (described at bottom):
#   1. In the startup block near _maybe_refresh_fg() call:
#        load_brain_overlays()
#   2. Inside /api/brain/ingest-now route, right after _process_uploaded_brain_files():
#        load_brain_overlays(force=True)
# =============================================================================

# ── Column alias tables ───────────────────────────────────────────────────────
import difflib
import os
import csv
import threading

BRAIN_BATTER_ALIASES = {
    # standard hitting stats → fgbat keys
    "avg": "fg_avg", "batting_avg": "fg_avg", "ba": "fg_avg",
    "obp": "fg_obp", "on_base": "fg_obp",
    "slg": "fg_slg", "slugging": "fg_slg",
    "ops": "fg_ops",
    "woba": "fg_woba",
    "wrc": "fg_wrc", "wrc_plus": "fg_wrc", "wrc+": "fg_wrc",
    "pa": "fg_pa", "plate_appearances": "fg_pa",
    "hr": "fg_hr", "home_runs": "fg_hr",
    "rbi": "fg_rbi",
    "sb": "fg_sb", "stolen_bases": "fg_sb",
    "war": "fg_war",
    "babip": "fg_babip",
    "k_pct": "fg_kpct", "k%": "fg_kpct", "so_pct": "fg_kpct",
    "bb_pct": "fg_bbpct", "bb%": "fg_bbpct",
    "iso": "fg_iso",
    "hard_pct": "sv_hard_pct",
    "barrel_pct": "sv_brl_pct", "brl_pct": "sv_brl_pct",
    "xwoba": "sv_xwoba",
    "xba": "sv_xba",
    "xslg": "sv_xslg",
    "sprint_speed": "sv_sprint_speed",
    "exit_velocity": "sv_avg_ev", "avg_ev": "sv_avg_ev",
    "launch_angle": "sv_avg_la", "avg_la": "sv_avg_la",
}

BRAIN_PITCHER_ALIASES = {
    # standard pitching stats → fgpit keys
    "era": "fg_era",
    "fip": "fg_fip",
    "xfip": "fg_xfip",
    "whip": "fg_whip",
    "k9": "fg_k9", "k_per_9": "fg_k9",
    "bb9": "fg_bb9", "bb_per_9": "fg_bb9",
    "hr9": "fg_hr9",
    "k_pct": "fg_kpct", "k%": "fg_kpct",
    "bb_pct": "fg_bbpct", "bb%": "fg_bbpct",
    "lob_pct": "fg_lobpct", "lob%": "fg_lobpct",
    "gb_pct": "fg_gbpct", "gb%": "fg_gbpct",
    "ip": "fg_ip",
    "war": "fg_war",
    "babip": "fg_babip",
    "xera": "sv_xera",
    "xfip_sv": "sv_xfip",
    "whiff_pct": "sv_whiff_pct",
    "csw_pct": "sv_csw_pct",
    "avg_ev_p": "sv_avg_ev_p",
    "hard_pct_p": "sv_hard_pct_p",
}


def _brain_norm_col(col):
    return col.strip().lower().replace(" ", "_").replace("%", "pct").replace("/", "_per_").replace("-", "_")


def _brain_detect_category(headers):
    bat_hits = sum(1 for h in headers if _brain_norm_col(h) in BRAIN_BATTER_ALIASES)
    pit_hits = sum(1 for h in headers if _brain_norm_col(h) in BRAIN_PITCHER_ALIASES)
    if bat_hits >= pit_hits and bat_hits > 0:
        return "batter", BRAIN_BATTER_ALIASES
    if pit_hits > 0:
        return "pitcher", BRAIN_PITCHER_ALIASES
    # fallback: check for obvious pitcher-only cols
    pit_cols = {"era", "fip", "whip", "k9", "bb9", "ip", "lob_pct", "gb_pct"}
    if any(_brain_norm_col(h) in pit_cols for h in headers):
        return "pitcher", BRAIN_PITCHER_ALIASES
    return "batter", BRAIN_BATTER_ALIASES


def _brain_safe_float(v):
    try:
        v = str(v).strip().replace("%", "").replace(",", "")
        if v in ("", "-", "N/A", "NA", "null", "None"):
            return None
        return float(v)
    except (ValueError, TypeError):
        return None


def _brain_extract_name(row):
    for key in ("name", "player", "player_name", "Name", "Player", "Player Name", "player name"):
        if key in row and str(row[key]).strip():
            return str(row[key]).strip()
    return None


def _brain_map_row(row, alias_table):
    out = {}
    for raw_col, val in row.items():
        norm = _brain_norm_col(raw_col)
        mapped_key = alias_table.get(norm)
        if mapped_key:
            fv = _brain_safe_float(val)
            if fv is not None:
                out[mapped_key] = fv
    return out


def _brain_parse_rows(filepath):
    """Parse a CSV/TSV brain upload file. Returns list of (name, stat_dict, category)."""
    results = []
    ext = os.path.splitext(filepath)[1].lower()
    delimiter = "\t" if ext == ".tsv" else ","
    try:
        with open(filepath, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh, delimiter=delimiter)
            headers = reader.fieldnames or []
            category, alias_table = _brain_detect_category(headers)
            for row in reader:
                name = _brain_extract_name(row)
                if not name:
                    continue
                stats = _brain_map_row(row, alias_table)
                if stats:
                    results.append((name, stats, category))
    except Exception as ex:
        print(f"[brain_merge_patch] parse error {filepath}: {ex}")
    return results


_brain_bat_overlay = {}    # lowercase_name → fgbat/svbat dict
_brain_pit_overlay = {}    # lowercase_name → fgpit/svpit dict
_brain_overlay_lock = threading.Lock()
_brain_overlay_loaded = False


def load_brain_overlays(force=False):
    """
    Scan the brain upload folder and merge all CSV/TSV files into the
    _brain_bat_overlay and _brain_pit_overlay dicts.
    Called once at startup (after FG cache ready) and after every manual ingest.
    """
    global _brain_overlay_loaded
    if _brain_overlay_loaded and not force:
        return
    try:
        import importlib
        _app = importlib.import_module("app")
        upload_dir = getattr(_app, "BRAIN_UPLOAD_DIR", None)
        if not upload_dir:
            _here = os.path.dirname(os.path.abspath(__file__))
            upload_dir = os.path.join(_here, "brain_uploads")
    except Exception:
        _here = os.path.dirname(os.path.abspath(__file__))
        upload_dir = os.path.join(_here, "brain_uploads")

    if not os.path.isdir(upload_dir):
        _brain_overlay_loaded = True
        return

    new_bat: dict = {}
    new_pit: dict = {}
    for fname in os.listdir(upload_dir):
        if not fname.lower().endswith((".csv", ".tsv")):
            continue
        fpath = os.path.join(upload_dir, fname)
        rows = _brain_parse_rows(fpath)
        for name, stats, cat in rows:
            key = name.strip().lower()
            if cat == "batter":
                if key not in new_bat:
                    new_bat[key] = {}
                new_bat[key].update(stats)
            else:
                if key not in new_pit:
                    new_pit[key] = {}
                new_pit[key].update(stats)

    with _brain_overlay_lock:
        _brain_bat_overlay.clear()
        _brain_bat_overlay.update(new_bat)
        _brain_pit_overlay.clear()
        _brain_pit_overlay.update(new_pit)
    _brain_overlay_loaded = True
    print(f"[brain_merge_patch] overlays loaded — {len(new_bat)} batters, {len(new_pit)} pitchers")


def _brain_fuzzy(name, overlay):
    """Fuzzy lookup in a brain overlay dict (same 0.78 cutoff as _fuzzy_lookup)."""
    if not name or not overlay:
        return {}
    k = name.strip().lower()
    if k in overlay:
        return dict(overlay[k])
    m = difflib.get_close_matches(k, overlay.keys(), n=1, cutoff=0.78)
    return dict(overlay[m[0]]) if m else {}


# ── Patched lookup functions — REPLACE the existing 4 functions ──────────────
# Priority order: live API data wins conflicts; brain fills any missing keys.
# Uses lazy importlib.import_module("app") so this file can be imported
# standalone without NameError on _fg_lock / _sv_lock / _fuzzy_lookup.

def _get_app():
    """Lazy import of app module to avoid circular import at load time."""
    import importlib
    return importlib.import_module("app")


def fg_batter(name):
    """FanGraphs batter stats with Brain overlay fallback.
    Live FG data wins any key conflict; brain fills gaps for missing players.
    Lazy-imports app globals to avoid NameError when imported as a module.
    """
    try:
        _app = _get_app()
        with _app._fg_lock:
            c = dict(_app._fg_bat)
        live = _app._fuzzy_lookup(name, c)
    except Exception:
        live = {}
    with _brain_overlay_lock:
        brain = _brain_fuzzy(name, _brain_bat_overlay)
    if not live and not brain:
        return {}
    if not live:
        return brain
    if brain:
        merged = dict(brain)
        merged.update(live)
        return merged
    return live


def fg_pitcher(name):
    """FanGraphs pitcher stats with Brain overlay fallback.
    Live FG data wins any key conflict; brain fills gaps for missing players.
    """
    try:
        _app = _get_app()
        with _app._fg_lock:
            c = dict(_app._fg_pit)
        live = _app._fuzzy_lookup(name, c)
    except Exception:
        live = {}
    with _brain_overlay_lock:
        brain = _brain_fuzzy(name, _brain_pit_overlay)
    if not live and not brain:
        return {}
    if not live:
        return brain
    if brain:
        merged = dict(brain)
        merged.update(live)
        return merged
    return live


def sv_batter(name):
    """Savant batter stats with Brain overlay. Brain fills missing sv_ keys only."""
    try:
        _app = _get_app()
        with _app._sv_lock:
            xs = dict(_app._sv_bat_xstats)
            sc = dict(_app._sv_bat_statcast)
        r = dict(_app._fuzzy_lookup(name, xs) or {})
        r.update(_app._fuzzy_lookup(name, sc) or {})
    except Exception:
        r = {}
    with _brain_overlay_lock:
        brain = _brain_fuzzy(name, _brain_bat_overlay)
    for k, v in brain.items():
        if k not in r or r[k] in (None, "", "N/A", "NA"):
            r[k] = v
    return r


def sv_pitcher(name):
    """Savant pitcher stats with Brain overlay. Brain fills missing sv_ keys only."""
    try:
        _app = _get_app()
        with _app._sv_lock:
            xs = dict(_app._sv_pit_xstats)
            ap = dict(_app._sv_arsenal_pct)
            av = dict(_app._sv_arsenal_velo)
        r = dict(_app._fuzzy_lookup(name, xs) or {})
        r["sv_arsenal_pct"]  = _app._fuzzy_lookup(name, ap)
        r["sv_arsenal_velo"] = _app._fuzzy_lookup(name, av)
    except Exception:
        r = {}
    with _brain_overlay_lock:
        brain = _brain_fuzzy(name, _brain_pit_overlay)
    for k, v in brain.items():
        if k not in r or r[k] in (None, "", "N/A", "NA"):
            r[k] = v
    return r

# =============================================================================
# WIRING — add these 2 lines in app.py:
#
# 1) In the startup block (near _maybe_refresh_fg() and _maybe_refresh_savant()):
#       load_brain_overlays()
#
# 2) Inside the route that calls _process_uploaded_brain_files() (ingest-now):
#       result = _process_uploaded_brain_files(force=True)
#       load_brain_overlays(force=True)   ← ADD THIS LINE
# =============================================================================
