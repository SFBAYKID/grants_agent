---
name: lead-fill-provenance-and-cron-firing
description: Where the 58 Salesforce field writes actually came from (39 org / 14 ZoomInfo / 5 page-verified), the Birmingham premise that was false, the do-not-call flag that never reaches Salesforce, and which 5 of the 10 cron jobs have still NEVER fired
metadata:
  type: project
---

Measured read-only on production 2026-08-10 (~00:54 PT Monday), revision `c36a3e5`.

## Only THREE Salesforce fields can ever come from a person

`salesforce_lead_fill.proposed_fields` sources the payload in two halves:

- **From the ORGANIZATION row** (`enrich-orgs` / poller / NCES, no person involved):
  `State, City, Street, PostalCode, Website, Industry, Number_of_Students__c` **and
  `Phone`** — `Phone` is `lead.org_phone`, the switchboard, *not* anyone's direct line.
- **From the selected CONTACT**: `Title`, `Email`, `MobilePhone`. That is the whole list.

So "was this lead enriched by ZoomInfo?" can only ever be a question about those three
fields. **Of the 58 fields written: 39 organization, 14 ZoomInfo (`vendor_licensed`),
5 our own page verification (`page_verified`).**

Contact selection is `verified` first, then `vendor_licensed`, taking `[0]` of each —
so a page-verified contact always beats a ZoomInfo one for the same lead.
Per lead: Galt, Golden Eagle and Modesto were filled from `page_verified` contacts;
Birmingham, Mammoth, Savanna, Pomona, Imperial and Edison from ZoomInfo.

## THE BIRMINGHAM PREMISE WAS FALSE — there is no page_verified row

The brief said Vic Chalabian was found twice independently, page-verified on 2026-07-16
and via ZoomInfo on 2026-08-10, and asked which row won. **Neither — because no
`page_verified` row for lead #231 exists.** Its six contact rows are three `not_found`,
one `linkedin_only` (`linkedin_claimed`), and two `vendor_licensed`. The 2026-07-16
core-verifier finding never persisted as a contact row. Birmingham's `Title` and `Email`
came from **ZoomInfo, id 86**, unambiguously.

Worth keeping as a general caution: a finding reported in a Slack thread is not evidence
that a row exists. Check `contacts`, not the transcript.

## THE DO-NOT-CALL FLAG NEVER REACHES SALESFORCE

`do_not_call` is enforced ONLY at storage time in `db_contacts.py`
(`"" if do_not_call else phone`, same for `mobile_phone`) — the number is blanked before
it is written locally. **Nothing anywhere maps it to a Salesforce field**; a whole-repo
grep for `DoNotCall` returns only the local column, the ZoomInfo response fields, and a
migration. Verified.

Consequence, live: Birmingham's Lead names the contact from row id 86, who **is**
do-not-call. No number was written (correctly), but the Salesforce record carries no
marker, so a rep who finds that person's number by any other route has nothing warning
them. (Reading `DoNotCall` back over SOQL 400s for this integration user, so the field
cannot be confirmed either way from here — but the code proves nothing sets it.)

## FIVE OF THE TEN CRON JOBS HAVE NEVER FIRED

"Installed" is not "working". Evidence from `cron.log` + the tables each job writes:

| job | schedule | fired? |
|---|---|---|
| keepalive `run_bot.sh` | `*/5 * * * *` | **YES** — 7,149 healthy + 61 restart lines |
| drip | `*/30 4-17 * * 1-5` | **YES** — 506 skips + 18 posts, last Fri 08-07 11:00 PT |
| poll | `0 7 * * 1-5` | **YES** — newest `source_observations` Fri 08-07 07:07 PT |
| rich-prepare | `45 7 * * 1-5` | **YES** — its own `rich prepare:` line in cron.log |
| watchdog | `3-59/10 * * * *` | **YES** — 9 runs |
| **nces-bind** | `20 7 * * 1` | **NEVER** — first chance today 07:20 PT |
| **remind** | `*/30 8-16 * * 1-5` | **NEVER** — installed Sat night; first chance today 08:00 PT |
| **nudge** | `*/15 8-14 * * 1-5` | **NEVER** — `followup_nudges` still 0 |
| **announce** | `0 8 * * 1-5` | **NEVER** — `announcements.posted_at` still NULL |
| **scan-threads** | `40 4 * * 1` | **NEVER** — its 15 asks came from my manual run, not cron |

All five get their first real firing **today, Monday 2026-08-10**. That makes today the
day the proactive stack either works or does not.

## cron.log grep traps — three false zeros in one pass

My first probe reported 0 runs for six jobs. All six were bad patterns:
- **`rich prepare:`** is printed with a SPACE, not the hyphen the cron line uses.
- **`nces`** matches "Institute of Education **Scien**ces" in poll output — 51 hits, none of them the job.
- **`announce`** matches "Broad Agency **Announce**ment" — 18 hits, none of them the job.

Derive the real signature from `sed 's/[0-9]*/N/g' | awk '{print $1,$2}' | sort | uniq -c`
before believing any zero, and corroborate against the TABLE the job writes.

## Branch position

Deployed `c36a3e5`; branch `review/rich-award-card-campaign-20260723`;
local HEAD == origin HEAD == `bbd42fb`. `git rev-list --count main..HEAD` = **136**
commits ahead of `main` (which sits at `b4a3230`), 0 behind. **`c36a3e5` is NOT an
ancestor of `main`** — production is deployed from the review branch, and nothing in this
work has reached `main`. `c36a3e5..bbd42fb` has an **empty deployable delta**
(`.claude/agent-memory` only), so production is current.

Related: [[salesforce-lead-fill-executed]], [[contact-fill-first-bulk-buy]],
[[salesforce-prod-lead-emptiness]], [[feeder-cron-scheduling-evidence]].
