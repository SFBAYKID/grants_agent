---
name: migration-46-total-quarantine
description: "Quarantines legacy claims" meant ALL of them - 32 verified contacts and every org projection went to zero; measure a data migration on a COPY before the live run
metadata:
  type: project
---

**A DATA MIGRATION'S BLAST RADIUS IS A NUMBER YOU MEASURE, NOT A PHRASE YOU READ.**

**Why:** migration 46 was described as quarantining "legacy contact labels and
organization projections that lack exact typed evidence". That reads like *some*. On
production it was **all of them**, because migration 41 creates
`organization_field_evidence` **empty with no backfill**, and 46 then nulls every
projection lacking a `current` row in it. Nothing legacy could possibly qualify.

**How to apply:** before applying any data-mutating migration to the live DB, take the
backup, `shutil.copy2` it to a throwaway, run the migrations against the THROWAWAY, and
print a before/after census. The live run then either matches the prediction exactly (it
did here, every figure) or you have found something worth stopping for. This costs one
extra minute and converts "should be fine" into evidence.

## Measured on production, 2026-08-13 (40 -> 46)

| | before | after |
|---|---|---|
| `contacts.contact_status='verified'` | 32 | **0** |
| `contacts.contact_status='not_found'` | 36 | **0** |
| `contacts.contact_status='unverified'` | 0 | **68** |
| `leads.org_website` non-empty | 126 | **0** |
| `leads.org_phone` / `org_street` / `org_city` | 73 / 79 / 79 | **0 / 0 / 0** |
| `leads.org_state` / `org_postal_code` / `org_general_email` | 78 / 76 / 17 | **0 / 0 / 0** |
| `leads.org_profile_status` non-empty | 146 | **0** |
| `contact_evidence status='verified'` | 22 | **0** (-> `superseded`) |
| `paid_enrichment_attempts` completed / failed | 191 / 37 | **134 / 94** |

Row totals preserved (leads 10761, contacts 172), `integrity_check=ok`, FK orphans 2 -> 2.
**Nothing was deleted** - labels downgraded, projections nulled.

## The consequence that matters operationally

**ZERO leads now have a `contact_status='verified'` contact**, and that is the exact
predicate `grant._request_outreach` and `nudge_promises.best_offer` use. So the outreach
draft path now refuses for **every lead in the database** until contacts are re-researched.
That is the honest outcome (the typed evidence never existed) but it is a live capability
change, not a silent internal cleanup - Grant will answer "no contact could be verified"
where it used to offer a draft.

Mitigating facts, all measured:
- **32 contacts still hold an email address**; only the LABEL moved to `unverified`. The
  data is recoverable by research, not lost.
- **57 research markers were reopened** (`legacy_rich_contact_requires_research` 30,
  `legacy_contact_requires_research` 27), which is what lets the paid path try again -
  the one-shot marker would otherwise block it.
- **340 leads carry `nces_id`**, so the Monday 07:20 `nces-bind` cron can repopulate
  official website evidence. `org_website_candidate` is **0** too - 41 backfills nothing.

Rollback source if this is ever judged wrong: `~/grant_watch.db.pre46.20260813T180041Z`,
taken with the listener stopped. See [[prod-state-58b3e24-verified]].
