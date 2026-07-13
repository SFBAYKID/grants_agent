---
name: workflow-design-review-2026-07
description: Verdict + required changes from the 2026-07-13 review of docs/workflow_design.md (multi-rep + Persequor + Salesforce)
metadata:
  type: project
---

Reviewed `docs/workflow_design.md` v1 on 2026-07-13. Verdict: **Approved with Required
Changes** (not rejected — architecture is sound, sequencing and idempotency details are
not). Required changes demanded, so they are not re-litigated later:

1. **request_id must be minted once and persisted on the outreach row before the first
   POST attempt.** The example format embeds a timestamp; regenerating on retry
   (queued_local backoff, timeout replay) mints a new id → duplicate card → possible
   double send after two rep taps. New id ONLY for deliberate post-enrichment resubmit.
2. **Build order flaw:** step 2 (Persequor client) retires Grant's only draft path while
   step 3 (enrichment) is what makes step 2 usable — contacts table is empty, so every
   [Draft email] dead-ends and `/grant contact` becomes the primary path. Recommended:
   enrichment before the Persequor client, or keep the fallback behind a flag for one
   verified cycle. Persequor's endpoint did not exist yet at review time (their design
   pending Chase approval; wiring + shared secret both ⚠️ OPEN).
3. **Stuck-state semantics undefined:** no failed/error terminal status, no max-age for
   `received`/`drafted`, no defined GET-404 behavior (Persequor redeploy/state loss).
4. **Edit-in-Gmail blind spot question** posed to Persequor: rep edits via /edit and
   sends from Gmail UI → does `sent` ever get emitted? If not, Grant records
   expired/claimed on an actually-sent email.
5. Weak spot flagged: `grant_watch/db.py` opens SQLite without WAL/busy_timeout while
   bot + cron (+ future APScheduler) processes share the file; fix alongside step 1.

Related: [[persequor-handoff-noop]] (critical finding #1 of that review).
