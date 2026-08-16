# MLB Analytics Hub Roadmap

Status: Phase 4.83 merged and deployed on 2026-08-16. Phase 4.84 is the active phase.

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

### Phase 4.63 — Actionable alert lifecycle

Implementation status: My Hub is adding a device-private alert inbox with stable canonical deduplication, bounded local history, new/seen/dismissed states, saved-player priority, preferred-market filtering, and explicit edge-threshold matching.

Only canonical actionable candidates with stable identity, fingerprint, positive edge, a real sportsbook price, and a real book can create alerts. Alert history remains on the device until an end-user authentication boundary exists; the administrative token is never treated as a user account.

Exit gate: duplicate refreshes cannot create duplicate alerts, dismissed alerts stay dismissed for the same canonical snapshot, and no unpriced, unidentified, non-positive, or non-actionable row can enter the inbox.

### Phase 4.64 — Alert freshness and market movement

Implementation status: My Hub is adding a second fail-closed alert boundary that requires an explicit odds timestamp and a server-computed age of no more than 15 minutes. Canonical snapshots are grouped by candidate identity so routine refreshes stay quiet while material edge or price movement creates one new alert and supersedes the prior snapshot.

Material movement is defined as at least a 1.0 percentage-point edge change or a 10-point American-price change. Immaterial fingerprint changes update the candidate snapshot without reopening seen or dismissed alerts. Candidate and alert histories remain bounded and private to the device.

Exit gate: stale or timestamp-less rows cannot alert, identical refreshes cannot duplicate, immaterial changes cannot create noise, and only a materially changed fresh snapshot can reopen a previously dismissed candidate.

### Phase 4.65 — Deployment single-flight and lease recovery

Implementation status: production-triggered GitHub Actions runs use one explicit
non-cancelling concurrency group, both deploy and rollback retry only the known
transient Fly.io machine-lease collision with bounded backoff, and every run
writes commit, workflow, smoke, and rollback provenance to the Actions summary.

Merge to `Main` remains the authoritative production deployment path. Operators
must not start a competing manual `flyctl deploy` while that workflow is queued
or active; all other deployment errors continue to fail immediately.

Exit gate: production workflows cannot overlap one another, transient lease
collisions recover within the bounded retry policy, non-lease failures fail
closed, and every attempted production deployment leaves reviewable provenance.

### Phase 4.66 — Declarative live production contract gate

Implementation status: one non-mutating standard-library gate inventories every
public product page, verifies mobile viewport and page-identity markers, checks
all locally referenced static assets, confirms administrative reads remain
unauthorized, and validates the health, readiness, journey, games, actionable
edge, calibration, and tracker API contracts within explicit response budgets.

Pull requests run the page, asset, authorization, readiness, journey, and games
subset that can be proven against the currently deployed baseline. A merge to
`Main` adds the Edge Finder, calibration, and tracker contracts against the
exact newly deployed commit before the release can pass; failures continue to
trigger the existing rollback path.

Exit gate: all 19 public product shells, every referenced local asset, all eight
administrative read boundaries, and seven critical API/readiness contracts pass
the live gate without mutating production data, and the post-deploy health
version matches the merge commit.

### Phase 4.67 — Durable recommendation scan lifecycle

Implementation status: cold and stale props scans now expose one bounded durable
job lifecycle across web and worker processes. Edge Finder reports
`ready`, `computing`, `failed`, or `unavailable` with safe job identity,
elapsed time, attempt, and timeout metadata; queued or running jobs that exceed
their 600-second completion window transition to an explicit failure instead of
remaining indefinitely in a loading state.

Recommendation rows are withheld unless the producer snapshot is fresh and
ready. The post-deploy live gate validates the durable job contract and
immediately rechecks commit identity, health latency, readiness, and worker
availability after Edge Finder can enqueue cold work.

Exit gate: no cold or stale Edge Finder request can run CPU-heavy work inside
Gunicorn, duplicate an active scan, remain computing beyond its bounded window,
or surface a recommendation before a fresh terminal worker snapshot exists; web
health and readiness remain within contract after scan submission.

### Phase 4.68 — Durable worker convergence receipt

Implementation status: the durable props-scan worker now stamps completed
snapshots with a 4.68 receipt containing the scan date, completion time, durable
worker source, and exact deployed release SHA. Edge Finder carries that receipt
without exposing queue internals or administrative data.

