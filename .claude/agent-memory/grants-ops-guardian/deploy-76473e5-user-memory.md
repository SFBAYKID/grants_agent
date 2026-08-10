---
name: deploy-76473e5-user-memory
description: Deploy 8cb557a→76473e5 on 2026-08-10 (CURRENT PROD) — schema 38→39 (user_memory), PID 37641, 0.436s outage incl. the deliberate migration window; prompt-cache ordering proven user-invariant on the deployed bytes
metadata:
  type: project
---

**LIVE 2026-08-10T06:51:04Z (droplet Sun 23:51:04 PT).**
`8cb557a5d27b1ca4cfb3d6bfe0f32a03b1677006` → `76473e5a12f09b1b54b00d1992964afa9efd847f`
(2 commits). **Schema 38 → 39.** Listener **36771 → 37641**. **Outage 0.436 s**, which
INCLUDES the deliberate bot-down migration window — the migration itself is a
`CREATE TABLE`, so the whole thing fits inside half a second.

Delta 13 paths = **9 deployable** + 4 `.claude/agent-memory/**`. 7 mods + **2 adds**
(`grant_watch/user_memory.py`, `tests/test_user_memory.py`). Pre-image 7/7 == `8cb557a`
blobs, adds absent. Archive `47157e38…7bca`, 45,105 B, member set == delta, upload
byte-exact. itemize 7 `>fcsT`/`>fc.T` + 2 `>f+++`, 0 deleting, catch-all empty, second
pass 0, `find -cnewer` exactly 9, post-image 9/9. **Closure 121/121 byte-identical.**
Import closure **118/118**. `.env` `9b68bc18…c634`, crontab `34002d4b…7ab5` (25 lines,
10 active) and `run_bot.sh` all unchanged — no cron change in this deploy.
Rollback: `~/backups/deploy-76473e5-20260810T065008Z/` (vacuum `1cdf5117…`, integrity ok,
leads 10715, contacts 85).

## Migration 39, applied with the bot DOWN

Pure `CREATE TABLE IF NOT EXISTS user_memory` + `CREATE INDEX ix_user_memory_live`. No
ALTER, no UPDATE, no backfill — it structurally cannot touch an existing row, and the
counts confirm it: **leads 10715, contacts 85, capability_asks 20, followup_nudges 0,
slack_event_receipts 417, announcements 1 — identical before and after.** Tables 47 → 48.

Columns, in order: `id, slack_user, kind, fact, evidence, audience, thread_ts,
message_ts, recorded_at, expires_at, superseded_by`. Landed **empty**.
`superseded_by` is a **self-referencing FK** (`foreign_key_list` shows
`user_memory.superseded_by → user_memory.id`); on an empty table it introduces no
orphan, and `foreign_key_check` still returns exactly the two approved
`source_observations` rows (10642, 11892). `integrity_check` ok.

## Restart still inert (re-proven, third time)

Captured either side of the relaunch:
```
POST_MIGRATION_FILE: db_size=26976256 db_mtime=1786344664185271232 wal_size=0 wal_mtime=1786344664246276234
POST_BOOT_FILE     : db_size=26976256 db_mtime=1786344664185271232 wal_size=0 wal_mtime=1786344664246276234
```
Identical to the nanosecond ⇒ the boot wrote nothing. `WATCHDOG_LINES_AT_BOOT=0`, boot
region exactly the 2 lines, 0 tracebacks. The `3-59/10` watchdog cron fired again at
**06:53** (third tick), so the migration restart did not disturb it.

## THE COST PROPERTY, proven behaviourally not by reading source

`_cached_system(memory)` must put the user-INVARIANT prompt first with the cache
breakpoint, and the per-person block after it. Reversed, every user would get a distinct
cache prefix and prompt caching would break for everyone at once. Measured on the
deployed bytes:

| block | len | cache_control |
|---|---|---|
| `[0]` `_SYSTEM` | 24,698 | `{'type': 'ephemeral'}` |
| `[1]` memory | varies | **None** |

And the decisive one: `_cached_system("- alice fact")` and `_cached_system("- bob fact")`
have a **byte-identical block 0 with identical cache_control**, while their last blocks
differ. With no memory it degrades to a single cached block. Re-run this check whenever
anything touches the system prompt assembly.

## The anti-fabrication guard — see a TRUE before believing the FALSEs

`evidence_supports(evidence, said)` requires the quote to be a **contiguous substring**
of what the person typed, after whitespace collapse and case folding. My first two probes
both returned `False` and I nearly reported the guard as working on that basis — but two
Falses are also what a *totally broken* guard returns, and a guard that never accepts
anything makes `capture` a silent no-op that stores nothing forever. Exercised both
directions before concluding:

- `'only work Texas'` / `'ONLY   WORK   texas'` / `'I hate spreadsheets'` → **True**
- `'only texas'` (words skipped, not contiguous) / `'loves spreadsheets'` / `'plays lacrosse'` → False
- `remember(...)` with unsupported evidence **raises ValueError**; with supported evidence
  it stores and `recall` returns it.

Done against an in-memory DB; production `user_memory` never touched (still 0 rows).

## `_remember_from` does NOT always get the caller's connection

Its docstring says "The CALLER's connection, never a new one, and never closed here."
True in `_handle_drip_thread` (passes `conn`). **Not true in `_converse_general`, which
calls `_remember_from(db.connect(), …)`** — and `db.connect()` is `sqlite3.connect(...)`,
a brand-new connection every call, never closed. CPython refcounting closes it when the
call returns, so this is not a live leak, but the stated invariant does not hold at that
call site. It mirrors two PRE-EXISTING calls on the adjacent lines
(`db.get_lead(db.connect(), …)`), so it is not introduced by this deploy. Worth fixing the
comment or the call, not urgent.

## `user_memory` is empty, and that is the correct result

No real Slack message arrived tonight (deployed 23:51 PT Sunday), so nothing was captured.
Reported as empty rather than exercised with a synthesised message — a fake user message
would have written a fabricated memory about a colleague into the one table whose entire
safety story is "only what they actually said".


## Followed immediately by `cee19ee` (comment-only)

**LIVE 2026-08-10T07:01:41Z, PID 37641 → 39941, 0.135 s outage.** One file,
`grant_watch/slack/grant.py`, correcting the `_remember_from` comment to say the
function uses the connection it is GIVEN and that `_converse_general` opens one
because it has none in scope. **Proven comment-only by AST equality** between the two
revisions (`ast.dump` identical) — the right instrument, since a textual diff cannot
distinguish a comment edit from a code edit that happens to look like one. Schema
stayed 39, closure 121/121, `.env` and crontab byte-identical, 0 tracebacks,
watchdog tick confirmed at 07:03.

Related: [[deploy-8cb557a-watchdog-boot-revert]], [[deploy-mechanism]],
[[tenant-db-write-safety]], [[verify-the-premise-not-the-claim]], [[backups-retention]].
