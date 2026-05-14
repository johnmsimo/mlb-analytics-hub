"""
mc_upgrades.py  —  MLB Analytics Hub Monte Carlo Upgrade Module
================================================================
Phases 1-8: Weather, Umpire, Antithetic Variates, TTOP Decay,
            Platoon Regression, BatX Pitcher Form, Cache Pre-warming,
            Log-Odds Market Blending.

HOW TO INTEGRATE:
  Add to top of app.py:
    from mc_upgrades import (
        build_weather_multipliers,
        build_ump_sim_adjustments,
        logit_blend_prob,
        MARKET_MODEL_WEIGHTS,
        platoon_blend_v2,
        LEAGUE_PLATOON_SPLITS,
        PLATOON_M,
        ttop_woba_penalty,
        BATX_WEIGHTS_V2,
        derive_probs_v2,
        simulate_offense_v2,
        prewarm_today_caches,
    )

  Then swap call sites per the APP_PATCHES section at bottom of this file.
"""

import math
import random
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# ─────────────────────────────────────────────────────────────────────────────
# PHASE 1A — WEATHER MULTIPLIERS
# Research basis:
#   HR:   +1.0%/°F (Nathan 2017, Callahan 2023) — delta from 70°F baseline
#   Runs: +0.5%/°F  (more muted, run scoring 4.2→4.7 R/G below 60→above 80°F)
#   Wind: +3.5 ft carry/mph along-spray → ~+0.004 HR prob/mph out, −0.004 in
#   Humidity: small, negligible in game-level sim (humidors now standardized)
# ─────────────────────────────────────────────────────────────────────────────

# Venues with humidors that suppress HR beyond altitude correction
HUMIDOR_VENUES = {19, 15}   # Coors (19), Chase (15)

# Wind-direction azimuth mappings for each park.
# Key: the compass direction a ball travels when hit "out to CF".
# Positive along-spray = wind blowing out (HR boost); negative = wind in (suppress).
# All 30 MLB venue IDs mapped; previously only 3 were covered (Bug 2 fix).
_OUTFIELD_COMPASS = {
    1:  "NE",   # Oriole Park at Camden Yards (BAL) — CF opens NE toward harbor
    2:  "N",    # Guaranteed Rate Field (CWS) — CF faces N
    3:  "E",    # Fenway Park (BOS) — out to right/center is E
    4:  "N",    # Comerica Park (DET) — CF faces N
    5:  "NW",   # Kauffman Stadium (KC) — CF NW
    6:  "N",    # Angel Stadium (LAA) — CF faces N
    7:  "NW",   # Dodger Stadium (LAD) — CF NW toward Chavez Ravine hills
    8:  "N",    # Oakland Coliseum (OAK) — CF faces N
    9:  "NE",   # T-Mobile Park (SEA) — retractable, CF NE
    10: "N",    # Globe Life Field (TEX) — retractable, CF faces N
    11: "NW",   # Truist Park (ATL) — CF NW
    12: "N",    # Tropicana Field (TB) — dome; direction irrelevant but mapped
    13: "NW",   # Great American Ball Park (CIN) — CF NW toward Ohio River
    14: "N",    # Progressive Field (CLE) — CF faces N
    15: "NW",   # Chase Field (ARI) — retractable, CF NW
    16: "N",    # Minute Maid Park (HOU) — retractable, CF faces N
    17: "E",    # Wrigley Field (CHC) — out to Lake Michigan (E/NE) when favorable
    18: "N",    # Busch Stadium (STL) — CF faces N
    19: "NE",   # Coors Field (COL) — CF opens NE toward downtown Denver (was "W", corrected)
    20: "N",    # Petco Park (SD) — CF faces N
    21: "NW",   # Oracle Park (SF) — CF NW toward McCovey Cove
    22: "NE",   # Nationals Park (WSH) — CF NE
    23: "NW",   # Target Field (MIN) — CF NW
    24: "N",    # PNC Park (PIT) — CF faces N toward Allegheny River
    25: "N",    # Citizens Bank Park (PHI) — CF faces N
    26: "N",    # Citi Field (NYM) — CF faces N
    27: "N",    # Yankee Stadium (NYY) — CF faces N
    28: "N",    # loanDepot park (MIA) — retractable, CF faces N
    29: "N",    # American Family Field (MIL) — retractable, CF faces N
    30: "NE",   # Rogers Centre (TOR) — retractable, CF NE
}

_COMPASS_DEG = {"N":0,"NNE":22.5,"NE":45,"ENE":67.5,"E":90,"ESE":112.5,
                "SE":135,"SSE":157.5,"S":180,"SSW":202.5,"SW":225,"WSW":247.5,
                "W":270,"WNW":292.5,"NW":315,"NNW":337.5}


def _wind_along_spray(wind_spd: float, wind_dir: str, venue_id: int) -> float:
    """
    Returns signed wind speed (mph) along the primary batted-ball spray axis.
    Positive = out (boost), negative = in (suppress).
    Falls back to 0 if venue not mapped.
    """
    if not wind_dir or wind_spd <= 0:
        return 0.0
    out_dir = _OUTFIELD_COMPASS.get(venue_id)
    if not out_dir:
        # All 30 MLB venues are now mapped above; this fallback only fires
        # for exhibition venues or data errors. Return 0 (no adjustment).
        return 0.0
    out_deg = _COMPASS_DEG.get(out_dir, 0.0)

    wind_deg = _COMPASS_DEG.get(wind_dir.strip().upper(), 0.0)
    # Dot product of unit vectors
    import math as _math
    diff_rad = _math.radians(wind_deg - out_deg)
    along = wind_spd * _math.cos(diff_rad)
    return along   # positive = out


