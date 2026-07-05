# Accuracy Blueprint — the path to industry-leading accuracy

**The operational definition of "most accurate":** in sports analytics the
only accuracy standard that can't be gamed is the market itself. The closing
line at sharp books is the strongest publicly observable predictor in the
industry — every published model, tout, and analytics product is measured
against it, and almost all of them lose to it. An app is *the most accurate
in the industry* when, market by market, its calibrated probabilities carry a
lower Brier score than the de-vig'd closing line and its picks beat the close
at a sustained, statistically significant rate. That is the bar this codebase
is built to clear, and every stage below is gated on a measurable step toward
it. CLV is already the tracker's primary KPI (`primaryKpi` in
`_tracker_live_summary`), so progress against this definition is visible on
the hero of `/tracker` every day.

The strategy has one core principle: **never guess what can be measured, and
never trust a measurement the pipeline can't audit.** Each stage either adds
predictive signal or tightens the loop that converts signal into calibrated,
market-beating probabilities.

---

## Stage 0 — Airtight measurement loop *(SHIPPED)*

A model only earns its held-out skill in production if miscalibration and
train/serve gaps are caught the day they appear. Shipped in this branch:

- `models/model_metrics.json` now carries held-out benchmarks for **all**
  seven models (was missing HR/TB/RBI — those markets could never be flagged
  `no_edge`/`degraded`). `regenerate_models.py` writes it automatically so it
  can never drift from the artifacts again.
- `/api/diag/serve-parity` now covers all five modeled markets (was 2/5) —
  the tool that catches silent feature-wiring gaps, the #1 killer of
  production accuracy (see the k-model rolling-form incident in CLAUDE.md).
- `xgb_engaged` in `/api/calibration/markets` reports the true model surface.

**Gate:** every modeled market shows `on_track` or an actionable flag in
`/api/calibration/markets`; zero `serve_gap` features in `/api/diag/serve-parity`.

## Stage 1 — Measured, self-correcting probability mixing *(SHIPPED)*

Three layers now learn from graded outcomes instead of hand-tuned constants:

1. **Smart-Consensus** (existing): XGB-vs-analytic blend shifted by measured
   per-model Brier once ≥40 graded picks per market.
2. **`prop_calibration`** (existing): per-market isotonic/log-odds recentre of
   displayed probabilities against realized outcomes.
3. **`market_blend_learner`** (new): the model-vs-market weight in
   `logit_blend_prob` — previously a frozen intuition table — is now fit to
   the Brier-optimal mix per market from graded history, shrunk toward the
   prior by sample size. As graded volume grows, every displayed probability
   converges on the optimal information mix automatically.

**Gate:** `blend_weights` in `/api/calibration/markets` shows `learned` with
`applied_brier < prior_brier` per market; no `drift_alerts`.

## Stage 2 — Retrain with historically reconstructable context *(SHIPPED — measured)*

`regenerate_models.py` dropped park/weather/umpire as "not reconstructable" —
but **park is**: the home venue of every historical game is known. Shipped:
hand-aware `park_hr` (Yankee porch 1.34 L / Oracle 0.80 L / …) into the HR
model and `park_factor` into TB, reconstructed per game from Statcast
`home_team` with verbatim copies of the app's park tables (train/serve
identical), and served live from every power-model call site.

**Measured outcome (honest):** global held-out AUC/Brier were flat-to-
marginally-better (HR 0.6839→0.6839, TB 0.6237→0.6241) — a venue multiplier
re-levels probabilities across parks rather than re-ranking batters, so the
global averages barely move. On the metric it targets, it delivered: the
weighted mean per-venue |predicted − actual| gap on the 2025 held-out season
improved 1.5% for HR (18/30 venues) and 5% for TB (max venue error
0.086→0.081). Strictly non-worse globally, better where it aims, zero serve
cost.

