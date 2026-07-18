"""
Analytic NFL prop projections — honest v1, no ML.

For each (player, market): mean = blend of season per-game and last-4 form,
scaled by a damped opponent defense-vs-position factor; spread = the player's
own game-log variance shrunk toward a market prior. Yardage/receptions use a
normal tail, TD counts use Poisson. Rows are tagged modelSource='analytic' so
a future ML layer can slot in the way XGB did in the MLB hub.

Self-test + leave-forward backtest: `python projections.py`
(backtest projects each week from ONLY prior weeks, then compares predicted
P(over) to realized over-rate — the honesty check, not a skill claim).
"""
from __future__ import annotations

import math

import nfl_data

# marketKey (tracker convention) -> (stat column, distribution)
# Distributions were chosen against the leave-forward backtest (see bottom):
# QB pass yards are near-symmetric (normal); rush/rec yards are strongly
# right-skewed (log-normal — a normal at the mean overstated P(over) by
# 8-17 points); receptions are target-driven counts (Poisson); TDs are
# Poisson with a damping for blowout clustering (overdispersion).
MARKETS = {
    "pass_yds":   ("passing_yards", "normal"),
    "pass_tds":   ("passing_tds", "poisson"),
    "rush_yds":   ("rushing_yards", "lognormal"),
    "receptions": ("receptions", "poisson"),
    "rec_yds":    ("receiving_yards", "lognormal"),
    "anytime_td": ("_scrimmage_tds", "poisson_damped"),
}
_LOG_SHIFT = 8.0            # log-normal shift: handles 0 and small negatives
_LOG_SD_PRIOR = {"rush_yds": 0.85, "rec_yds": 0.90}
_TD_DAMP = 0.85             # overdispersion damp on anytime-TD lambda

# Minimum projected volume for a market to be modelable — below this no book
# posts a line and the skewed low-usage tail wrecks calibration. Routes use
# this to suppress model probs on fringe rows; the backtest applies it too.
MIN_MEAN = {"pass_yds": 100.0, "pass_tds": 0.5, "rush_yds": 15.0,
            "receptions": 1.5, "rec_yds": 12.0, "anytime_td": 0.15}

ODDS_KEY_TO_MARKET = {
    "player_pass_yds": "pass_yds",
    "player_pass_tds": "pass_tds",
    "player_rush_yds": "rush_yds",
    "player_receptions": "receptions",
    "player_reception_yds": "rec_yds",
    "player_anytime_td": "anytime_td",
}
MARKET_LABELS = {
    "pass_yds": "Pass Yards", "pass_tds": "Pass TDs", "rush_yds": "Rush Yards",
    "receptions": "Receptions", "rec_yds": "Rec Yards", "anytime_td": "Anytime TD",
}

# Market-level SD priors (typical single-game spread) + shrink weight.
_SD_PRIOR = {"pass_yds": 70.0, "rush_yds": 28.0, "rec_yds": 28.0,
             "receptions": 2.0, "pass_tds": 1.0, "anytime_td": 0.6}
_SD_PRIOR_N = 6
_MIN_GAMES = 3
_RECENT_W = 0.40          # weight on last-4 form vs season mean
_OPP_DAMP = 0.5           # damping on the defense-vs-position ratio
_OPP_CLAMP = (0.75, 1.25)

# Which dvp ratio applies per market (looked up under the player's pos group).
_DVP_STAT = {"pass_yds": "passing_yards", "pass_tds": "passing_tds",
             "rush_yds": "rushing_yards", "receptions": "receptions",
             "rec_yds": "receiving_yards"}


def _stat_value(row: dict, col: str) -> float:
    if col == "_scrimmage_tds":
        return row["rushing_tds"] + row["receiving_tds"]
    return row[col]


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _poisson_sf(k: int, lam: float) -> float:
    """P(X >= k) for X ~ Poisson(lam)."""
    if lam <= 0:
        return 0.0
    term, cdf = math.exp(-lam), math.exp(-lam)
    for i in range(1, k):
        term *= lam / i
        cdf += term
    return max(0.0, min(1.0, 1.0 - cdf))


