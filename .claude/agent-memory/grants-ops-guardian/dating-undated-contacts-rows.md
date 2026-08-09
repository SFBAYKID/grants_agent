---
name: dating-undated-contacts-rows
description: How to date rows in `contacts` (which has NO created_at) and how a "Grant fabricated contacts" alarm resolved to a real CompletedPaidCall crash — the bot.log/search_requests correlation recipe
metadata:
  type: reference
---

Established 2026-08-09 by a read-only forensic audit of an apparent fabrication incident
(leads 231/233/237/239/242). Verdict was **not** fabrication and **not** data loss. Method matters
more than the case, so it is recorded here.

## `contacts` has NO created_at — but it can still be dated

`contacts` columns (schema 32): id, lead_id, name, title, email, phone, source_url, confidence,
contact_status, official_domain, field_evidence_json, contact_provenance, do_not_call,
vendor_person_id, provenance, asserted_by_slack_user, asserted_at. **`asserted_at` is the ONLY
timestamp and it is non-NULL only on `human_asserted` rows** — useless for dating discovery rows.

Four independent dating levers, in increasing strength:

1. **id monotonicity.** `id INTEGER PRIMARY KEY` with **no `sqlite_sequence` table at all** (nothing in
   this DB uses AUTOINCREMENT), so ids are `max(rowid)+1`. Contiguous id blocks = one batch write.
   Check `gaps = set(range(min,max)) - set(ids)`; at the time of the audit contacts was **1..85 with
   ZERO gaps**, which is strong evidence **no contact row has ever been deleted**.
2. **Neighbouring ids that DO have a ledger row.** `paid_enrichment_attempts` (lead_id, started_at)
   brackets any contact id block sitting between two dated blocks.
3. **Migration dates as a floor/ceiling.** `schema_migrations(version, applied_at, name)` — e.g.
   migration 21 "preparation evidence and paid-call state" applied **2026-07-24T22:00:02Z** created
   `paid_enrichment_attempts`. A contact batch with no ledger row, whose identical re-run later DID
   write ledger rows, must predate that migration.
4. **THE DECISIVE ONE — bot.log `[tool-turn]` order ↔ `search_requests.created_at`.**
   `bot.log` carries `[tool-turn N] <tool>:<args>` with **no timestamps**, but every `search_leads`
   call persists a `search_requests` row **with `created_at` and `result_lead_ids_json`**. Match the
   lead-id ORDER in `result_lead_ids_json` against the order of the following `find_contact` calls in
   bot.log and against the contacts id order — three independent orderings that agree pin the write
   to the exact minute. This dated an undated batch to `2026-07-23T18:14:35Z` (11:14:35 PDT).

## The `CompletedPaidCall` signature (what a "Grant lost my contacts" report really looks like)

`grant_watch/slack/contact_enrichment.py` guards, in order: existing `verified` → return it;
any `not_found` row → return `not_found`; then `_recall_prior_outcome`; then `paid_calls.execute(...,
"legacy_contact_enrichment", f"legacy-contact:{lead_id}", ...)`.

Before the `_recall_prior_outcome` fix, a lead whose FIRST pass ended in a **fallback**
(`linkedin_only` / `linkedin_org_email` / `org_email`) matched **none** of the short-circuits, so
every later pass re-entered `paid_calls.execute` with the same `request_key` and raised
`CompletedPaidCall` — surfacing to the rep as **"errored out … try again later"** for a lead whose
data was sitting in the table the whole time. Retry could never succeed (the key is permanent).

Recognise it by: `grep -c CompletedPaidCall bot.log` (2 lines per traceback, so 24 = 12 crashes),
each traceback preceded by `[tool-turn N] find_contact:{"lead_id": NNN}` and `[tool-error]`.
**Leads with a `not_found` row take the OTHER branch** and report "no verified person" instead —
so in one message some leads say "errored" and others say "nothing found" purely by branch, not by
data. The fix is deployed (`_recall_prior_outcome`, commit 3adebba, live 2026-08-09).

## Standing gotchas this surfaced

- `search_leads {"grade":"gold","state":"CA"}` returns **14** while `SELECT COUNT(*) FROM leads WHERE
  state='CA' AND lead_grade='gold'` returns **49** — the tool applies freshness/status filtering the
  raw table does not. Never quote one as the other. Raw CA totals 2026-08-09: gold 49, silver 374,
  watch 1071 (1494 total); nationwide gold 286, silver 685, watch 9744.
- **Re-enrichment is non-deterministic across runs.** The same LinkedIn lookup on lead 233 returned
  "Patrick S. Duffy" in July and "Francisco Mata" in August; lead 237 returned "John Simeon" then
  "Lyle Tavernier". Both rows persist, so a lead accumulates conflicting `linkedin_only` people with
  no ranking between them. `db.contacts_for_lead` order decides which one a human sees.
- There is **no delete/supersession audit anywhere** in the schema and **no triggers**. The only
  evidence a row ever existed is the row itself plus id continuity. Say so plainly rather than
  implying a deletion could be ruled in or out from an audit trail.

See [[readonly-db-forensics-recipe]] for the zero-write `mode=ro` connection this all runs over.