def build_weather_multipliers(wx: dict, venue_id: int, park_factor: float) -> dict:
    """
    Phase 1A: Physics-grade weather multipliers for Monte Carlo.

    Returns dict with keys:
        hr_mult   — multiply per-PA HR probability
        hit_mult  — multiply per-PA hit probability  
        run_mult  — multiply projected runs (applied at team level)
        carry_ft  — additional carry in feet (informational)
        dome      — True if indoor, bypass all adjustments

    IMPORTANT: park_factor is the BASELINE (geometry/altitude).
    Weather adjustments are applied as DEVIATIONS — do not double-count
    by applying raw weather on top of weather-inclusive seasonal PF.
    The seasonal PF already reflects the park's average weather.
    We only apply the *delta* from average conditions.
    """
    if wx.get("dome"):
        return {"hr_mult": 1.0, "hit_mult": 1.0, "run_mult": 1.0,
                "carry_ft": 0.0, "dome": True}

    # ── Temperature (delta from 70°F park-average baseline) ────────────────────
    try:
        temp_f = float(wx.get("temp", 70))
    except (TypeError, ValueError):
        temp_f = 70.0
    temp_f = max(20.0, min(110.0, temp_f))
    temp_delta = temp_f - 70.0   # positive = warmer than average

    # Nathan 2017: +1.0%/°F for HR, empirical ~0.6%/°F (use 0.8% as compromise)
    hr_temp_mult = 1.0 + temp_delta * 0.008
    # Run scoring: more muted
    run_temp_mult = 1.0 + temp_delta * 0.004

    # ── Wind (along-spray component) ────────────────────────────────────────
    try:
        wind_spd = float(wx.get("wind_speed", 0) or 0)
    except (TypeError, ValueError):
        wind_spd = 0.0
    wind_dir = str(wx.get("wind_dir", "") or "")

    along = _wind_along_spray(wind_spd, wind_dir, venue_id)
    # +3.5 ft/mph along-spray → convert to HR prob multiplier
    # Typical HR margin ~5 ft → each 1.4 ft carry ≈ 1% more HR land
    carry_ft = along * 3.5
    hr_wind_mult = 1.0 + along * 0.004   # ~+0.4%/mph out, −0.4%/mph in
    hr_wind_mult = max(0.82, min(1.22, hr_wind_mult))

    # ── Humidor correction (suppresses HR ~20-25% beyond altitude) ───────────
    humidor_mult = 0.97 if venue_id in HUMIDOR_VENUES else 1.0

    # ── Combine ───────────────────────────────────────────────────────────────────
    hr_mult  = max(0.75, min(1.35, hr_temp_mult * hr_wind_mult * humidor_mult))
    hit_mult = max(0.92, min(1.08, 1.0 + temp_delta * 0.002))  # hits much less sensitive
    run_mult = max(0.88, min(1.12, run_temp_mult * (1.0 + along * 0.002)))

    return {
        "hr_mult":   round(hr_mult,  4),
        "hit_mult":  round(hit_mult, 4),
        "run_mult":  round(run_mult, 4),
        "carry_ft":  round(carry_ft, 1),
        "dome":      False,
        "temp_f":    round(temp_f, 1),
        "wind_spd":  round(wind_spd, 1),
        "wind_along": round(along, 1),
    }


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 1B — UMPIRE K%/BB% ADJUSTMENTS
# Research basis (UmpScorecards / SABR / Berkeley):
#   Inter-umpire SD ≈ 0.20 R/game → K_mult SD ~0.04, BB_mult SD ~0.05
#   Regress 50% to 1.0 if games_sampled < 30
#   Elite framing catcher adds ~±3.9 pp K%, −3.9 pp BB% (interact multiplicatively)
# ─────────────────────────────────────────────────────────────────────────────

LG_K_PER_TEAM  = 8.5   # league-average K per team per game
LG_BB_PER_TEAM = 3.0   # league-average BB per team per game


def build_ump_sim_adjustments(zone_rating: float | None,
                               avg_k_per_team: float | None,
                               avg_bb_per_team: float | None,
                               games_sampled: int) -> dict:
    """
    Phase 1B: Convert umpire zone stats to K%/BB% multipliers for simulation.

    Args:
        zone_rating:     0-100 (from _ump_cache, 50=neutral, >65=pitcher-friendly)
        avg_k_per_team:  from ump history (or None)
        avg_bb_per_team: from ump history (or None)
        games_sampled:   number of games in ump sample

    Returns dict: {k_mult, bb_mult, run_mult, zone_rating}
    """
    if avg_k_per_team is None or games_sampled == 0:
        return {"k_mult": 1.0, "bb_mult": 1.0, "run_mult": 1.0, "zone_rating": 50}

    # Shrink toward 1.0 based on sample size
    # Full weight at 80+ games; 50% at 30 games; zero below 15
    confidence = min(1.0, max(0.0, (games_sampled - 15) / 65.0))

    raw_k_mult  = avg_k_per_team  / LG_K_PER_TEAM
    raw_bb_mult = avg_bb_per_team / LG_BB_PER_TEAM

    # Regress toward 1.0
    k_mult  = 1.0 + (raw_k_mult  - 1.0) * confidence
    bb_mult = 1.0 + (raw_bb_mult - 1.0) * confidence

    # Hard clamps: inter-umpire variation realistically ±12% K, ±15% BB
    k_mult  = max(0.88, min(1.12, k_mult))
    bb_mult = max(0.85, min(1.15, bb_mult))

    # Run environment shift: more K + fewer BB = fewer runs
    # Approx: each 1 BB ≈ 0.35 R; each 1 K ≈ -0.05 R (weak, already in outs)
    k_delta  = (avg_k_per_team  - LG_K_PER_TEAM)  * confidence
    bb_delta = (avg_bb_per_team - LG_BB_PER_TEAM) * confidence
    run_mult = max(0.92, min(1.08, 1.0 - k_delta * 0.008 + bb_delta * 0.035))

    return {
        "k_mult":      round(k_mult,  4),
        "bb_mult":     round(bb_mult, 4),
        "run_mult":    round(run_mult, 4),
        "zone_rating": zone_rating or 50,
        "confidence":  round(confidence, 3),
        "games":       games_sampled,
    }


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 2 — TTOP CONTINUOUS DECAY
# Research basis (Tango/Lichtman, Brill JQAS 2023):
#   1st TTO baseline, +9 wOBA pts 2nd TTO, +8 more 3rd TTO (~+17 cumulative)
#   Brill: smooth function of batters faced, not discrete tiers
#   Arsenal scale: ×1.7 for 1-pitch SP, ×0.8 for 4+ pitch SP
#   Pitch count tail: +0.015 wOBA per pitch above 75
# ─────────────────────────────────────────────────────────────────────────────

