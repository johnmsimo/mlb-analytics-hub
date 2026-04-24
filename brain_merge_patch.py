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
    "bb_pct": "fg_bbpct", "bb%": "fg_bbpct", "walk_pct": "fg_bbpct",
    "k_pct": "fg_kpct", "k%": "fg_kpct", "strikeout_pct": "fg_kpct",
    "iso": "fg_iso",
    "bats": "fg_bats",
    # savant/statcast → sv keys
    "xba": "sv_xba", "xavg": "sv_xba",
    "xslg": "sv_xslg",
    "xwoba": "sv_xwoba",
    "ev": "sv_ev", "exit_velo": "sv_ev", "avg_ev": "sv_ev",
    "hh_pct": "sv_hh_pct", "hard_hit_pct": "sv_hh_pct", "hard_hit%": "sv_hh_pct",
    "brl_pct": "sv_brl_pct", "barrel_pct": "sv_brl_pct", "barrel%": "sv_brl_pct",
    "brl_pa": "sv_brl_pa",
    "la": "sv_la", "launch_angle": "sv_la",
    "ss_pct": "sv_ss_pct", "sweet_spot_pct": "sv_ss_pct",
    "max_ev": "sv_max_ev",
}

BRAIN_PITCHER_ALIASES = {
    # standard pitching stats → fgpit keys
    "era": "fg_era",
    "fip": "fg_fip",
    "xfip": "fg_xfip",
    "whip": "fg_whip",
    "k9": "fg_k9", "k_per_9": "fg_k9", "so9": "fg_k9",
    "bb9": "fg_bb9", "bb_per_9": "fg_bb9",
    "hr9": "fg_hr9", "hr_per_9": "fg_hr9",
    "k_pct": "fg_kpct", "k%": "fg_kpct",
    "bb_pct": "fg_bbpct", "bb%": "fg_bbpct",
    "babip": "fg_babip",
    "lob": "fg_lob", "lob_pct": "fg_lob", "lob%": "fg_lob",
    "war": "fg_war",
    "ip": "fg_ip",
    "g": "fg_g", "games": "fg_g",
    "gs": "fg_gs", "games_started": "fg_gs",
    "w": "fg_w", "wins": "fg_w",
    "l": "fg_l", "losses": "fg_l",
    "gb_pct": "fg_gb_pct", "gb%": "fg_gb_pct",
    # savant pitcher → sv keys
    "xera": "sv_xera",
    "xwoba": "sv_xwoba_p",
    "k_pct_sv": "sv_k_pct",
    "bb_pct_sv": "sv_bb_pct",
    "whiff_pct": "sv_whiff", "whiff%": "sv_whiff",
    "era_sv": "sv_era_p",
}

# Name column candidates (tried in order)
_BRAIN_NAME_FIELDS = ["name", "player_name", "playername", "fullname", "full_name", "player"]

# Keyword sets used to auto-detect file category from column headers
_PITCHER_KW = {"era", "fip", "whip", "k9", "bb9", "ip", "gs", "xera",
               "k_pct", "bb_pct", "gb_pct", "xfip", "lob", "lob_pct", "pitcher"}
_BATTER_KW  = {"avg", "obp", "slg", "ops", "woba", "wrc", "pa", "hr",
               "rbi", "sb", "iso", "xba", "xslg", "hh_pct", "brl_pct", "ev", "batter"}


def _brain_norm_col(col):
    return col.strip().lower().replace(" ", "_").replace("-", "_").replace("%", "_pct")


def _brain_detect_category(headers):
    cols = {_brain_norm_col(h) for h in headers}
    ps = len(cols & _PITCHER_KW)
    bs = len(cols & _BATTER_KW)
    if ps > bs: return "pitcher"
    if bs > ps: return "batter"
    return "mixed"


def _brain_safe_float(v):
    try:
        if v in (None, "", "N/A", "NA", "-.--", ".---"):
            return None
        return float(v)
    except Exception:
        return None


def _brain_extract_name(row):
    for field in _BRAIN_NAME_FIELDS:
        v = row.get(field, "").strip()
        if v:
            return v.lower()
    ln = row.get("last_name", "").strip()
    fn = row.get("first_name", "").strip()
    if ln and fn:
        return f"{fn} {ln}".lower()
    if ln:
        return ln.lower()
    return None


def _brain_map_row(row, alias_table):
    """Map one CSV/JSON row dict → internal stat dict using alias table."""
    out = {}
    for col, val in row.items():
        if col in alias_table:
            key = alias_table[col]
            fv = _brain_safe_float(val)
            out[key] = fv if fv is not None else (val or None)
        elif col.startswith("fg_") or col.startswith("sv_"):
            fv = _brain_safe_float(val)
            if fv is not None:
                out[col] = fv
            elif val:
                out[col] = val
    return {k: v for k, v in out.items() if v is not None}


