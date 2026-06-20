# MLB Analytics Hub — Comprehensive Health Check

Run date: **2026-06-20**. Branch: `claude/comprehensive-app-health-check-9uih05`.
Method: fresh dependency install, full `app.py` import (156 routes), Flask test-client
smoke of every served page + heavy model/tracker routes against a live game
(`game_pk=824263`), frontend↔route cross-reference, data-file & model inventory,
devig math verification.

## Coverage checklist
- [x] 1. Baseline — syntax, deps, imports, app boot, core routes, JSON validity, model load, network
- [x] 2. Prop projection / game model routes
- [x] 3. Tracker subsystem (routes, auto-sync, dedup)
- [x] 4. Odds layer (devig math, graceful odds-off degradation)
- [x] 5. Deep API route smoke (game, projection, simulate, sharp-card, ai-boxscore, etc.)
- [x] 6. Frontend ↔ route cross-reference (every `/api/` the HTML calls)
- [x] 7. Data freshness & completeness
- [x] 8. Dependency / version drift

---

## Accuracy deep-pass (2026-06-20, round 2)

Beyond "does it return 200", verified the projection engine is firing on real data and producing
plausible numbers. Caches populate fully (616 FG batters / 697 pitchers, 568 Savant xStats, 697
pitcher xStats, 678 arsenals); name lookups resolve; the analytic `_project_batter` output is sane
(e.g. 1.51 expected hits, 2.19 TB, full adjustment ledger); NRFI devig normalizes to 1.0;
`/api/game-projection` run totals are plausible (CWS 5.1 / DET 3.2). One **high-severity accuracy
bug** found and fixed:

### 🔴 A1 — Uncalibrated XGBoost models were corrupting hit projections  ✅ FIXED
- `models/xgb_*.pkl` are raw `XGBClassifier`s (`calibrated_classifiers_ = False`) whose
  `predict_proba` is **uncalibrated** — bimodal/extreme (≈0.0012–0.0015 for ordinary hitters,
  ≈0.998 for the odd one). They depend on a per-market isotonic calibrator
  (`models/iso_{market}.pkl`) that **does not exist** in the repo (only the 4 raw models are
  tracked; not gitignored), so `apply_isotonic` was an identity+clamp and the raw extremes flowed
  straight into the blend.
- Measured impact: feeding the garbage XGB prob into the hit blend (weight up to 0.60) dragged a
  **true ~0.65 hitter down to a final 0.30 (verdict PASS)**; an inflated XGB pushed others to 0.84
  (LEAN_OVER). With XGB absent the blend correctly returns the analytic ~0.65. **The app was less
  accurate with the XGB hit model on than off.**
- Also: `[xgb DEBUG]` `print()`s fired on every batter on every projection (stdout spam + overhead).
- **Fix (`xgb_prop_scorer.py`):** added `_xgb_calibrated(market_key)` — XGB output is only used
  when a trained `iso_{market}.pkl` is loaded. `xgb_ready()` now requires model **and** calibrator,
  and `_score_full` / `xgb_hit_prob_bulk` bail out when uncalibrated, so callers fall back to the
  analytic model (consistent with the existing HR/TB/RBI graceful-degradation pattern). Removed all
  debug prints. Self-healing: committing `iso_batter_hits.pkl` / `iso_pitcher_strikeouts.pkl`
  re-engages XGB automatically. Verified: `xgb_ready('hits'/'k') = False`, projection runs clean
  (0 debug lines), hit projections now driven by the sane analytic model.
- **Follow-up (not a regression):** to actually *use* the XGB models, train + commit per-market
  isotonic calibrators alongside them (the regeneration playbook in `docs/` should emit
  `iso_{market}.pkl`). Until then the analytic model is authoritative — which is the more accurate
  state today.

---

## Findings (severity: 🔴 High · 🟠 Medium · 🟡 Low/Info)

### 🟠 M1 — Settings page (`/settings`) calls ~9 endpoints that don't exist → 404  ✅ FIXED
**Resolved (2026-06-20):** `settings.html` repointed to the real routes — `/api/cache/warm`,
`/api/brain/ingest-status`, `/api/brain/ingest-trigger`, `/api/brain-data/{upload,list,delete}`
(upload now sends `files`/`type` form fields; delete is `POST {filename}`). The "Final Daily
Summary" panel was wired to the existing `/api/model-actual/daily-summary{,/push,/push-status,/stored}`
backend (mirroring dashboard.html). Verified: every settings endpoint now returns non-404; the only
unmatched frontend reference left anywhere is the dead `/api/matchup-card` (L1).

