# Phase 5.0 — Data Intelligence

Phase 5.0 converts the frozen Phase 4.86 baseline into an enforceable data
admission boundary. It governs all 50 distinct serve features used by the 14
champion artifacts across the five supported Tracker markets.

This phase does not change production probabilities. It does not load a pickle,
train a challenger, or authorize promotion.

## Current-source contract

Every production feature must belong to exactly one source contract with:

1. a point-in-time historical reconstruction path;
2. a live serve path on the same semantic scale;
3. an explicit maximum freshness age;
4. a pregame, prior-games-only, or static leakage policy.

An ungoverned feature, duplicate source assignment, feature-count drift, stale
report, unavailable live path, unreconstructable history, or unsafe leakage
policy fails pull-request quality closed.

The seven current source families are Statcast/FanGraphs season skill, opposing
starter profile, handedness/platoon, lagged recent form, lineup role, park
factors, and Statcast bat tracking.

## Phase 5.1 admission queue

The Phase 4.86 weakness order remains authoritative. Phase 5.0 admits two data
experiments for model research:

1. `rbi_opportunity_context` for `rbi_1.5` and `rbi`: lagged on-base quality of
   the hitters expected immediately before the batter.
2. `pitch_mix_contact_matchup` for hits and total-bases priorities: prior-game
   contact quality against the opposing starter's current pitch mix.

Admission means the signal is eligible to be built and tested. It is not proof
of lift and cannot change a champion.

Weather and confirmed-lineup publication state are blocked until point-in-time
historical reconstruction exists. Umpire, injury, and bullpen signals are
blocked until historical, live, freshness, and leakage requirements are met.
Sportsbook consensus is intentionally routed to Phase 5.3 decision intelligence
and cannot enter champion training.

## Promotion boundary

Any Phase 5.1 challenger must still improve held-out Brier, avoid held-out log
loss and AUC regression, avoid market ECE regression, preserve a strictly later
disjoint holdout, pass serve parity and market gates, and retain an explicit
rollback target. Review and merge remain mandatory; automatic promotion remains disabled. CLV stays observational until 500 valid observations and ROI remains
descriptive.