The post-deploy gate derives a future-date probe scoped by the required release
SHA, so a fresh receipt from an earlier deployment cannot satisfy the check. It
accepts only bounded fail-closed computing states while polling and proves Redis
and worker readiness, and cannot pass until the worker returns a fresh
`ready` snapshot whose receipt matches both the probe date and deployed commit.
The convergence wait is bounded to 61 attempts at 10-second intervals and
remains inside the existing deployment timeout and rollback path.

Exit gate: a release cannot pass merely because cold work was queued; the exact
deployed worker must finish a new scan, persist its receipt through Redis, return
it through Gunicorn, and preserve health/readiness throughout the bounded
convergence window.

### Phase 4.69 — Actionable recommendation evidence receipts

Implementation status: every Edge Finder row intended for My Hub now preserves
the canonical selection, current sportsbook quote, server-computed freshness,
model version and probability, de-vigged market probability, calibrated edge,
and market-promotion decision through the canonical response layer. That layer
issues one 4.69 evidence receipt bound to the candidate identity and decision
fingerprint only when the evidence is complete and internally consistent.

My Hub requires the receipt before a signal or alert is actionable and shows a
concise `Why this qualifies` explanation with model probability, fair market
probability, sportsbook price, and quote freshness. Missing, stale, mismatched,
uncalibrated, or unexplained evidence fails closed. The post-deploy contract gate
independently revalidates every receipt against its enclosing live edge row.

Exit gate: no recommendation can appear in My Hub or its in-app alert inbox
without a fresh, priced, calibrated, identity-bound 4.69 receipt whose
probability, edge, sportsbook, selection, validation versions, and explanation
match the canonical row.

### Phase 4.70 — Saved-player verified opportunity digest

Implementation status: My Hub turns the device-private watchlist into an
explicit opportunity digest. Every saved player receives one visible state:
loading while canonical evidence is computing, verified when at least one
actionable 4.69 receipt exists, no verified opportunity when current rows do not
qualify, or unavailable when the evidence source fails.

Receipted signal cards add one-tap save and remove controls while retaining the
manual watchlist form. The digest ranks each player's strongest current edge,
shows selection, sportsbook price, quote freshness, and receipt version, and
states when additional verified markets exist. Saved names remain local to the
device and never create a server-side profile.

Exit gate: a saved player can never appear to have an opportunity unless the
underlying row passes the complete actionability and 4.69 evidence contracts;
missing, stale, unpriced, mismatched, or unavailable evidence produces an
explicit non-recommendation state, and all save/remove controls preserve the
390px touch contract.

### Phase 4.71 — Verified decision handoff

Implementation status: a saved player's strongest current 4.69-receipted
opportunity can be prepared as an expiring, device-private decision draft and
opened in Tracker review mode. The draft carries the canonical candidate and
fingerprint, selection, model probability, edge, sportsbook price, receipt
version, explanation, and quote expiry needed to preserve decision context.

Preparing a draft never creates a tracked pick. Tracker labels the handoff,
locks the canonical evidence fields for review, and waits for an explicit save
through the existing admin-authenticated endpoint. That endpoint re-resolves
the canonical candidate before persistence. Invalid, altered, unsupported, or
expired drafts are discarded locally and create no server mutation.

Exit gate: only a currently actionable row with a complete 4.69 evidence
receipt can produce a 4.71 draft; the draft expires with its quote, storage
failure and validation failure remain visible non-actions, Tracker never
auto-saves the handoff, and the review control preserves the 390px touch
contract.

### Phase 4.72 — Verified decision learning loop

Implementation status: Tracker's existing 30-day performance response now includes
an aggregate-only learning slice for decisions saved through the 4.71 verified
handoff. It reports decision, pending, and graded counts plus descriptive
outcome, ROI, unit, and closing-line aggregates without returning tracker rows
or player identities to My Hub.

My Hub exposes explicit no-decision, awaiting-outcome, learning, sample-ready,
and unavailable states. Fewer than ten graded decisions remain visibly labeled
as an early descriptive sample; missing or malformed source attribution shows
no conclusion. Full audit detail remains in Tracker.

Exit gate: only rows carrying the exact server-persisted 4.71 handoff source are
included, the response is aggregate-only with no row payload, zero-risk and
missing-CLV samples do not fabricate ROI or beat-close values, and My Hub never
turns a small or unavailable sample into a recommendation.

