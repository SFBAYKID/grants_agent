---
name: prod-state-87d4e00-verified
description: CURRENT PROD 2026-08-13 - 87d4e00, schema 47, PID 124668, 14 files, 116.4s deliberate migration outage, zero cron ticks lost
metadata:
  type: project
---

**PRODUCTION IS `87d4e004673b2734ea49c40f9af69f63205986f8`** (origin/main head,
"Merge pull request #6"). Deployed 2026-08-13 by the guardian. Supersedes
[[prod-state-58b3e24-verified]].

**Why:** four defect fixes from the re-research pass (see [[rerearch-pass-20260813]]) —
enrich-orgs retry cooldown, dedup canonical-key grouping, fill-contacts vendor-id
validation, nces-bind ArcGIS key-range paging.
**How to apply:** this is the rollback fingerprint and the current-state reference.

## The numbers

- **14 deployable files** (22 delta paths − 8 `.claude/agent-memory/**`), all **M**,
  **0 additions, 0 deletions** — so `--delete` was omitted and was genuinely unnecessary.
  All **14/14 byte-identical** to the pinned commit's blobs; second rsync pass **empty**.
- **Schema 46 → 47**, applied deliberately with the listener DOWN via
  `migrations.apply_migrations(conn)` — there is **NO `migrate` CLI subcommand**;
  migrations otherwise apply as a side effect of any CLI process opening the DB.
- Migration 47 is additive: `leads.org_profile_checked_at TEXT` + index
  `ix_leads_org_profile_attempt`. **leads 10761 → 10761 unchanged.** Columns 44 → 45,
  indexes 91 → 92, **tables 52 → 52** (no new table). All rows NULL by design.
- PID **121468 → 124668**, exactly one listener.
- **Outage 116.4 s measured** (kill 21:04:32.565Z → process start 21:06:29Z), deliberately
  long: migration window, not a plain restart.
- Tracebacks **13 → 13** (zero new). `bot.log` 1138 → 1140 (exactly the two boot lines).
- `integrity_check` ok; FK orphans **2 → 2 compared**; `followup_nudges` 31 → 31,
  `contacts` 178 → 178, `posts` 37, `capability_asks` 34 — all preserved.
- **`.env` byte-identical**: sha `e168372900965e181a8a2466c49799b946a8dee13f3181ff81eb040d9b096f79`,
  size 3953, mode 600. **`.env` copy count 63 → 63, no new copy.**
- Crontab **byte-identical**, sha `34002d4bc67e21f59d1ae6ba11054a9658caae5a2a10b0b5b8345909de6d7ab5`,
  25 lines / 10 active jobs.

## Backups taken (keep)

- `~/grant_watch.db.pre47.20260813T210404Z` — taken with the listener already STOPPED, so it
  is the exact pre-migration-47 state. `integrity_check=ok`, schema 46, leads 10761 verified
  **against the COPY**. Written via the SQLite **backup API from a `mode=ro` source**.
- `~/crontab.backup.pre-87d4e00.20260813T210404Z`,
  `~/.deployed_revision.bak.pre-87d4e00.20260813T210404Z`.
- **`~/grant_watch.db.pre46.20260813T180041Z` confirmed UNTOUCHED** — still Chase's rollback
  for the migration-46 quarantine decision.

## Verified post-deploy

- All 8 changed/critical modules **imported cleanly on the droplet BEFORE the relaunch**.
  `MIGRATIONS` declares 44 entries, max version 47; `migration_47_...` callable;
  `db.canonical_entity_key` present.
- Droplet `pytest` on the 6 shipped test files: **91 passed**.
- Running-bytes proof: all 5 changed modules carry mtime **13:57:14** (rsync `-a` preserves
  the commit's own timestamp) and the listener started **14:06:29** — file write strictly
  precedes process start.

## The cron pause cost exactly ONE tick, and it was the one I replaced by hand

Window 14:04:04 → 14:06:49 PDT. In that 2m45s the only scheduled tick was the `*/5`
keepalive at 14:05 — which is precisely the job I substituted with a manual
`nohup bash run_bot.sh`. Watchdog (`3-59/10`) next fired 14:13, nudge (`*/15 8-14`) 14:15,
drip/remind (`*/30`) 14:30 — **all after the restore**. Deploying in the gap between :05
and :13 on a weekday afternoon costs nothing; check the tick calendar before choosing the
window rather than assuming a pause is free.
