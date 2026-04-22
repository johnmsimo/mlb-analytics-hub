=============================================================================
  MLB ANALYTICS HUB — COMPLETE FEATURE BUILD PLAN
  Derived from: Buckets to Bucks, Props.Cash, Propcaster, PropFinder analyses
  Tracker: DELETED — full redesign included below
=============================================================================

─────────────────────────────────────────────────────────────────────────────
  SECTION 1 — NEW TRACKER DESIGN (Full Rebuild)
─────────────────────────────────────────────────────────────────────────────

PHILOSOPHY SHIFT
─────────────────
The old tracker was a 3-stage workflow that required manual slate capture,
a separate working board, then a separate review. It was powerful but
friction-heavy. The new tracker removes that friction by:

  1. Props flow INTO the tracker automatically from the research board
     (no manual capture step — you click "Save Pick" on any prop card)
  2. The working board IS the research board (no separate page)
  3. Grading and closing are background operations, not manual steps
  4. Performance review is always live, not a separate "Stage 3" visit

DESIGN SUMMARY
──────────────
Name:     tracker.html  (same route: /tracker)
Layout:   2-column — LEFT: live bet slip / RIGHT: performance panel
No stages. One page, always live.

─── LEFT COLUMN: LIVE BET SLIP ────────────────────────────────────────────

Section A: TODAY'S PICKS (auto-populated from "Save Pick" on props board)
  - Each pick card shows:
      Player | Market | Line | Side | HUB RATING badge
      Adj Prob | EV% | Edge | Model Mean | BvP Grade
      Opening Price | Best Available Price (multi-book)
      Stake $ (Kelly-computed, editable) | Status pill
  - "Remove" button per pick
  - "Add Manual Pick" button (for picks you found elsewhere)
  - Color coded border: green=win, red=loss, yellow=pending, grey=push

Section B: PARLAY BUILDER (collapsible)
  - Drag any pick from Section A into the parlay
  - Shows: combined prob, combined EV%, payout multiplier
  - Correlation health score (0-100 diversification score)
  - Per-leg correlation warnings (e.g., "⚠️ Negative: high K suppresses hits")
  - "Suggested Add" — pulls from MC correlation matrix
  - Save parlay as a single tracked unit with marketKey="parlay"

Section C: DAILY SETTINGS (collapsible, persisted to TRACKER_STORE)
  - Bankroll $
  - Unit size %
  - Max daily risk %
  - Max bet %
  - Kelly fraction
  - Edge threshold (auto-filter below this)
  - Per-market multipliers (live adjustment)
  - Max team exposure %

─── RIGHT COLUMN: LIVE PERFORMANCE ────────────────────────────────────────

Section D: TODAY SNAPSHOT (real-time, updates as games complete)
  - Picks: N | Graded: N | Pending: N
  - Hit Rate: XX% | Units: +/- X.XX | P/L: $XXX
  - At Risk: $XXX | Bankroll: $XXXX
  - +CLV Rate: XX% | Avg CLV: +/-X.X%

Section E: MODEL TRACK RECORD BANNER (last 30 days by market)
  ┌──────────────────────────────────────────────────────┐
  │  Model Hit Rate · Last 30 Days                       │
  │  Hits 61% ✅  HR 54% ⚠️  Ks 67% ✅  TB 58% ✅      │
  │  RBI 52% ⚠️   Runs 55% ✅  Parlay 38% 📊            │
  └──────────────────────────────────────────────────────┘

Section F: PERFORMANCE TABS (always visible, no navigation required)
  Tab 1: CALIBRATION
    - Hit rate vs target per market (bar chart)
    - Daily series (last 14 days line chart)
    - Multiplier history per market
    - Model recommendation: INCREASE / HOLD / DECREASE per market

  Tab 2: VALUE / CLV
    - +CLV rate (rolling 7/14/30 window toggle)
    - CLV distribution by market (histogram)
    - Best and worst CLV entries table

  Tab 3: BANKROLL
    - Bankroll curve chart (rolling window selector)
    - Daily P/L bar chart
    - Tier audit table (A/B/C/D results)
    - Card bucket audit (singles vs parlay)

  Tab 4: ATTRIBUTION
    - By market: bets, staked $, profit $, ROI, avg CLV
    - By confidence tier: A/B/C/D
    - Daily breakdown table

  Tab 5: BACKTEST
    - FROM / TO date picker
    - Full historical re-simulation
    - Downloadable CSV of results

─── GRADING + CLOSING (background, auto) ──────────────────────────────────
  - Closing prices: captured automatically at game time via background thread
    No manual "Capture Closing" button needed
  - Grading: auto-triggered when MLB Stats API marks game as Final
    Background thread polls /api/game/<game_pk> every 15 min for live games
  - Manual override still available (edit actual result on any card)

