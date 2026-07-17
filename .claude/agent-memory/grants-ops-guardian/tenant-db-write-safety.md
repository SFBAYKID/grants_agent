---
name: tenant-db-write-safety
description: How to safely back up and mutate rows in the live tenant SQLite DB while the bot is running
metadata:
  type: project
---

The tenant's live DB is `~/grants_agent/grant_watch.db` in **WAL mode** (`-wal`/`-shm` present),
and the Grant bot holds an open connection while running. Scoped, single-row cleanups against it
are safe if done carefully. Proven 2026-07-17 clearing a stranded `create_contact_record` action.

**Why:** WAL allows many concurrent readers plus one writer, so read-only SELECT inspection never
blocks (or is blocked by) the running bot. A guarded write from a second `.venv/bin/python` sqlite3
connection succeeds while the bot runs, as long as it grabs the write lock atomically and asserts
scope before committing.

**How to apply:**
- **Back up first, as an associated set.** `cp -a grant_watch.db BAK` **and** `-wal`→`BAK-wal`,
  `-shm`→`BAK-shm` (suffix the backup name so SQLite finds the WAL/SHM on restore). Backups live in
  `~` (e.g. `~/grant_watch.db.bak.<UTCstamp>`). Confirm `sha256sum BAK grant_watch.db` match.
- **Inspect read-only** with a normal connection issuing only SELECTs (avoid `mode=ro` WAL quirks).
- **Write behind a fail-closed guard**, via `.venv/bin/python` (need `cursor.rowcount`): `BEGIN
  IMMEDIATE` → re-verify the exact target set inside the txn (e.g. "exactly one committing row and it
  is this id") → UPDATE with a `WHERE id=? AND state=<expected>` predicate → **assert
  `rowcount == 1`, else `rollback()` and stop** → only then `commit()`. Never guess an id; use the one
  proven by inspection.
- Relevant tables (grants CRM approval flow): `crm_actions` (PK `id` TEXT; cols incl `action_type`,
  `state`, `external_write_started`, `last_error`, `updated_at`, `campaign_id`) and `crm_action_items`
  (`action_id`→crm_actions, `lead_id`→leads, `state`, `salesforce_id`, `campaign_member_id`). A stranded
  create with `salesforce_id IS NULL` on its item means no Salesforce record was written, so flipping it
  to `failed` loses nothing real. See [[tenant-and-layout]] and [[deploy-mechanism]].