### Phase 4.73 — Verified decision market learning lens

Implementation status: the aggregate-only 4.72 learning payload now carries a
nested 4.73 market lens for the five canonical My Hub markets. Each market keeps
decision, pending, graded, outcome, ROI, unit, and closing-line aggregates in
canonical order without returning tracker rows or player identities.

My Hub renders market sample progress beneath the overall learning state.
Markets remain awaiting outcomes, learning, or sample ready using the same
ten-graded-decision threshold. The lens is explicitly descriptive: it does not
rank markets, change device preferences, or create recommendations.

Exit gate: only exact 4.71 handoff rows in supported canonical markets are
included, unknown markets are omitted, malformed or duplicate market aggregates
fail closed, missing ROI or CLV stays blank, and neither backend nor frontend
reorders markets by performance.

### Phase 4.74 — Explicit market preference review

Implementation status: represented 4.73 market-learning rows now expose an
explicit preference control that reuses the device-local Discover market
preference store. The Learn view and existing preference chips stay synchronized
after a user tap.

Performance never ranks markets, suggests a preference, or mutates the store.
Unknown, duplicate, malformed, or unavailable market learning remains
non-interactive, and no preference is persisted to the server.

Exit gate: every preference change requires an explicit user action on a
represented canonical market, both control surfaces reflect the same device
state, phone controls retain a 44px touch target, and descriptive learning alone
cannot add or remove a preference.

### Phase 4.75 — Market preference change receipt

Implementation status: every explicit market preference change now produces a
session-only receipt in My Hub showing the affected canonical market, the
control surface that initiated the change, and the count of currently matching
actionable signals when the edges response is ready. A single explicit undo
restores the prior device-local preference state.

The receipt never uses ROI, CLV, win rate, or market-learning performance to
suggest or apply a preference. Unknown stored keys are discarded, unavailable
edge states suppress the impact count, and neither apply nor undo writes to the
server.

Exit gate: each valid change and undo requires a user tap, receipts are
announced accessibly, impact counts come only from current actionable edges in a
ready state, the undo target is exactly the preceding preference value, and
phone controls retain a 44px touch target.

### Phase 4.76 — Personalized signal provenance

Implementation status: every rendered My Hub signal now carries stable,
accessible provenance showing that its canonical market is explicitly preferred
and, when applicable, that its player is on the device-local watchlist. Reason
keys remain in fixed preferred-market then saved-player order.

Provenance is explanatory only. It does not change signal eligibility, reorder
model edges, inspect ROI, CLV, win rate, or market-learning performance, or
create a new recommendation. Rows that are not actionable or no longer match a
supported preferred market render no provenance and no signal card.

Exit gate: every displayed provenance reason is derived only from current
actionability plus explicit device state, unknown markets fail closed, saved
player priority is labeled without implying stronger model evidence, and
assistive technology receives the same reason labels shown visually.

### Phase 4.77 — Alert eligibility provenance

Implementation status: every visible My Hub alert now carries fixed-order,
accessible provenance for its explicitly preferred market, configured edge
threshold, fresh quote, and eligible event. The event reason distinguishes a new
actionable opportunity from a material edge or price change without changing
the existing alert ledger.

Provenance is explanatory only. It does not create alerts, alter thresholds,
change lifecycle state, inspect ROI, CLV, win rate, or learning performance, or
write to the server. Non-actionable rows, unsupported markets, stale quotes,
below-threshold edges, malformed ledger records, and inactive states render no
alert card.

Exit gate: every displayed alert reason is derived from the same canonical
eligibility checks that create the inbox, reason keys remain in stable order,
assistive technology receives the visible labels, quiet refreshes stay
suppressed, and provenance performs no ledger mutation.

### Phase 4.78 — Verified alert review handoff

Implementation status: every fully eligible alert now exposes an explicit
Review in Tracker control. A user tap rechecks the current alert provenance,
active ledger state, canonical actionability, and quote expiry before preparing
the existing 4.71 device-local decision draft and navigating to Tracker.

Preparing from an alert records its device-local origin but does not save a
pick, mutate alert lifecycle state, post to the server, or bypass Tracker's
explicit save, admin authorization, or canonical revalidation. Ineligible or
expired alerts remain on My Hub with a clear failure message and no draft.

