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
