# Phase 6 — Market-beating Accuracy and Intelligence

Phase 6 is the proof layer above the Phase 5 intelligence stack. It does not
add another opaque score. It answers whether the model is actually more
accurate than the market on the same decisions, where it is wrong, and whether
any improvement is statistically strong enough to change production policy.

## 6.0 measurement contract

Every eligible comparison binds two immutable receipts:

1. Phase 5.4 freezes the identity, market, side, exact line, served probability,
   source, model version, and prediction timestamp before the outcome.
2. Phase 6.0 freezes a complete over/under price from one real sportsbook at
   the exact line near first pitch, selects the prediction side, de-vigs the
   pair with the power method, and excludes all outcome and model fields.

Only after both receipts verify can the row enter the paired scorecard. The
scorecard contains aggregates only; raw Tracker records, dollar stakes,
bankroll, settings, notes, admin identity, and rejected rows remain private.
Legacy rows without both receipts are withheld rather than reconstructed after
the fact, so the Phase 6 sample grows prospectively from verified captures.

The de-vigged side probability is stored separately as
`closingFairProbability` for model-vs-market Brier and ECE. Conventional CLV
continues to compare the released side price with the selected raw closing side
price; the two denominators are not silently mixed.

## Industry-claim gate

The 90-day overall state is `market_leading` only when all conditions hold:

- at least 500 paired graded model/closing observations;
- the upper bound of the 95% interval for model-minus-market Brier is below 0;
- at least 500 rows have verified opening/closing lineage for CLV;
- Beat-Close rate is greater than 52.4%;
- the 95% Wilson lower bound for Beat-Close is greater than 50%.

Otherwise the state is `insufficient_sample`, `insufficient_clv_sample`, or
`not_market_leading`. The API never converts missing evidence into a claim.

## Phase 6 sequence

| Phase | Deliverable | Production effect |
| --- | --- | --- |
| 6.0 | Verified close + accuracy control plane | Measurement only |
| 6.1 | Contextual error atlas | Diagnosis only |
| 6.2 | Champion/challenger intelligence | Shadow proposals |
| 6.3 | Live drift intervention | Fail-closed downgrade/suppression |
| 6.4 | Simulation/correlation calibration | Verification and blocking |
| 6.5 | Decision policy optimization | Reviewed threshold proposals |

No Phase 6 component may place a wager, auto-promote a champion, silently
change a probability, or expose private Tracker data.

## 6.1–6.5 evidence chain

Phase 6.5 adds a third prospective receipt to every newly tracked decision.
The receipt binds the Phase 5.4 prediction fingerprint to privacy-safe context
buckets, shadow challenger probabilities, simulation inputs, and the decision
policy inputs that existed when the recommendation was saved. It explicitly
excludes outcomes and closing fields. A later edit to any bound input changes
the SHA-256 fingerprint and removes the observation from every Phase 6.1–6.5
aggregate.

An observation enters `/api/accuracy/intelligence?window=120` only when all
three receipts remain intact:

1. Phase 5.4 prediction receipt `5.4.0`;
2. Phase 6.0 exact-line two-way closing benchmark receipt `6.0`;
3. Phase 6.5 pre-outcome intelligence evidence receipt `6.5.0`.

The response returns bounded aggregate counts and fixed rejection codes. It
never returns Tracker rows, rejected row contents, bankroll, dollar stake,
notes, settings, or customer identity.

## 6.1 contextual error atlas

The atlas compares paired model and verified-closing Brier and ECE across
market, side, line band, sportsbook, lineup status, pitcher hand, park,
weather, umpire, model version, confidence tier, and quote-freshness band.
Cohorts below 30 observations are suppressed. A cohort needs 100 observations
before its paired 95% interval may label the model better or worse; otherwise
it remains exploratory or inconclusive. The atlas diagnoses risk and cannot
change serving policy.

## 6.2 champion/challenger intelligence

Pre-calibration, component-model, and simulation probabilities run as shadow
challengers on the exact observations served by the champion. Each report uses
the oldest 70% as reference and the newest 30% as a temporal holdout. A review
candidate needs at least 300 total and 100 holdout observations, paired Brier
confidence entirely better than both the champion and closing market, and no
material ECE regression. The result is a review artifact only; promotion still
requires an explicit code and deployment decision.

## 6.3 live drift intervention

Each market compares a 14-day recent window with the preceding 60-day baseline.
At least 30 recent and 100 baseline observations are required. The bounded
states are `insufficient_sample`, `stable`, `watch`, `degraded`, and
`suppressed`:

- `watch` preserves the candidate but downgrades its confidence;
- `degraded` filters the candidate to research-only;
- `suppressed` filters the candidate to no-bet.

These interventions run after existing market-validation gates and before a
candidate reaches a recommendation surface. They do not alter probabilities,
retrain a model, or promote a challenger.

## 6.4 simulation and correlation calibration

Prospective simulation probabilities are graded with Brier and ECE. When a
realized stat and a frozen p10–p90 distribution are available, the report also
measures interval coverage and mean absolute outcome error. Simulation status
requires 100 verified observations.

Same-game market/side pairs are measured from joint outcomes. A factor is
eligible for Guided Parlays only after 50 verified pairs and a directional 95%
joint-lift interval; the factor is bounded to 0.50–1.50. Unmeasured or
inconclusive same-game dependence remains visible and untrackable.

## 6.5 decision policy lab

For each market, the policy lab evaluates a bounded edge-threshold grid on a
70% reference sample, then measures the selected proposal on the untouched 30%
holdout. A proposal needs 150 reference and 75 holdout selections and must
improve a shadow objective that reports ROI, verified CLV, and drawdown
together. Every proposal carries a prospective-selection warning. The API can
emit `review_candidate`, but automatic threshold and staking changes remain
disabled.

## Public and production contracts

The verification page renders five aggregate cards and fails closed to an
unavailable state when contract `6.5` cannot be validated. The product journey
advertises all phase versions and sample gates. The production contract gate
checks the endpoint, receipt chain, denominators, raw-row boundary, drift and
correlation safeguards, and all automatic-change prohibitions after deployment.
