# Tracker Data Schema Review
## Comparing BUILD_PLAN.md vs. Current Implementation (app.py)

---

## SCHEMA COMPLIANCE REPORT

### ✅ IMPLEMENTED FIELDS (22 fields)
These fields are currently being captured in tracker entries:

| Field | Type | Build Line | Status |
|-------|------|-----------|--------|
| `date` | YYYY-MM-DD | 3521 | ✅ |
| `gamePk` | int | 3521 | ✅ |
| `team` | string | 3521 | ✅ |
| `player` | string | 3521 | ✅ |
| `playerId` | int | 3521 | ✅ |
| `marketKey` | string | 3521 | ✅ |
| `line` | float | 3521 | ✅ |
| `recommendedSide` | "Over" \| "Under" | 3521 | ✅ (hard-coded "Over") |
| `rawProb` | float | 3521 | ✅ |
| `adjProb` | float | 3521 | ✅ |
| `modelMean` | float | 3521 | ✅ |
| `edge` | float\|null | 3521 | ✅ |
| `bookmaker` | string | 3521 | ✅ |
| `marketPrice` | float | 3521 | ✅ |
| `marketImplied` | float | 3521 | ✅ |
| `openingPrice` | float | 3521 | ✅ |
| `openingImplied` | float | 3521 | ✅ |
| `hubRating` | int (0-100) | 3523 | ✅ **(NEW in recent patch)** |
| `evPct` | float\|null | 3523 | ✅ **(NEW in recent patch)** |
| `reason` | string | 3523 | ✅ **(NEW in recent patch)** |
| `status` | "pending" | 3523 | ✅ (hard-coded pending) |
| `grade` | "pending" \| "win" \| "loss" \| "push" | 3523 | ✅ |
| `actual` | float\|null | 3523 | ✅ (null at capture) |
| `closingPrice` | float\|null | 3523 | ✅ (backfilled at close) |
| `closingImplied` | float\|null | 3523 | ✅ (backfilled at close) |
| `closingBookmaker` | string\|null | 3523 | ✅ (backfilled at close) |
| `closingCapturedAt` | ISO timestamp\|null | 3523 | ✅ (set at close) |
| `clvEdge` | float\|null | 3523 | ✅ (computed at close) |
| `profitUnits` | float\|null | 3523 | ✅ (computed when graded) |
| `opp` | string | 3523 | ✅ |

### ❌ MISSING FIELDS (11 required fields)
These fields are specified in BUILD_PLAN but NOT implemented:

| Field | Type | Purpose | Spec Line |
|-------|------|---------|-----------|
| `id` | UUID | Enables reliable CRUD operations | 114 |
| `savedAt` | ISO timestamp | When user clicked "Save Pick" | 115 |
| `source` | enum | "props_board" \|  "cheatsheet" \| "manual" | 116 |
| `bestAvailablePrice` | float | Best price across all books | 136 |
| `bestAvailableBook` | string | Which book has best price | 137 |
| `parlayId` | UUID\|null | Links to parlay unit | 139 |
| `parlayLeg` | int\|null | Leg number if in parlay | 140 |
| `stakeDollars` | float | Computed via Kelly (not stored) | 144 |
| `kellyFraction` | float | Fraction used at save time | 145 |
| `confidenceTier` | enum | "A" \| "B" \| "C" \| "D" | 146 |
| `bvpGrade` | enum | "A+" \| "A" \| "B" \| "C" \| "D" | 128 |
| `pitchTypeAdvantage` | enum | "favorable" \| "neutral" \| "unfavorable" | 129 |
| `nrfiProb` | float\|null | NRFI probability if applicable | 131 |
| `gradedAt` | ISO timestamp\|null | When game was graded | 153 |
| `matchupStorylines` | [string] | Claude matchup insights | 161 |

**Missing: 11 fields (14% of spec)**

---

## IMPACT ANALYSIS

### 🔴 CRITICAL GAPS (Block functionality)

#### 1. **No `id` field** → CRUD operations unreliable
- Cannot reliably delete individual picks
- Cannot reliably update stakes
- Deduplication relies on tuple key: `(date, gamePk, player, marketKey, line)`
- **Risk**: If same player plays multiple games or has duplicate markets, merge logic breaks
- **Fix**: Add UUID generation on capture in `_build_tracker_rows_for_game()`

#### 2. **No `savedAt` / `source` fields** → Cannot audit user behavior
- Unknown when picks were added
- Unknown source (manual vs. board)  
- Cannot build "Save Pick" workflow from props board
- **Risk**: Future feature implementation (Save Pick button) has no backend schema
- **Fix**: Add `savedAt: datetime.now().isoformat()` and `source: "props_board"` defaults

---

### 🟡 FUNCTIONAL GAPS (Limit features)

#### 3. **No `bestAvailablePrice` / `bestAvailableBook`** → Cannot track line shopping
- Props board doesn't show "Shop for best lines" feature
- Performance analytics can't measure line shopping ROI
- **Fix**: Add call to `_get_best_odds_across_books()` at capture time (or defer to close)

