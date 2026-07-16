# Nationwide source catalog

This directory contains generated views of the canonical catalog at
`data/source_catalog/sources.csv`. Do not hand-edit the CSV reports here. Regenerate them with:

```bash
python -m grant_watch.source_catalog
```

Use `python -m grant_watch.source_catalog --check` in health gates. Secret-free selected-result
evidence for new Firecrawl research lives in `data/source_catalog/discovery_checks.csv` and validates
with `python -m grant_watch.source_discovery`. Per-entity county research lives in state shards under
`data/source_catalog/coverage_tasks/counties/` and validates with
`python -m grant_watch.coverage_universe`. School-district and incorporated-place queues live beside
it and validate with `python -m grant_watch.school_district_universe` and
`python -m grant_watch.incorporated_place_universe`.
Raw Firecrawl searches live under `data/source_catalog/firecrawl_batches/` and validate with
`python -m grant_watch.source_discovery_batch --validate`. These immutable batches do not promote
catalog rows, entity links, selected discovery checks, or runtime pollers; promotion is a separate
human-reviewed step.

## Evidence rules

- `verified`: the exact claim axis was checked. A verified official owner does not imply verified
  anonymous access or a working parser.
- `assumed`: the claim is supported indirectly but was not demonstrated.
- `needs-testing`: the candidate or behavior has not been executed.
- `integration_status=discovered`: a directory or live Firecrawl search found the endpoint only.
- `live_zero_verified`: live access and parsing ran but returned no matching security record.
- `live_positive_verified`: live access returned real source data. This still does not mean every
  geography or failure path is covered.

## Generated lists

- `verified_public_no_auth.csv`: no-auth sources whose access behavior was directly checked.
- `candidate_public_no_auth.csv`: sources classified as no-auth candidates whose access evidence is
  still `assumed` or `needs-testing`.
- `api_key_sources.csv`: API-keyed sources only. Only an environment variable name may be recorded;
  secret values are forbidden.
- `account_or_paid_sources.csv`: free-account, supplier-account, or paid sources without API keys.
- `credentialed_sources.csv`: combined convenience view of the preceding two lists.
- `unknown_or_manual_access.csv`: candidates whose access boundary is not yet characterized or is
  manual-only. This prevents a search result from being mislabeled as an open feed.
- `state_coverage.csv`: separate state, county, city, district, RFP, grant, and contract-award counts
  for all 50 states plus DC. Research-status columns distinguish `not_researched`,
  `researched_not_found`, and structurally `not_applicable` layers.

## Scope status as of 2026-07-15

- `verified`: Firecrawl returned at least one live discovery result for every state and DC during
  the nationwide search pass.
- `verified`: the canonical catalog validates 270 records: 19 federal, 105 state, 56 county,
  59 school-district, 15 city, 3 education-service-agency, 1 regional-government, 1 special-district,
  1 multi-jurisdiction, and 10 national portal-family sources.
- `verified`: all 50 states plus DC have at least one state-level, grant, and exact school-district
  candidate. This does not imply exhaustive district coverage within any state.
- `verified`: exact county candidates exist where a county layer was found. Connecticut, DC, and
  Rhode Island are marked `not_applicable`; Vermont is `researched_not_found`, with evidence in
  `data/source_catalog/coverage_exceptions.csv`.
- `verified`: generated access views classify 34 sources as verified public without authentication,
  11 as no-auth candidates, 2 as public APIs requiring keys, 15 as free-account sources, 4 as
  supplier-account sources, and 204 as unknown-access candidates.
- `needs-testing`: most candidates still require direct access characterization, robots/terms review,
  pagination/schema checks, a recorded fixture, and a positive physical-security result.
- `verified`: the official 2025 Census county Gazetteer is pinned by URL and SHA-256. Its 3,144
  county-equivalents are sharded by state with 56 linked candidates, 15 structural exceptions, and
  3,073 explicitly `not_researched` entities. Census documents the public release at
  `https://www.census.gov/geographies/reference-files/2025/geo/gazetter-file.html`.
- `verified`: four official 2025 Census school-district layers are independently pinned by URL,
  SHA-256, and row count. Their 13,363 entities contain 66 linked candidates, 19 structural
  placeholders, and 13,278 `not_researched` tasks.
- `verified`: the official 2025 incorporated-place Gazetteer is pinned by URL, SHA-256, row count,
  and exact functional-status totals. Its 32,058 places contain 14 linked candidates, 12,587
  structural rows, and 19,457 `not_researched` tasks. This is not a unique-government count; an
  explicit Brewster, Massachusetts record documents the missing county-subdivision/MCD layer.
- `needs-testing`: county, city, and school-district discovery is not exhaustive. No claim of
  every US county, city, school district, grant program, or portal endpoint is made.
- `verified`: thirty Firecrawl checks persist selected result evidence and scraped-content hashes.
- `verified`: raw batch `20260716T004633Z` contains 27 successful searches, 27 attempts, and 126
  results across three entity namespaces and three states; its validator recomputes schema-v1 task,
  request, and response evidence hashes.
- `needs-testing`: that immutable first batch predates schema v2, so its task IDs do not independently
  hash-bind state, entity name/kind, or universe vintage. New schema-v2 batches bind the complete
  Census target snapshot as well as the request contract; the live schema-v1 evidence was not
  rewritten retroactively and schema v1 is rejected by every creation, mutation, and execution path.
- `needs-testing`: the earlier nationwide Firecrawl queries and raw result bodies were not persisted.
  Those older candidate URLs cannot be independently replayed from repository evidence alone.
- `needs-testing`: a catalog candidate is not a working poller. Only integration rows explicitly
  marked `live_positive_verified` or `live_zero_verified` have been exercised against live data.