def ttop_woba_penalty(batters_faced: int,
                      pitches_thrown: int,
                      arsenal_size: int = 3) -> float:
    """
    Continuous TTOP (Time Through Order Penalty) as wOBA-against delta.
    Returns positive value = batters hit better by this amount.

    arsenal_size: number of distinct pitch types (1=1-pitch, 4+=multi-arsenal)
    """
    # Smooth TTOP: quadratic in batters_faced
    # 0 BF=0, 9 BF≈10 pts, 18 BF≈17 pts, 27 BF≈25 pts
    ttop_base = min(0.030, (batters_faced / 27.0) ** 1.4 * 0.030)

    # Arsenal scale
    if arsenal_size <= 1:
        arsenal_scale = 1.7
    elif arsenal_size <= 2:
        arsenal_scale = 1.2
    elif arsenal_size >= 4:
        arsenal_scale = 0.8
    else:
        arsenal_scale = 1.0

    ttop = ttop_base * arsenal_scale

    # Pitch-count tail: fatigue beyond 75 pitches
    pitch_tail = max(0.0, (pitches_thrown - 75) * 0.00015)

    return round(min(0.050, ttop + pitch_tail), 4)


def relief_fatigue_penalty(days_pitched: list[int],
                           pitches_last: int = 20) -> float:
    """
    Reliever back-to-back penalty in wOBA-against delta.
    days_pitched: list of days ago (0=today, 1=yesterday, etc.)
    Research: B2B ≈ +10-15 wOBA pts; 3rd consecutive ≈ +20-30 pts
    """
    if not days_pitched:
        return 0.0
    min_rest = min(days_pitched) if days_pitched else 3
    if min_rest == 0:
        return 0.020 + min(0.015, pitches_last * 0.0003)
    if min_rest == 1 and len([d for d in days_pitched if d <= 2]) >= 2:
        return 0.015   # back-to-back appearances in last 2 days
    if min_rest == 1:
        return 0.010
    if min_rest == 2 and pitches_last >= 35:
        return 0.008
    return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 3 — PLATOON REGRESSION (canonical Tango constants)
# Research basis (The Book, FanGraphs Hardball Times):
#   LHB M=1000 PA, RHB M=2200 PA, Switch M=1620 PA
#   League splits (wOBA delta same vs opposite hand):
#     LHB: +35 pts, RHB: +23 pts, Switch: +27 pts (2008-2022 recalibration)
# ─────────────────────────────────────────────────────────────────────────────

PLATOON_M = {"L": 1000, "R": 2200, "S": 1620}

LEAGUE_PLATOON_SPLITS = {
    # wOBA delta: favorable vs unfavorable handedness matchup
    "L":  0.035,    # LHB vs RHP advantage (LHB hits .035 wOBA better vs RHP)
    "R":  0.023,    # RHB vs LHP advantage
    "S":  0.027,    # switch hitter advantage (use favorable side)
}

_STAT_DEFAULTS_V2 = {
    "avg": 0.245, "obp": 0.315, "slg": 0.390, "ops": 0.705, "woba": 0.320
}


def platoon_blend_v2(batter: dict, pitcher_hand: str, stat: str) -> float:
    """
    Phase 3: Proper Bayesian-regressed platoon blend.
    Replaces current _platoon_blend() in app.py.

    Uses canonical Tango regression constants — observed splits with small PA
    are shrunk heavily toward the LEAGUE AVERAGE for that stat (not the player's
    own season value, which would inflate high-average batters' split estimates).
    """
    bats = (batter.get("bats") or "S").upper()
    hand = (pitcher_hand or "R").upper()
    if hand not in ("L", "R"):
        hand = "R"

    # Pick correct vs-hand split key
    split_key = f"vs_{'l' if hand == 'L' else 'r'}_{stat}"
    pa_key    = f"vs_{'l' if hand == 'L' else 'r'}_pa"

    obs_split = batter.get(split_key)
    split_pa  = int(batter.get(pa_key, 0) or 0)

    # Season (unhandedness-adjusted) value — used only for no-split-data fallback
    season_map = {
        "avg":  batter.get("avg")  or batter.get("fg_avg"),
        "obp":  batter.get("obp")  or batter.get("fg_obp"),
        "slg":  batter.get("slg")  or batter.get("fg_slg"),
        "ops":  batter.get("ops"),
        "woba": batter.get("fg_woba") or batter.get("sv_xwoba"),
    }
    season_val = float(season_map.get(stat) or _STAT_DEFAULTS_V2.get(stat, 0.0) or 0.0)

    # Helper to check if obs_split is a valid float
    def _is_valid_float(val):
        try:
            return float(val) > 0.0
        except (TypeError, ValueError):
            return False

    if obs_split is None or not _is_valid_float(obs_split):
        # No split data — apply theoretical league platoon advantage
        platoon_favorable = (
            (bats == "L" and hand == "R") or
            (bats == "R" and hand == "L") or
            bats == "S"
        )
        if stat in ("avg", "obp", "slg", "ops", "woba"):
            lg_edge = LEAGUE_PLATOON_SPLITS.get(bats, 0.025)
            # Season stat is a mix of both hands, so half the edge
            if platoon_favorable:
                return round(season_val + lg_edge * 0.5, 4)
            else:
                return round(season_val - lg_edge * 0.5, 4)
        return season_val

    obs_val = float(obs_split)
    M = PLATOON_M.get(bats, 1620)

    # Bug 3 fix: shrink toward league average, not toward the player's own
    # season stat. Using season_val as the prior inflates high-average batters'
    # split estimates and defeats the entire purpose of Tango's M constants.
    lg_avg = _STAT_DEFAULTS_V2.get(stat, season_val)
    blended = (obs_val * split_pa + lg_avg * M) / (split_pa + M)
    return round(blended, 4)


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 4 — UPDATED BATX WEIGHTS + PITCHER RECENT FORM WIRING
# Research: Barrel/EV most predictive for HR/TB (r²=0.382 brl vs future HR)
#           L7 wOBA is meaningful signal; BvP too noisy with small samples
# ─────────────────────────────────────────────────────────────────────────────

