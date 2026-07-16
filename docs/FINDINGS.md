# FINDINGS — Integrated Grant Lead Research (through 2026-07-15)

This records live integrations, verified lead findings, and open implementation work. It is not the
complete nationwide candidate list. See `docs/source_inventory/README.md` and its generated CSVs for
the canonical discovery catalog.

## What is built locally

- **verified (offline tests):** typed package, versioned truth/event schema, official-source
  pollers, scoring/dedup, Slack proactive/search tools, Excel/Google Sheets export jobs,
  contact integrity gates, Persequor outbox/retry, NCES enrichment, Salesforce reader snapshots,
  and the disabled create-only Salesforce Campaign approval workflow.
- **verified (live read-only, 2026-07-13/14):** USAspending, Grants.gov, SAM.gov, WEBS fetch/parser,
  California Grants Portal, OregonBuys recent-bids fetch/table parse, and NCES district data.
- **needs-testing:** Salesforce and Persequor live round trips, a positive OregonBuys security bid,
  Salesforce Campaign sandbox creation, production cron, and production deployment.

## Nationwide discovery snapshot

- **verified (catalog validation):** 270 candidate and integrated source records validate across
  federal, state, county, city, school-district, specialized-jurisdiction, multi-jurisdiction, and
  portal-family levels.
- **verified (access evidence):** 34 no-auth sources have verified access, 11 additional no-auth
  classifications remain candidates, 2 public APIs require keys, 15 sources require free accounts,
  4 require supplier accounts, and 204 candidates still have unknown access.
- **verified (gap pass):** exact official endpoints were added for Ada County, Troy School District,
  Houston ISD, and Seattle Public Schools. Connecticut is an evidenced `not_applicable` county layer
  because its official FAQ states that county government was dissolved in 1960.
- **verified (new evidence):** thirty Firecrawl selected-result checks now persist queries, result
  metadata, deterministic evidence hashes, and scraped-content hashes.
- **verified (raw discovery batch):** batch `20260716T004633Z` persisted 27 tasks, 27 attempts, and
  126 returned results across CA, NH, and TX without storing credentials. All 27 searches succeeded.
  Eight official sources were promoted only after manual page review and successful selected-page
  scraping; two timed-out page scrapes and irrelevant or third-party results were not promoted.
- **verified (county universe):** the pinned 2025 Census Gazetteer produces 3,144 county-equivalent
  tasks across 50 states plus DC: 56 linked source candidates, 15 structural exceptions, and 3,073
  entities explicitly marked `not_researched`.
- **verified (county batch):** Firecrawl search and scrape checks added official procurement pages for
  Los Angeles, Orange, Oakland, Allegheny, and King counties. Their portal/account boundaries are
  recorded separately from runtime integration status.
- **verified (district/place batch):** reviewed Firecrawl batches added large-entity and sampled
  district/place sources. The school queue now has 66 linked candidates among 13,363 Census entities;
  the incorporated-place queue has 14 linked candidates among 32,058 Census places.
- **verified (access nuance):** Philadelphia's Public Purchase portal states that registration is
  free but requires login to view bids. The other nine new pages exposed opportunity metadata without
  authentication; this does not imply anonymous bid submission.
- **needs-testing:** county, city, and school-district coverage remains incomplete. The place queue is
  not a unique-government registry and does not include every county subdivision/MCD. The earlier
  185 Firecrawl-discovered rows predate immutable check storage and are not independently replayable.

## What remains

- **needs-testing:** verify live contact enrichment and positive RFP extraction against real rows.
- **needs-testing:** complete Persequor status reflection and one test-mode end-to-end brief.
- **needs-testing:** run Salesforce match-quality shadow testing, then a sandbox-only Campaign action.
- **needs-testing:** deploy only a committed revision through grants-ops-guardian and install scoped cron.
- **assumed roadmap:** PA PCCD parser, MI CSSGP, COPS announcement, SSE state-subgrant, board-agenda,
  and additional compliant RFP watchers remain valuable next sources.

## Verified API facts (tested live through 2026-07-14)
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
- **SAM.gov**: a key is mandatory; the configured keyed opportunities integration was exercised live.
  Pagination and parsing are fixture-tested. Assistance Listings is cataloged separately and still
  needs an executable poller.
- **PA PCCD**: award PDFs fetchable from pa.gov without auth (verified via direct PDF pull).
- **California Grants Portal**: official CKAN metadata and CSV feeds need no API key. A 2026-07-14
  dry run parsed 831 records and wrote nothing. The parser keeps portal publication dates as
  provenance rather than treating them as award dates.
- **NCES EDGE**: the official 2024–25 directory/enrollment service needs no key. Tustin Unified
  matched uniquely in the live check with NCES district id `0640150` and 21,220 students.
- **OregonBuys**: the public recent-bids PDF fetched and its table parsed in a 2026-07-14 dry run;
  it contained zero matching physical-security bids at that moment. The authenticated full-search
  workflow is intentionally not bypassed.

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