`settings.html` is served live at `/settings` (`app.py:4538`) but its JS targets a
stale API surface that was renamed/removed. Confirmed 404 via test client:

| settings.html calls | Actual registered route |
|---|---|
| `POST /api/cache/warmup` | `POST /api/cache/warm` |
| `GET /api/brain/files`, `DELETE /api/brain/files/<id>` | `/api/brain-data/list`, `/api/brain-data/delete` |
| `POST /api/brain/upload` | `/api/brain-data/upload` |
| `GET /api/brain/status` | `/api/brain/ingest-status` |
| `POST /api/brain/ingest` | `/api/brain/ingest-trigger` |
| `GET /api/summary/daily`, `POST /api/summary/{build,push,rebuild-push}` | none (closest: `/api/model-actual/daily-summary*`) |

Effect: the Settings page's **cache-warm button, brain-file upload/list/delete UI, and
daily-summary panel are all broken**. `dashboard.html` (more recently updated) calls the
*correct* names (`/api/cache/warm`, `/api/brain/ingest-trigger`, …), so this is `settings.html`
having drifted behind an API rename. Fix = update the fetch URLs in `settings.html` (and decide
whether the `/api/summary/*` panel maps onto the `model-actual/daily-summary` routes).

### 🟠 M2 — XGBoost model/runtime version mismatch  ✅ FIXED
**Resolved (2026-06-20):** confirmed empirically that all 4 `models/*.pkl` load and predict with
**zero** version warnings under `xgboost==3.2.0` (vs. the warning under 2.1.1), proving the
artifacts are 3.2.0-native. Re-pinned `requirements.txt` from `xgboost==2.1.1` → `xgboost==3.2.0`,
aligning runtime with the artifacts, the docs, and CLAUDE.md.

- `requirements.txt:13` pins `xgboost==2.1.1`; `docs/xgb_model_regeneration.md` + `CLAUDE.md`
  mandate `xgboost==3.2.0` ("Verify requirements.txt contains exactly xgboost==3.2.0").
- On load, all 4 `models/*.pkl` emit XGBoost's *"loading a serialized model … generated by an
  older version … please export by calling Booster.save_model"* warning — confirming the
  artifacts were serialized under a different XGBoost than the 2.1.1 runtime.
- Model `meta` only carries `target/model_type/exported_at_utc/task` — **not** the
  `xgboost_version`/`sklearn_version` the docs claim it logs.
- Risk: silent prediction drift today, hard load-failure on a future XGBoost bump. Pick a single
  source of truth — either re-pin runtime to the training version, or re-export the `.pkl`s under
  2.1.1 — and align `requirements.txt`, the docs, and the artifacts.

### 🟡 L1 — "Matchup card" feature is fully orphaned (dead code)  ✅ FIXED (deleted)
- `/api/matchup-card/<game_pk>` was **not registered**; its only implementation was
  `api_matchup_card_route.py`, a paste-in snippet whose helper calls (`props_fetch_game`,
  `fgpitcher`, `matchupscore`, `PARKFACTORS`…) used **outdated names** that no longer exist in
  `app.py`. `matchup_card.html` was unserved, unlinked (no deepdive mount), and its referenced
  `static/.../matchup_card.{css,js}` assets never existed.
- **Resolved (2026-06-20):** deleted `api_matchup_card_route.py` + `matchup_card.html` and removed
  the route/page from CLAUDE.md (per user decision to delete the abandoned feature).

### 🟡 L2 — HR/TB/RBI XGBoost models referenced but absent  ✅ DOCUMENTED (deferred)
`xgb_prop_scorer` looks for `xgb_hr_over_0.5.pkl` / `xgb_tb_over_1.5.pkl` / `xgb_rbi_over_0.5.pkl`,
which are not committed. Confirmed the scorer skips missing files (`if not os.path.exists(path):
continue`), so `xgb_hr_prob/tb/rbi` return `None` and those markets fall back to the analytic
`_project_batter` model — expected, not a bug. **Resolved (2026-06-20):** documented as
intentionally-deferred in CLAUDE.md (XGBoost models section) with the training/regeneration path.
Not training unvalidated production models from a health-check session (per user decision).

