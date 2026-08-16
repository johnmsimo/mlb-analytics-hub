# Phase 5 — Accuracy and Intelligence

Phase 4.86 closes the reliability roadmap with a reproducible baseline that
future challengers must beat. It freezes 14 production champions across five
Tracker markets without loading or auto-promoting their binary artifacts.

## Baseline coverage

| Tracker market | Models | Held-out cohort | Median AUC | Weakest Brier skill | Current calibration |
| --- | ---: | ---: | ---: | ---: | ---: |
| Batter hits | 2 | 43,839 | 0.6281 | 3.482% | 4,000 |
| Batter home runs | 1 | 43,839 | 0.6833 | 4.234% | 4,000 |
| Batter RBIs | 2 | 43,839 | 0.6163 | 1.826% | 4,000 |
| Batter total bases | 3 | 43,839 | 0.6405 | 3.795% | 4,000 |
| Pitcher strikeouts | 6 | 4,227 | 0.7249 | 8.000% | 4,000 |

The first Phase 5 model priorities are `rbi_1.5`, `rbi`, `hits_1.5`,
`tb_2.5`, and `tb_3.5`. The order is deterministic: weakest held-out Brier
skill first, then lower held-out AUC, then larger generalization gap.

## Promotion comparison contract

A Phase 5 challenger may proceed only when it:

1. improves held-out Brier against its frozen champion;
2. does not regress held-out log loss or AUC;
3. does not regress the associated market ECE;
4. preserves a strictly later, disjoint holdout;
5. matches the ordered production serve features and canonical market;
6. passes the existing held-out, calibration, serve-parity, and market gates;
7. remains review- and merge-gated with an explicit rollback target.

CLV remains observational until at least 500 valid closing-line observations
exist. ROI remains descriptive and cannot promote a model.

## Phase 5 sequence

1. **5.0 — Data intelligence:** source quality, coverage, lineup, park, weather,
   umpire, injury, platoon, pitch-mix, Statcast, and sportsbook evidence.
2. **5.1 — Market-specific modeling:** independent feature and model experiments
   for hits, total bases, home runs, RBIs, and strikeouts.
3. **5.2 — Predictive intelligence:** calibrated ensembles, uncertainty,
   matchup simulation, drift response, and explainability.
4. **5.3 — Decision intelligence:** price shopping, no-bet zones, market-specific
   thresholds, expected value, and risk-aware staking.
5. **5.4 — Continuous learning:** shadow challengers, scheduled evaluation,
   evidence-based promotion, monitoring, and rollback.