**Stage 2b (learned season-specific park factors) — RUN, gate NOT met,
hypothesis refuted.** Implemented home-vs-road learned factors from our own
Statcast pull (recency-weighted trailing 3 seasons, shrunk toward 1.0,
leakage-safe: season S trains on factors ending S−1; exported to
`data/park_factors_learned.json` for serve with static fallback). Measured
against the ≥15% venue-gap gate: HR −2.9%, TB −1.2%, TB2.5 +4.2% — **the
venue miscalibration is not park-level-driven.** Park is a rank-14–19
feature in these models, so even corrected levels barely move venue
calibration; the residual venue gaps are model bias, not table staleness.
The learned infrastructure ships anyway: global AUC/Brier identical within
noise, and it self-adapts to venue changes (Sacramento, Steinbrenner) where
the static table was demonstrably stale (Coors TB 1.088 learned vs 1.00
static). The right lever for venue-level calibration is a per-venue
recalibration layer in `prop_calibration`, fed by graded picks — logged as
future work, gated on graded volume per venue.

## Stage 3 — Cover the whole board with trained models *(SHIPPED — measured)*

The stacked fusion previously fired only at each model's trained line;
off-line prices fell back to unvalidated analytic probs. Shipped seven
alt-line models, all passing the held-out gate — several discriminate
*better* than their standard-line siblings:

| model | held-out AUC | Brier skill vs base |
|-------|-------------|---------------------|
| hits 1.5 | 0.633 | +0.006 |
| tb 2.5 | 0.640 | +0.006 |
| tb 3.5 | 0.663 | +0.005 |
| rbi 1.5 | 0.626 | +0.002 |
| k 2.5 | 0.716 | +0.009 |
| k 6.5 | 0.737 | +0.026 |
| k 7.5 | 0.759 | +0.017 |

Routing is line-exact (`xgb_line_ready` / `xgb_batter_prob_full`): a row only
gets an XGB probability from a model trained at that threshold; the K
nearest-line fallback is capped at 1.0 K (a 9.5 line can no longer silently
borrow the 5.5 model). Verdict tiers anchor to line-aware base rates derived
from training positive rates, so a 31% probability reads STRONG_BET at TB 2.5
(base .23) and LEAN_UNDER at TB 1.5 (base .40). Benchmarks flow into
`/api/calibration/markets` automatically via `TRACKER_MARKET`.

**Live gate to watch:** share of tracker rows with `modelSource='stacked'`
rising toward 80%+ of prop volume as alt-line prices get captured.

## Stage 4 — Signal no one else is modeling yet *(SHIPPED — measured honestly)*

**Bat-tracking (shipped, marginal by the numbers, kept for structure).**
Season bat speed / fast-swing rate / squared-up rate / blast rate
(2024–2025 leaderboards pulled for training; 2026 served live) are wired
through `_build_batter_market_features` with a key property: **pre-2024
training rows are never imputed** — `bt_*` features stay NaN and XGBoost's
native missing-branch learns the pre-bat-tracking era, and a batter without
a bat-tracking row at serve passes NaN too (identical semantics, no
fabricated medians). The scorer does the lookup itself (local day-cached
CSV), so every call site has parity for free.

Measured on 2025 held-out: tb_2.5 +0.0011 AUC (0.6407), tb_3.5 +0.0008
(0.6638), everything else flat within ±0.0002, nothing regressed.
Feature-importance ranks 19–30 — season-level bat tracking is largely
collinear with the EV/barrel/ISO skills the models already carry. Kept
because it costs nothing at serve and its expected live edge is
early-season (bat speed stabilizes in ~50 swings vs ~200+ PA for xSLG),
which a full-season holdout structurally can't measure. The weekly regen
loop re-measures it every run.

- **Brain overlays** (still open): the `/api/brain/*` ingest path lets
  proprietary data flow into the same caches the models read — a structural
  data advantage competitors can't replicate.

## Stage 5 — Compounding: volume × automation

