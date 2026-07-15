# Nationwide RFP and Grant Source Strategy

Updated 2026-07-15. This is a source-discovery plan, not a claim that every state
portal has already been integrated. Every source is labeled under the repository's
evidence rules.

## Coverage foundation

- **verified:** Grants.gov `search2` and `fetchOpportunity` are public, keyless search
  endpoints for federal funding opportunities. Grant already polls Grants.gov.
  Official documentation: https://www.grants.gov/api/api-guide
- **verified:** USAspending publishes award data through a public API and downloadable
  archives. Grant already polls prime awards and subawards nationwide by configured
  state/program. Official documentation: https://api.usaspending.gov/docs/
- **verified:** SAM.gov permits public opportunity search without an account and
  publishes downloadable contract-opportunity data; its API authentication boundary
  remains separate. Official page: https://sam.gov/opportunities
- **verified:** NASPO maintains procurement profiles for every state. It is a discovery
  directory, not a solicitation feed: https://www.naspo.org/states/
- **needs-testing:** Two third-party directories enumerate the official procurement
  portal for all 50 states plus DC. They can seed discovery, but each destination must
  be independently verified as official before configuration:
  https://govbid.ca/resources/procurement-portal-directory and
  https://www.govbidportals.com/states

## State-by-state acquisition workflow

For each state and DC, create one reviewed source record with:

1. Official procurement-office page and official solicitation portal.
2. Public access mode: JSON API, CSV, RSS/Atom, server-rendered HTML, PDF index, or
   login-only.
3. Jurisdiction coverage: state agencies only, or cities/schools may also publish.
4. Stable identifiers, posted/due/award date meanings, detail URL, pagination, and
   modification/cancellation behavior.
5. Robots/terms result, minimum polling interval, timeout, and response-size ceiling.
6. A captured real security solicitation fixture plus empty, malformed, pagination,
   duplicate, modification, cancellation, and HTTP-failure tests.
7. `verified` only after a live dry run reads real source data. A parser with no real
   positive row remains `needs-testing`.

Portal families should share transport helpers but keep one state source module or
configuration per jurisdiction. Likely families include Bonfire, OpenGov, PlanetBids,
Public Purchase, DemandStar, BidNet, and custom state portals. Public search pages do
not imply that automated access or document downloads are allowed.

## Safe dynamic fallback

Grant must not generate or execute arbitrary server-side code. When an indexed source
cannot answer a question, it may use a bounded web-research adapter that:

- accepts only an HTTPS URL on an independently verified official public hostname;
- rejects localhost, private/link-local/reserved IPs, credentials, and non-HTTP schemes;
- checks redirects, robots/terms, timeouts, content type and size, and per-host rate
  limits;
- uses Firecrawl only as a fetch/render transport, never as evidence by itself;
- records the exact URL, retrieval time, content hash, and evidence excerpt;
- extracts only typed fields and keeps unknown fields unknown;
- never persists a GOLD/SILVER lead until source evidence passes the same verification
  and event-date rules as a fixed poller.

If a new site repeatedly produces useful verified data, it graduates from fallback
research into a reviewed, fixture-backed configured poller. Login, CAPTCHA, paywall,
or terms boundaries are reported honestly and never bypassed.

## Rollout order

1. Expand nationwide USAspending/Grants.gov configurations and measure state coverage.
2. Verify all 51 official state procurement destinations from NASPO plus state sites.
3. Prioritize states with existing sales territory and public keyless feeds.
4. Add city, county, and school-district portal discovery using official entity sites,
   NCES district domains, and portal-family detection.
5. Add award notices, school-board agendas/minutes, and grant-recipient PDFs only after
   the higher-structure sources are stable.

The inventory must track states as `discovered`, `official-url-verified`,
`access-characterized`, `parser-tested`, and `live-verified`; “all states discovered”
must never be reported as “all states integrated.”
