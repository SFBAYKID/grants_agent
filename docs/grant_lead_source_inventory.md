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

---

## TIER 2 — CONFIRMED TO EXIST, ACCESS NOT YET WIRED

### 6. Michigan MSP Competitive School Safety Grant Program (CSSGP)
- michigan.gov/msp → Grants & Community Services → school safety; award lists published per FY (PDFs)
- Lead type: GOLD. Eligible costs are literally Verkada's catalog: access control systems,
  intercom w/ access, barrier systems, duress/panic alarms, doorway hardening
- Eligible: public AND nonpublic schools, districts, ISDs. ~$10M FY22; proposed much larger since
- TODO: verify latest-cycle award list URL + parse

### 7. FEMA NSGP state-published subrecipient lists
- State Administrative Agencies (WA Military Dept, Cal OES, MI State Police, PEMA) publish
  awardee lists, often earlier than USASpending subaward data
- Lead type: GOLD. TODO: locate each state's posting page

### 8. NEW: School Safety Enhancement (SSE) program — 84.184A
- Brand-new FY26 federal program, $93M, apps close 7/28/2026; grants go to ~30 state education
  agencies ($500K–$5M) which then SUBGRANT to districts; physical security focus (locks, secure
  entry, perimeter, visitor screening) per the Uvalde report
- Lead type: future GOLD wave — state awards ~fall 2026, then state subgrant rounds = fresh
  district lead lists in early 2027. Watch ed.gov 84.184A page + each state's subgrant process

### 9. SAM.gov Opportunities API
- Lead type: SILVER (federal-side RFPs; some school/city solicitations w/ federal nexus)
- Verified: keyless request rejected (key required). BLOCKED on Chase signing in to grab key
- Unverified: rate limits, keyword-vs-title-only search

### 10. COPS/DOJ press releases + annual SVPP awardee PDFs
- cops.usdoj.gov publishes state-by-state award lists at announcement time (~Sept/Oct) —
  faster than USASpending. Lead type: GOLD, seasonal. TODO: wire an autumn watcher

---

## TIER 3 — KNOWN CHANNELS, UNVERIFIED (next pass)

### 11. School board meeting agendas/minutes (BoardDocs, Simbli, etc.)
- THE silver-lead source: boards must approve grant applications and vendor contracts.
  "Resolution to apply for SVPP/CSSGP" in minutes = applying now
- Fragmented per district; needs a crawler + Claude extraction. Not verified this session.

### 12. Regional bid platforms (silver leads)
- CA: PlanetBids (widely used by districts/cities) — unverified
- MI: BidNet Direct / MITN network — unverified
- PA: PennBid — unverified
- National: OpenGov Procurement, Public Purchase, DemandStar, Bonfire — unverified
- These are account-based portals; per-portal access rules TBD

### 13. Bond measures (Ballotpedia school bond elections, CA especially)
- Passed facilities bond = large multi-year budget that typically includes security scope
- Lead type: GOLD-adjacent, big dollars, slow cycle. Unverified.

### 14. State single-audit / grant transparency portals
- e.g., PA has Egrants records; states publish grant transparency data. Unverified.

## Dead ends / honest notes
- ESSER: expired (obligation deadlines passed) — ignore
- CA: no dedicated state school-hardening grant found equivalent to MI CSSGP or PA PCCD.
  CA districts fund security via SVPP, NSGP (nonpublics), local bonds. I did not find one —
  that's "not found," not "doesn't exist"; worth one deeper pass on CDE/Cal OES.
- WA OSPI capital pots (Urgent/Emergency Repair) = health/safety repairs, not security tech — low value
- Applications themselves (federal) are NOT public — "silver" must come from board minutes,
  state solicitation Q&As, and RFPs, not from grants.gov
