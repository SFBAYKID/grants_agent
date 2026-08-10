---
name: deploy-d664548-followups-live
description: Deploy 14221fc→d664548 on 2026-08-09 (CURRENT PROD) — schema 35→36, PID 25636, 0.76s outage; the follow-up system TURNED ON (12th cron line), 25 gold leads org-enriched, 5 capability asks seeded but deliberately NOT declared available
metadata:
  type: project
---

**LIVE 2026-08-10T01:08:56Z (droplet 18:08 PT).** Production moved
`14221fc0cacb824ecb6da8b36e1306ff290e8444` → `d66454838036936d3336a6aea6e8a18084c4e3b8`
(4 commits). **Schema 35 → 36.** Listener **24507 → 25636** (OLDPID == the PID
[[deploy-14221fc-email-coaching-fix]] recorded ⇒ no out-of-band restart). Outage **0.76 s**
(T0 01:08:56.156 → new PID 01:08:56.914) — the fastest yet; Bolt pair present, fresh log
region exactly 2 lines, 0 tracebacks.

## Shape

Delta 16 paths = **12 deployable** + 4 `.claude/agent-memory/**` (never deployed), 0 under
`data/**`, 0 `.env*`. 10 modifications + **2 adds** (`grant_watch/org_backfill.py`,
`grant_watch/slack/nudge_variants.py`, both proven absent beforehand). Archive
`c80ad7c3…cb0c`, 266,240 B, member set asserted EXACTLY equal to the intended 12 and all 12
hashed against `git show d664548:<path>` before leaving the laptop. Pre-image: all 10
overwritten files hashed byte-exact at 14221fc ⇒ no out-of-band drift. Staged →
`rsync -cai --no-times --no-perms`, itemize 10 `>fcsT`/`>fc.T` + 2 `>f+++++++++`, **0
deleting**, catch-all empty, `find -cnewer` exactly 12, second pass 0 lines.

**`git archive` emits PARENT DIRECTORY entries** (`grant_watch/`, `grant_watch/slack/`,
`tests/`). A member-set equality check that does not filter `/$` fails with a bogus
"3 extra members". Filter them; they are not files and rsync handles them.

## Migration 36 — additive, and REQUIRED by the code that shipped with it

Two guarded `ADD COLUMN` on `followup_nudges` (`variant` TEXT, `engaged_at` TIMESTAMP) +
`CREATE INDEX IF NOT EXISTS ix_nudges_variant`. Registry diff proved it: 22 → 23 migration
functions, one new name. Applied with the bot **DOWN** (a restart never applies migrations).
Verified after: MAX 36, both columns present with the right types, index SQL exact,
`schema_migrations` row 36 stamped 01:08:56Z, tables still 46, `integrity_check` ok,
`foreign_key_check` still EXACTLY the two approved orphans (10642, 11892), leads 10715.

**It is not optional.** `_record`'s INSERT now names `variant`, and `nudge_variants.choose`
does `COALESCE(variant,'a')` — so on a schema-35 DB even `nudge --dry-run` raises. Code and
migration must land together.

**Registry versions 10, 11, 12 are ABSENT at both revisions** (33 migrations, max 36). That
is the pre-existing side-lineage gap of [[migration-version-collision]], not damage from
this deploy — check it at BOTH revisions before reporting a hole.

## What was turned on

- **12th cron line added** (11 → 12, prefix-sha proof that all 11 pre-existing lines are
  byte-identical): `15 9,14 * * 1-5 … cli nudge --execute >> cron.log 2>&1`. Crontab sha
  `0ba78a3b…f8d6f` → `63495d445812caaec8b8c1b086e72aa8db2bcd249730a5400e07bc61042ea1f7`.
  Both times are inside `in_window` (weekday, ET≥7, PT<17) and 5 h apart vs `MIN_GAP` 4 h.
  Caps confirmed in the shipped source: `MAX_NUDGES_PER_DAY=2`,
  `MAX_NUDGES_PER_TARGET_PER_DAY=1` — and `_sent_today` is keyed on **audience**, so it is
  2/day PER CHANNEL, not 2/day overall.
