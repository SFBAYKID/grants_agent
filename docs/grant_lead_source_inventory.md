# Grant-Lead Data Source Inventory — Monarch Connected
Compiled 2026-07-13. Every source labeled with verification status from live testing this session.
Lead definitions: **GOLD** = entity just received security funding. **SILVER** = entity is applying / has an open RFP.

---

## TIER 1 — VERIFIED TODAY (live data pulled in this session)

### 1. USASpending API — prime awards
- Endpoint: POST https://api.usaspending.gov/api/v2/search/spending_by_award/ — no key
- Lead type: GOLD (federal award = money in hand)
- Programs to poll: CFDA 16.071 + 16.710 (SVPP — split across both, filter 16.710 by
  description containing "school violence"/"SVPP"), 16.839 (BJA STOP — more software/threat
  assessment than cameras, still relevant)
- Verified output: 75 active SVPP awards CA/MI/PA/WA, ~$28M
- Limits found: 100 rows/page (paginate), time_period floor 2007-10-01

### 2. USASpending API — SUBAWARDS (`subawards: true`)
- Same endpoint, unlocks pass-through recipients
- Lead type: GOLD — this is how you see NSGP (CFDA 97.008) end recipients: private schools,
  synagogues, churches, nonprofits at $100–300K each (NSGP is nearly pure physical security)
- Verified output: named WA subrecipients w/ dates (Nov 2024 round)
- Caveat: subaward reporting lags prime awards; some states report late/incomplete

### 3. Grants.gov search2 API
- Endpoint: POST https://api.grants.gov/v1/api/search2 — no key
- Lead type: pipeline signal (opportunity opens → application season → award wave)
- Also supports oppStatuses "forecasted" = earliest possible signal
- Verified output: FY26 SVPP (closes 8/4/26), FY26 Port Security, etc.
- Caveat: keyword noise — "surveillance"/"security" pull CDC + cyber; use phrase list + scoring

### 4. PA PCCD award PDFs (pa.gov)
- Lead type: GOLD, highest density found anywhere
- Verified: pulled the 6/3/2026 Targeted School Safety awards PDF — 347 nonpublic schools,
  $19.4M, projects START 7/1/2026 (shopping NOW)
- Also: $100M/yr formula grants to ALL PA public school entities (FY25-26 round announced Dec 2025)
- Access: PDFs on pa.gov — scrape the school-safety-award-documents directory; parse w/ pdfplumber

### 5. WEBS — Washington bid calendar
- URL: https://pr-webs-vendor.des.wa.gov/BidCalendar.aspx — public, no login
- Lead type: SILVER (live RFPs)
- Verified: public, ~189 rows today, server-rendered ASP.NET (requests + BS4 parseable)
- Caveats: only state agencies REQUIRED to post; districts/cities optional. Keyword scan of
  visible text found 0 security hits today (collapsed rows not checked — parse raw HTML).
  Better long-term: register as vendor w/ commodity codes → parse notification emails via Gmail.

### 6. California Grants Portal / data.ca.gov
- Official CKAN metadata plus daily opportunity and fiscal-year award CSVs; no key
- Lead type: GOLD only when a named recipient award has physical-security evidence;
  application-window records remain lower-priority pipeline signals
- Verified 2026-07-14: live dry run parsed 831 records and wrote zero rows; current physical-security
  opportunity count was zero
- Caveat: portal publication/update dates are provenance, not award-action dates; undated awards are
  backfill-suppressed and cannot be described as "just awarded"

### 7. OregonBuys recent-bids PDF
- Official Oregon DAS seven-day selected-bids publication; no key
- Lead type: SILVER physical-security solicitations
- Verified 2026-07-14: PDF fetch, text/table extraction, and a truthful zero-match live dry run
- Needs-testing: entity parsing on a live physical-security row. The broader search requires a
  supplier session and is intentionally not automated around that boundary.

### 8. NCES EDGE 2024–25 (enrichment, not a lead source)
- Official school membership aggregation and district office city; no key
- Verified 2026-07-14: Tustin Unified uniquely matched NCES id `0640150`, enrollment 21,220
- Conservative behavior: exact normalized district match within one state; ambiguous names remain
  unmatched. Statewide production coverage remains needs-testing.

