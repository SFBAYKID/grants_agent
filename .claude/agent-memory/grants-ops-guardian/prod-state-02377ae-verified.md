---
name: prod-state-02377ae-verified
description: CURRENT PROD 2026-08-11 — 02377ae (origin/main), schema 39, PID 71882, 0.19s outage; the capability-wording guard now bites on the DELIVERY path, proven True→False with a same-run control
metadata:
  type: project
---

Deployed 2026-08-11 01:14–01:19 PDT, `9ef2ad7` → `02377ae`. Supersedes
[[prod-state-9ef2ad7-verified]] as the "what is running" pointer; that file's
capability-wording coverage section is now **superseded by the table below**, and
[[prod-state-f7cff1d-verified]] remains the reference for the manifest-diff noise floor.
**Never answer "what is running" from this index; read `.deployed_revision`.**

| Fact | Value |
|---|---|
| `.deployed_revision` | `02377ae075447910aa55f32bd8bc61ee7f23b420` |
| Listener | PID **71882** (was 71366), single, `.venv/bin/python -u -m grant_watch.slack.grant` |
| Outage | **0.19 s** (old process dead in 0.03 s) |
| Schema | **39**, 39 rows — the delta touched no migration (`git diff … -- migrations.py` = 0 bytes) |
| `followup_nudges` | 26 (2 delivered / 24 suppressed) — unchanged |
| Crontab | 25 lines, sha `34002d4bc67e21f5…` — byte-identical across the deploy |
| `.env` | sha `9b68bc1885080…`, 67 lines, 3396 bytes, mtime unchanged — byte-identical |
| FK orphans | 2, the two approved `source_observations` rows — **compared pre/post** |
| Backup | `~/backups/predeploy-02377ae-20260811T081515Z/` (26 M, 3 tar members, copy `integrity_check` ok) |
| Disk | 71% used, 14 G free (was 97% in July — the purge held) |

Delta = **3 files**, all modifications, zero adds/deletes: `CLAUDE.md`,
`grant_watch/slack/nudges.py`, `tests/test_nudge_followups.py`. `git diff --name-only`
listed 9 paths; **6 were `.claude/agent-memory/**` and never ship.** The coordinator's
stated delta was correct this time — measured anyway, which is the point.

## The change, and how to re-verify it cheaply

`_capability_is_live()` now ends `return wording_exists(capability)` instead of
`return True`, so the **delivery** path refuses a slug with no hand-written sentence, not
only the declare path (`mark_available`). The gap it closes: a row armed BEFORE the declare
guard shipped already carries `available_since` and never passes through that guard again,
so it would render the generic *"Good news — I can do that one now"* to everyone who asked.

**`capability_not_ready` is NOT in `PERMANENT_SUPPRESSIONS`** (verified in source: that
frozenset is `stale`, `resolved_since_queued`, `engaged_since_queued`, `lead_parked`,
`answered_since_offer`). So the suppression is **transient** — writing a sentence later
revives the ask instead of burning it. Worth re-checking if that set is ever edited.

## The check that proves it bites — and the control that proves it did not over-reach

Run BOTH, on the deployed bytes, **before the kill** — a behavioural failure then costs
zero outage:

```python
nudges._capability_is_live("track_applications")  # must be False - it refuses
nudges._capability_is_live("campaign_load")       # must be True  - no over-reach
```

Measured 2026-08-11: `track_applications` was **True on the pre-deploy bytes and False on
the post-deploy bytes** — a real before/after, not a check that could only ever pass. This
is the same "verify the assertion in BOTH directions" discipline as the ancestry gate.

**Better than the two-probe version: enumerate every slug production actually holds.**
All 23 were evaluated; exactly **7 are refused, and they are exactly the 7 without wording**
(`campaign_member_enrichment`, `direct_lead_field_edit`, `filter_by_application_status`,
`format_spreadsheet_for_dataloader`, `format_spreadsheet_for_upload`,
`salesforce_batch_upload`, `salesforce_upload`). Every one has **`armed_and_open = 0`**, so
**no ask that could fire was silenced** — `ARMED_AND_OPEN_WITHOUT_WORDING` is still **0**,
now by construction.

**The three asks that can currently fire, named and measured** (`available_since` set AND
`state='open'`): **`campaign_load`, `contact_supplied`, `reminders`** — all three
`live=True` after the deploy. That is the specific thing a rollback would have been
protecting; check these three by name next time rather than trusting a count.

**`email_results` needs `load_dotenv` or it reads False and looks like a regression.** Its
branch is now `is_configured() AND wording_exists()`. A bare probe script without
`load_dotenv` has no `RESEND_API_KEY`, so it returns False for a reason that has nothing to
do with the new guard. Bit me in the preflight probe; caught because I knew to expect it
([[oneoff-scripts-need-load-dotenv]]). Load dotenv explicitly and print
`is_configured()` alongside, so the answer can be judged rather than believed.

## Post-checks that passed

- `nudge --dry-run` head **COMPARED, not merely measured**: identical before and after —
  `card_unengaged (a)`, Hoxie School District No 46, $500,000, correctly `[held: outside
  business hours]` at 01:1x PDT. Taking the pre-deploy dry run during preflight is what made
  this a comparison; [[prod-state-9ef2ad7-verified]] could only report it as measured.
- `pytest tests/test_nudge_followups.py` on the droplet: **29 passed** in 6.44 s.
- Import smoke asserted the bytes are the NEW ones **behaviourally** (the `track_applications`
  probe) rather than merely importable — a stale or half-applied file passes "does it import"
  and fails this.

Related: [[deploy-mechanism]], [[prod-state-9ef2ad7-verified]], [[tenant-and-layout]],
[[ssh-rate-limit-and-stdin-traps]], [[oneoff-scripts-need-load-dotenv]],
[[nudge-queue-state-20260809]], [[capability-nudges-sort-last]].