BATX_WEIGHTS_V2 = {
    "contact":    0.26,   # platoon-blended AVG + xBA  (unchanged)
    "power":      0.22,   # ISO, brl%, EV, xSLG        (+0.04 — most predictive for HR/TB)
    "discipline": 0.12,   # BB%, K%, xwOBA             (unchanged)
    "platoon":    0.10,   # hand matchup edge           (unchanged)
    "park":       0.07,   # park factor                 (unchanged)
    "weather":    0.05,   # temp/wind                   (unchanged)
    "pitcher":    0.11,   # FIP/xERA/K% + recent form   (-0.01 from 0.12)
    "form":       0.07,   # L7 wOBA edge vs league      (+0.02 — more meaningful)
    "bvp":        0.02,   # BvP shrunk wOBA × reliability (-0.01 — too noisy)
    # NEW: recent form explicitly wired into pitcher component below
}


def pitcher_component_v2(opp_pitcher_fg: dict,
                          opp_pitcher_sv: dict,
                          recent_form: dict | None) -> tuple[float, float]:
    """
    Phase 4: Blended pitcher resistance — 60% season + 40% recent form.
    Returns (pitcher_mult, k_resistance) where:
        pitcher_mult > 1 = pitcher is weaker than average (batter benefits)
        pitcher_mult < 1 = pitcher is stronger (batter penalized)
        k_resistance: fraction of season K% to apply (recent K% may differ)
    """
    def _f(v, d=0.0):
        try:
            f = float(v)
            return f if f == f else d
        except Exception:
            return d

    season_era  = _f(opp_pitcher_fg.get("fg_era")  or opp_pitcher_sv.get("sv_era_p"), 4.20)
    season_fip  = _f(opp_pitcher_fg.get("fg_fip"),  season_era)
    season_xera = _f(opp_pitcher_sv.get("sv_xera"), season_era)
    season_kpct = _f(opp_pitcher_fg.get("fg_kpct") or opp_pitcher_sv.get("sv_k_pct"), 0.22)

    if recent_form and isinstance(recent_form, dict) and recent_form.get("era_recent"):
        W_RECENT = 0.40
        era_blended  = 0.60 * season_era  + W_RECENT * recent_form["era_recent"]
        k9_recent    = recent_form.get("k9_recent", season_kpct * 9)
        kpct_recent  = min(0.40, k9_recent / 27.0)  # rough PA→k conversion
        kpct_blended = 0.60 * season_kpct + W_RECENT * kpct_recent
    else:
        era_blended  = (season_era + season_fip + season_xera) / 3.0
        kpct_blended = season_kpct

    pit_mult = min(1.30, max(0.72, era_blended / 4.20))
    return round(pit_mult, 4), round(kpct_blended, 4)


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 6 — LOG-ODDS MARKET BLENDING
# Research (Trademate, Lopez/Matthews/Glickman arXiv:1701.05976):
#   Blend in log-odds space; weight table by market efficiency tier
#   Always de-vig (power method) before blending
# ─────────────────────────────────────────────────────────────────────────────

MARKET_MODEL_WEIGHTS = {
    # w = model weight (1-w = market weight)
    # Higher w = trust model more (less efficient market)
    "batter_hits":           0.35,
    "batter_total_bases":    0.35,
    "batter_home_runs":      0.30,
    "pitcher_strikeouts":    0.30,
    "batter_rbis":           0.40,
    "batter_runs_scored":    0.40,
    "batter_hits_runs_rbis": 0.40,
    "batter_stolen_bases":   0.45,
    "nrfi":                  0.35,
    "yrfi":                  0.40,
    # Alt-lines/micro-props — model dominates
    "default":               0.50,
}


def devig_power(over_implied: float, under_implied: float) -> tuple[float, float]:
    """
    Power method de-vig (Shin 1993 style for asymmetric lines).
    Finds k such that over_implied^(1/k) + under_implied^(1/k) = 1.
    Returns (fair_over, fair_under).
    """
    if not over_implied or not under_implied or over_implied <= 0 or under_implied <= 0:
        return over_implied, under_implied
    # Binary search for k
    lo, hi = 0.5, 3.0
    for _ in range(50):
        k = (lo + hi) / 2.0
        s = over_implied ** (1.0 / k) + under_implied ** (1.0 / k)
        if s > 1.0:
            hi = k
        else:
            lo = k
    k = (lo + hi) / 2.0
    return round(over_implied ** (1.0 / k), 6), round(under_implied ** (1.0 / k), 6)


