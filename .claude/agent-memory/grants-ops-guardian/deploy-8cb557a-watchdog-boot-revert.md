---
name: deploy-8cb557a-watchdog-boot-revert
description: Deploy 7837cda→8cb557a on 2026-08-10 (CURRENT PROD) — one file, schema 38, PID 36771, 0.128s outage; removes the boot watchdog pass so a restart is INERT again, proven by db+wal mtimes identical to the nanosecond across a restart
metadata:
  type: project
---

**LIVE 2026-08-10T06:39:41Z (droplet Sun 23:39:41 PT).**
`7837cdad8bc5d5b8089cc5b72cd4d0d6398faa83` → `8cb557a5d27b1ca4cfb3d6bfe0f32a03b1677006`
(1 commit). **Schema stayed 38.** Listener **36059 → 36771**, **0.128 s outage** — fastest
recorded. Delta 4 paths = **1 deployable** (`grant_watch/slack/grant.py`) + 3
`.claude/agent-memory/**`. `watchdog.py` and `cli.py` untouched.

Pre-image `aeab1dc2…` == the `7837cda` blob; post-image `7a5fe5b6…` == the `8cb557a` blob;
itemize 1 `>fcsT`, 0 deleting, catch-all empty, second pass 0, `find -cnewer` exactly 1.
**Closure 120/120 byte-identical.** `.env` `9b68bc18…c634` and crontab `34002d4b…7ab5`
(25 lines, 10 active jobs) both untouched — this deploy changed no jobs.
Rollback: `~/backups/deploy-8cb557a-20260810T063837Z/`.

## What it undoes, and why that was right

`7837cda` ran the watchdog inside `main()`. Two properties died with it, and both are
restored here:
1. It needed a writable `db.connect()` — **which is the migration runner** — so a plain
   restart, including a `*/5` keepalive relaunch, silently applied migrations.
2. It ran `dry_run=False`, making **restarting the process a message-mutating act**.

The `3-59/10` cron tick already covers the restart case within ten minutes. Losing
instant repair to keep restarts inert is the right trade.

## AST beats grep here, and grep would have said "still present"

`grep -n "db.connect" ` inside the `main()` slice returns **6 hits** at `8cb557a` — all of
them the explanatory comment describing what was removed. The decisive check is the AST:

```
CALLS in main(): ['RuntimeError', 'SocketModeHandler', 'configured_channel_ids',
                  'create_app', 'handler.start', 'load_dotenv', 'print']
IMPORTS inside main(): none
```
No `connect`, no `watchdog`, no `auth_test`. Comments are not in the AST, which is exactly
why this is the right instrument — same family as the retired-symbol check in
[[deploy-7837cda-watchdog]] and the AST-diff habit in [[deploy-5f09200-fallback-routing]].

## The inertness proof — measure the FILE, not just the schema number

"schema_max unchanged" proves little when no migration is pending: it would read 38 either
way. The measurement that actually discriminates is the file itself, captured immediately
either side of the restart:

```
BEFORE_RESTART: schema_max=38 count=38 tables=47 db_size=26959872
                db_mtime=1786343469773736266 wal_size=0 wal_mtime=1786343605909583290
AFTER_RESTART : schema_max=38 count=38 tables=47 db_size=26959872
                db_mtime=1786343469773736266 wal_size=0 wal_mtime=1786343605909583290
```

**Identical to the nanosecond, including the `-wal`.** A writable `db.connect()` would have
touched at least the WAL. Reusable rule: to prove a process is read-only or inert, compare
`db_mtime` + `wal_mtime` in nanoseconds, not row counts.

Boot log region = exactly the 2 boot lines, **`WATCHDOG_LINES_IN_FRESH_REGION=0`**,
0 tracebacks, PID_COUNT 1, 53 venv maps.

## Production contacts coverage, 2026-08-10 (85 rows)

Answering "no mobile number on the Salesforce leads": **`mobile_phone` is 0 / 85 — the
column exists (migration 37) and is entirely empty in production.**

| | non-empty | of 85 |
|---|---|---|
| `mobile_phone` | **0** | 0.0% |
| `phone` | 16 | 18.8% |
| `email` | 22 | 25.9% |

| contact_status | n | email | phone | mobile |
|---|---|---|---|---|
| linkedin_only | 36 | 0 | 0 | 0 |
| not_found | 26 | 0 | 0 | 0 |
| verified | 20 | 20 | 15 | 0 |
| vendor_licensed | 2 | 2 | 0 | 0 |
| human_asserted | 1 | 0 | 1 | 0 |

`provenance`: linkedin_claimed 36, NULL 26, page_verified 20, vendor_licensed 2,
human_asserted 1. **62 of 85 rows (73%) carry no email, no phone and no mobile** —
`linkedin_only` + `not_found`. The mobile column can only fill from a ZoomInfo paid pull,
and only 2 rows have ever come from that path. Chase's complaint is accurate and structural,
not a rendering bug.

Related: [[deploy-7837cda-watchdog]], [[deploy-cdfdaf9-threadscan]], [[deploy-mechanism]],
[[zoominfo-first-live-spend-20260809]], [[deploy-a718066-mobile-phone]].
