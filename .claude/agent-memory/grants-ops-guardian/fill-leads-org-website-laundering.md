---
name: fill-leads-org-website-laundering
description: 2026-08-09 STOPPED fill-leads --execute — organization_fields() wrote org_website even when org_profile_status='not_found', so 3 of 5 leads would have got a vendor CDN or cde.ca.gov as their Website in PRODUCTION Salesforce. FIXED in 0716a17 and proven at the destination
metadata:
  type: project
---

**RESOLVED 2026-08-09 by `0716a17`** (option 2 below), deployed and proven by reading the
records back out of Salesforce: leads #233/#234/#235 now hold **no Website at all**, while
#231/#232 kept their correct ones. See [[deploy-0716a17-org-profile-gate]] — including the
distinction that cleared their *contacts*: a bare host is a failed search's fallback, a deep
link carrying the record's own key is a citation. The record below is the original finding.

**The contact provenance is guarded. The ORGANIZATION provenance is not.**

`salesforce_lead_fill.proposed_fields` carefully allowlists contacts to
`{verified, vendor_licensed}` so a `linkedin_only` title cannot become a CRM `Title`
([[deploy-65f05c7-fill-leads-fix]]). But it then calls
`enrich.salesforce_contact_records.organization_fields(lead)`, which reads `org_website`,
`org_street`, `org_city`, `org_postal_code` **with no regard for `leads.org_profile_status`**.

`org_profile_status='not_found'` means the org-profile lookup FAILED — and yet `org_website`
is still populated, with whatever URL the search last landed on. Measured on prod
2026-08-09 for the first 5 linked leads:

| lead | entity | `org_profile_status` | proposed `Website` |
|---|---|---|---|
| 231 | Birmingham Community Charter High School | `found` | `https://bcchs.net` OK |
| 232 | Montebello Unified School District | `found` | `https://montebello.k12.ca.us` OK |
| 233 | San Ysidro Schools Public Financing Corp | **`not_found`** | `https://resources.finalsite.net` **CMS vendor CDN** |
| 234 | Fairfax Elementary School District | **`not_found`** | `https://cde.ca.gov` **CA Dept of Education** |
| 235 | Valle Lindo School District | **`not_found`** | `https://cde.ca.gov` **CA Dept of Education** |

`org_profile_source_url` == `org_website` in every row, so the junk URL is just the page the
failed lookup landed on. `cde.ca.gov` is the dangerous one: it is authoritative-looking, so a
rep would never doubt it, and it is the *state agency*, not the district.

## WHY THIS IS WORSE THAN AN ORDINARY BAD WRITE: fill-blanks errors are SELF-SEALING

`fill_lead_blanks` only ever writes a field Salesforce currently holds nothing in. So once it
writes `Website='https://cde.ca.gov'`, **that field is closed to the tool forever** — a later
run, after the bug is fixed and the real district site is known, finds it non-empty and skips
it. The tool that made the error is structurally incapable of correcting it; only a human can.
"It cannot overwrite" cuts both ways, and this is the direction nobody counts.

## The write mode really is safe — that was never the issue

Verified on the live records before stopping: on all 5 Leads every allowlisted field was EMPTY
**except `State`, already `'CA'`**, which the blanks filter correctly skips. So there was zero
overwrite risk and the "reads before it writes" guarantee held exactly as documented. The
defect is entirely in *what value* was queued. This is [[verify-the-premise-not-the-claim]]
paying off a second time on the same command.

## Prod write target is the REAL org — confirm this every time

`verify_write_scope()` returned `organization_id='00D41000002jIQ8EAM'`, `name='Monarch'`,
**`is_sandbox=False`**, `instance_name='USA598'`. `fill-leads --execute` writes to PRODUCTION
Salesforce. It is read-only and safe to call just to see this.

## Scoped alternatives handed to Chase (none executed)

1. `fill-leads --limit 2 --execute` — leads #231/#232 only, both `org_profile_status='found'`,
   every proposed value checked. The clean subset, available with no code change.
2. Fix `organization_fields` to skip `org_website` (and the address fields) unless
   `org_profile_status == 'found'`. Needs a deploy.
3. Repair the three `org_website` values in the local DB first, then run the full 5.

## Verification recipe worth reusing

Walk `linked_leads` → `proposed_fields` → print each contact's `contact_status` AND the lead's
`org_profile_status`/`entity_name`, then GET each Lead's allowlisted fields through the
gateway's own `_auth()` before writing. The CLI preview prints field NAMES only — never the
values, and never the evidence. Both bad Websites were invisible in the preview line.

Related: [[deploy-65f05c7-fill-leads-fix]], [[verify-the-premise-not-the-claim]],
[[org-column-coverage-20260810]], [[relayed-consent-is-not-consent]].