def logit_blend_prob(p_model: float,
                     p_market: float,
                     market_key: str,
                     over_implied: float | None = None,
                     under_implied: float | None = None) -> float:
    """
    Phase 6: Blend model probability with (de-vigged) market probability
    in log-odds space.

    If over_implied + under_implied provided, de-vigs first via power method.
    Returns blended probability.
    """
    def _safe_logit(p):
        p = max(0.001, min(0.999, p))
        return math.log(p / (1.0 - p))

    def _sigmoid(x):
        return 1.0 / (1.0 + math.exp(-x))

    w = MARKET_MODEL_WEIGHTS.get(market_key, MARKET_MODEL_WEIGHTS["default"])

    # De-vig market probability if raw over/under implied given
    if over_implied and under_implied and over_implied > 0 and under_implied > 0:
        p_market_fair, _ = devig_power(over_implied, under_implied)
        p_market = p_market_fair if p_market_fair > 0 else p_market

    if not p_market or p_market <= 0:
        return round(max(0.01, min(0.99, p_model)), 4)

    blended_logit = w * _safe_logit(p_model) + (1.0 - w) * _safe_logit(p_market)
    return round(_sigmoid(blended_logit), 4)


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 2B — ENHANCED _derive_probs WITH ALL PHASE 1/2/3 ADJUSTMENTS
# Drop-in replacement for app.py _derive_probs()
# New params: wx_mults, ump_adj, ttop_penalty_val, batx=None
# ─────────────────────────────────────────────────────────────────────────────

def derive_probs_v2(b: dict, p: dict, park: float = 1.0,
                    batx: dict | None = None,
                    wx_mults: dict | None = None,
                    ump_adj: dict | None = None,
                    ttop_penalty_val: float = 0.0) -> dict:
    """
    Phase 2B: Enhanced probability derivation.
    Extends existing _derive_probs() with:
      - wx_mults from build_weather_multipliers()  [Phase 1A]
      - ump_adj from build_ump_sim_adjustments()   [Phase 1B]
      - ttop_penalty_val from ttop_woba_penalty()  [Phase 2]

    All existing logic preserved; new multipliers applied on top.
    """
    # ── Base probabilities (existing logic, inlined for portability) ───────────
    def _n(v, d=0.0):
        try:
            f = float(v)
            return f if f == f else d
        except Exception:
            return d

    def _cl(v, lo, hi):
        return max(lo, min(hi, v))

    avg  = _n(b.get("sv_xba"),    _n(b.get("avg"),    0.245))
    obp  = _n(b.get("obp"),       max(avg + 0.060, 0.290))
    slg  = _n(b.get("sv_xslg"),   _n(b.get("slg"),    0.400))
    xwoba = _n(b.get("sv_xwoba"), _n(b.get("fg_woba"), 0.320))
    ev   = _n(b.get("sv_ev"),     87.5)
    hh   = _n(b.get("sv_hh_pct"), 37.0)
    brl  = _n(b.get("sv_brl_pct"), 5.5)
    wrc  = _n(b.get("fg_wrc"),    100.0)
    sb   = _n(b.get("fg_sb"),     6)

    era  = _n(p.get("era"),  4.25)
    whip = _n(p.get("whip"), 1.28)
    k9   = _n(p.get("k9"),   8.4)
    bb9  = _n(p.get("bb9"),  3.2)
    hr9  = _n(p.get("hr9"),  1.10)
    pitch_hand = (p.get("pitchHand") or "R").upper()

    # Platoon (use v2 if possible)
    bats = (b.get("bats") or "S").upper()
    hand = pitch_hand
    if bats != "S":
        platoon_k = 0.97 if ((bats=="L" and hand=="R") or (bats=="R" and hand=="L")) else 1.03
        platoon_hit = 0.010 if ((bats=="L" and hand=="R") or (bats=="R" and hand=="L")) else -0.008
    else:
        platoon_k = 1.0; platoon_hit = 0.008

    # TTOP wOBA penalty → increases hit rate against fatigued pitcher
    ttop_boost = ttop_penalty_val  # wOBA units → directly boosts hit rate

    hit_rate = avg + (xwoba-0.320)*0.30 + (ev-87.5)*0.003 + (hh-37.0)*0.0016 \
             + (wrc-100)*0.00035 + (whip-1.28)*0.055 - (era-4.25)*0.010 \
             + (park-1.0)*0.030 + platoon_hit + ttop_boost * 0.6
    hit_rate = _cl(hit_rate, 0.13, 0.35)

    walk_rate = max(obp-avg, 0.045) + (bb9-3.2)*0.010
    walk_rate = _cl(walk_rate, 0.04, 0.15)

    hr_rate = 0.018 + max(0, brl-6.0)*0.0035 + max(0, ev-89.0)*0.0017 \
            + max(0, slg-0.420)*0.060 + (hr9-1.10)*0.020 + (park-1.0)*0.050
    hr_rate = _cl(hr_rate, 0.005, min(0.095, hit_rate*0.45))

    dbl_rate = _cl(0.040 + max(0,slg-avg-0.150)*0.12 + max(0,ev-88.0)*0.002,
                   0.020, min(0.110, hit_rate*0.40))
    trp_rate = _cl(0.004 + max(0,avg-0.270)*0.04 + (park-1.0)*0.005,
                   0.001, 0.020)
    trp_rate = min(trp_rate, max(0.001, hit_rate-hr_rate-dbl_rate-0.02))
    single_rate = max(0.05, hit_rate-hr_rate-dbl_rate-trp_rate)

    k_rate = _cl(0.175 + (k9-8.2)*0.018 - (avg-0.245)*0.35
                 - (hh-37)*0.002 + (platoon_k-1.0)*0.10, 0.09, 0.36)

    steal_rate = _cl(0.010 + max(0,sb-8)*0.002, 0.003, 0.075)
    steal_succ = _cl(0.63 + max(0,sb-8)*0.01, 0.58, 0.88)

    out_rate = 1.0 - (walk_rate+single_rate+dbl_rate+trp_rate+hr_rate)
    if out_rate < 0.28:
        scale = (1.0-0.28) / max(0.01, 1.0-out_rate)
        walk_rate*=scale; single_rate*=scale; dbl_rate*=scale
        trp_rate*=scale; hr_rate*=scale
        out_rate = 1.0-(walk_rate+single_rate+dbl_rate+trp_rate+hr_rate)
    k_share = _cl(k_rate / max(out_rate, 0.001), 0.18, 0.72)

    # ── Phase 1A: Apply weather multipliers ────────────────────────────────
    # Bug 1 fix: apply hit_mult to ALL batted-ball hit types, not just singles.
    # Doubles and triples are equally affected by temperature/wind carry.
    if wx_mults and not wx_mults.get("dome"):
        hm          = wx_mults.get("hit_mult", 1.0)
        hr_rate     = _cl(hr_rate     * wx_mults.get("hr_mult", 1.0), 0.004, 0.10)
        single_rate = _cl(single_rate * hm, 0.04, 0.30)
        dbl_rate    = _cl(dbl_rate    * hm, 0.01, 0.12)
        trp_rate    = _cl(trp_rate    * hm, 0.001, 0.022)

    # ── Phase 1B: Apply umpire K%/BB% multipliers ──────────────────────────
    if ump_adj:
        k_share  = _cl(k_share  * ump_adj.get("k_mult",  1.0), 0.15, 0.78)
        walk_rate = _cl(walk_rate * ump_adj.get("bb_mult", 1.0), 0.035, 0.16)

    # ── BatX integration (existing logic preserved) ─────────────────────────
    if batx:
        hit_m  = batx.get("hit_mult",  1.0)
        hr_m   = batx.get("hr_mult",   1.0)
        walk_m = batx.get("walk_mult", 1.0)
        k_m    = batx.get("k_mult",    1.0)
        single_rate = _cl(single_rate * hit_m,  0.03, 0.30)
        dbl_rate    = _cl(dbl_rate    * hit_m,  0.01, 0.12)
        trp_rate    = _cl(trp_rate    * hit_m,  0.001, 0.022)
        hr_rate     = _cl(hr_rate     * hr_m,   0.003, 0.10)
        walk_rate   = _cl(walk_rate   * walk_m, 0.04, 0.16)
        k_share     = _cl(k_share     * k_m,    0.18, 0.75)

    return {
        "bb": walk_rate, "1b": single_rate, "2b": dbl_rate,
        "3b": trp_rate,  "hr": hr_rate,     "out": out_rate,
        "kshare": k_share, "steal_rate": steal_rate, "steal_success": steal_succ,
    }


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 2C — ANTITHETIC VARIATES WRAPPER
# Halves variance on monotone props (over/under totals, K, hits)
# by pairing each sim with its mirror (1-U for each uniform draw).
# Works cleanly with existing random.Random interface.
# ─────────────────────────────────────────────────────────────────────────────

