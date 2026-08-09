---
name: zoominfo-first-live-spend-20260809
description: First live ZoomInfo credit spend on prod (2026-08-09) verified read-only — ledger is correct but requested_by is never populated; DNC phone suppression proven in code; crm_campaign_attempts is NOT on the create-preview path
metadata:
  type: project
---

**First real ZoomInfo spend in production, 2026-08-09T21:45:29Z**, lead 4897 (Scottsbluff Public
School, NE, SVPP $404,420). Verified read-only (`mode=ro`) against the live hot WAL —
see [[readonly-db-forensics-recipe]].

**Ledger is internally consistent.** `zoominfo_credit_periods`: one row `2026-08`,
`credit_limit=1000`, `consumed=2`. `zoominfo_credit_spends`: exactly ONE row, `state='settled'`,
`reserved=2`, `billed=2`, `lead_id=4897`, no error, 0.3 s start→finish. No `indeterminate`,
no `reserved` leftover. `sum(billed) == consumed == 2`.

**GAP — credit spends are UNATTRIBUTABLE.** `zoominfo_credit_spends.requested_by` is `''`.
`apply_for_lead(..., requested_by: str = "")` plumbs the value all the way to the ledger, but the
ONLY production caller — `slack/tools.py:807`, `apply_for_lead(conn, int(lead_id), ids)` — never
passes it. So the column defaults empty on every spend and "who spent our credits" cannot be
answered from the ledger. `crm_actions` DOES capture `requested_by`; this table does not.
Code-level finding, not yet reported as a bug to fix.

**Do-not-call suppression is real, in persistence, not at the caller.** `db.save_vendor_contact`
writes `"" if do_not_call else phone` — a flagged number is never stored, because
`salesforce_contact_records` copies `contacts.phone` straight into a Lead's dialable Phone field.
CAUTION when verifying this from data alone: on this run BOTH vendor contacts had an empty phone
(the non-flagged one too, because ZoomInfo returned no number for him), so the stored rows do NOT
by themselves discriminate suppression from "vendor sent nothing". The discriminator is Grant's
summary line — `"N phone number(s) were withheld because the record is flagged do-not-call"` only
renders when `suppressed_numbers >= 1`, which requires `detail.do_not_call AND a non-empty number`.

**`contact_status` AND `contact_provenance` are BOTH `'vendor_licensed'`** for these rows (a single
INSERT sets both literals), so a `contact_status='vendor_licensed'` query works — do not assume the
value lives only in the provenance column. `verified` count stayed at 19 = exactly migration 29's
`page_verified` backfill, so no purchased record was laundered into `'verified'`.

**`crm_campaign_attempts` (migration 31) is NOT written by the campaign CREATE-preview path.**
Its only writer is `enrich/salesforce_campaign_batch._record_attempt/_close_attempt` — the campaign
MEMBERSHIP batch. A `salesforce_campaign_create_preview` produces a `crm_actions` row and NOTHING in
`crm_campaign_attempts`. Empty table after a create-preview test is CORRECT, not a wiring failure.
Also: a purely conversational refusal (Grant declining to delete records) reaches no tool and so
leaves no row in any table — the "refused request is visible" property covers refusals INSIDE the
batch path only.

**create_campaign TTL is 24 h** (`expires_at - created_at = 86400 s`) for every row since
2026-07-23; only the 2026-07-16 row used the old 900 s TTL. Don't read `expires_at == created_at`
as a bug — that was a redaction artifact (below).

**REDACTION LESSON.** A phone regex of `\d[\d\-\.\(\) ]{7,}\d` eats ISO-8601 timestamps and UUID
segments, silently destroying the evidence being gathered (`expires_at='<REDACTED>T21:41:06'` read
as "expires at creation"). When redacting PII out of operator output, redact SPECIFIC COLUMNS to
booleans and reserve regex scrubbing for free text, with a phone pattern anchored to 3-3-4 grouping
so it cannot match `YYYY-MM-DD`.
