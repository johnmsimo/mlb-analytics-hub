# Phase 5.4 continuous learning

Phase 5.4 closes the measurement loop without allowing the application to
self-modify. Every new Tracker prediction receives an immutable pregame receipt
before an outcome exists. The receipt binds its identity, market, side, exact
line, served probability, optional pre-calibration probability, opening market
evidence, source, model version, and prediction time to one SHA-256 fingerprint.

A graded observation enters continuous-learning metrics only when the receipt
still matches, the outcome is a win or loss, and the outcome timestamp is after
the prediction. Missing receipts, changed prediction evidence, duplicate
receipts, pushes, backfills, missing timestamps, and future outcomes fail closed.

## Learning gates

| Layer | Trusted observations required |
| --- | ---: |
| Smart-consensus review | 40 with at least two component probabilities |
| Market-blend review | 60 with pre-blend probability and market evidence |
| Calibration review | 80 with pre-calibration probability |
| Shadow model-retraining review | 200 |
| Industry-level CLV claim | 500 with verified closing-line evidence |

The runtime learning payload reports Brier score, log loss, ECE, 30/90-day
windows, market-level readiness, and drift-review reasons. It never changes a
probability, blend weight, edge threshold, stake, or champion model. Every
adaptation remains a shadow proposal requiring the existing held-out,
calibration, serve-parity, market-validation, review, merge, and rollback gates.