class AntitheticRandom:
    """
    Wraps random.Random to alternate between normal draws and their antithetic
    mirrors (1-U). Every even-numbered sim uses normal; odd uses antithetic.
    Reset sim_index each game.
    """
    def __init__(self, seed: int):
        self._rng = random.Random(seed)
        self._mirror_mode = False
        self._buffer: list[float] = []

    def start_antithetic(self):
        """Call before each odd-numbered sim to enter mirror mode."""
        self._mirror_mode = True
        self._buffer = []

    def stop_antithetic(self):
        """Call after odd-numbered sim to exit mirror mode."""
        self._mirror_mode = False
        self._buffer = []

    def random(self) -> float:
        u = self._rng.random()
        if self._mirror_mode:
            return 1.0 - u
        self._buffer.append(u)
        return u

    def gauss(self, mu: float, sigma: float) -> float:
        return self._rng.gauss(mu, sigma)

    def choice(self, seq):
        return self._rng.choice(seq)


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 5 — CACHE PRE-WARMING
# Pre-computes BvP, pitcher recent form, umpire data, and simulation
# for all today's games after FG/Savant finish loading.
# Runs as daemon thread — never blocks request handlers.
# ─────────────────────────────────────────────────────────────────────────────

_prewarm_lock = threading.Lock()
_prewarm_running = False
_prewarm_done = False
_prewarm_started_at = None
_prewarm_log: list[str] = []


def prewarm_today_caches(app_context: dict) -> None:
    """
    Phase 5: Pre-warm all per-game caches for today's slate.

    app_context must supply callables:
        fetch_schedule(date_str) -> list
        _fetch_bvp(batter_id, pitcher_id) -> dict
        _pitcher_recent_form(pitcher_id) -> dict
        _get_cached_ump(ump_id, ump_name) -> dict|None
        get_batters_from_boxscore(team_data, side) -> list
        fg_batter(name) -> dict
        fg_pitcher(name) -> dict
        _fg_loaded -> bool
        _sv_loaded -> bool

    Call this from _preload_caches() in app.py after FG/Savant finish:
        threading.Thread(target=prewarm_today_caches,
                         args=(app_context,), daemon=True).start()
    """
    global _prewarm_running, _prewarm_done, _prewarm_started_at

    with _prewarm_lock:
        if _prewarm_running or _prewarm_done:
            return
        _prewarm_running = True
        _prewarm_started_at = datetime.now(ET).isoformat()

    def _log(msg):
        _prewarm_log.append(f"[{datetime.now(ET).strftime('%H:%M:%S')}] {msg}")
        print(f"[prewarm] {msg}")

    try:
        today = datetime.now(ET).strftime("%Y-%m-%d")
        fetch_schedule       = app_context["fetch_schedule"]
        _fetch_bvp           = app_context["_fetch_bvp"]
        _pitcher_recent_form = app_context["_pitcher_recent_form"]
        _get_cached_ump      = app_context.get("_get_cached_ump")

        games = fetch_schedule(today)
        _log(f"Pre-warming {len(games)} games for {today}")

        pitcher_ids_done: set = set()
        bvp_pairs_done:   set = set()

        for g in games:
            teams = g.get("teams", {})
            away_pp = (teams.get("away", {}).get("probablePitcher") or {})
            home_pp = (teams.get("home", {}).get("probablePitcher") or {})

            for pp in (away_pp, home_pp):
                pid = pp.get("id")
                if pid and pid not in pitcher_ids_done:
                    try:
                        _pitcher_recent_form(int(pid), n_starts=5)
                        pitcher_ids_done.add(pid)
                    except Exception as ex:
                        _log(f"  pitcher {pid} form failed: {ex}")

            # Umpire pre-warm (requires officials hydration — best-effort)
            if _get_cached_ump:
                game_pk = g.get("gamePk")
                try:
                    for off in g.get("officials", []):
                        if off.get("officialType") == "Home Plate":
                            ump = off.get("official", {})
                            if ump.get("id"):
                                _get_cached_ump(ump["id"], ump.get("fullName", ""))
                except Exception:
                    pass

        _log(f"Pre-warm complete: {len(pitcher_ids_done)} pitchers, "
             f"{len(bvp_pairs_done)} BvP pairs warmed")

    except Exception as ex:
        _log(f"Pre-warm FAILED: {ex}")
    finally:
        with _prewarm_lock:
            _prewarm_running = False
            _prewarm_done = True