- **`enrich-orgs --grade gold --limit 25 --execute`** — the first corpus sweep of the
  `org_*` columns. See [[org-column-coverage-20260810]] for the before/after numbers.
- **`capability-seed --execute`** recorded **5** asks, all in **C01DGT9D11D (production,
  real colleagues)**. `available_since` is NULL on all 5 and `capability --execute` was NOT
  run, so none can produce a message yet — verified: `_capability_asks` skips any row whose
  `available_since` is None, and the seed path never sets it. **Seeding is safe; declaring
  is the act that messages people.**

## The nudge queue on the day it was armed

39 due, **25 suppressed `stale`, 14 eligible — every one in C01DGT9D11D (production).**
The first **7** are `card_unengaged` with `target_slack=''` ⇒ threaded reply, **nobody
@-mentioned**. Mentions start at eligible #7: `U04ASV42UJD` (4 subjects) and `U01E908206M`
(1). At 2/day that is roughly the first three days of channel-only nudges before any
individual is pinged — re-read eligible #0's `target_slack` before each forced run, the
guarantee is ordering-dependent and time-limited ([[nudge-queue-state-20260809]]).

**THE FIRST REAL CRON TICK PERMANENTLY BURNS THE 25 STALE SUBJECTS.** `run()` writes
`_record(state='suppressed', reason='stale')` for every permanent-suppression candidate it
walks past, and the uniqueness key retires them forever. Expected, irreversible, and it
happens Monday 09:15 PT — not a bug, but say it out loud before arming the cron.

`nudge --dry-run --force` proven inert again: DB mtime, size and `followup_nudges` count
(0) all unchanged across the run.

## Postflight fingerprints

Revision `d664548…4e3b8` (40 B, no trailing newline). PID 25636 uid 1001, cwd
`/home/grantwatch/grants_agent`, 53 venv maps, PID_COUNT 1. `.env` sha
`9b68bc18…c634` **unchanged**, 67 lines/33 keys, `sh -n .env` silent, and both
`RESEND_API_KEY` and `RESEND_FROM_EMAIL` non-empty in `/proc/25636/environ`
(ENVIRON_TOTAL 51). `run_bot.sh` `07773019…06bb` unchanged. `nudge-report` →
"No follow-ups have been delivered yet, so there is nothing to compare."
`remind --dry-run` → "remind: skip: nothing due". Disk 67%, 16 G.

**Rollback artifacts (700/600):** `~/backups/deploy-d664548-20260810T010259Z/` —
`grant_watch.db.vacuum` (25,051,136 B, sha
`56012a42e980f13d89cf6ef2fd2fe665612b428a28573415c70dffb62ca3b6c1`, COPY verified integrity
ok / schema 35 / 46 tables / leads 10715 / same 2 FK orphans),
`code_at_14221fc.tar.gz` (69,397 B, sha `44e154fd447752b0ccbf4df5078d966a2405a19e661117348d1edfe2746ac2bf`,
10 members, `gzip -t` OK), `env.bak` (== `9b68bc18…`), `crontab.bak` (== `0ba78a3b…`),
`deployed_revision.bak` (40 B). Plus `~/backups/crontab.bak.20260810T012015Z` taken by the
cron edit itself. Rollback = restore the tar, `rm` the 2 added files, remove the 12th cron
line, re-stamp 14221fc, restart. **A DB rollback is now NOT free** — unlike the last two
deploys this one wrote real data (org_* on 21 leads, 5 capability_asks rows), so restoring
the `.vacuum` copy would discard that work. `~/backups` is **229 M** and still has no
retention policy ([[disk-footprint-and-cruft]]).

Note the `.vacuum` sha is IDENTICAL to the one 14221fc took — the DB genuinely had no
writes between the two deploys, which is a cheap corroboration that nothing else was
mutating it.

Related: [[nudge-variant-ab-is-inert]] (the measurement this deploy shipped does not yet
measure anything), [[ssh-rate-limit-and-stdin-traps]].
