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

## Stage 2 — Retrain with historically reconstructable context

`regenerate_models.py` currently drops park/weather/umpire as "not
reconstructable" — but **park is**: the home venue of every historical game is
known, and season park factors are a static join. Park is one of the strongest
real HR/TB signals and the live scorer already serves `park_factor` for hits.

- Add `park_factor` (and venue HR-index) to the HR/TB training matrices;
  retrain via `python regenerate_models.py --markets hr tb`.
- Add month-of-season temperature climatology per venue (also historically
  known) as a weather proxy for the power markets.

**Gate:** held-out 2025 test AUC for HR ≥ 0.70 (from 0.68) and TB ≥ 0.64
(from 0.62), both with predicted-mean ≈ actual (the regen gate already
enforces beating base-rate Brier).

## Stage 3 — Cover the whole board with trained models

The stacked fusion only fires at each model's trained line; off-line markets
(TB 2.5, hits 1.5, K alt-lines beyond 3.5/4.5/5.5) fall back to analytic
probs. Train the alt-line variants (the pipeline already parameterizes line —
it's a config addition per market) so every price on the board gets a
held-out-validated probability.

**Gate:** share of tracker rows with `modelSource='stacked'` rises above 80%
of prop volume; alt-line markets appear in `/api/calibration/markets` with
their own benchmarks.

## Stage 4 — Signal no one else is modeling yet

- **Bat-tracking**: `savant_bat_tracking_{year}.csv` is already ingested.
  Swing speed, squared-up rate, and blast rate are leading indicators of
  contact quality that most competitors have not folded into prop models —
  wire them into `_build_batter_market_features` + `regenerate_models.py`
  (both sides, per the serve-parity rule) and let the held-out gate decide.
- **Brain overlays**: the `/api/brain/*` ingest path lets proprietary data
  (internal projections, scouting) flow into the same caches the models read
  — a structural data advantage competitors can't replicate.

**Gate:** feature-importance rank of bat-tracking features in retrained
models; held-out AUC/Brier improvements pass the regen ship gate.

## Stage 5 — Compounding: volume × automation

Every learning layer in this app gets strictly better with graded volume:
Smart-Consensus (≥40/market), blend learner (≥60/market), isotonic
recalibration (≥80/market). The compounding loop:

1. Auto-capture grades the full board daily (`TRACKER_AUTO_SYNC_ENABLED=1`).
2. Closing-line capture prices every pick against the close (true CLV).
3. Weekly `regenerate_models.py` run refreshes artifacts + benchmarks in one
   command; drift guard flags any regression within ~25 graded picks.

Nothing in this loop requires manual tuning — the operator's job reduces to
reading `/api/calibration/markets` and shipping retrains when Stage 2-4
features land.

**Gate (the industry-leading bar):** rolling 90-day Beat-Close% > 52.4% with
n ≥ 500 graded-with-CLV picks, and per-market live Brier at or below the
de-vig'd market's own Brier over the same window. Clearing that bar means the
app's numbers are more accurate than the sharpest consensus in the industry —
which is what "most accurate in sports analytics" means in practice.

---

### Priority order (reliability-per-effort)

| # | Item | Effort | Expected impact |
|---|------|--------|-----------------|
| 1 | Stage 2 park-factor retrain (HR/TB) | one training run | biggest single model-skill gain available |
| 2 | Stage 3 alt-line models | config + training runs | extends validated skill to the whole board |
| 3 | Stage 4 bat-tracking features | moderate (two-sided wiring) | novel signal, first-mover accuracy edge |
| 4 | Stage 5 weekly regen automation | small (CI/cron) | locks in compounding, prevents decay |

Stages 0-1 (shipped) are the foundation: they guarantee that every point of
skill added by Stages 2-4 survives into production and that any decay is
caught and corrected automatically. That closed loop — measure, learn,
retrain, verify — is the most reliable and efficient path to being, and
staying, the most accurate app in the industry.