#### 4. **No `parlayId` / `parlayLeg`** → Parlay builder incomplete
- Spec requires: "Save parlay as a single tracked unit with `marketKey='parlay'`"
- Current HTML has parlay builder UI (lines 140-160 in tracker.html) but backend doesn't support it
- Cannot correlate picks to tracking purpose (parlay vs. single)
- **Fix**: 
  - Add fields to schema
  - Track parlay grouping at save time
  - POST `/api/tracker/parlay` endpoint not yet implemented

#### 5. **No `stakeDollars` / `kellyFraction`** → Kelly stake not tracked per entry
- Kelly fraction is global (from settings)
- Individual entry stakes not stored (only computed in UI)
- Cannot replay daily with precise stake history
- **Fix**: Compute per-entry stake at capture and store in entry

#### 6. **No `confidenceTier` enum** → Can't track bet tiers
- Spec requires A/B/C/D tiers for bankroll tier audit (tracker.html:486)
- Currently no tier logic on captures
- Performance UI shows tier audit but has no data
- **Fix**: Implement `_confidence_tier(adj_prob, edge)` call at capture

#### 7. **No `bvpGrade` / `pitchTypeAdvantage` / `nrfiProb`** → Model grades incomplete
- Spec requires "BvP Grade" badge on pick cards (BUILD_PLAN:59)
- Currently only showing hubRating, not bvpGrade
- Cannot filter by matchup advantage
- **Fix**: Add hitter/pitcher splits analysis at capture

#### 8. **No `gradedAt`** → Grading timestamp missing
- Cannot measure grading lag
- Analytics can't correlate market move to grading time
- **Fix**: Set `gradedAt: datetime.now().isoformat()` in `_close_one()`

---

### 🟠 ANALYTICS GAPS (Nice-to-have)

#### 9. **No `matchupStorylines`** → Context lost
- Spec calls for Claude matchup insights
- Cannot explain why picks were made beyond raw math
- Review tools miss narrative insight
- **Fix**: Add optional field, populate from Claude API on save (or defer)

#### 10. **No `profitDollars`** → Unit vs. Dollar confusion
- Currently only `profitUnits` is stored
- Tracker UI expects both `profitDollars` and `profitUnits` (see tracker.html profit columns)
- Need to compute from `stakeDollars * profitUnits`
- **Fix**: Add field computation in `_recalc_tracker_entry()`

---

## RECOMMENDED FIX PRIORITY

### Phase 1 (CRITICAL - Do Now)
Build these to unblock Save Pick workflow and CRUD operations:
1. **Add `id: uuid.uuid4().hex`** at row creation
2. **Add `savedAt: datetime.now().isoformat()`** at row creation
3. **Add `source: "props_board"`** at row creation (parameterize later)
4. **Fix `stakeDollars` computation** and store per entry
5. **Add `gradedAt` timestamp** in `_close_one()`

### Phase 2 (HIGH - This Week)  
Build these to fix tier audit and confidence system:
1. **Implement `confidenceTier`** computation (A/B/C/D based on edge/prob)
2. **Implement `profitDollars`** computation (stake × units)
3. **Add `bvpGrade`** using existing hitter/pitcher split data

### Phase 3 (MEDIUM - Next Week)
Build these for line shopping and parlay tracking:
1. **Implement `bestAvailablePrice` / `bestAvailableBook`** lookup
2. **Add `parlayId` / `parlayLeg`** fields and POST `/api/tracker/parlay` route
3. **Implement `pitchTypeAdvantage`** enum computation

### Phase 4 (LOW - Later)
Build these for advanced insights:
1. **Add `matchupStorylines`** (Claude-generated)
2. **Add `nrfiProb`** for NRFI prop tracking

---

## CODE LOCATIONS TO UPDATE

| Component | File | Lines | Fix |
|-----------|------|-------|-----|
| Row builder | app.py | 3533, 3555 | Add `id`, `savedAt`, `source`, `confidenceTier`, `bvpGrade`, `stakeDollars` |
| Quick fallback | app.py | 3609 | Same fields |
| Close handler | app.py | 4100–4200 | Add `gradedAt`, `profitDollars`, `closingBookmaker` |
| Entry recalc | app.py | 4070–4190 | Add `profitDollars` computation |
| Performance summary | app.py | 3700+ | Use new tier audit logic |
| Tracker HTML board | tracker.html | 303–320 | Add display columns for new badges |
| Tracker row render | tracker.html | 860+ | Render new fields in board table |

---

## TESTING CHECKLIST

- [ ] Capture creates unique `id` per entry
- [ ] `savedAt` captures correct ISO timestamp
- [ ] `stakeDollars` computed from Kelly before entry save
- [ ] Tier audit shows A/B/C/D distribution correctly
- [ ] Close capture sets `gradedAt` on graded entries
- [ ] `profitDollars` matches `stakeDollars × grade outcome`
- [ ] Parlay builder saves entries with `parlayId` linkage
- [ ] Deduplication logic still works with new `id` field
- [ ] **No regression**: Existing captures still load and grade correctly

---

## SCHEMA COMPLETENESS

**Current:** 20/31 fields = 65% ✅
**After Phase 1:** 25/31 fields = 81% ✅✅  
**After Phase 2:** 28/31 fields = 90% ✅✅✅  
**After Phase 3:** 30/31 fields = 97% ✅✅✅✅  
**After Phase 4:** 31/31 fields = 100% ✅✅✅✅✅