---

## TIER 2 — CONFIRMED TO EXIST, ACCESS NOT YET WIRED

### 9. Michigan MSP Competitive School Safety Grant Program (CSSGP)
- michigan.gov/msp → Grants & Community Services → school safety; award lists published per FY (PDFs)
- Lead type: GOLD. Eligible costs are literally Verkada's catalog: access control systems,
  intercom w/ access, barrier systems, duress/panic alarms, doorway hardening
- Eligible: public AND nonpublic schools, districts, ISDs. ~$10M FY22; proposed much larger since
- Future work — owner: Grant data-source maintainer. Verify the latest-cycle award-list URL and parser.

### 10. FEMA NSGP state-published subrecipient lists
- State Administrative Agencies (WA Military Dept, Cal OES, MI State Police, PEMA) publish
  awardee lists, often earlier than USASpending subaward data
- Lead type: GOLD. Future work — owner: Grant data-source maintainer. Locate each state's posting page.

### 11. NEW: School Safety Enhancement (SSE) program — 84.184A
- Brand-new FY26 federal program, $93M, apps close 7/28/2026; grants go to ~30 state education
  agencies ($500K–$5M) which then SUBGRANT to districts; physical security focus (locks, secure
  entry, perimeter, visitor screening) per the Uvalde report
- Lead type: future GOLD wave — state awards ~fall 2026, then state subgrant rounds = fresh
  district lead lists in early 2027. Watch ed.gov 84.184A page + each state's subgrant process

### 12. SAM.gov Opportunities API
- Lead type: SILVER (federal-side RFPs; some school/city solicitations w/ federal nexus)
- Verified: keyless request rejected (key required). BLOCKED on Chase signing in to grab key
- Unverified: rate limits, keyword-vs-title-only search

### 13. COPS/DOJ press releases + annual SVPP awardee PDFs
- cops.usdoj.gov publishes state-by-state award lists at announcement time (~Sept/Oct) —
  faster than USASpending. Lead type: GOLD, seasonal. Future work — owner: Grant data-source
  maintainer. Wire an autumn watcher before the next award season.

---

## TIER 3 — KNOWN CHANNELS, UNVERIFIED (next pass)

The nationwide discovery and safe-adapter design is maintained in
`docs/nationwide_source_strategy.md`. It covers all-state portal discovery without
mislabeling a directory entry as a verified integration.

### 14. School board meeting agendas/minutes (BoardDocs, Simbli, etc.)
- THE silver-lead source: boards must approve grant applications and vendor contracts.
  "Resolution to apply for SVPP/CSSGP" in minutes = applying now
- Fragmented per district; needs a crawler + Claude extraction. Not verified this session.

### 15. Regional bid platforms (silver leads)
- CA: PlanetBids (widely used by districts/cities) — unverified
- MI: BidNet Direct / MITN network — unverified
- PA: PennBid — unverified
- National: OpenGov Procurement, Public Purchase, DemandStar, Bonfire — unverified
- These are account-based portals; per-portal access rules TBD

### 16. Bond measures (Ballotpedia school bond elections, CA especially)
- Passed facilities bond = large multi-year budget that typically includes security scope
- Lead type: GOLD-adjacent, big dollars, slow cycle. Unverified.

### 17. State single-audit / grant transparency portals
- e.g., PA has Egrants records; states publish grant transparency data. Unverified.

## Dead ends / honest notes
- ESSER: expired (obligation deadlines passed) — ignore
- CA: no dedicated state school-hardening grant found equivalent to MI CSSGP or PA PCCD.
  CA districts fund security via SVPP, NSGP (nonpublics), local bonds. I did not find one —
  that's "not found," not "doesn't exist"; worth one deeper pass on CDE/Cal OES.
- WA OSPI capital pots (Urgent/Emergency Repair) = health/safety repairs, not security tech — low value
- Applications themselves (federal) are NOT public — "silver" must come from board minutes,
  state solicitation Q&As, and RFPs, not from grants.gov