### 🟡 L3 — Empty / missing data files  ✅ DOCUMENTED
- `data/savant_swing_take_2026.csv` is header-only — this is a **known upstream Baseball Savant
  quirk** (the swing-take leaderboard CSV "frequently returns header-only", per
  `savant_bat_tracking.py:39`); `sv_swing_take()` returns `{}` and callers fall back. Documented in
  CLAUDE.md as expected.
- `data/mlb_matchups_*.csv` (daily BvP snapshots) are date-stamped, pipeline-generated, ephemeral —
  not "checked in". CLAUDE.md corrected.
- `lineups_*.json` / `umpires_*.json` are runtime artifacts the app regenerates on demand
  (observed during the audit). CLAUDE.md corrected to classify them as runtime, not committed.

### 🟡 L4 — Dependency version drift / reproducibility  ✅ FIXED
- Floor-only pins (`pandas>=2.0`, `numpy>=1.24`) resolved to **pandas 3.0.3 / numpy 2.4.6** on a
  fresh install — non-reproducible across builds. The app imported, served, and ran model
  predictions correctly under those versions here.
- **Resolved (2026-06-20):** added upper bounds in `requirements.txt` — `pandas>=2.0,<3.1`,
  `numpy>=1.24,<2.5` — which permit the validated current versions while preventing an unannounced
  major-version jump.

### 🟡 L5 — Doc drift (CLAUDE.md)  ✅ FIXED
- **Resolved (2026-06-20):** added `edge_lab.html` (`/edge-lab`) and `settings.html` (`/settings`)
  to the page table; removed the `/api/matchup-card` route + `matchup_card.html` references (L1);
  corrected the data-persistence section so BvP-snapshot / lineup / umpire files are described as
  runtime-generated rather than checked-in (L3).

---

## Verified healthy ✅
- **Syntax/import**: all ~60 `*.py` compile; `app.py` imports in ~1.3s; **156 routes** registered.
- **All 16 served HTML pages return 200**: `/`, `/props`, `/tracker`, `/settings`, `/deep-dive`,
  `/consistency`, `/edge-lab`, `/batter-vs-pitcher`, `/value-bets`, `/nrfi`, `/tools`,
  `/pitcher-deep-dive`, `/gameside-deepdive`, `/breakout-detector`, `/cheatsheets`, `/hr-analytics`.
- **Heavy model/game routes 200** against live game 824263: `/api/game`, `/api/game-projection`,
  `/api/props/projections` (38 KB), `/api/pitcher-matchup`, `/api/simulate` (async `computing`
  state by design), `/api/market` (graceful odds-off), `/api/nrfi`, `/api/umpire`, `/api/f5`,
  `/api/bullpen/fatigue`, `/api/lineup-status`, `/api/lineup`, `/api/sharp-card`, `/api/ai-boxscore`,
  `/api/props/quick`, `/api/props/trends`.
- **Tracker**: all date-param routes 200 (`/date`, `/brier`, `/portfolio`, `/bankroll/dashboard`,
  `/today`, `/performance`, `/settings`); auto-sync is well-guarded (processes only dates with
  `pending` entries, forwards `ADMIN_TOKEN`, per-date try/except).
- **NRFI devig math correct**: additive/multiplicative/power all normalize to 1.0; −120/+100 →
  overround 4.55%, fair ≈ −110.
- **All `data/*.json` parse**; 4 XGB models load (with the M2 warning); `statsapi.mlb.com` reachable.
- The `RuntimeError: cannot schedule new futures after interpreter shutdown` seen during the
  one-shot test runs is a **benign teardown artifact** of the daemon auto-sync thread — a
  long-lived gunicorn process won't hit it. Not a bug.

## Recommended fix order — all resolved
1. ~~**M1** — repoint `settings.html` fetch URLs to the real routes.~~ ✅ done
2. ~~**M2** — resolve the XGBoost version of record and align requirements/docs/artifacts.~~ ✅ done
3. ~~**L1** — delete the orphaned matchup-card feature.~~ ✅ done
4. ~~**L2** — document HR/TB/RBI models as deferred (graceful fallback confirmed).~~ ✅ done
5. ~~**L3** — document swing-take upstream quirk + correct ephemeral-data claims.~~ ✅ done
6. ~~**L4** — upper-bound pandas/numpy in requirements.txt.~~ ✅ done
7. ~~**L5** — fix CLAUDE.md page table + data-persistence drift.~~ ✅ done

Every finding from this audit is now either fixed or documented. No open items.
