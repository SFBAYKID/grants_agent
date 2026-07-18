# Closing the RFP gap (and why it unlocks cities)

Investigated live 2026-07-18. Two of Chase's concerns — "I've never seen an RFP"
and "89 cities is willfully low" — turn out to be the SAME gap.

## The data exists; we just don't ingest it

A live web search found real, open school/city security RFPs in minutes:
- **Livonia Public Schools (MI)** — Access Control/Intercom/Security RFP, due 2026-01-26
  (contact Angela Sutton, asutton7@livoniapublicschools.org).
- **Chadwick R-1 School District (MO)** — cloud access-control RFP, due 2026-06-24.
- **Long Beach USD (CA)** — security RFP 2526-010, due 2026-02-23.
- **City of Kemah, TX** — RFP 2026-05, video-surveillance cameras (police dept).

Aggregators that carry these nationally: HigherGov, RFP School Watch
(rfpschoolwatch.com/bid-opportunities/access-control), Bid Banana / TheBidLab,
plus e-procurement platforms schools/cities post on (BidNet, DemandStar,
PlanetBids, Bonfire, Ionwave, Public Purchase).

## What works TODAY (verified live)

Grant's **web-search tool already surfaces these reactively, with links, honestly
labeled**. Asked in Slack to find school-district security RFPs, Grant returned
HigherGov, RFP School Watch, Bid Banana, and the City of Kemah listing — and
flagged them as "raw web findings, not verified Grant leads." So a rep can ask
and get real RFP links now. What's missing is the *proactive* side and *verified*
RFP leads in the database.

## Why "89 cities" is low — and how RFPs fix it

Our lead database only contains entities that **received federal security awards**
(SVPP / NSGP). Those programs overwhelmingly fund schools and nonprofits, not
cities — so only ~120 city leads exist, and that's honest, not a bug. Cities'
security activity shows up almost entirely as **procurement RFPs**, not federal
grants. The 32,058 Census incorporated places we catalogued are the *target*
universe (potential customers), not funded leads.

**Therefore: closing the RFP data gap is exactly what unlocks broad city
coverage.** City camera/access-control RFPs are the city equivalent of a school's
SVPP award.

## Proposal — an RFP discovery poller (SILVER leads + a proactive alert)

We already run Firecrawl for contact enrichment; reuse it for RFP discovery.

1. **Source:** a Firecrawl-based RFP discovery poller (and/or a HigherGov feed)
   that periodically searches for school/city security-camera & access-control
   RFPs, the same verbatim-verification discipline finder.py uses.
2. **Extract & verify:** entity, state, program, **posted date, response-due
   date**, and the **direct RFP link** — only when they appear verbatim on a page
   we actually fetched. No date/contact is ever guessed.
3. **Store as SILVER** (`rfp_posted` event) so they flow through the existing
   search, export, contact, and Salesforce paths already tested.
4. **Proactive RFP alert** (new drip type, matches Chase's example):
   > *Alief ISD (TX) just opened an RFP for security cameras — responses due
   > Aug 20. Want the details?*  Source: <link>
   Same honesty invariants: source link on every claim, human-approved outreach.

## Recommended sequence
1. Prove one source end-to-end: point the discovery poller at a national RFP
   aggregator, verify it returns real school/city security RFPs with dates+links.
2. Land them as SILVER leads (reactive search/export/contacts work immediately).
3. Build the proactive RFP alert on top (roadmap item C/D).

This is real feature work, not a config change — flagged for Chase's go-ahead
before building. The reactive web-search path already covers the urgent need.
