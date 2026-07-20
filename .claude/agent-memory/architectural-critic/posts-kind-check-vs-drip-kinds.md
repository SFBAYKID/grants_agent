---
name: posts-kind-check-vs-drip-kinds
description: posts.kind CHECK(kind IN 'nugget','bulletin') was never widened for new drip kinds platinum/rfp — live record_post crashes AFTER Slack send; tests miss it
metadata:
  type: project
---

The `posts` table CHECK constraint drifted behind the drip ladder. Recurring-class fragility: a
SQLite CHECK enum + a test topology that hides enum drift.

**The bug (confirmed live at HEAD ba0a7b7, 2026-07-19):**
`migrations.py` defines `posts.kind TEXT NOT NULL CHECK(kind IN ('nugget','bulletin'))` and NEVER
rebuilds/relaxes it. But `drip.pick()` returns kinds `platinum`/`nugget`/`rfp`/`bulletin`, and
`run_drip()` passes `kind` straight to `db.record_post()`, which INSERTs into `posts`. So a live
(non-dry-run) **platinum** or **rfp** post: reserves → posts to Slack → `record_post` raises
`sqlite3.IntegrityError: CHECK constraint failed`. Message is already in Slack; `notification_outbox`
orphaned in `sending`; lead stuck `status='new'`; cron tick exits non-zero with a traceback.
Then reserve_notification returns None on later ticks (no duplicate), but the stuck top-ranked lead
silently blocks every lower tier beneath it until it ages out of top rank. Proven empirically:
platinum + rfp both crash after `client.calls==1`; nugget control succeeds (outbox=delivered,
status=surfaced).

**Why tests are green anyway:** every LIVE `run_drip` test in `tests/test_drip.py` uses `_mk_lead`
(kind='nugget', an allowed value). Platinum/rfp are only exercised through `pick()` and
`build_platinum`/`build_rfp_alert` — never through `record_post`. So the constraint violation is
invisible to the suite. **Why:** the test topology tests the picker and the builders but not the
end-to-end delivery per kind. **How to apply:** whenever a new drip `kind`/`style`/enum value is
added, demand a LIVE run_drip test for that kind (fake client, real temp DB) that reaches
`record_post`, and check every CHECK/enum column (`posts.kind`, `engagement.kind`) that the value
flows into. Smallest correct fix: a migration that rebuilds `posts` with
`CHECK(kind IN ('platinum','nugget','rfp','bulletin'))` — do NOT remap platinum→nugget (pick()
counts `kind=='bulletin'` and engagement keys off kind).

Related: [[grant-onchat-search-weakspots]] (same repo, untested state-machine blind spots).
