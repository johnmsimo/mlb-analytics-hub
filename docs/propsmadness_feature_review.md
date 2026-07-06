# PropsMadness — Complete Feature Review

Reviewed: 2026-07-06, via the live app at
`https://propsmadness.com/?market=featured&match=10040&player=7948`
(MLB · Mets @ Braves · Freddy Peralta · Strikeouts view), rendered in a real
browser, plus direct inspection of its JSON API (`api.propsmadness.com`).

## What it is

PropsMadness is a **player-prop research and visualization tool** (not a book,
not a picks service). It aggregates sportsbook prop lines and pairs them with
deep player/matchup statistics so users can judge overs/unders themselves.
Freemium SaaS: free users see only "featured" players; Premium ($20/mo or
$200/yr, −20%) unlocks everything.

**Leagues:** MLB, WNBA, FIFA World Cup 2026 (tagged NEW), NBA and NFL
(present in nav, greyed out in-season/off-season). One shared UI across all
leagues.

---

## 1. Core layout & navigation

- **Top bar:** league switcher (MLB / WNBA / World Cup / NBA / NFL with
  logos), Walkthrough (video tutorial), glossary shortcut, account avatar
  (Clerk auth: sign-in/up, subscription management).
- **Left sidebar — slate browser:**
  - Search box: "Search players or teams…" (live-filters the slate).
  - Market dropdown: `Featured` + every prop market (see §2).
  - Game dropdown: `All Games` or a single game.
  - Game cards for the full slate (team logos, day + start time), expandable
    into the game's player list. Each player row shows headshot, position
    badge, line + O/U odds for the selected market — or an `UNLOCK` lock for
    non-featured players (free tier gating).
- **Main panel:** market tabs across the top (pitcher markets, then batter
  markets — irrelevant tabs greyed per player type), then the selected
  player's research view (§3–§7).
- **Deep linking:** URL query params (`?market=…&match=…&player=…`) restore
  the exact view — shareable links.
- **Footer:** Glossary, Blog, Help, Contact, T&C, Privacy, cookie
  preferences (Essential/Analytics/Functional), social links (Instagram, X,
  YouTube, Reddit).

## 2. Prop markets (MLB)

Twelve markets, pitcher and batter:

| Pitcher | Batter |
|---|---|
| Strikeouts | Hits |
| Pitcher Outs | Home Runs |
| Earned Runs | Total Bases |
| Hits Allowed | Hits+Runs+RBIs |
| Pitcher Walks | Runs, RBIs, Batter Walks |

Plus a **Featured** meta-market (the curated free board). Other leagues
mirror this (WNBA: Points etc.; World Cup: Goals etc.).

## 3. Odds & line features

- **Primary line chip:** current line + Over/Under American odds with
  sportsbook logo (FanDuel is the default/fallback book).
- **Alt lines API/UI:** every alternate line for the market across books —
  observed BetMGM, Caesars, DraftKings, FanDuel, Hard Rock, Sportsbet
  (e.g. K 1.5 through 9.5 with per-book prices).
- **Line Shopper:** cross-book price comparison; "Customize your own Line
  Shopper" is a Premium feature.
- **Closing lines:** a per-game closing-line archive for the season
  (`/closing-lines/<market>`). The chart has a **LINE ⇄ CLOSING LINES
  toggle** — flipping it re-computes the hit rate against each game's actual
  closing line instead of today's line (e.g. 58.3% → 33.3%).
- **Game-level odds:** run line, total runs (and league equivalents:
  spread/total) per match.

## 4. Hit-rate chart (the centerpiece)

- Game-by-game bar chart of the stat vs the line: green = over, red =
  under, dashed `?` bar for today's pending game.
- Opponent logo + date under each bar; line value badge on the axis.
- **Hit Rate KPI:** e.g. "58.3% (7/12) — 12 of 18 games" (respects active
  filters; the "of 18" shows filtered vs total sample).
- **Stat strip** (market-aware): for K's — IP, K/GS, Pitches/GS, ERA, K%,
  BB%, CSW% — each with season value plus red/green delta vs baseline.
- Chart respects every filter in §5, so the hit rate is always
  "hit rate under these conditions."

## 5. Filter engine (Filters panel)

- **Season:** 2024 / 2025 / 2026 / All.
- **Games window:** last 10 / 20 / Max / custom "− 12 +" stepper (with a
  lock toggle to persist).
- **Suggested** (one-tap smart filters): Innings Pitched, Pitch Count, H2H,
  `Opp K% #8`, `Opp Whiff% #13` (+2 more) — chips embed the opponent's rank
  out of 30, red = tough matchup, green = favorable.
- **Opp Rankings tab:** opponent lineup ranks for K%, Whiff%, BB% — Overall,
  vs RHP, and vs LHP, all color-coded.
- **Splits tab:** H2H · Home · Away · Day · Night · Regular · Playoffs ·
  Win/Loss margin · Game Total Runs · closing-line splits (CL Run Line, CL
  Total Runs, CL K) · Park & Weather splits (HR +4%, R +2%, 1B +6%) ·
  Overall Park & Weather ("Hitter Friendly").
- **Stats tab:** threshold filters on the player's own per-game stats —
  Innings Pitched, Pitch Count, Strikeouts, K%, SwStr%, CSW%, Chase%,
  Zone%, F-Strike%, BB%, K-BB% — with an **Average / Median mode toggle**.
- **Teammates filter:** filter games by whether a teammate was in the
  lineup, each teammate annotated with an impact value (e.g. Lindor −0.3).

