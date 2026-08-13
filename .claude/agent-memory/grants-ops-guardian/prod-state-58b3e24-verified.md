---
name: prod-state-58b3e24-verified
description: CURRENT PROD 2026-08-13 - 58b3e24, schema 46, PID 121468, paid-provider authority cutover; 139 files, ~4min deliberate outage
metadata:
  type: project
---

**PRODUCTION IS `58b3e244f3c5e6961dfc858a6bb3579963f5bf28`** (origin/main head, "Merge
pull request #5"). Deployed 2026-08-13 by the guardian. Supersedes
[[prod-state-0223c10-verified]].

**Why:** the 30-finding remediation plus the paid-provider authority cutover.
**How to apply:** this is the rollback fingerprint and the current-state reference.

## The numbers

- **139 deployable files** (142 delta paths - 3 `.claude/**`), 109 modified + **30 new**,
  **0 deletions**. All **139/139 byte-identical** to the pinned commit's blobs; second
  rsync pass **empty**; `--delete` omitted after a preview showed zero deletions.
- **Schema 40 -> 46**, applied deliberately with the listener DOWN. Table count 48 -> 52
  (`organization_field_evidence`, `firecrawl_runtime_periods`, `firecrawl_runtime_attempts`,
  `firecrawl_runtime_provider_state`).
- PID **108300 -> 121468**, exactly one listener, clean Bolt boot.
- **Outage ~4 minutes (18:03:30 -> 18:07:29Z), deliberately long** - this is a migration
  + ledger-cutover window, not the usual sub-second restart. Do not compare it to the
  0.1-0.5s figures in the older records.
- Tracebacks **13 -> 13** (zero new). `bot.log` 1136 -> 1138 (exactly the two boot lines).
- FK orphans **2 -> 2 compared** pre/post. `integrity_check` ok. `followup_nudges` 30 -> 30,
  `capability_asks` 34, `posts` 37, `leads` 10761, `contacts` 172 - all preserved.
- Crontab **byte-identical**, sha `34002d4bc67e21f59d1ae6ba11054a9658caae5a2a10b0b5b8345909de6d7ab5`,
  10 active job lines / 25 total.
- **`.env` is NOT byte-identical this time, by design** - exactly 7 keys appended (see
  [[paid-provider-authority-cutover]]). Prefix sha of the original 3396 bytes proven
  unchanged; new sha `e168372900965e181a8a2466c49799b946a8dee13f3181ff81eb040d9b096f79`,
  size 3953, mode 600. **`.env` copy count 64 -> 64: no new copy was written.**

## Backups taken (kept, do not delete without asking)

- `~/grant_watch.db.bak.20260813T180041Z` (+`-wal`/`-shm`) - pre-deploy, `integrity_check=ok`
  run against the COPY.
- `~/grant_watch.db.pre46.20260813T180041Z` - taken with the listener already STOPPED, so it
  is the exact pre-migration state. **This is the rollback source for migrations 41-46.**
- `~/crontab.backup.pre-58b3e24.20260813T180041Z`, `~/.deployed_revision.bak.20260813T180041Z`.
- Both DB backups were taken through the **SQLite backup API from a `mode=ro` source**, not
  `cp` of the `.db`/`-wal`/`-shm` set. With a live writer a plain `cp` of the set can tear;
  the backup API is consistent and needs no writer stop. Use it.

## Verified post-deploy

- `python -m grant_watch.paid_provider_runtime_check` -> **`verified`**, both before the
  restart and again after it (the runbook requires the rerun).
- **14 modules imported cleanly on the droplet BEFORE the kill** - a Bolt process can be up
  and broken, so prove the import resolves rather than inferring it from a running PID.
- Droplet `pytest` on the paid-provider/health/db set: **95 passed**.
- Three cron ticks observed on the new code: `watchdog: nothing stuck`, `nudge: skip:
  holding for today's 11:20 PT slot`, `grant_keepalive status=healthy` (the keepalive found
  the listener already up and did NOT spawn a second one).