def opponent_factor(market: str, position: str, opponent: str, dvp: dict) -> float:
    """Damped, clamped defense-vs-position multiplier (1.0 = league average)."""
    if market == "anytime_td":
        cell = (dvp.get(opponent) or {}).get(position if position in nfl_data.POS_GROUPS else "WR") or {}
        num = cell.get("rushing_tds", 0) + cell.get("receiving_tds", 0)
        den = cell.get("rushing_tds_ratio"), cell.get("receiving_tds_ratio")
        ratios = [r for r in den if isinstance(r, (int, float)) and r > 0]
        ratio = sum(ratios) / len(ratios) if ratios else 1.0
        _ = num
    else:
        stat = _DVP_STAT.get(market)
        grp = position if position in nfl_data.POS_GROUPS else "WR"
        ratio = ((dvp.get(opponent) or {}).get(grp) or {}).get(f"{stat}_ratio", 1.0)
        if not isinstance(ratio, (int, float)) or ratio <= 0:
            ratio = 1.0
    f = 1.0 + (ratio - 1.0) * _OPP_DAMP
    return max(_OPP_CLAMP[0], min(_OPP_CLAMP[1], f))


def _shrunk_sd(vals: list[float], mean: float, prior_sd: float) -> float:
    n = len(vals)
    var = (sum((v - mean) ** 2 for v in vals) / (n - 1)) if n > 1 else 0.0
    return math.sqrt((n * var + _SD_PRIOR_N * prior_sd ** 2) / (n + _SD_PRIOR_N))


def project_stat(rows: list[dict], market: str, opponent: str | None = None,
                 dvp: dict | None = None, position: str = "WR") -> dict | None:
    """Project one player-market from their game rows (chronological).
    Returns distribution params or None below the games floor. `mean` is the
    projection headline (the distribution median for log-normal markets —
    that's what a book line anchors to)."""
    col, dist = MARKETS[market]
    vals = [_stat_value(r, col) for r in rows]
    n = len(vals)
    if n < _MIN_GAMES:
        return None

    opp_f = 1.0
    if opponent and dvp:
        opp_f = opponent_factor(market, position, opponent, dvp)

    season_mean = sum(vals) / n
    l4 = vals[-4:]
    l4_mean = sum(l4) / len(l4)
    blend = (1 - _RECENT_W) * season_mean + _RECENT_W * l4_mean

    out = {"n": n, "opp_factor": round(opp_f, 3), "dist": dist,
           "season_mean": round(season_mean, 2), "l4_mean": round(l4_mean, 2)}

    if dist == "lognormal":
        logs = [math.log(max(v, -5.0) + _LOG_SHIFT) for v in vals]
        mu_season = sum(logs) / n
        mu_l4 = sum(logs[-4:]) / len(logs[-4:])
        mu = (1 - _RECENT_W) * mu_season + _RECENT_W * mu_l4 + math.log(opp_f)
        sigma = _shrunk_sd(logs, mu_season, _LOG_SD_PRIOR[market])
        out.update({"mu": round(mu, 4), "sigma": round(sigma, 4),
                    "mean": round(math.exp(mu) - _LOG_SHIFT, 2)})
    elif dist == "normal":
        mean = blend * opp_f
        sd = max(_shrunk_sd(vals, season_mean, _SD_PRIOR[market]), 1.0)
        out.update({"mean": round(mean, 2), "sd": round(sd, 2)})
    else:  # poisson variants
        lam = max(blend * opp_f, 1e-6)
        if dist == "poisson_damped":
            lam *= _TD_DAMP
        out.update({"mean": round(lam, 3)})
    return out


def prob_over(proj: dict, line: float) -> float:
    """P(stat > line). Half-point lines make > vs >= moot for counts."""
    dist = proj["dist"]
    if dist in ("poisson", "poisson_damped"):
        k = math.floor(line) + 1
        return round(_poisson_sf(k, max(proj["mean"], 1e-6)), 4)
    if dist == "lognormal":
        z = (math.log(max(line, -5.0) + _LOG_SHIFT) - proj["mu"]) / max(proj["sigma"], 1e-6)
        return round(1.0 - _norm_cdf(z), 4)
    z = (line - proj["mean"]) / max(proj["sd"], 1e-6)
    return round(1.0 - _norm_cdf(z), 4)