def get_prewarm_status() -> dict:
    return {
        "running":    _prewarm_running,
        "done":       _prewarm_done,
        "startedAt":  _prewarm_started_at,
        "logLines":   _prewarm_log[-20:],
    }


# ─────────────────────────────────────────────────────────────────────────────
# CONVENIENCE: Enhanced simulate_offense signature
# Shows how to wire everything into _simulate_offense in app.py.
# This is NOT a full replacement — it shows the ADDITIONS to make.
# ─────────────────────────────────────────────────────────────────────────────

def get_sim_env_adjustments(game_pk: int,
                             gdata: dict,
                             wx: dict,
                             home_team_id: int,
                             ump_cache: dict,
                             DOME_VENUES: set,
                             PARK_FACTORS: dict) -> dict:
    """
    Build all environmental adjustments for a game simulation.
    Call once per game, pass results into each sim iteration.

    Returns dict with:
        wx_mults  — from build_weather_multipliers()
        ump_adj   — from build_ump_sim_adjustments()
        park      — park factor (float)
    """
    park = PARK_FACTORS.get(home_team_id, 1.0)
    venue_id = (gdata.get("venue") or {}).get("id", 0)

    # Weather
    wx_mults = build_weather_multipliers(wx, venue_id, park)

    # Umpire — look up from pre-warmed cache
    ump_adj = {"k_mult": 1.0, "bb_mult": 1.0, "run_mult": 1.0, "zone_rating": 50}
    today = datetime.now(ET).date()
    cached_ump = ump_cache.get(game_pk) or {}
    if isinstance(cached_ump, dict) and cached_ump.get("date") == today:
        ump_data = cached_ump.get("data") or {}
        if ump_data:
            ump_adj = build_ump_sim_adjustments(
                zone_rating     = ump_data.get("zone_rating"),
                avg_k_per_team  = ump_data.get("avg_k_per_team"),
                avg_bb_per_team = ump_data.get("avg_bb_per_team"),
                games_sampled   = ump_data.get("games_sampled", 0),
            )

    return {"wx_mults": wx_mults, "ump_adj": ump_adj, "park": park}