def _brain_parse_rows(filepath):
    """Return (list[normalized_row_dicts], detected_category) for a CSV or JSON file."""
    ext = os.path.splitext(filepath)[1].lower()
    rows = []
    try:
        if ext == ".csv":
            with open(filepath, "r", encoding="utf-8-sig", newline="") as f:
                reader = csvmod.DictReader(f)
                if not reader.fieldnames:
                    return [], "unknown"
                category = _brain_detect_category(reader.fieldnames)
                for raw in reader:
                    rows.append({_brain_norm_col(k): (v or "").strip()
                                 for k, v in raw.items()})
        elif ext == ".json":
            with open(filepath, "r", encoding="utf-8-sig") as f:
                payload = json.load(f)
            if isinstance(payload, dict):
                for key in ("rows", "data", "players", "items", "records"):
                    if isinstance(payload.get(key), list):
                        payload = payload[key]
                        break
                else:
                    payload = [payload]
            if not isinstance(payload, list) or not payload:
                return [], "unknown"
            category = _brain_detect_category(payload[0].keys())
            for raw in payload:
                rows.append({_brain_norm_col(k): str(v).strip()
                             for k, v in raw.items()})
        else:
            return [], "unknown"
    except Exception as ex:
        print(f"[BrainMerge] parse error {os.path.basename(filepath)}: {ex}")
        return [], "unknown"
    return rows, category


# ── In-memory overlay caches ──────────────────────────────────────────────────
_brain_bat_overlay = {}    # lowercase_name → fgbat/svbat dict
_brain_pit_overlay = {}    # lowercase_name → fgpit/svpit dict
_brain_overlay_lock = threading.Lock()
_brain_overlay_loaded = False


def load_brain_overlays(force=False):
    """
    Parse all ingested brain upload files and populate _brain_bat_overlay /
    _brain_pit_overlay so fg_batter/fg_pitcher/sv_batter/sv_pitcher can use them.
    Safe to call multiple times (rebuilds from scratch each time).
    Called at startup and after any new ingest.
    """
    global _brain_bat_overlay, _brain_pit_overlay, _brain_overlay_loaded
    if not os.path.exists(BRAIN_DATA_DIR):
        return
    state = _get_brain_upload_state()
    files_state = state.get("files", {})
    new_bat, new_pit = {}, {}

    for fname in sorted(os.listdir(BRAIN_DATA_DIR)):
        fpath = os.path.join(BRAIN_DATA_DIR, fname)
        if not os.path.isfile(fpath):
            continue
        entry = files_state.get(fname, {})
        if entry.get("ingestState") != "ingested":
            continue  # only process fully ingested files

        rows, auto_cat = _brain_parse_rows(fpath)
        file_cat = entry.get("category") or auto_cat  # manual override from UI wins

        for row in rows:
            name = _brain_extract_name(row)
            if not name:
                continue

            if file_cat == "pitcher":
                mapped = _brain_map_row(row, BRAIN_PITCHER_ALIASES)
                if mapped:
                    new_pit.setdefault(name, {}).update(mapped)
            elif file_cat == "batter":
                mapped = _brain_map_row(row, BRAIN_BATTER_ALIASES)
                if mapped:
                    new_bat.setdefault(name, {}).update(mapped)
            else:  # "mixed" — whichever alias table yields more keys wins
                bm = _brain_map_row(row, BRAIN_BATTER_ALIASES)
                pm = _brain_map_row(row, BRAIN_PITCHER_ALIASES)
                if len(bm) >= len(pm) and bm:
                    new_bat.setdefault(name, {}).update(bm)
                elif pm:
                    new_pit.setdefault(name, {}).update(pm)

    with _brain_overlay_lock:
        _brain_bat_overlay = new_bat
        _brain_pit_overlay = new_pit
        _brain_overlay_loaded = True

    print(f"[BrainMerge] overlays ready: "
          f"{len(new_bat)} batters, {len(new_pit)} pitchers")


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

def fg_batter(name):
    """FanGraphs batter stats with Brain overlay fallback.
    Live MLB API data wins any key conflict; brain fills gaps for missing players.
    """
    with _fg_lock:
        c = dict(_fg_bat)
    live = _fuzzy_lookup(name, c)
    with _brain_overlay_lock:
        brain = _brain_fuzzy(name, _brain_bat_overlay)
    if not live and not brain:
        return {}
    if not live:
        return brain
    if brain:
        # brain as base (lower priority), live overwrites
        merged = dict(brain)
        merged.update(live)
        return merged
    return live


def fg_pitcher(name):
    """FanGraphs pitcher stats with Brain overlay fallback.
    Live MLB API data wins any key conflict; brain fills gaps for missing players.
    """
    with _fg_lock:
        c = dict(_fg_pit)
    live = _fuzzy_lookup(name, c)
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
    """Savant batter stats with Brain overlay. Brain fills any missing sv_ keys only."""
    with _sv_lock:
        xs = dict(_sv_bat_xstats)
        sc = dict(_sv_bat_statcast)
    r = dict(_fuzzy_lookup(name, xs) or {})
    r.update(_fuzzy_lookup(name, sc) or {})
    with _brain_overlay_lock:
        brain = _brain_fuzzy(name, _brain_bat_overlay)
    # brain fills gaps only — does NOT overwrite live Savant values
    for k, v in brain.items():
        if k not in r or r[k] in (None, "", "N/A", "NA"):
            r[k] = v
    return r


def sv_pitcher(name):
    """Savant pitcher stats with Brain overlay. Brain fills any missing sv_ keys only."""
    with _sv_lock:
        xs = dict(_sv_pit_xstats)
        ap = dict(_sv_arsenal_pct)
        av = dict(_sv_arsenal_velo)
    r = dict(_fuzzy_lookup(name, xs) or {})
    r["sv_arsenal_pct"]  = _fuzzy_lookup(name, ap)
    r["sv_arsenal_velo"] = _fuzzy_lookup(name, av)
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
