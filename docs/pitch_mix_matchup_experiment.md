# Phase 5.2 — Pitch-Mix/Contact Matchup Challenger

Phase 5.2 evaluates the second signal admitted by the Phase 5.0 data-intelligence
gate. It starts with `hits_1.5`, the next weakest frozen champion after the RBI
lines, and applies the same evidence to the admitted hit, total-base, and
home-run challenger queue.

## Feature definition

`pitch_mix_contact_edge` measures how a batter's pregame contact results against
fastball, breaking-ball, and offspeed families align with the opposing starter's
pregame pitch mix. Batter family wOBA is shrunk toward a 0.320 league anchor by
25 plate appearances. The arsenal-weighted result is compared with that
batter's overall pregame wOBA and clipped to -0.200 through +0.200.

Historical reconstruction uses only pitch-level events before the target game.
Live scoring uses the current Baseball Savant pitcher arsenal and batter
pitch-type result table. Both paths return the neutral 0.000 edge when the
batter has fewer than 15 observed plate appearances, the pitcher has fewer than
50 observed pitches, identity is missing, or the source is unavailable.

## Challenger boundary

The feature is appended only to Phase 5.2 challenger feature lists. It remains
absent from every committed champion feature map, so this phase does not change production probabilities
and does not add a live network call for a production artifact.

The manual workflow trains challengers in frozen weakness order and writes one
JSON evidence report. It never writes a pickle, changes the production feature
map, or promotes a model.

## Decision gate

Each challenger uses the frozen 2021-2024 training seasons and 2025 holdout. It
must improve held-out Brier score while holding or improving AUC and log loss on
the identical cohort. A metric winner is shadow-eligible only. Promotion remains
false until market ECE also passes shadow calibration and a human reviews the
evidence.

CLV remains observational until 500 valid observations and ROI remains
descriptive.