NEW TRACKER DATA SCHEMA (per entry in TRACKER_STORE)
──────────────────────────────────────────────────────
{
  id:                 uuid (new — enables reliable CRUD)
  date:               YYYY-MM-DD
  savedAt:            ISO timestamp (when user clicked "Save Pick")
  source:             "props_board" | "cheatsheet" | "manual"

  # Identity
  player:             string
  playerId:           int
  gamePk:             int
  team:               string
  opp:                string
  marketKey:          string
  line:               float
  recommendedSide:    "Over" | "Under"

  # Model outputs
  rawProb:            float
  adjProb:            float
  modelMean:          float
  edge:               float
  evPct:              float          ← NEW: explicit EV%
  hubRating:          int (0-100)    ← NEW: HUB RATING badge value
  bvpGrade:           string         ← NEW: "A+"|"A"|"B"|"C"|"D"
  pitchTypeAdvantage: string         ← NEW: "favorable"|"neutral"|"unfavorable"
  nrfiProb:           float|null     ← NEW: if applicable

  # Market data
  bookmaker:          string
  marketPrice:        float
  marketImplied:      float
  bestAvailablePrice: float          ← NEW: best price across all books
  bestAvailableBook:  string         ← NEW
  openingPrice:       float
  openingImplied:     float

  # Parlay linkage (null for singles)
  parlayId:           uuid|null      ← NEW
  parlayLeg:          int|null       ← NEW

  # Staking
  stakeDollars:       float
  kellyFraction:      float          ← NEW: fraction used at save time
  confidenceTier:     "A"|"B"|"C"|"D"

  # Grading (auto-populated)
  status:             "pending"|"graded"|"void"
  actual:             float|null
  grade:              "pending"|"win"|"loss"|"push"
  gradedAt:           ISO timestamp|null
  closingPrice:       float|null
  closingImplied:     float|null
  closingBookmaker:   string|null
  closingCapturedAt:  ISO timestamp|null
  clvEdge:            float|null
  profitDollars:      float|null
  profitUnits:        float|null

  # Metadata
  reason:             string (short model rationale)
  matchupStorylines:  [string]       ← NEW: from Claude matchup insights
}

NEW TRACKER API ROUTES
───────────────────────
POST /api/tracker/pick              → Save single pick (from props board)
DELETE /api/tracker/pick/<id>       → Remove pick
PATCH /api/tracker/pick/<id>        → Update stake, actual result, grade
GET  /api/tracker/today             → All picks for today with live status
GET  /api/tracker/performance       → Rolling performance data (all tabs)
POST /api/tracker/parlay            → Save parlay as linked unit
GET  /api/tracker/model-record      → Per-market hit rates (30d)
GET  /api/tracker/backtest          → Historical backtest
POST /api/tracker/settings          → Save bankroll + multiplier settings
GET  /api/tracker/settings          → Load settings


─────────────────────────────────────────────────────────────────────────────
  SECTION 2 — NEW FEATURES BUILD PLAN
─────────────────────────────────────────────────────────────────────────────

Organized into 4 sprints by effort and dependency order.
Sprint 1 = data already in memory, mostly frontend work
Sprint 2 = 1-2 new API calls or backend functions
Sprint 3 = new data sources or significant new pages
Sprint 4 = visualization-heavy or architectural

─────────────────────────────────────────────────────────────────────────────
  SPRINT 1 — Zero New Data (Pure frontend + formula work)
  Estimated effort: 1–3 days per feature
─────────────────────────────────────────────────────────────────────────────

1.1  HUB RATING BADGE
─────────────────────
Where:    props.html, props board cards, cheatsheet rows, tracker picks
What:     Single 0-100 score displayed as colored badge replacing "score"
Formula:
  def hub_rating(adj_prob, edge, l10_over_rate):
      trend_bonus = (l10_over_rate - 0.5) * 20     # -10 to +10
      edge_bonus  = min(edge * 100, 20)              # 0 to +20
      prob_base   = adj_prob * 60                    # 0 to 60
      raw = prob_base + edge_bonus + trend_bonus
      return max(0, min(100, round(raw)))

Tier mapping:
  85-100  → 🟢 ELITE   (A-tier automatic)
  70-84   → 🔵 STRONG  (B-tier)
  55-69   → 🟡 MODERATE(C-tier)
  <55     → 🔴 LOW     (D-tier)

Backend: Add hub_rating field to capture route output
Frontend: Replace "score" column with HUB RATING badge in all tables
Tracker:  hubRating stored per entry, filterable in working board

─────────────────────────────────────────────────────────────────────────────

1.2  EV% BADGE + "+EV ONLY" FILTER TOGGLE
──────────────────────────────────────────
Where:    props.html top filter bar, every prop card
What:     Explicit EV% number + toggle to hide negative-EV props
Formula:
  ev_pct = (adj_prob * (1/market_implied - 1)) - ((1 - adj_prob) * 1)
  Display: "+4.2% EV" green | "-1.1% EV" red

Backend:  Add ev_pct to every entry in capture route
Frontend:
  - New badge on each prop card row: [+4.2% EV]
  - New toggle button top of props.html: "Show +EV Only"
  - When toggled, JS filter hides rows where ev_pct <= 0

─────────────────────────────────────────────────────────────────────────────

1.3  MODEL TRACK RECORD BANNER
───────────────────────────────
Where:    props.html (top of page), tracker.html Section E
What:     Per-market hit rate over last 30 days shown as compact banner
Backend:  New endpoint GET /api/tracker/model-record
  Returns: { hits: 0.61, hr: 0.54, pitcher_strikeouts: 0.67,
             batter_total_bases: 0.58, batter_rbis: 0.52,
             batter_runs_scored: 0.55, parlay: 0.38 }
  Logic:   Reads TRACKER_STORE for last 30 days, computes win rate per
           marketKey from graded entries only

Frontend: Slim banner HTML rendered on page load
  ┌──────────────────────────────────────────────────────┐
  │  Model Hit Rate · Last 30 Days                       │
  │  Hits 61%✅  HR 54%⚠️  Ks 67%✅  TB 58%✅          │
  └──────────────────────────────────────────────────────┘

─────────────────────────────────────────────────────────────────────────────