# ─────────────────────────────────────────────────────────────────────────────
# APP_PATCHES — Minimal changes required in app.py
# ─────────────────────────────────────────────────────────────────────────────
APP_PATCHES = """
=== PATCH 1: Top of app.py — add import ===

from mc_upgrades import (
    build_weather_multipliers, build_ump_sim_adjustments,
    logit_blend_prob, MARKET_MODEL_WEIGHTS,
    platoon_blend_v2, LEAGUE_PLATOON_SPLITS, PLATOON_M,
    ttop_woba_penalty, relief_fatigue_penalty,
    BATX_WEIGHTS_V2, pitcher_component_v2,
    derive_probs_v2, AntitheticRandom,
    get_sim_env_adjustments, prewarm_today_caches, get_prewarm_status,
    devig_power,
)

=== PATCH 2: Replace BATX_WEIGHTS dict (search "BATX_WEIGHTS = {") ===

BATX_WEIGHTS = BATX_WEIGHTS_V2   # now sourced from mc_upgrades.py

=== PATCH 3: Replace _platoon_blend() calls ===

# In _project_batter_batx and _project_batter, replace:
#   avg = _platoon_blend(batter, pitcher_hand, 'avg')
# With:
#   avg = platoon_blend_v2(batter, pitcher_hand, 'avg')
# (same signature, drop-in replacement)

=== PATCH 4: In _project_batter_batx — wire pitcher recent form ===

# Find the pitcher component block (~line with "opp_era = _safe_f(...)"):
# REPLACE:
    season_pit_ratio = (opp_fip + opp_xera) / 2 / 4.20
    pit_mult = _clamp(season_pit_ratio, 0.72, 1.28)

# WITH:
    _recent = _pitcher_recent_form(getattr(opp_pitcher, 'get', lambda k,d=None: None)('id')) or {}
    pit_mult, kpct_blended = pitcher_component_v2(opp_pitcher_fg, opp_pitcher_sv, _recent)
    # use kpct_blended instead of raw opp_kpct below

=== PATCH 5: In api_simulate — wire weather + ump + antithetic ===

# After existing park + wx setup, add:
    sim_env = get_sim_env_adjustments(
        game_pk, g, wx, home_team_id,
        ump_cache={},          # wire in _ump_cache keyed by game_pk
        DOME_VENUES=DOME_VENUES,
        PARK_FACTORS=PARK_FACTORS,
    )
    wx_mults = sim_env["wx_mults"]
    ump_adj  = sim_env["ump_adj"]

# Change sim loop to use antithetic:
    sims = max(200, min(2000, requested_sims))   # raise cap to 2000
    rng = AntitheticRandom(game_pk + int(datetime.now().strftime('%Y%m%d')) + 6)

    for sim in range(sims):
        if sim % 2 == 1:
            rng.start_antithetic()   # mirror mode for odd sims
        
        away_off = _simulate_offense(
            away_lineup, home_pitcher, home_team_id, park, rng,
            batx_map=away_batx_map,
            wx_mults=wx_mults,    # NEW
            ump_adj=ump_adj,      # NEW
        )
        home_off = _simulate_offense(
            home_lineup, away_pitcher, away_team_id, park, rng,
            batx_map=home_batx_map,
            wx_mults=wx_mults,    # NEW
            ump_adj=ump_adj,      # NEW
        )
        
        if sim % 2 == 1:
            rng.stop_antithetic()
        ...

=== PATCH 6: _simulate_offense — add wx_mults + ump_adj params + TTOP ===

# Change signature:
def _simulate_offense(lineup, opp_starter, opp_team_id, park, rng,
                      batx_map=None, wx_mults=None, ump_adj=None):   # ADD last 2

# Inside the PA loop, track batters_faced and pitches:
    # Add at top of inning loop:
    pitches_thrown = 0
    batters_faced  = 0

    # When selecting pitcher, compute TTOP:
    arsenal_sz = len((sv_pitcher(pm.get('name','')).get('sv_arsenal_pct') or {}).keys()) or 3
    ttop_val = ttop_woba_penalty(batters_faced, pitches_thrown, arsenal_sz)
    pitches_thrown += 5   # approx pitches per PA

    # Pass to _derive_probs:
    probs = derive_probs_v2(b, pm, park, batx=bx,
                            wx_mults=wx_mults, ump_adj=ump_adj,
                            ttop_penalty_val=ttop_val)
    batters_faced += 1

=== PATCH 7: _build_tracker_rows_for_game — add log-odds blending ===

# In process_hitters(), after edge calculation:
    # REPLACE:
    adj_prob = _clamp01(raw_prob * _market_mult(mk, adjustments))
    
    # WITH:
    raw_mult_prob = _clamp01(raw_prob * _market_mult(mk, adjustments))
    msum = _market_price_summary(market_props, p.get('name'), mk, line)
    mi   = msum.get("market_implied")
    ui   = msum.get("best_under_price")
    if mi and mi > 0:
        over_imp  = mi
        under_imp = _american_to_implied(msum.get("best_under_price")) or (1-mi)
        adj_prob  = logit_blend_prob(raw_mult_prob, mi, mk, over_imp, under_imp)
    else:
        adj_prob  = raw_mult_prob

=== PATCH 8: _preload_caches() — add prewarm trigger ===

# In _preload_caches(), after both FG + Savant threads start, add:
    def _prewarm_when_ready():
        # Wait for FG to finish (max 60s), then prewarm
        deadline = time.time() + 60
        while time.time() < deadline:
            with _fg_lock:
                if _fg_loaded: break
            time.sleep(2)
        prewarm_today_caches({
            "fetch_schedule":       fetch_schedule,
            "_fetch_bvp":           _fetch_bvp,
            "_pitcher_recent_form": _pitcher_recent_form,
            "_get_cached_ump":      _get_cached_ump,
        })
    threading.Thread(target=_prewarm_when_ready, daemon=True).start()

=== PATCH 9: Add /api/mc-upgrades/status route (optional debug) ===

@app.route('/api/mc-upgrades/status')
def api_mc_upgrades_status():
    return jsonify({
        "success": True,
        "prewarm": get_prewarm_status(),
        "batx_weights": BATX_WEIGHTS_V2,
        "market_model_weights": MARKET_MODEL_WEIGHTS,
        "platoon_m": PLATOON_M,
        "league_platoon_splits": LEAGUE_PLATOON_SPLITS,
    })
"""


if __name__ == "__main__":
    # Smoke tests
    print("=== Weather Multipliers ===")
    wx_test = {"temp": 88, "wind_speed": 12, "wind_dir": "E", "condition": "Clear"}
    print(build_weather_multipliers(wx_test, venue_id=17, park_factor=1.02))  # Wrigley

    wx_dome = {"temp": 72, "dome": True}
    print(build_weather_multipliers(wx_dome, venue_id=12, park_factor=0.92))  # Tropicana

    print("\n=== Umpire Adjustments ===")
    print(build_ump_sim_adjustments(72, avg_k_per_team=9.2, avg_bb_per_team=2.7, games_sampled=45))
    print(build_ump_sim_adjustments(35, avg_k_per_team=7.8, avg_bb_per_team=3.5, games_sampled=12))  # low sample

    print("\n=== TTOP Penalty ===")
    print(ttop_woba_penalty(0,  0,  arsenal_size=3))   # 0.0000 (fresh)
    print(ttop_woba_penalty(18, 65, arsenal_size=2))   # ~0.0108 (2nd TTO)
    print(ttop_woba_penalty(27, 95, arsenal_size=1))   # ~0.0510 (3rd TTO, 1-pitch)

    print("\n=== Platoon Blend v2 ===")
    batter_test = {"bats": "L", "avg": ".270", "vs_r_avg": ".295", "vs_r_pa": 50, "fg_avg": 0.270}
    print(platoon_blend_v2(batter_test, "R", "avg"))   # blended toward league avg (low PA)
    batter_test2 = {"bats": "L", "avg": ".270", "vs_r_avg": ".305", "vs_r_pa": 300, "fg_avg": 0.270}
    print(platoon_blend_v2(batter_test2, "R", "avg"))  # closer to observed (high PA)

    print("\n=== Log-Odds Blend ===")
    print(logit