def relevant_markets(position: str) -> list[str]:
    if position == "QB":
        return ["pass_yds", "pass_tds", "rush_yds", "anytime_td"]
    if position == "RB":
        return ["rush_yds", "receptions", "rec_yds", "anytime_td"]
    if position in ("WR", "TE"):
        return ["receptions", "rec_yds", "anytime_td"]
    return []


# -------------------------------------------------------------------- backtest

def backtest_market(season: int, market: str, min_prior_games: int = 4) -> dict:
    """Leave-forward calibration check: project each player-week from ONLY
    prior weeks at a synthetic line (prior mean rounded to x.5), grade vs the
    actual stat. Reports predicted-vs-actual over rate + reliability buckets."""
    logs = nfl_data.player_game_logs(season)
    col, _dist = MARKETS[market]
    preds: list[tuple[float, int]] = []
    for _pid, rows in logs.items():
        for i in range(min_prior_games, len(rows)):
            prior, this = rows[:i], rows[i]
            pos = this["position"]
            if market not in relevant_markets(pos):
                continue
            proj = project_stat(prior, market, opponent=None, dvp=None, position=pos)
            if not proj or proj["mean"] < MIN_MEAN[market]:
                continue
            if market == "anytime_td":
                line = 0.5
            else:
                line = math.floor(proj["season_mean"]) + 0.5
            p = prob_over(proj, line)
            actual = 1 if _stat_value(this, col) > line else 0
            preds.append((p, actual))
    if not preds:
        return {"market": market, "n": 0}
    n = len(preds)
    mean_p = sum(p for p, _ in preds) / n
    actual_rate = sum(a for _, a in preds) / n
    buckets = {}
    for p, a in preds:
        b = min(int(p * 5), 4)
        cell = buckets.setdefault(b, [0, 0])
        cell[0] += p
        cell[1] += a
    counts = {b: sum(1 for p, _ in preds if min(int(p * 5), 4) == b) for b in buckets}
    table = {f"{b*20}-{b*20+20}%": {
        "n": counts[b],
        "pred": round(buckets[b][0] / counts[b], 3),
        "actual": round(buckets[b][1] / counts[b], 3)}
        for b in sorted(buckets)}
    brier = sum((p - a) ** 2 for p, a in preds) / n
    return {"market": market, "n": n, "mean_pred": round(mean_p, 3),
            "actual_rate": round(actual_rate, 3), "brier": round(brier, 4),
            "buckets": table}


def _selftest() -> None:
    rows = [{"passing_yards": y, "passing_tds": t, "rushing_yards": 10,
             "rushing_tds": 0, "receiving_yards": 0, "receiving_tds": 0,
             "receptions": 0}
            for y, t in [(250, 2), (310, 1), (180, 0), (275, 3), (220, 1), (290, 2)]]
    proj = project_stat(rows, "pass_yds")
    assert proj and 230 < proj["mean"] < 280, proj
    p_low, p_hi = prob_over(proj, 199.5), prob_over(proj, 299.5)
    assert p_low > 0.6 > 0.35 > p_hi, (p_low, p_hi)
    tds = project_stat(rows, "pass_tds")
    assert tds and 1.0 < tds["mean"] < 2.2, tds
    p15 = prob_over(tds, 1.5)
    assert 0.3 < p15 < 0.75, p15
    assert project_stat(rows[:2], "pass_yds") is None          # games floor
    # opponent damping is clamped
    dvp = {"XX": {"QB": {"passing_yards_ratio": 2.0}}}
    f = opponent_factor("pass_yds", "QB", "XX", dvp)
    assert f == _OPP_CLAMP[1], f
    print("projections self-test OK")


if __name__ == "__main__":
    _selftest()
    season = nfl_data.stats_season()
    print(f"backtest season {season}:")
    for mk in MARKETS:
        r = backtest_market(season, mk)
        if r.get("n"):
            print(f"  {mk:11s} n={r['n']:5d} pred={r['mean_pred']:.3f} "
                  f"actual={r['actual_rate']:.3f} brier={r['brier']:.4f}")