## 6. Game conditions bar

Venue name · stadium type (outdoor/dome) · matchup (NYM @ ATL) · temperature
· wind speed **and field-relative direction** ("10 mph · In from center
field", computed from wind bearing vs field orientation) · derived park +
weather effects (HR +4% | Runs +2% | 1B +6%, plus 2B/3B) · overall verdict
("Hitter Friendly" / pitcher friendly).

## 7. Matchup research modules

- **H2H header:** pitcher vs opposing team, last 3 seasons (PA · H · TB ·
  HR · K · BB); opponent selector dropdown (whole team or a specific
  batter).
- **Matchup Analyzer (percentile bars):** pitcher vs opposing lineup, one
  row per stat, each side's 1–99 percentile rank on a bar (colored only
  when one side has a clear edge). Stats: BA, BB%, Chase%, Whiff%, K%,
  Contact%, Zone%, SwStr%, xBA, xwOBA. Split toggles on both sides
  (Overall / vs LHB / vs RHB and Overall / vs LHP / vs RHP), pitch-count
  and PA sample sizes shown, season selector.
- **By pitch type table:** for each pitch (Fastball 94mph, Changeup,
  Curveball, Slider, "+5 more"): pitcher usage (count + %), velocity, and
  both sides' BA / SLG / wOBA / Whiff% with percentiles; opposing lineup's
  aggregate numbers vs that pitch type mirrored on the right; standout
  cells highlighted.
- **Official Opposing Lineup table:** confirmed batting order with hand
  (L/R/S), PA, K%, Chase%, BB%, Whiff%, Contact%, Zone%, CSW%, SwStr% —
  green (batter-friendly) / red (pitcher-friendly) cell coloring, split
  tabs Overall / vs RHP / **vs this specific pitcher**, season selector,
  and an in-app "how to read the colors" explainer.
- **Bullpen panel:** relievers most likely to enter — pitch count over last
  3 games, days of rest, K%, BB%, ERA, WHIP, same color coding.

## 8. Accounts, monetization, community

- **Auth:** Clerk (email/social sign-in, subscription billing embedded).
- **Free tier:** featured players only; everything else shows `UNLOCK`.
- **Premium — $20/mo or $200/yr (−20%, $16.67/mo):**
  1. Unlock all players
  2. Customize your own Line Shopper
  3. Defensive insights
  4. Spot favorable matchups
  5. Find similar players
  6. Private Discord community
- **Affiliate program** (Tolt), **Intercom** in-app support chat,
  **walkthrough video**, onboarding "Suggested Filters" coach-marks.

## 9. Content & education

- **Glossary:** 118 terms across leagues (MLB 54, NBA 24, NFL 26, WNBA 24,
  World Cup 25).
- **Blog:** prop-betting strategy and product guides (e.g. a 5-minute
  beginner's guide covering search → filters → odds comparison).
- Inline "?" tooltips and explainer boxes throughout the UI.

## 10. Under the hood (observed)

- **Frontend:** Next.js (App Router, RSC, Turbopack) behind Cloudflare;
  dark theme; responsive.
- **API:** REST at `api.propsmadness.com` — notable endpoints:
  `offer/<league>/bets/featured`, `players/<id>/match/<id>/bet-offers`
  (+`/alt/<market>`), `players/<id>/season/current/closing-lines/<market>`,
  `players/<id>/match-statistics`, `players/<id>/h2h/team/<id>`,
  `matches/<id>/{matchup/all, lineups/statistics/all, bullpen/statistics,
  venue/weather, bets}`, `team-rankings/season/current/by-match/...`,
  `sportsbook` (auth-gated).
- **Data depth:** the per-game statistics payload carries ~200 fields per
  game — full Statcast expected stats (xBA/xSLG/xwOBA/xERA), barrel% /
  hard-hit% / launch angle, platoon splits both directions, SIERA/FIP/wRC+,
  per-game park factors, opponent lineup stats and opponent-pitcher
  percentiles. Odds/logos reference **OpticOdds** CDN (likely the odds data
  vendor).
- **Analytics/ops:** PostHog (feature flags, surveys), Facebook pixel,
  Intercom, Tolt.

---

## Notable ideas relative to this repo (mlb-analytics-hub)

Things PropsMadness does that we don't (or do differently) — candidate
inspiration for our props/tracker pages:

1. **Filterable hit-rate bar chart vs the line** with a closing-line toggle
   — our tracker records CLV but doesn't visualize per-game over/under
   history against either line.
2. **Conditional hit rates via a filter engine** (splits, opponent ranks,
   park/weather, stat thresholds, teammate-in-lineup) — our
   `/api/props/trends/*` is fixed-window.
3. **Alt-line + multi-book line shopping** surfaced per player (we have
   `/api/props/line-shopping/<pk>` but only the primary line UI).
4. **Wind rendered field-relative** ("In from center field") — we store
   raw weather; the field-orientation translation is a nice touch.
5. **Percentile-bar matchup analyzer** with per-pitch-type join — we compute
   an equivalent (`_arsenal_matchup_from_stats`) but render only a verdict
   note, not the full visual comparison.
6. **Lineup-vs-this-pitcher split tab** and **bullpen fatigue with rest/PC
   L3** presented as betting context on the same screen.
7. Freemium gating + walkthrough/glossary onboarding (product, not model).

What we have that it lacks: model probabilities/EV (it shows **no
projections, edges, or model outputs at all** — pure descriptive stats),
Kelly staking, bet tracking/grading, parlay tools, calibration/CLV
analytics, AI narratives.
