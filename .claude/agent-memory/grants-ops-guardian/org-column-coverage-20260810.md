---
name: org-column-coverage-20260810
description: The leads.org_* coverage numbers — near-empty at 14221fc (22/10,715 street), and the first bounded sweep at d664548 which took gold street 16→32; includes the per-run yield to budget the next batch
metadata:
  type: project
---

**UPDATED 2026-08-09 after the first `enrich-orgs` sweep — see "AFTER" below.** The
original measurement is kept because it is what justified building the sweep.

Read-only measurement on the production `grant_watch.db` at revision 14221fc, schema 35,
taken to decide whether commit `00bd7cb` ("organization-only Leads carry the organization's
own facts") ships a real improvement or an empty one.

**All 10,715 leads:**

| column | non-empty | share |
|---|---|---|
| `org_street` | 22 | 0.21% |
| `org_city` | 22 | 0.21% |
| `org_postal_code` | 20 | 0.19% |
| `org_website` | 37 | 0.35% |
| `org_phone` | 19 | 0.18% |
| `enrollment` (>0) | 158 | 1.47% |

**Gold only (286 leads):** street 16 (5.6%), city 16 (5.6%), zip 14 (4.9%), website 24
(8.4%), phone 13 (4.5%), enrollment 41 (14.3%).

Grade split: watch 9,744 / silver 685 / gold 286.

**Why:** the `org_*` columns are written by ONE enrichment path, and that path has run
against a few dozen leads, not the corpus. The mapping commit is therefore correct but
almost entirely INERT today — a Salesforce Lead created from a randomly chosen lead row
will still carry no Street/City/PostalCode/Website/Number_of_Students__c. Gold is ~25x
better than the average and is where leads actually get used, but 16 of 286 is still not
"organization-only Leads now carry their facts".

**How to apply:** do not describe that change as closing the ask. It closes the CODE half.
The open half is coverage — backfilling `org_*` for the leads reps actually touch (gold
first) is a separate body of work, and until it happens the honest claim is "the fields are
mapped and will populate as enrichment reaches each lead". Re-run the same query before
claiming otherwise; the numbers move only when that enrichment path runs.

## AFTER — the first sweep, 2026-08-09 (`enrich-orgs --grade gold --limit 25 --execute`)

Outcome line verbatim: `enrich-orgs (gold): considered 25, filled 21, unreachable 4,
errored 0`.

| gold (286 rows) | before | after | delta |
|---|---|---|---|
| `org_street` | 16 (5.6%) | **32 (11.2%)** | +16 |
| `org_website` | 24 (8.4%) | **44 (15.4%)** | +20 |
| `org_phone` | 13 (4.5%) | **29 (10.1%)** | +16 |

Corpus-wide: street 22 → 38, website 37 → 57, phone 19 → 35 (of 10,715). `org_profile_status`
for gold went `found` 16 → 32, `not_found` 10 → 17, NULL 259 → 236.

**Budgeting the next batch:** ~0.85 street-or-better per lead scraped, so the remaining
**254** gold candidates are roughly 10 more runs of 25. "filled" counts street OR website OR
phone, so it slightly overstates address coverage — 21 filled produced only 16 streets.

**Two things to fix before spending more.** (1) **Gold contains 30 duplicated entity
names**, and the sweep pays per LEAD ROW, so this run scraped MODESTO CITY SCHOOLS twice,
MT. MORRIS three times and CASTLE ROCK twice — wasted credits and a dedup opportunity.
(2) `candidates()` claims to put "the biggest opportunities first" via
`ORDER BY COALESCE(amount,0) DESC`, but the run walked ids 1,2,3,4,5… — i.e. `amount` is
NULL/0 across gold, so the ordering silently degrades to **id order**. Neither is a
correctness bug; both mean the money is not going where the docstring says it is.

The query is safe to repeat on a `mode=ro` connection against the hot WAL.

The query is safe to repeat on a `mode=ro` connection against the hot WAL
([[readonly-db-forensics-recipe]]).

Related: [[deploy-14221fc-email-coaching-fix]] (the deploy this was measured during),
[[migration-version-collision]] (the `org_*` columns' own history is entangled with the
side-lineage numbering — migration 9's `org_*` columns were once masked and never applied).