Every learning layer in this app gets strictly better with graded volume:
Smart-Consensus (≥40/market), blend learner (≥60/market), isotonic
recalibration (≥80/market). The compounding loop:

1. Auto-capture grades the full board daily (`TRACKER_AUTO_SYNC_ENABLED=1`).
2. Closing-line capture prices every pick against the close (true CLV).
3. **Weekly regen is automated** (`.github/workflows/model-regen.yml`,
   Mondays 09:00 UTC + manual dispatch): retrains every market, refreshes
   bat-tracking leaderboards + learned park factors + benchmarks, and opens
   a PR — the held-out ship gate runs inside, and nothing reaches production
   unreviewed. Drift guard flags any live regression within ~25 graded picks.

Nothing in this loop requires manual tuning — the operator's job reduces to
reviewing the weekly regen PR and reading `/api/calibration/markets`.

**Gate (the industry-leading bar):** rolling 90-day Beat-Close% > 52.4% with
n ≥ 500 graded-with-CLV picks, and per-market live Brier at or below the
de-vig'd market's own Brier over the same window. Clearing that bar means the
app's numbers are more accurate than the sharpest consensus in the industry —
which is what "most accurate in sports analytics" means in practice.

---

### Status ledger (all stages executed; measured outcomes)

| # | Item | Outcome |
|---|------|---------|
| 1 | Stage 2 park-factor retrain (HR/TB) | **Shipped.** Park features rank 11; venue calibration gap improved (HR −1.5%, TB −5% at the time); global AUC level |
| 2b | Learned season-specific park factors | **Infrastructure shipped, gate NOT met** (HR −2.9%, TB −1.2%, TB2.5 +4.2% venue gap). Hypothesis refuted: venue gap is model bias, not table staleness. Kept for self-adapting venue changes |
| 3 | Alt-line models (7 new) | **Shipped.** All pass held-out gate; k_7.5 AUC 0.759, tb_3.5 0.663; whole board now stacked-fusion-covered |
| 4 | Bat-tracking features | **Shipped, marginal** (tb alt lines +0.001, rest flat, ranks 19–30). Zero serve cost; early-season upside unmeasurable in full-season holdout; re-measured weekly |
| 5 | Weekly regen automation | **Shipped** (`model-regen.yml` — retrain + gate + PR, Mondays) |

### Stage 6 — Un-gating the volume-gated layers (calibration backfill)

The volume gates themselves turned out to be code-addressable: the isotonic
recalibrator needs (model prob → outcome) pairs, and the current season's
games are already played, out-of-sample (models train through 2024), and
feature-reconstructable. `calibration_backfill.py` scores every committed
model over the season to date and grades against actual outcomes —
thousands of pairs per market on day one instead of ≥80 graded picks after
weeks. `_build_prop_calibrator` consumes them as a *prior*: top-up to 600
pairs, displaced 4:1 by real tracker picks (which carry the exact fused
`preCalProb` distribution), fully retired at ~150 picks/market. Refreshed
weekly by the regen workflow.

### What remains genuinely time-gated

Only the market-anchored layers that need real prices: Smart-Consensus
(≥40 graded picks carrying both per-model probs), the blend learner (≥60,
needs de-vig'd opening prices), and the industry-leading bar itself
(rolling 90-day Beat-Close% > 52.4%, n ≥ 500 with CLV — needs real closing
lines, which cannot be backfilled without a paid historical-odds source).
Keep auto-capture + closing-line capture on, review the weekly regen PR,
and watch `/api/calibration/markets`. Displayed-probability calibration —
the thing users see — is armed from day one via the backfill.

Stages 0-1 (shipped) are the foundation: they guarantee that every point of
skill added by Stages 2-4 survives into production and that any decay is
caught and corrected automatically. That closed loop — measure, learn,
retrain, verify — is the most reliable and efficient path to being, and
staying, the most accurate app in the industry.
