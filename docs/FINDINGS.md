# FINDINGS — Grant Lead Research Session (2026-07-13)
Everything discovered, verified, and still open. Companion to `grant_lead_source_inventory.md`.

## What was built this session
1. **`grant_watch.py`** — v1 poller scaffold: Grants.gov + USASpending (prime awards) + WEBS
   scraper + SAM.gov stub, SQLite dedupe, console alerts. Grants.gov/USASpending payloads are
   exact copies of live-verified calls. Script itself NOT yet executed end-to-end. WEBS parse
   selectors unverified. No Slack, no contact enrichment, no subawards poller yet.
2. **`data/svpp_active_awards_CA_MI_PA_WA.csv`** — 75 active SVPP awards (real, pulled live).
3. **`docs/grant_lead_source_inventory.md`** — full source map w/ verification status per source.
4. This findings doc + `CLAUDE.md` build briefing.

## What still needs to be built
(Phases 1–5 detailed in CLAUDE.md)
- Run/fix/verify v1 script; proper DB schema; seed from CSV
- New pollers: USASpending subawards (NSGP), PA PCCD PDFs, MI CSSGP PDFs, COPS fall
  announcement watcher, SSE (84.184A) state-subgrant watcher
- Contact enrichment (website/staff-directory extraction via Claude API; ZoomInfo flagging)
- Slack weekly digest + approve-to-email flow via existing @Persequor agent
- Cron; then DigitalOcean Postgres migration; then state expansion by config
- SAM.gov poller once Chase retrieves his API key (sam.gov → Workspace → Account Details;
  email OTP step is his)

## Verified API facts (tested live in browser, 2026-07-13)
- **Grants.gov**: `POST https://api.grants.gov/v1/api/search2`, no auth. Body e.g.
  `{"keyword":"school violence prevention","oppStatuses":"posted","rows":25}`.
  Returns `data.hitCount`, `data.oppHits[]` (id, number, title, agency, openDate, closeDate, cfdaList).
  FY26 SVPP live: opp id 362738, `O-COPS-2026-172540`, CFDA 16.071, closes **2026-08-04**
  (JustGrants step 2 closes 2026-08-11).
- **USASpending**: `POST https://api.usaspending.gov/api/v2/search/spending_by_award/`, no auth.
  award_type_codes ["02","03","04","05"] = grants. 100 rows/page max; paginate on
  `page_metadata.hasNext`. `time_period` floor 2007-10-01.
  `subawards:true` swaps the result shape to Sub-Awardee fields — this exposes NSGP (97.008)
  end recipients (verified: named WA synagogues/churches/schools, $120–300K, Nov 2024 round).
- **SVPP CFDA split**: 16.071 (FY25+) AND 16.710 (FY21–FY24; filter description for
  "school violence|SVPP" — 16.710 alone contains 450 CA COPS awards, only 71 are SVPP).
- **WEBS**: `https://pr-webs-vendor.des.wa.gov/BidCalendar.aspx` public, no login, ~189 rows
  on test day. Frameset app; parse raw HTML. Filter-by-org uses ASP.NET VIEWSTATE postbacks —
  default <All> view is fine. State agencies must post; districts/cities/higher-ed optional.
- **SAM.gov**: keyless request rejected (key mandatory). Rate limits + search fields UNVERIFIED.
- **PA PCCD**: award PDFs fetchable from pa.gov without auth (verified via direct PDF pull).

## The lead lists (as of 2026-07-13)

### Active SVPP money by state (gold leads — in data/ CSV)
| State | Active awards | Active $ | FY25 cohort (newest) |
|---|---|---|---|
| CA | 35 | $14.1M | 14 |
| MI | 28 | $10.3M | 12 |
| PA | 9 | $2.8M | 2 |
| WA | 3 | $780K | 2 |

### Hottest: FY25 cohort, $500K max awards, spend to 9/30/2028
Birmingham Community Charter HS (CA), Galt Joint Union Elementary SD (CA), Modesto City
Schools (CA), Bellaire Public SD (MI), Mt. Morris Consolidated Schools (MI — won $500K in
BOTH FY23 and FY25 = $1M repeat winner), Castle Rock SD 401 (WA).

### Use-it-or-lose-it: windows expiring 2026-09-30 (~11 weeks)
CA: Tustin USD, Gold Oak Union SD, Placer Union HSD, Colton Joint USD, El Dorado HSD,
CORE Butte Charter, Oxford Prep, Guadalupe Joint Union.
MI: Godfrey Lee PS, Mt. Morris (FY23 award), Memphis Community SD, East Jordan PS,
Saginaw Chippewa Tribe, Westwood Community SD.
PA: School District of Philadelphia ($500K, FY22), Harrisburg SD, Lehigh CTI.

### PA PCCD — single richest source found
- **347 nonpublic schools awarded 6/3/2026, $19.4M, project start 7/1/2026** — full named
  list w/ county + amount in the awards PDF (fetched + verified). These schools have fresh
  money and (likely) no vendor locked. Purest gold leads in the dataset.
- Plus **$100M/yr formula grants to ALL PA public school entities** (FY25-26 round opened
  Dec 2025; eligible-amount-per-district appendix published in the solicitation).
- Award PDFs directory: pa.gov → PCCD → schoolsafety → school-safety-award-documents.

### Other active gold leads
- STOP (16.839): Spokane SD 81 holds ~$2M across two active awards; ESD 101 & ESD 112 $1M each;
  Pacific County $975K. Caveat: STOP skews software/threat-assessment > cameras.
- NSGP (97.008): per-state subrecipient lists via subawards — WA sample includes Islamic Center
  of Bothell ($149.7K), Temple B'nai Torah ($150K), St. Michael's Church ($300K), etc.
  MI/CA/PA lists not yet pulled — Phase 1 task.

## Program calendar (why timing matters)
- **FY26 SVPP**: apps close 8/4/26 → awards announced ~Sept–Oct 2026 → freshest gold wave of
  the year. Silver window NOW (districts preparing applications need vendor quotes).
- **FY26 SSE (84.184A)** — NEW $93M program: state-agency apps close 7/28/26; ~30 states win
  $500K–$5M; states then subgrant to districts (locks, secure entry, perimeter, visitor
  screening per the Uvalde report). Expect district-level lead waves starting early 2027.
- **PA**: $100M formula round annually (opened Dec 2025); nonpublic targeted round awarded June.
- **NSGP**: annual; recent subaward dates clustered Oct–Nov.
- **MI CSSGP**: annual-ish cycles via MSP; award lists published per FY.

## Honest limitations / open questions
- Award $ = obligated, not remaining. Outlay-vs-obligation pull would show who still has money.
- SVPP/STOP fund non-camera items too (training, mental health) — not every dollar addressable.
- Federal applications are not public → SILVER leads must come from board minutes, RFP portals
  (PlanetBids CA, MITN MI, PennBid PA — all unverified), and state solicitation activity.
- No CA state hardening program equivalent to MI/PA was found (not proven absent — one more
  pass on CDE/Cal OES warranted). CA nonpublics: use NSGP; CA publics: SVPP + local bonds.
- WEBS keyword scan on test day: 0 security hits in visible rows — inconclusive (collapsed
  rows not scanned); the Python parser must work from raw HTML.
- ESSER is dead (deadlines passed). Ignore.