1.4  HR PROFILE PANEL (5 Statcast Signals)
───────────────────────────────────────────
Where:    Expandable panel on batter_home_runs prop cards in props.html
          Also on player modal HR section
What:     5-signal Statcast HR environment score
Signals (all already in _sv_bat_statcast cache):
  1. barrel_pct     → percentile rank (0-100)
  2. hard_hit_pct   → percentile rank
  3. exit_velocity  → percentile rank
  4. pull_pct_air   → (new field needed from Savant — see Sprint 2)
  5. park_factor    → from PARK_FACTORS dict

Formula:
  def hr_edge_score(barrel_pct, hard_hit_pct, ev_pct_rank,
                    park_factor, wind_speed, wind_dir_out):
      base = (barrel_pct * 0.30 + hard_hit_pct * 0.25 + ev_pct_rank * 0.20)
      park_mod = (park_factor - 1.0) * 50          # -5 to +7.5
      wind_mod = wind_speed * 1.5 if wind_dir_out else -wind_speed * 0.8
      return max(0, min(100, round(base + park_mod + wind_mod)))

Display: Mini panel that expands on HR prop card click
  ┌─── HR PROFILE: Player Name ──────────────────────────┐
  │  Barrel%    ████████████░░ 20.4%  (Top 5%)           │
  │  Hard Hit%  ████████████████ 58%  (Top 2%)           │
  │  Exit Velo  ████████████░░ 95.2   (Top 8%)           │
  │  Park Factor 1.06  Slight hitter ↑                   │
  │  Wind: 12mph blowing OUT 🟢                          │
  │  HR EDGE SCORE: 87 / 100 ✅                          │
  └──────────────────────────────────────────────────────┘

─────────────────────────────────────────────────────────────────────────────

1.5  POWER FILTER BAR (props.html)
────────────────────────────────────
Where:    props.html — collapsible filter row above the prop table
What:     Multi-dimension filter chips using data already in memory
Filters to add (all data already available):
  [vs LHP] [vs RHP]               ← from hitter_split_profile()
  [Home] [Away]                   ← from parse_game()
  [Wind Out] [Wind In] [Calm]     ← from Open-Meteo weather data
  [Hitter Park] [Neutral] [Pitcher Park]  ← from PARK_FACTORS
  [Slots 1-3] [Slots 4-6] [Slots 7-9]    ← from boxscore lineup order
  [Lineup Confirmed] [Lineup Pending]     ← from lineup status cache

Implementation:
  Each prop entry gains these pre-computed boolean/string fields at
  capture time. Frontend filters apply client-side via JS (no API calls).

─────────────────────────────────────────────────────────────────────────────

1.6  "SAVE PICK" BUTTON ON EVERY PROP CARD
────────────────────────────────────────────
Where:    props.html, cheatsheet page, deep dive prop rows
What:     One-click to send prop into tracker without navigating away
Frontend: Button on each prop card: [💾 Save Pick]
  onclick → POST /api/tracker/pick with full prop entry JSON
  Success → Button changes to [✅ Saved] with green flash
  If already saved → [✅ Already Tracked]

Backend:  POST /api/tracker/pick
  Accepts full prop entry, assigns uuid, computes ev_pct + hub_rating,
  writes to TRACKER_STORE[today][entries], returns {id, success}

Cross-page state:  LocalStorage set "tracked_ids" so props board can
  mark already-saved picks without re-fetching

─────────────────────────────────────────────────────────────────────────────

