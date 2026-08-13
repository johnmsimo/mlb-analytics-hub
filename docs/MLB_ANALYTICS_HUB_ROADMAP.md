# MLB Analytics Hub Roadmap

Status: Phase 4.61 merged and deployed on 2026-08-13. Phase 4.62 is the active phase.

This roadmap is the durable handoff from the top-to-bottom production audit of
the live MLB Analytics Hub. The work remains incremental, fail-closed, and
production-safe. Each phase should preserve the contracts established by the
previous phase and ship only after its exit gate passes.

## Prioritized phases

### Phase 4.51 — Secure the administrative boundary

Protect \`/settings\`, administrative GET endpoints, uploaded-file listings,
brain ingestion, training, summaries, and operational metadata.

Exit gate: no private settings or admin identity is visible without
authentication; all admin reads and writes have security tests.

### Phase 4.52 — Establish one actionability contract

Every row must clearly be one of:

\`Research → Projected → Priced → Validated → Actionable → Graded\`

Only \`Actionable\` rows may appear in Value Bets, Best Bets, primary Picks, or
betting recommendations.

Exit gate: zero unpriced edge rows and zero research-only rows presented as
bets.

### Phase 4.53 — Fix freshness and computation states

Implementation status: shared freshness/computation contract wired into affected surfaces; bounded retries and visible stale/failed/unavailable labels are being enforced.

Create shared states: \`ready\`, \`computing\`, \`partial\`, \`stale\`, \`failed\`, and
\`unavailable\`.

Fix the stuck 100% Club, partial Value Bets behavior, stale Consistency cache,
and missing cache-readiness messaging.

Exit gate: no page remains indefinitely in a loading state; every stale value is
visibly labeled.

### Phase 4.54 — Calibration and drift enforcement

Implementation status: calibration evidence and drift gates are wired into market validation and recommendation contracts.

Use market-specific Brier score, ECE, sample size, confidence intervals, and
drift status. When a market fails calibration, automatically downgrade or
suppress strong recommendations.

Exit gate: no \`HIGH CONF\` or \`STRONG BET\` label can appear for a market failing
its calibration gate.

### Phase 4.55 — Odds and closing-line reliability

Implementation status: canonical opening/current/closing odds lineage, freshness,
verified closing receipts, and separate graded-versus-CLV-graded denominators are
being enforced in tracker, validation, and Picks contracts.

Capture opening price, current price, closing price, book, timestamp, line, and
freshness for every market. Separate graded from CLV-graded everywhere.

Exit gate: rolling CLV metrics have consistent denominators and at least 500
valid CLV observations before industry-level claims are made.

### Phase 4.56 — Data correctness and entity validation

Implementation status: the Phase 4.56 entity-validation boundary is being added
to the shared candidate contract. Explicit identity, lineup, stat, handedness,
probable-pitcher, asset, market-name, and line contradictions fail closed before
recommendation surfaces.

Reject player/team identity mismatches, invalid lineup status, impossible or
suspicious stats, incorrect handedness, stale probable pitchers, missing logos
and assets, and inconsistent market names or lines before recommendation
surfaces.

Exit gate: invalid rows are rejected before reaching any recommendation surface.

### Phase 4.57 — Canonical cross-page consistency

Implementation status: the canonical 4.57 candidate projection, identity, decision snapshot, producer adapters, cross-page audit endpoint, and regression coverage are wired into the shared WSGI startup path.

Make Props, Value Bets, Cheatsheets, Edge Lab, Tracker, Deep Dive, and Gameside
consume the same validated candidate contract.

Exit gate: the same player/market/line has the same probability, edge, price,
status, and recommendation everywhere.

### Phase 4.58 — Mobile-first product pass

Implementation status: the shared mobile contract now adds a 390px phone-width
layer for Tracker, Value Bets, Consistency, Pitcher Deep Dive, Settings, and
Tools. Dense tables use contained touch scrolling, controls and forms stack
within the viewport, and primary actions retain 44px touch targets.

Perform actual iPhone-width testing for every page, especially Tracker, Value
Bets, Consistency, Deep Dive, Settings, and Tools.

Exit gate: no horizontal overflow, clipped tables, inaccessible controls, or
hidden primary actions at 390px width.

### Phase 4.59 — Performance and cache readiness

Implementation status: bounded cache warmup, explicit route budgets, parallel independent best-bets refreshers, pooled deep-dive loaders, and visible timeout/partial fallback states are wired into the runtime.

Reduce slow schedule/API calls, improve cold-start warmup, parallelize
independent loaders, and add route-level performance budgets.

Exit gate: health/readiness is fast, first meaningful content is under two
seconds for cached views, and deep dives have explicit timeout/fallback states.

### Phase 4.60 — Production quality gates

Implementation status: Redis worker readiness is being hardened so an idle durable queue cannot manufacture socket timeouts; the worker block timeout is now explicitly longer than its Redis socket timeout, with regression coverage and deployment validation in progress.

Expand deployment validation beyond compilation and pytest with browser smoke
tests for every route, API contract tests, mobile layout tests, stale-data
tests, actionability tests, asset checks, calibration/drift checks, and
Redis-worker readiness checks.

Exit gate: a deployment cannot pass if any critical page is stuck, stale,
unauthorized, or showing invalid betting data.

### Phase 4.61 — Champion/challenger model operations

Implementation status: metadata-only model operations gates, serve-feature alias parity, challenger comparison, and explicit rollback records are wired into the weekly regeneration workflow. New candidates remain blocked from production until the four gates pass and the PR is reviewed and merged.

Formalize model lineage, weekly retraining, challenger comparison, rollback,
feature parity, and live-versus-held-out evaluation.

Exit gate: no model reaches production without passing held-out, calibration,
serve-parity, and market-validation gates.

### Phase 4.62 — Product consolidation and growth

Implementation status: a mobile-first My Hub consolidates canonical actionable signals, calibration health, tracker evidence, the existing Props watchlist, saved players, preferred markets, in-app edge thresholds, and clearer price/probability explanations into one Discover → Validate → Track → Learn workspace.

After trust and reliability are complete, consolidate the experience around:

1. Discover
2. Validate
3. Track
4. Learn

Then add watchlists, saved players, alerts, personalized markets, and clearer
explanations.

## Audit findings carried into the roadmap

- \`/settings\` exposes an administrative surface before authentication.
- Value Bets can contain \`SKIP\` or model-only rows without verified prices.
- Several pages can remain in computing or partial states without a shared
  state contract; 100% Club was the clearest stuck workflow.
- Calibration drift was visible in RBI metrics and must affect recommendation
  strength.
- Freshness and CLV denominators are not consistently communicated.
- Research-only and priced/actionable data are mixed across surfaces.
- External team-logo assets had failed loads.
- Tracker, Value Bets, Consistency, Deep Dive, Settings, and Tools need a
  real iPhone-width pass.

## Operating rule

Do not declare the product production-grade based only on a passing unit suite.
Every phase must include contract tests plus the relevant live/browser or
deployment gate, and recommendations must fail closed whenever price,
freshness, identity, calibration, or authorization evidence is missing.