Exit gate: the review control appears only on rendered eligible alerts, every
handoff requires a tap, the alert and evidence receipt are revalidated at tap
time, the draft expires with its quote, no ledger state changes on prepare, and
Tracker remains the only explicit server-save boundary.

### Phase 4.79 — Decision draft origin provenance

Implementation status: Tracker now accepts a prepared 4.71 decision draft only
when its device-local origin is exactly `saved_player_digest` or
`eligible_alert`. The review notice displays that source before the user can
explicitly save.

Origin provenance is explanatory only. It does not alter recommendation
strength, authorize a save, change canonical revalidation, mutate server state
during review, or persist as a new server-side pick field. Missing and unknown
origins invalidate and remove the device-local draft without creating a pick.

Exit gate: every accepted prepared draft carries one allowed origin, the same
origin is visibly and accessibly identified in Tracker before save, malformed
origins fail closed, and existing admin authorization, canonical
revalidation, and explicit-save boundaries remain unchanged.

### Phase 4.80 — Verified draft expiry guard

Implementation status: Tracker now displays the prepared draft's quote-valid-
until time and revalidates the complete 4.71 draft immediately before building
or posting the explicit-save payload.

If the quote expires while the review modal is open, Tracker clears the
device-local draft, closes the review, reports that no pick was created, and
suppresses the POST. Manual picks remain unaffected. The client guard does not
replace admin authorization or the server's canonical revalidation.

Exit gate: every verified review shows its quote expiry before save, every save
attempt rechecks the full draft contract, expired drafts produce no server
request or pick, and recommendation strength, authorization, and canonical
revalidation remain unchanged.

### Phase 4.81 — Live verified draft expiry state

Implementation status: Tracker now updates a visible quote-expiry countdown once
per second while a verified draft is under review. Fresh and expired states are
machine-readable, and assistive technology receives state-change announcements
without hearing every countdown tick.

When the quote expires, Tracker immediately marks the review expired and
disables explicit Save. The Phase 4.80 save-time guard remains in place as a
final client-side backstop, manual picks remain unaffected, and no server
mutation occurs from the timer.

Exit gate: every verified review exposes a live freshness state, expiration
disables Save before a user tap can post, cleanup stops the timer and restores
manual controls, and admin authorization plus server canonical revalidation
remain unchanged.

### Phase 4.82 — Mobile resume freshness reconciliation

Implementation status: Tracker now pauses the verified-draft countdown while the
page is hidden, then rechecks the absolute quote expiry and restarts the timer
when the page becomes visible or returns through the browser page cache.

This closes the iPhone and background-tab throttling gap without trusting missed
interval ticks. Resume handling performs no fetch or server mutation, manual
picks remain unaffected, and the Phase 4.80 save-time guard remains the final
client-side backstop before canonical server revalidation.

Exit gate: hidden reviews stop their timer, every visible or pageshow transition
reconciles against absolute expiresAt, expired resumes disable Save before any
POST, and explicit save, admin authorization, and canonical revalidation remain
unchanged.

### Phase 4.83 — Cross-tab verified draft invalidation

Implementation status: Tracker now listens for same-device storage changes to
the active 4.71 draft. Replacement, removal, or a full storage clear closes and
resets the current review immediately so stale in-memory evidence cannot remain
saveable.

The invalidating tab does not delete a replacement written by another tab or
auto-open it. The user must explicitly reload and review the newest draft.
Cross-tab invalidation performs no API call or server mutation, while the
existing expiry, authorization, and canonical revalidation guards remain in
place.

Exit gate: every external draft-key change invalidates an active review, newer
replacement data remains device-local for explicit review, no invalidation can
post a pick, and manual Tracker entry remains unaffected.

### Phase 4.84 — Explicit newest-draft recovery

Implementation status: when cross-tab invalidation includes a replacement that
passes the complete 4.71 draft contract, Tracker now exposes a 44px Review
newest draft control. Removal, clear, malformed, and expired events offer no
recovery action.

The control never auto-opens a draft. A tap re-reads device storage and runs the
normal Tracker validation and review path before explicit Save. Review performs
no API call or server mutation, and manual entry remains available.

Exit gate: recovery is offered only for a valid replacement, every recovery
requires a user tap and fresh storage read, unavailable drafts fail closed
without a pick, and admin authorization plus canonical revalidation remain
unchanged.

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