1.7  INLINE PROPS STRIP ON DASHBOARD GAME CARDS
──────────────────────────────────────────────────
Where:    dashboard.html game cards (Today's Games tab)
What:     Collapsible "Quick Props" strip showing top 2-3 edges per game
Display:  Below each game card, a toggled div showing:
  [Player Name]  [Market]  [Line]  [HUB RATING]  [EV%]  [💾]
Data source: /api/props/trends/<game_pk> already returns this data
Toggle: Click game card footer area to expand/collapse props strip
"Save Pick" button works inline (no page navigation required)

─────────────────────────────────────────────────────────────────────────────

1.8  SERIES CONTEXT BADGES ON GAME CARDS
──────────────────────────────────────────
Where:    dashboard.html game cards, deepdive.html game header
What:     Series position context shown as badge
Data:     MLB Stats API schedule response already contains
          seriesGameNumber and gamesInSeries in raw game data
          Add extraction to parse_game():
            'series_game': raw.get('seriesGameNumber', 1)
            'series_total': raw.get('gamesInSeries', 3)

Badges:
  "G1 of 3" → SERIES OPENER (stronger pitching, full bullpen)
  "G3 of 3" → SERIES FINALE (weaker rotation, bullpen fatigued)
  "G2 of 3" → MID-SERIES

Edge score modifier in parse_game():
  if series_game == series_total:    edge -= 0.3  (back-end starters)
  if series_game == 1:               edge += 0.2  (ace matchup likely)

─────────────────────────────────────────────────────────────────────────────
  SPRINT 2 — 1-2 New API Calls or Backend Functions
  Estimated effort: 2-4 days per feature
─────────────────────────────────────────────────────────────────────────────

2.1  BvP MATCHUP GRADE
───────────────────────
Where:    props.html prop cards, cheatsheet board, player modal
What:     Career batter-vs-pitcher grade shown as letter badge (A+→D)

New MLB Stats API call:
  GET /people/{player_id}/stats
    ?group=hitting&type=vsPlayer
    &opposingPlayerId={pitcher_id}
    &season=CURRENT + career

Cache: _bvp_cache dict keyed by (batter_id, pitcher_id)
  Populated on demand (first request triggers background fetch)
  TTL: once per day per pairing

Grade formula:
  Min 10 PA required for meaningful grade
  ops_ratio = batter_ops_vs_pitcher / batter_season_ops
  A+  = ops_ratio >= 1.40 AND PA >= 20
  A   = ops_ratio >= 1.20 AND PA >= 15
  B   = ops_ratio >= 1.05 AND PA >= 10
  C   = ops_ratio 0.85-1.05
  D   = ops_ratio < 0.85 OR PA < 10 (insufficient sample)

Display:
  [A+ BvP] green badge    [D BvP] red badge
  Tooltip on hover: "4-for-9 (.444) vs this pitcher | 2 HR | 1.102 OPS"

New endpoint: GET /api/bvp/<batter_id>/<pitcher_id>
  Returns: {grade, ops, avg, hr, pa, sample_note}

─────────────────────────────────────────────────────────────────────────────

2.2  PITCH TYPE ADVANTAGE SCORE
────────────────────────────────
Where:    props.html prop cards, batter matchup rows in deepdive.html
What:     How well this batter hits the pitcher's primary pitch type

Backend function: _pitch_type_advantage(batter_id, pitcher_id)
  Step 1: Get pitcher's primary pitch from _sv_arsenal_pct
          (already in cache — e.g., "FF" fastball at 58%)
  Step 2: Get batter's BA vs each pitch type from Savant statcast search
          pybaseball.statcast_batter(player_id, dt_start, dt_end)
          filtered to pitch_type == primary_pitch → compute AVG, SLG
  Step 3: Compare to batter's overall AVG

  Output:
    favorable   → batter avg vs this pitch >= .280 or 30+ pts above norm
    neutral     → within 20 pts of overall avg
    unfavorable → batter avg vs this pitch <= .200 or 30+ pts below norm

Display on prop cards:
  🟢 "Crushes sliders (.321)" — pitcher throws 48% sliders
  🔴 "Struggles vs fastball (.178)" — pitcher throws 61% fastballs
  ⚪ "Neutral matchup" — no significant pitch type edge

Cache: _pitch_adv_cache keyed by (batter_id, pitcher_id), daily TTL

─────────────────────────────────────────────────────────────────────────────

2.3  OPPONENT-SPECIFIC GAME LOGS
──────────────────────────────────
Where:    Player modal → Game Log tab
What:     Filter game log to show only games vs current opposing pitcher

New endpoint: GET /api/player/<player_id>/bvp/<pitcher_id>
  MLB Stats API: /people/{id}/stats?type=vsPlayer&opposingPlayerId={pid}
  Returns: career stats + last 5 matchup games with scores

Frontend changes to player modal Game Log tab:
  - Add toggle: [All Games] [vs This Pitcher]
  - When "vs This Pitcher" active, render BvP games table:
    Date | Score | AB | H | HR | RBI | K | BB | Result
  - Summary stat line at top:
    "vs. Pitcher Name: 14 AB | .357 AVG | 2 HR | 1.102 OPS"

─────────────────────────────────────────────────────────────────────────────

2.4  MATCHUP STORYLINES (Claude AI narrative)
──────────────────────────────────────────────
Where:    deepdive.html — new "📋 Key Storylines" card at top of page
What:     3-5 plain-language bullets generated by Claude for each game

Expand Claude prompt in /api/game-projection/<game_pk> to return
additional JSON block:

  "matchup_insights": [
    "Judge is 4-for-8 (.500) career vs this pitcher — BvP lean on hits",
    "Wind blowing IN at 14 mph — suppresses HR props both lineups",
    "Umpire zone 72 (pitcher-friendly) — fade under 5K pitcher prop",
    "Starter in DEALING mode (recent ERA 1.8 vs season 3.4) — K upside",
    "Series finale — back-end rotation expected for home team"
  ]

Claude receives: pitcher form, umpire tendency, weather, BvP highlights,
  series context, wind direction in the existing context payload

Display: Card with blue left border, headline "📋 Key Storylines",
  each insight as a bullet pill with context icon

─────────────────────────────────────────────────────────────────────────────

2.5  INJURY REPORT INTEGRATION
────────────────────────────────
Where:    props.html (banner + per-card flag), dashboard.html game cards
What:     Real-time IL status and game-time decisions per player

Data source: MLB Stats API
  GET /transactions?sportId=1&startDate={today}&endDate={today}
  Returns: all transactions including IL placements, activations, DL moves

New backend function: _fetch_injury_status()
  Runs in background thread at startup, refreshes every 60 minutes
  Builds: _injury_cache = {player_id: {status, type, date, description}}
  Status types: "IL_10", "IL_60", "DTD" (day-to-day), "GTD" (game-time)

Props board integration:
  - Filter option: [Hide IL Players] (default ON)
  - Per-card badge: 🏥 IL or ⚠️ GTD next to player name
  - Prop auto-flagged with "VERIFY STATUS" if player is DTD/GTD

Dashboard integration:
  - 🏥 INJURY ALERT banner on game card if key player is IL/GTD
  - Shows: "Luis Robert Jr. — 10-Day IL (knee)" on game card

─────────────────────────────────────────────────────────────────────────────

2.6  MONTE CARLO CORRELATION EXPOSURE + SGP BUILDER
─────────────────────────────────────────────────────
Where:    deepdive.html — new "🔗 SGP Builder" card
          tracker.html Parlay Builder section

What:     Expose the cross-player correlation matrix already computed
          implicitly inside 5,000 MC sim runs

Backend changes to /api/simulate/<game_pk>:
  After 5,000 runs, compute correlation matrix:
    For each player pair (A, B), compute Pearson r across all sim runs
    between their hit counts, HR, TB, RBI, Runs outcomes
  Add to response:
    "correlations": [
      {"playerA": "Trout", "marketA": "batter_hits",
       "playerB": "Trout",  "marketB": "batter_runs",
       "r": 0.78, "strength": "STRONG+"},
      {"playerA": "Ohtani", "marketA": "pitcher_strikeouts",
       "playerB": "Judge",  "marketB": "batter_hits",
       "r": -0.44, "strength": "NEGATIVE",
       "warning": "High K game suppresses opposing hits"}
    ]
    "top_sgp_combos": [  # top 5 positively correlated pairs
      {"legs": ["Trout HR", "Trout R"], "combined_prob": 0.21, "r": 0.71}
    ]

SGP Builder UI in deepdive.html:
  "🔗 SGP Builder" section below Monte Carlo card
  Shows top 5 suggested correlated parlay combos with:
    Combined probability | Correlation strength | Combined EV%
    [Add to Parlay] button → sends to tracker Parlay Builder

─────────────────────────────────────────────────────────────────────────────

2.7  NRFI / YRFI BOARD
───────────────────────
Where:    New tab on dashboard.html ("NRFI" tab) + deepdive section
What:     No Run First Inning prop analysis for every game

New endpoint: GET /api/nrfi/<game_pk>
New function: _compute_nrfi(game_pk)

Inputs (all from existing caches):
  - Away starter 1st-inning ERA: from pitcher recent_form game log,
    filter to inning==1 from game log data
  - Home starter 1st-inning ERA: same
  - Leadoff hitter stats (slot 1 in each lineup):
    xBA, OBP vs pitcher handedness from hitter_split_profile()
  - Park factor
  - Weather (temperature, wind)

NRFI probability model:
  P(no run | inning 1) ≈ (1 - away_team_score_rate_i1)
                        * (1 - home_team_score_rate_i1)
  
  away_score_rate_i1 = (home_starter_1st_inn_era / 9)
                       * park_factor * handedness_adj
  
  Outputs:
    nrfi_prob: float (0-1)
    nrfi_edge: nrfi_prob - market_nrfi_implied (from Odds API)
    yrfi_prob: 1 - nrfi_prob
    key_factors: ["Ace on mound", "Wind IN", "Weak leadoff hitter"]

Dashboard NRFI tab:
  Table of all today's games sorted by nrfi_prob:
    Game | Away SP | Home SP | NRFI Prob | NRFI Edge | Book Price | 💾

marketKey added: "nrfi" and "yrfi" to tracker market list

─────────────────────────────────────────────────────────────────────────────

2.8  H+R+RBI COMBO PROP MARKET
────────────────────────────────
Where:    props.html prop board, cheatsheet, tracker
What:     Combined hits+runs+RBI prop line — most popular MLB SGP market

New market key: "batter_hits_runs_rbis"
Projection formula (using existing MC sim outputs per player):
  hrr_mean = mean_hits + mean_rbi + mean_run
  hrr_prob_over_line = P(hits + rbi + runs > line)
  (compute from distribution across 5000 sim runs)

Lines supported: 1.5, 2.5, 3.5 (standard sportsbook lines)
Add to Odds API fetch: "batter_hits_runs_rbis" as market key

─────────────────────────────────────────────────────────────────────────────

2.9  DEFENSIVE RANKINGS PANEL
───────────────────────────────
Where:    Teams tab on dashboard.html, batter matchup rows in deepdive.html
What:     Ranked 1-30 table of teams by pitching quality metrics

Backend: New endpoint GET /api/teams/pitching-rankings
  Uses existing team_pitching_context() data for all 30 teams
  Ranks by: ERA, WHIP, K/9, HR/9 (separate columns + composite rank)

Display in Teams tab:
  Sortable table: Team | ERA Rank | WHIP Rank | K/9 Rank | Composite
  Color coded: Top 10 green, Bottom 10 red, Middle yellow

Display on deepdive batter matchup rows:
  Small badge next to pitcher name:
    "🔴 Top 5 Staff" | "🟢 Bottom 10 Staff" | "⚪ Mid-Pack"

─────────────────────────────────────────────────────────────────────────────

2.10  CROSS-SPORTSBOOK BEST AVAILABLE PRICE
─────────────────────────────────────────────
Where:    Every prop card (props.html, cheatsheet, tracker entry)
What:     Best over/under price across all books from Odds API

Backend changes to _build_props_for_game():
  Currently picks one bookmaker's price per prop
  New: loop all books for each player/market, find best_over_price
  Add to each entry:
    best_over_price:  float (best odds available for over)
    best_over_book:   string
    best_under_price: float
    best_under_book:  string
    line_range:       [min_line, max_line] across books
    book_count:       int (how many books offer this prop)

Display:
  Instead of one price, show: "Best: +115 @ DraftKings | Avg: -108"
  Line shopping indicator if line varies across books:
    "⚠️ Line varies: 0.5 (FD) vs 1.5 (DK) — check alt lines"

─────────────────────────────────────────────────────────────────────────────
  SPRINT 3 — New Pages, New Data Sources
  Estimated effort: 3-5 days per feature
─────────────────────────────────────────────────────────────────────────────

3.1  CHEATSHEET PAGE (Entire New Page)
───────────────────────────────────────
Route:    /cheatsheets
File:     cheatsheet.html (embedded in app.py as CHEATSHEET_HTML)
API:      GET /api/cheatsheets/today

Three MLB cheatsheets auto-computed every morning:

CHEATSHEET A: HITS BOARDcd /workspaces/mlb-analytics-hub
git status --short
git add -A
git commit -m "Commit all current unstaged changes"
git rev-parse --short HEAD
git --no-pager show --stat --oneline -1
  Columns: Player | Team | Batting Slot | Matchup | vs Hand | L10% | BvP | HUB | EV% | 💾
  Sorted by: HUB RATING desc
  Filter chips: by game, by market, by batting slot, vs LHP/RHP

CHEATSHEET B: BATTING ORDER MATCHUP
  For each game, rank all batters by composite matchup score:
    matchup_score = (bvp_grade_pts * 0.30)
                  + (l10_over_rate * 0.25)
                  + (handedness_split_ops * 0.20)
                  + (park_factor_adj * 0.15)
                  + (pitch_type_advantage_pts * 0.10)
  Displayed as ranked list per game:
    GAME: NYY @ BOS  |  Top Matchups:
    1. Aaron Judge   | A+ BvP | 80% L10 | Favorable PF | HUB 91
    2. ...

CHEATSHEET C: PITCHER WEAKSPOT
  For each starting pitcher today:
    - Which batting slots have historically hit them best
    - Which pitch type is most exploitable (from _sv_arsenal_pct)
    - L5 ERA, K/9, BB/9 with form badge
    - Recommended fade or target props
  Display as grid: 1 card per pitcher
    ┌── Gerrit Cole ────────────────────────────────┐
    │ Weakspot: Slots 2-4 (.312 avg against)        │
    │ Pitch vulnerability: Slider (.298 opp BA)     │
    │ L5 form: 🔥 DEALING (2.14 ERA last 5)         │
    │ Prop rec: FADE hits over 0.5 for slot 7-9     │
    └───────────────────────────────────────────────┘

New API endpoint: GET /api/cheatsheets/today
  Builds all three cheatsheets from in-memory caches
  Cache TTL: 30 minutes (refreshed when lineup confirmed)
  Background thread triggers rebuild when new lineups confirmed

─────────────────────────────────────────────────────────────────────────────

3.2  PITCHER PROP VULNERABILITY PROFILE
─────────────────────────────────────────
Where:    pitcher_deepdive.html — new section at bottom
          deepdive.html pitcher cards — collapsible sub-section
What:     Which batter props are most exploitable given THIS pitcher

Function: _pitcher_prop_vulnerability(pitcher_id, game_pk)
  Computes for each prop market how favorable this pitcher is for
  OPPOSING batters:

  hits_vulnerability:
    opp_batting_avg_allowed * park_factor → if >= .275, flag "EXPLOITABLE"
  hr_vulnerability:
    hr_allowed_per_9 > league_avg → flag "EXPLOITABLE"
  k_vulnerability (for batter K props — FADE signal):
    pitcher_k_rate > 28% → flag "FADE BATTER HITS" (high K environment)
  tb_vulnerability:
    hard_hit_pct_allowed > 40% → flag "EXPLOITABLE TB UPSIDE"

Display as "Prop Vulnerability" badge grid on pitcher card:
  [HITS ↑ EXPLOITABLE] [HR ↑ EXPLOITABLE] [K ↓ FADE HITS] [TB ↑ UP]

─────────────────────────────────────────────────────────────────────────────

3.3  PULL% IN AIR — ADDITIONAL SAVANT FIELD
─────────────────────────────────────────────
Where:    HR Profile panel (Feature 1.4), player modal
What:     Add pull_pct_air (pulled flyballs %) to Savant cache

In _maybe_refresh_savant():
  pybaseball.statcast_batter_exitvelo_barrels() already returns
  pull_percent field — add to _sv_bat_statcast cache

Field name: pull_pct_air
Used in: HR edge score computation (higher pull% + hitter's park = HR edge)

─────────────────────────────────────────────────────────────────────────────

3.4  PROP COMPARISON TABLE (Line Shopping View)
─────────────────────────────────────────────────
Where:    New sub-page or modal on props.html
What:     Side-by-side view of one prop across all sportsbooks

Triggered by: clicking a prop card → expand to show "Line Shopping" view
Display:
  ┌── Aaron Judge — Hits Over ──────────────────────────────────────────┐
  │  DraftKings:  Line 0.5  Over -165  Under +135                       │
  │  FanDuel:     Line 0.5  Over -170  Under +140  ← Best Under        │
  │  BetMGM:      Line 1.5  Over +115  Under -140  ← Best Over Alt     │
  │  Caesars:     Line 0.5  Over -160  Under +130                       │
  │  ESPN Bet:    Line 0.5  Over -155  Under +125  ← Best Price        │
  │                                                                      │
  │  Model: 73% over 0.5 | EV at DraftKings: +3.8% | EV at FD: +2.1%  │
  └─────────────────────────────────────────────────────────────────────┘

Backend: Odds API already returns all books — restructure response to
  group by player/market/side rather than by bookmaker

─────────────────────────────────────────────────────────────────────────────

3.5  SPRAY CHART (SVG Field Diagram)
──────────────────────────────────────
Where:    Player modal → new "🗺️ Spray Chart" tab
What:     Visual batted ball distribution on field SVG

Data source: pybaseball.statcast_batter() returns hc_x, hc_y coordinates
  Pull current season batted balls for any batter on demand

New endpoint: GET /api/player/<player_id>/spray
  Returns: list of {hc_x, hc_y, events, hit_distance, launch_angle,
                    exit_velocity, pitch_type} for current season

Frontend SVG:
  Hardcoded baseball diamond SVG (scalable, 400×400 viewport)
  Dots plotted at (hc_x * scale_x, hc_y * scale_y)
  Color by outcome: 🟢 Single/Double/Triple | 🔵 HR | 🔴 Out
  Size by exit velocity (larger = harder hit)
  Filter chips: [All] [Hits Only] [vs LHP] [vs RHP] [Home] [Away]

Park overlay: Hardcoded wall distances for all 30 parks as JSON dict
  Applied as arc overlay showing current game's park dimensions

─────────────────────────────────────────────────────────────────────────────

3.6  STRIKE ZONE CHART (SVG Zone Grid)
────────────────────────────────────────
Where:    Player modal → Pitch Arsenal tab (new sub-view)
What:     9-zone grid showing pitcher's tendencies + batter's zone results

Data: pybaseball.statcast_pitcher() pitch location data
  plate_x, plate_z per pitch → map to 3×3 zone grid

New endpoint: GET /api/player/<player_id>/zonechart
  Returns: zone_data[0-8] = {
    zone_id, pitch_pct, swing_rate, whiff_rate, ba, slg
  }

Frontend: SVG 3×3 grid (standard MLB strike zone proportions)
  Each cell colored by:
    RED   = whiff rate > 35% (pitcher dominates this zone)
    GREEN = BA > .300 in this zone (batter excels here)
    GREY  = neutral
  Tooltip: "Zone 5 (Heart): .312 BA | 18% Whiff | 42% Swing"
  Toggle: [Pitcher View] vs [Batter View]

─────────────────────────────────────────────────────────────────────────────
  SPRINT 4 — Architectural / Major Additions
  Estimated effort: 1-2 weeks per feature
─────────────────────────────────────────────────────────────────────────────

4.1  FULL TRACKER REBUILD
──────────────────────────
As described in Section 1. This is the largest single build item.
Estimated effort: 5-7 days
Includes: New schema, new routes, Parlay Builder, background auto-grading,
          live performance panel, Model Track Record banner

4.2  PROP PERFORMANCE EXPORT (CSV / PDF)
─────────────────────────────────────────
Where:    Tracker review tabs, Cheatsheet page
What:     Download button for any table or chart

Formats: CSV (all entry data for external analysis), PDF (summary card)
New route: GET /api/tracker/export/<date>?format=csv|pdf

4.3  BULK PROP SCAN (All Games, All Markets)
─────────────────────────────────────────────
Where:    New "🔍 Full Scan" button on props.html
What:     Pre-computes all props for all today's games in one batch call

New endpoint: GET /api/props/scan/today
  Loops all game_pks from today's schedule
  Runs _build_props_for_game() for each
  Returns flat list of all props, sorted by HUB RATING
  Cached with 20-minute TTL

Frontend: "Run Full Scan" button triggers this once per session
  Populates entire props board with all games' props
  Filter bar becomes the primary navigation tool

4.4  CONSISTENCY SHEETS
────────────────────────
Where:    New tab on dashboard.html or standalone /consistency route
What:     DraftKings-style consistency sheets showing over/under rates
          at different sample sizes (L5, L10, L20, Season)

Format: One sheet per market type
  Columns: Player | Team | Opp | L5 Over% | L10 Over% | L20 Over% | Season%
  Color: > 70% green | 50-70% yellow | < 50% red
  Filter: by game, by batting slot, by team

─────────────────────────────────────────────────────────────────────────────
  SECTION 3 — NEW ROUTES SUMMARY
─────────────────────────────────────────────────────────────────────────────

NEW PAGE ROUTES:
  /cheatsheets                         → Cheatsheet hub page
  /consistency                         → Consistency sheets page (Sprint 4)

NEW API ROUTES — RESEARCH:
  /api/cheatsheets/today               → All 3 cheatsheets computed
  /api/bvp/<batter_id>/<pitcher_id>    → BvP grade + history
  /api/player/<id>/bvp/<pitcher_id>    → Opponent-specific game log
  /api/player/<id>/spray               → Spray chart batted ball data
  /api/player/<id>/zonechart           → Strike zone chart data
  /api/nrfi/<game_pk>                  → NRFI/YRFI probability
  /api/props/scan/today                → Bulk all-game prop scan
  /api/teams/pitching-rankings         → Ranked defensive table

NEW API ROUTES — TRACKER:
  POST /api/tracker/pick               → Save single pick
  DELETE /api/tracker/pick/<id>        → Remove pick
  PATCH /api/tracker/pick/<id>         → Update pick (stake, actual)
  GET  /api/tracker/today              → All today's picks + live status
  GET  /api/tracker/performance        → Rolling performance (all sub-tabs)
  POST /api/tracker/parlay             → Save parlay unit
  GET  /api/tracker/model-record       → Per-market hit rates 30d
  GET  /api/tracker/export/<date>      → CSV/PDF export
  POST /api/tracker/settings           → Save bankroll + settings
  GET  /api/tracker/settings           → Load settings

─────────────────────────────────────────────────────────────────────────────
  SECTION 4 — NEW IN-MEMORY CACHES NEEDED
─────────────────────────────────────────────────────────────────────────────

_bvp_cache          keyed by (batter_id, pitcher_id), daily TTL
                    Populated on demand via background thread

_pitch_adv_cache    keyed by (batter_id, pitcher_id), daily TTL
                    Pitcher primary pitch vs batter avg against that pitch

_injury_cache       keyed by player_id
                    MLB Stats API transactions, refreshes every 60 min

_nrfi_cache         keyed by game_pk
                    NRFI probability per game, refreshes when lineup confirmed

_cheatsheet_cache   keyed by date string
                    Pre-computed cheatsheet data, 30-min TTL

_correlation_cache  keyed by game_pk
                    MC correlation matrix per game, valid same-day

_spray_cache        keyed by player_id
                    Batted ball coordinates, daily TTL

─────────────────────────────────────────────────────────────────────────────
  SECTION 5 — UPDATED app.py EMBEDDED CONSTANTS
─────────────────────────────────────────────────────────────────────────────

New HTML constants to embed in app.py:
  CHEATSHEET_HTML       → new /cheatsheets page
  TRACKER_HTML          → full rebuild (replaces deleted version)
  CONSISTENCY_HTML      → new /consistency page (Sprint 4)

Updated HTML constants:
  DASHBOARD_HTML        → Add NRFI tab, series badges, inline props strip
  DEEP_DIVE_HTML        → Add Key Storylines card, SGP Builder card,
                           Pitch Vulnerability section
  PROPS_HTML            → Add HUB RATING, EV% badge, Power Filter Bar,
                           Save Pick button, Model Track Record banner,
                           HR Profile panel, Line Shopping view

─────────────────────────────────────────────────────────────────────────────
  SECTION 6 — RECOMMENDED BUILD ORDER
─────────────────────────────────────────────────────────────────────────────

PHASE 1 — TRACKER REBUILD (Do first — everything else feeds into it)
  [1] New tracker data schema + TRACKER_STORE migration
  [2] New tracker API routes (pick CRUD, settings, model-record)
  [3] New tracker.html (2-column layout, live bet slip, parlay builder)
  [4] Performance tabs (calibration, CLV, bankroll, attribution, backtest)

PHASE 2 — CORE PROP ENHANCEMENTS (Highest ROI, all Sprint 1)
  [5] HUB RATING badge everywhere
  [6] EV% badge + +EV filter toggle
  [7] Save Pick button on all prop cards
  [8] Model Track Record banner
  [9] Power Filter Bar
  [10] Inline Props Strip on dashboard game cards

PHASE 3 — MATCHUP INTELLIGENCE (Sprint 2)
  [11] BvP Matchup Grade
  [12] Pitch Type Advantage Score
  [13] Matchup Storylines (Claude)
  [14] HR Profile Panel (5 Statcast signals)
  [15] Opponent-Specific Game Logs
  [16] MC Correlation + SGP Builder
  [17] Cross-Sportsbook Best Available Price

PHASE 4 — NEW MARKETS + PAGES (Sprint 2-3)
  [18] NRFI / YRFI Board
  [19] H+R+RBI Combo Market
  [20] Cheatsheet Page (all 3 cheatsheets)
  [21] Defensive Rankings Panel
  [22] Series Context Badges
  [23] Injury Report Integration
  [24] Pitcher Prop Vulnerability Profile

PHASE 5 — VISUALIZATIONS + POLISH (Sprint 3-4)
  [25] Spray Chart (player modal)
  [26] Strike Zone Chart (player modal)
  [27] Prop Comparison / Line Shopping view
  [28] Consistency Sheets page
  [29] Bulk Prop Scan
  [30] CSV / PDF Export

─────────────────────────────────────────────────────────────────────────────
  SECTION 7 — WHAT YOUR APP WILL HAVE THAT NO COMPETITOR HAS
─────────────────────────────────────────────────────────────────────────────

After this build plan is complete, MLB Analytics Hub will be the ONLY
free tool that combines all of the following in one place:

  ✅ Monte Carlo simulation (5,000 runs) with correlation matrix
  ✅ HUB RATING — single proprietary composite score per prop
  ✅ EV% displayed explicitly on every prop card
  ✅ BvP grade with historical matchup data
  ✅ Pitch type advantage score (batter vs pitcher's arsenal)
  ✅ Umpire analytics with zone rating and K/BB/run tendencies
  ✅ Lineup change detection (live scratch alerts)
  ✅ NRFI/YRFI board with AI-computed probability
  ✅ SGP/Parlay Builder with correlation health scoring
  ✅ Model Track Record banner (calibration transparency)
  ✅ Cross-sportsbook best available price (line shopping)
  ✅ Full CLV tracker with bankroll curve and attribution
  ✅ AI matchup storylines (Claude-generated narrative)
  ✅ Cheatsheet system (Hits Board, Batting Order, Pitcher Weakspot)
  ✅ Spray charts and strike zone visualization
  ✅ Series context badges (series opener/finale edge modifiers)
  ✅ Injury report integration
  ✅ H+R+RBI combo prop market
  ✅ Defensive rankings panel (all 30 teams ranked)
  ✅ One-click "Save Pick" from research to tracker

Props.Cash:    $19.99/month — lacks Monte Carlo, CLV, umpire, NRFI
Propcaster:    Paid — lacks simulation, umpire, CLV, cheatsheets
PropFinder:    $14.99/month — lacks simulation, CLV, umpire, NRFI
Buckets2Bucks: Free — lacks nearly everything above

MLB Analytics Hub: FREE. Outperforms all of them.

=============================================================================
  END OF BUILD PLAN
=============================================================================
