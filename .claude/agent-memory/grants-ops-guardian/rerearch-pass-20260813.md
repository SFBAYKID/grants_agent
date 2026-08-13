---
name: rerearch-pass-20260813
description: The post-migration-46 re-research pass - nces-bind restored NOTHING quarantined but made the paid path authoritative and cheaper; 303 Firecrawl + 6 ZoomInfo credits; two real defects found
metadata:
  type: project
---

**RE-RESEARCH PASS, 2026-08-13**, authorised by Chase verbatim *"accept the quarantine and
re-research the contacts"*. Repairs [[migration-46-total-quarantine]] on
[[prod-state-58b3e24-verified]]. Listener **never restarted** (PID 121468 throughout,
zero outage); crontab sha unchanged; tracebacks 13 -> 13.

**Why:** records what a free stage can and cannot recover, so the next pass does not
re-derive it. **How to apply:** run `nces-bind` first for LEVERAGE, not for restoration.

## `nces-bind` restored ZERO of the quarantined fields - and was still worth running

Free/keyless. `nces_id` 340 -> **496**, `nces_website_status verified` 0 -> **389**.
Contacts, `contact_evidence` and every `org_*` projection were **byte-for-byte unchanged**
- it writes a different column family entirely.

Its real payoff is in `organization_profile._resolve_site`, which checks
`nces_website_status=='verified'` **first** and returns an `authoritative=True`
SiteCandidate with a recorded evidence match and **zero Firecrawl calls**, before it will
fall through to a paid `_search`. So Stage A buys (a) a skipped paid search and (b) the
best available evidence class. `contact_evidence._default_finder` uses the same field as
`trusted_website` -> `official_domain`.

**`--limit-states N` picks the STATES by gold-pending count, then enriches every
district/school-shaped lead in those states** - so 37 states meant 1055 rows considered,
not the 179 the gold-pending query suggested. 408 bound, ~35 min.

**CA FAILED**: `ValueError: NCES pagination repeated a page without advancing`. A
fail-closed guard, but California - the biggest state - got nothing. Re-run CA alone.

## What the paid stage bought (droplet-observed, a FLOOR - see [[paid-provider-authority-cutover]])

| | Firecrawl | ZoomInfo |
|---|---|---|
| this pass | **303 calls** (287 ok / 14 http_408 / 2 indeterminate; 208 scrape + 95 search) | **6 credits**, 3 leads, all settled |
| ledger after | 303 of 3000 for 2026-08 | 20 of 1000 consumed, 980 left |

No 429 and **no provider backoff row was ever written** - the new gateway's rate state
stayed empty at 20 req/min.

- `enrich-orgs --grade gold --limit 50 --execute` -> considered 50, **filled 25**,
  unreachable 21, errored 4. `organization_field_evidence` 0 -> **87 current rows over 25
  leads**; `org_profile_status` 0 -> 50, `org_website` 0 -> 12 (**all 12 evidence-backed**,
  checked by joining value to a `current` evidence row).
- `rich-prepare --limit 25 --execute` -> 14 contact-refreshed, 25 crm-checked, 42 local
  writes, **`contact_evidence verified` 0 -> 1** (and see
  [[contacts-verified-not-writable-by-any-cli]] for why that is the ceiling).

## Two defects that waste real money - fix before the next pass

1. **`enrich-orgs` re-scrapes what just failed.** `candidates()` excludes only
   `org_profile_status='found'`, so the 21 `not_found` + 2 `unreachable` from a run are
   candidates again immediately. Measured: a following 75-slot batch would spend **~108 of
   ~352 calls** re-fetching pages that returned nothing minutes earlier. This is why the
   pass was stopped at 303 rather than running the budget out.
2. **The dedup the module's own docstring claims does not hold.**
   `GROUP BY COALESCE(NULLIF(canonical_entity_key,''), entity_name)` puts a NULL-key row
   (`'MODESTO CITY SCHOOLS'`) and its populated twin (`'modesto city schools|CA'`) in
   **different groups**, so the same organization is scraped twice in ONE run. Observed
   live for Modesto, Castle Rock 401, Mt. Morris and Bellaire - Modesto and Mt. Morris
   being the exact examples the docstring names as the bug it fixed. **30 gold
   organizations** are split this way.

## `fill-contacts` CRASHES the whole batch on one bad vendor ID

`ValueError: invalid ZoomInfo person ID: '-883527167'` - ZoomInfo returned a NEGATIVE id,
`zoominfo.normalize_person_ids` rejects it, and the exception escapes `fill_contacts`,
killing the run on lead 4 of 50. `org_backfill` catches per-lead and continues;
this does not. 3 leads (6 credits) landed first: Cuba City, Menominee Indian, Mount Horeb
- good titles (a CTO, a Director of Technology & Business). **Do not retry blind**: the
same lead reproduces it. Needs per-lead exception handling, which is a code change.

## Discipline that held

`--retry-indeterminate` was **never passed** (17 pre-existing indeterminate
`paid_enrichment_attempts`, plus 2 Firecrawl ReadTimeouts this pass);
`docs/paid_provider_cutover.md` §6 forbids silently retrying a possible charge. ZoomInfo
**unsettled spends = 0**, so no reconciliation was required. Backup
`~/grant_watch.db.pre_rerearch.20260813T185725Z` taken via the SQLite backup API;
`~/grant_watch.db.pre46.20260813T180041Z` left untouched.
