---
name: deploy-fe56807-stage3
description: Stage-3 production deploy 90f0420→fe56807 on 2026-08-09 — schema 28→31, PID 1227→12836, ~4s outage; the rsync itemize-direction false PASS and the --no-times refinement
metadata:
  type: project
---

**LIVE 2026-08-09T21:28Z.** Production moved `90f042062f7fa8c6dff6901674b92a2f95ef390e` →
`fe568077216c59fe7ec8dbe6197987e5753f6066` (20 commits), schema **28 → 31**, listener PID
**1227 → 12836**. Outage ~4 s (pkill 21:28:07Z, listener up 4 s later). All `verified`.

**Shape:** pinned `git archive fe56807` artifact (sha256 `40ab18d8…3abc`, 947 files, 143 dirs,
0 symlinks, no `.env`/`secrets`/db/`.venv`/`.git`/`__pycache__`), proven `diff -r`-IDENTICAL to a
pristine `git worktree` checkout of the same hash (947/947 files compared), scp'd, sha-verified
droplet-side, extracted to `~/.deploy_staging/`, then droplet-local checksum rsync into the live
tree. **54 content transfers = 18 adds + 36 mods, 0 deletions**, second pass 0 (idempotent).
`find -cnewer` = exactly those 54; all 54 live sha256 == staging sha256.
Deployable delta = 55 tracked paths minus `.env.example` (the mandatory `.env.*` exclude skips it —
doc-only, deliberately not shipped, same as every prior deploy). 21 further paths were
`.claude/agent-memory/*`, excluded.

**Protected + unchanged, before AND after:** `.env` sha `f4abd546…2a99` / 66 lines / 32 keys;
crontab sha `575fbc7c…1a72` / 10 lines (NOT touched this stage); `grant_watch.db` sha
`abeef597…c195f` before migration; `run_bot.sh` sha `07773019…06bb` mtime 1784192756 mode 755;
`secrets/` intact; `.venv`, `bot.log`, `cron.log`, `backups/` never in the transfer set.

**Migrations 29/30/31 applied by an EXPLICIT one-shot writable connect while the bot was DOWN**
(`.venv/bin/python -c "from grant_watch import db; db.connect().close()"`), not by the restart —
[[deploy-mechanism]]'s rule that a bot restart does NOT migrate was assumed, not re-tested. Doing it
with no listener running means no concurrent writer during a data-mutating migration.
`migration_runner` is per-migration `BEGIN IMMEDIATE` + rollback-on-exception, FKs off during and
restored in a `finally`. Results: `integrity_check` ok; `foreign_key_check` EXACTLY the two known
`source_observations` orphans (10642, 11892), no new ones; tables 38 → **42**; migration 29's
provenance backfill hit its prediction EXACTLY — `page_verified` **19**, `linkedin_claimed` **36**,
NULL **26**, `vendor_licensed` **0**, total 81; `contact_provenance`/`do_not_call`/`vendor_person_id`
all present; `followup_nudges`, `crm_campaign_attempts`, `zoominfo_credit_periods`,
`zoominfo_credit_spends` all exist and are EMPTY.

**Postflight:** import smoke 0 failures over all 17 changed/new modules BEFORE the kill;
`MIGRATIONS` registry max 31 (versions still skip 10–12, the known side-lineage gap —
[[migration-version-collision]]). Listener uid 1001, cwd `/home/grantwatch/grants_agent`, argv
`.venv/bin/python -u -m grant_watch.slack.grant`, 53 `/proc/PID/maps` hits under the tenant `.venv`,
PID_COUNT 1, stable across 75 s / 76 s / 161 s / 196 s observations. Exactly ONE boot:
`bot.log` went 905 → 907 lines, 1 new "Bolt app is running", 0 tracebacks. `drip --dry-run` →
`drip: skip: weekend` (rc 0), `nudge --dry-run` → `nudge: skip: outside business hours` (rc 0) —
`nudge` is new in this commit, so its existence is itself proof the new code is live. Both use
`connect_readonly()`, and `cmd_nudge` sets `client = None` on a dry run, so no Slack call is even
constructible. Disk unchanged at 67% / 17G free.

**Write allowlist proved BEHAVIORALLY, not by string match:** `write_channel_allowed()` returns True
for `C01DGT9D11D` and `C0B02721MNK` only; False for an unlisted id, empty string, a truncated
prefix, and a lowercased copy. Nothing wider. `writer_enabled()` True (pre-existing, unchanged —
see [[campaign-writes-flag-armed-in-prod]]). `ZOOMINFO_MONTHLY_CREDITS=1000`. Both new vars confirmed
in `/proc/12836/environ`, so the restart is what made them live.

**Rollback artifacts (both retained, mode 600):**
`~/backups/stage1-preflight-20260809T210645Z/` — `grant_watch.db.pre-stage1` `63add322…fa6f` +
`code_at_90f0420.tar.gz` `62502a70…2140`; and a fresh
`~/backups/stage3-premigration-20260809T213000Z/grant_watch.db.pre-migration29`, which hashed
**byte-identical** to the Stage-1 copy — proof the live DB had not changed at all between 14:06 and
14:21 PT (its `-wal` was 2 days stale). Kept both deliberately; 25 MB against 17 G is nothing.
