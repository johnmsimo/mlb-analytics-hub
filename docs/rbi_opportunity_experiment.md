# Phase 5.1 — RBI Opportunity Challenger

Phase 5.1 begins market-specific modeling with the weakest frozen lines:
`rbi_1.5` and `rbi`. The challenger adds one signal,
`rbi_traffic_obp`: the mean pregame season-to-date OBP of the three lineup
slots immediately preceding the target batter.

## Train/serve parity

Historical training reconstructs batting order from Statcast and computes each
player's season-to-date OBP using only plate appearances before the target game.
The current game and RBI target are never read. Live scoring uses the confirmed
MLB lineup and current season StatsAPI OBP. Missing lineup slots or unavailable
stats fall back to the declared league OBP of 0.320 on both paths.

The feature is appended only to `RBI_CHALLENGER_FEATURES`. It is deliberately
absent from the committed champion feature map, so merging this phase does not change production probabilities.

## Experiment gate

The manual `RBI opportunity challenger` workflow trains both RBI challenger
lines on 2021–2024 and evaluates on the frozen 2025 cohort. It uploads a JSON
evidence artifact and writes no model pickle.

A challenger may enter shadow evaluation only when it:

1. improves held-out Brier against the frozen champion;
2. does not regress held-out AUC;
3. does not regress held-out log loss;
4. uses the same 2025 test season and held-out cohort;
5. preserves the ordered serve feature and leakage contracts.

Even a challenger that clears those gates is not promotion eligible. It must
first accumulate shadow calibration evidence without market ECE regression.
Automatic promotion remains disabled, review and merge remain mandatory, CLV
remains observational until 500 valid observations, and ROI remains descriptive.
