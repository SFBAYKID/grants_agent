---
name: org-column-coverage-20260810
description: Measured 2026-08-10 — the leads.org_* columns feeding organization-only Salesforce Leads are almost entirely EMPTY in production (22/10,715 street, 37 website), so the mapping commit ships an inert improvement for ~99% of leads
metadata:
  type: project
---

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

The query is safe to repeat on a `mode=ro` connection against the hot WAL
([[readonly-db-forensics-recipe]]).

Related: [[deploy-14221fc-email-coaching-fix]] (the deploy this was measured during),
[[migration-version-collision]] (the `org_*` columns' own history is entangled with the
side-lineage numbering — migration 9's `org_*` columns were once masked and never applied).
