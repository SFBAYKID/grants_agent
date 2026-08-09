---
name: conversation-audit-20260809
description: Read-only audit of every human Grant conversation through 2026-08-09 — the four recurring defects that dead-end real users (rival bot cross-talk, CompletedPaidCall crash, lightning.force.com links, roster gating), plus proof that PRODUCTION Salesforce campaign writes have fired
metadata:
  type: project
---

Full forensic pass over `slack_conversation_threads` + `slack_event_receipts` + `crm_actions` +
bot.log, done read-only over the scoped grants SSH on 2026-08-09.

**Scale:** 89 registered threads, 377 event receipts, 6 distinct humans. 74 threads are Chase's own
playground (`C0B02721MNK`) dev testing; **15 threads in production `C01DGT9D11D` are the real
user record** (Nelly 4, Chase 5, Kerry 2, Brett 2, Jocelyn 1, Anthony 1).

**Two Slack ids that are NOT in `config/reps.json`:**
- `U06RXJKRXSR` = **Jocelyn** (jocelyn@monarchconnected.com), a real Monarch human. Still unmapped
  today, so her Google Sheet exports fail closed to Excel (`export_jobs.error` = "Google Sheet export
  needs a rep mapped in config/reps.json", 3 rows 2026-07-23).
- `U0BH0ESRJ4W` = **Grant itself**. Rows "initiated_by" it are bot/dry-run artifacts, not humans.

### The four defects that actually dead-end users

1. **A RIVAL BOT HIJACKS GRANT'S THREADS.** `Monarch_Sales_Agent` (the Monarch website project's
   agent) is a member of `C01DGT9D11D` and replies to *Grant's* messages as though Grant were its
   user — "What do you need, Grant?" — and repeatedly tells the human that campaign loading is
   impossible and to use Data Loader, while Grant is actively doing it. Grant then reads those
   messages as thread context and **flip-flops on its own capability** ("Anything said earlier
   claiming I couldn't build the preview at all was wrong"). Nelly's 2026-07-23 thread (47 receipts)
   is mostly this. It is the single largest source of user confusion in the record.
2. **`CompletedPaidCall` escapes as an unhandled exception** from `find_contact` (12 tracebacks in
   bot.log). Once a lead's `paid_enrichment_attempts` row is `completed`, every later contact lookup
   for it CRASHES instead of replaying the stored outcome. Users see "the contact search errored out
   … worth trying again a bit later" — but retry can NEVER succeed. Hit leads 233/237/239/242 for
   Nelly on 08-06 AND again on 08-07. See [[deployed-vs-local-drift-20260809]].
3. **`lightning.force.com` campaign links are rejected.** Salesforce's own UI copies
   `d41000002jiq8eam.lightning.force.com/...`; Grant only accepts `my.salesforce.com` and answers
   "that link doesn't seem to be from our configured Salesforce org". Bit Nelly on 07-27, 08-06 and
   08-07. Grant CAN find the same campaign by name, so this is a URL-normalization gap, not a
   permissions one.
4. **Roster gating is invisible until the write step.** Nelly's whole 08-06 campaign flow ran to the
   final button and THEN failed with "your Slack account isn't mapped to an approved rep email".
   That is what forced roster deploy 4c6a543. Same class of failure still open for Jocelyn.

### PRODUCTION Salesforce writes HAVE fired — CLAUDE.md is stale on this
`crm_campaign_batches.writer_is_sandbox = 0`, `writer_org_id = 00D41000002jIQ8EAM`,
`writer_host = d41000002jiq8eam.my.salesforce.com`. On **2026-08-06T18:38:36Z** action
`80761526…` created **13 organization-only Leads + 13 Campaign Members** in PRODUCTION Salesforce
(campaign `701UZ00000te467YAA` "California Grant 2026"), approved by Nelly. Three
`crm_campaign_write_attempts` rows record it. Earlier, 7 `create_campaign` actions completed
2026-07-23 (Nelly 4, Jocelyn 3) — also production. **CLAUDE.md's "no production Campaign write has
fired" and "the five ledger tables are EMPTY" are both OUT OF DATE.**

### Aggregate outcome counts (2026-08-09)
- `crm_actions` 48: 18 complete, 5 partial, 9 ready (never clicked), 6 dry_run, 3 cancelled,
  2 failed, 1 expired, 1 unknown. Every `partial` is `add_campaign_members` where most orgs
  resolved `unresolved` (no Salesforce record).
- `search_requests` 151, all state=complete, **zero returned 0 results**. `result_complete=0` on 131
  is NOT failure — it only means the snapshot wasn't the full set (top-N answers).
- `rich_card_actions` = **0 rows. No human has ever clicked a rich-card button.**
- `paid_enrichment_attempts` 62: 51 completed, 11 indeterminate — every indeterminate is
  `SourceUnreachable`.
- `contacts` 81: 19 verified (all with email), 36 linkedin_only (0 email), 26 not_found (0 email).
  No fabricated address anywhere — the honesty invariant holds.
- `outreach` 8: 7 submitted to Persequor, 1 stuck `draft` with a NULL `created_at`; `sent_at` NULL on
  all 8 (sending happens inside Persequor and is never reconciled back).
- Contact enrichment silently caps at **10 per run** regardless of what the user asked for (Kerry
  asked for 231, Nelly for 100). Grant discloses the cap only AFTER the run.
