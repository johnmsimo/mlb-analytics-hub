# Phase 5.3 decision intelligence

Phase 5.3 admits fresh sportsbook consensus as **decision-only evidence**. It
does not add prices to any model feature list, retrain or promote a champion,
place a wager, approve a decision, or make a row actionable.

## Fail-closed decision path

A candidate can qualify for human review only when all of the following hold:

- the canonical market and over/under side are supported;
- market and market-side validation are both promoted;
- at least two independent sportsbooks provide complete two-way prices at the
  candidate's exact line;
- every accepted quote has a real book, named source, timestamp, and age of no
  more than five minutes;
- de-vigged fair probabilities have a spread of no more than eight percentage
  points;
- model edge and expected value clear the market-specific no-bet thresholds.

The engine price-shops only among the accepted quotes. A qualified result is
still `actionable: false`, `decisionApproved: false`, and
`decisionReviewRequired: true`.

## Market thresholds

| Market | Minimum edge | Minimum EV |
| --- | ---: | ---: |
| Batter hits | 2.5% | 3.0% |
| Batter RBIs | 3.5% | 4.0% |
| Batter total bases | 3.0% | 3.5% |

Stake output is a preview, not an instruction. It uses quarter Kelly, is capped
at 1% of bankroll, and is emitted only after every decision gate passes. Human
review and the existing authenticated Tracker save boundary remain mandatory.
