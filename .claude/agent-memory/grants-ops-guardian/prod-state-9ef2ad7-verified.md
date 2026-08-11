---
name: prod-state-9ef2ad7-verified
description: CURRENT PROD 2026-08-11 — 9ef2ad7 (origin/main), schema 39, PID 71366, 0.18s outage; and the capability-wording coverage check that proves no colleague can get the generic fallback sentence
metadata:
  type: project
---

Deployed 2026-08-11 00:55–01:05 PDT, `f7cff1d` → `9ef2ad7`. Supersedes
[[prod-state-f7cff1d-verified]] as the "what is running" pointer — but that file's
**manifest-diff noise floor (the 6 always-benign paths)** is still current and is the
thing to read before panicking about a sha mismatch. **Never answer "what is running"
from this index; read `.deployed_revision`.**

| Fact | Value |
|---|---|
| `.deployed_revision` | `9ef2ad72fe4122dfaef11e805dcb0a220a5d8b53` |
| Listener | PID **71366** (was 68476), single, `.venv/bin/python -u -m grant_watch.slack.grant` |
| Outage | **0.18 s** |
| Schema | **39**, 39 rows — the delta touched no migration |
| `followup_nudges` | 26 (2 delivered / 24 suppressed-stale) — unchanged |
| Crontab | 25 lines, sha `34002d4bc67e21f5…` — byte-identical across the deploy |
| `.env` | sha `9b68bc1885080…`, 67 lines — byte-identical across the deploy |
| FK orphans | 2, the two approved `source_observations` rows — **compared pre/post, not hardcoded** |
| Backup | `~/backups/predeploy-9ef2ad7-20260811T075527Z/` (26 M, 7 tar members, copy `integrity_check` ok) |

Delta = **7 files**, all modifications, zero adds/deletes: `CLAUDE.md`, three runtime
(`slack/search_planning.py`, `slack/grant_prompt.py`, `slack/nudge_messages.py`), three
test. The 8th path in `git diff` was `.claude/agent-memory/**` and never ships.

**The branch name lies and that is fine.** `git rev-parse HEAD` ran on
`review/rich-award-card-campaign-20260723`, but its tip **was** `origin/main` — the
ancestry gate passed on the hash, which is the only thing that matters. Do not reject a
deploy because the branch name is not `main`; check `merge-base --is-ancestor`.

## The capability-wording coverage check — worth re-running after any nudge change

`mark_available` broadcasts: it reopens EVERY open ask for a slug at once. A slug missing
from the four wording maps in `slack/nudge_messages.py` does not degrade quietly, it sends
the generic *"Good news — I can do that one now"* to all of them.

Measured on the deployed bytes 2026-08-11, read-only:

- Production `capability_asks` holds **34 rows, 23 distinct slugs**.
- **7 slugs still have no hand-written wording**: `campaign_member_enrichment`,
  `direct_lead_field_edit`, `filter_by_application_status`,
  `format_spreadsheet_for_dataloader`, `format_spreadsheet_for_upload`,
  `salesforce_batch_upload`, `salesforce_upload`. One open ask each.
- **All 7 are UNARMED** (`available_since IS NULL`), and `ARMED_AND_OPEN_WITHOUT_WORDING`
  is **0**. So nobody can receive the generic sentence today.

`9ef2ad7` did not create this — it **reduced** it, adding 6 slugs (`salesforce_campaign_add`,
`add_campaign_members_via_ids`, `pull_lead_ids_for_campaign`, `contact_lookup`,
`search_scoping`, `filter_by_award_date`). 4 slugs exist in code that production has never
recorded — harmless.

**The guard is real and wired**: `capability_asks.mark_available` calls
`nudge_messages.wording_exists()` and raises `ValueError` **before** the UPDATE. But note
its limit — **it only blocks NEW declarations.** `nudges._capability_is_live()` checks
runtime deps only (`email_results` → Resend key) and does **not** consult wording, so a row
whose `available_since` was set *before* the guard existed would sail through and render the
fallback. That is why the check to run is not "is the guard present" but:

> is there any row with `available_since IS NOT NULL` **and** `state='open'` **and**
> `not wording_exists(capability)`?

Recipe: `.claude/agent-memory/…` — see the script shape in the deploy scratchpad; it opens
`file:…grant_watch.db?mode=ro` and imports `wording_exists` from the deployed bytes so the
answer is about production's code, not the laptop's.

## Post-checks that passed

- `search_confirmation({"record_kind":"opportunity","date_from":"2026-08-01"}, "x")` →
  starts `Search plan:`, no scoping question. **Control** `search_confirmation({}, "find me
  some grants")` → still asks the scoping question, so the fix did not over-reach. Both run
  on the deployed bytes, and both were ALSO run BEFORE the kill — a behavioural failure then
  costs zero outage.
- `pytest tests/test_search_confirmation.py tests/test_human_question_acceptance.py` on the
  droplet: **20 passed, 87 skipped**, 1.97 s. The 87 skips are the LLM acceptance matrix,
  correctly dormant because `GRANT_LLM_ACCEPTANCE` was deliberately not set.
- `nudge --dry-run` head of queue: **`card_unengaged` (variant a), Hoxie School District
  No 46, $500,000**, correctly `[held: outside business hours]` at 01:0x PDT. **I did not
  measure the queue head before the deploy, so I cannot say whether it moved** — reported as
  measured, not compared. Memory's 2026-08-10 note that the head @-mentions a person is
  therefore not contradicted-or-confirmed by this run.

Related: [[deploy-mechanism]], [[prod-state-f7cff1d-verified]], [[tenant-and-layout]],
[[ssh-rate-limit-and-stdin-traps]], [[nudge-queue-state-20260809]].
