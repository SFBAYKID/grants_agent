---
name: deploy-3cf9df0-campaign-status
description: Deploy fe56807→3cf9df0 on 2026-08-09 (SUPERSEDED by 2239a18) — code-only, schema stayed 31, PID 14494; the rsync --no-perms lesson and the dirty-working-tree near-miss
metadata:
  type: project
---

**SUPERSEDED 2026-08-09T22:23:49Z — production is now `2239a18` at schema 32, listener PID 15679.
See [[deploy-2239a18-human-asserted]]. The record below is history; its rollback artifacts are
still on disk.**

**LIVE 2026-08-09T21:58:55Z.** Production moved `fe568077216c59fe7ec8dbe6197987e5753f6066` →
`3cf9df0fc4b53820eea237279a59a4218731132e` (5 commits). Ships the read-only Slack tool
`salesforce_campaign_status` (**TOOL_SCHEMAS 15 → 16**, measured both sides). Listener
**12836 → 14494**, outage ~2 s. NO migration, NO schema change (**31 before and after**),
`.env` and crontab untouched. All `verified`.

**Shape:** 11-path delta minus 5 `.claude/agent-memory/*` = **6 deployable** (4 mods + 2 adds:
`docs/status_log.md`, `tests/test_campaign_status.py`). Pinned `git archive 3cf9df0` artifact
(sha256 `20030a26…f199a`, 950 files / 143 dirs / 0 symlinks), `diff -r`-proven identical to a
pristine `git worktree` checkout (950/950), scp'd + sha-verified droplet-side, staged, then
droplet-local checksum rsync. Real run itemized **exactly 6 `>f` lines, 0 deletions**, second
dry-run empty. `find -cnewer` = exactly those 6; all 6 live sha256 == the pinned blobs.

**PINNING BY HASH WAS LOAD-BEARING, not ceremony.** Chase was still committing to the branch, and
the working tree at deploy time had **uncommitted edits to 2 of the 6 shipped files**
(`tool_schemas.py`, `tools.py`) plus an **in-progress `migrations.py`**. A working-tree deploy
would have shipped an unfinished migration to production. The `git archive <hash>` + `diff -r`
worktree proof made the drift structurally irrelevant.
**Corollary — verify against the ARTIFACT, not the repo.** My first "16 tools" check imported from
the dirty working tree and was therefore not evidence about the pinned commit. Re-ran it with
`cd /private/tmp && PYTHONPATH=<artifact_extract>` (neutral cwd, or the dirty repo shadows
PYTHONPATH via `''` on `sys.path`). Same family as the "0-files-compared PASS" trap in
[[deploy-mechanism]]: right check, wrong tree.

**ADD `--no-perms` TO THE DROPLET-LOCAL CHECKSUM RSYNC (new standing rule).** `git archive` +
`tar -x` under the droplet's umask 002 produced a staging tree at **664/775**, while the live tree
is also 664/775 — except `deploy_rsync.sh` at 755. The catch-all (see [[deploy-mechanism]]) caught
one unplanned line: `.f...p..... deploy_rsync.sh`, a permission-only change that would have added
**group-write to an executable** on the shared box — content sha identical, and git says `100755`,
so the bit came purely from my own extraction umask. Then `chmod -R go-w` on staging OVERCORRECTED:
it made staging 644/755 and rsync wanted to re-permission **~940 live files** 664→644. Neither
extreme is the intended delta. `--no-perms` is the precise tool — existing files keep their modes,
new files land at the source mode (644 here, owner grantwatch:grantwatch). Final combo:
**`rsync -cai --no-times --no-perms`**, no `--delete` (delta had zero deletions; previewed with
`-cain --delete` first).

**Postflight (all verified):** revision stamped full hash; `integrity_check` ok; `foreign_key_check`
EXACTLY the two known `source_observations` orphans (10642, 11892), no new ones; tables 42.
Listener uid 1001, cwd `/home/grantwatch/grants_agent`, argv `.venv/bin/python -u -m
grant_watch.slack.grant`, 53 `/proc/PID/maps` hits under the tenant `.venv`, PID_COUNT 1, stable at
0/30/60 s and again at 2 m. Exactly ONE boot: `bot.log` 915 → 917, one "Grant is listening" + one
"Bolt app is running", 0 tracebacks. `.env` sha `f4abd546…2a99` / 66 lines / 32 keys AND mtime
unchanged; crontab sha `575fbc7c…1a72` / 10 lines; `run_bot.sh` sha/mtime/mode untouched;
`grant_watch.db` mtime unchanged by the sync. `drip --dry-run` → `drip: skip: weekend` (rc 0),
`nudge --dry-run` → `nudge: skip: outside business hours` (rc 0); schema re-read 31 after both, so
the dry runs neither migrated nor wrote. Disk 67% / 17G free, unchanged.

**Tool-live proof and its LIMIT.** `salesforce_campaign_status` is in `TOOL_SCHEMAS` (16), input
schema `{name_or_link}`, imported under the tenant venv from the on-disk code whose hashes equal
the pinned blobs, in a process that started AFTER the sync. NOTE the `.pyc` mtimes (…697/…696)
PRE-DATE the process start (…735) — they were compiled by my pre-restart import smoke and reused by
the bot, so they are evidence the bytecode is post-sync, NOT independent evidence of the boot.
`needs-testing`: the tool has never been invoked from live Slack. First real call is the first real
test.

**Rollback artifacts (mode 600, retained):**
`~/backups/deploy-3cf9df0-20260809T215358Z/code_at_fe56807.tar.gz` (sha256 `110d1a05…4cf4`, 1049
members, no `.env`/`secrets`/db) + `.deployed_revision.bak` (`fe56807…`). Code-only rollback is
sufficient because nothing migrated. Staging dir and the transferred artifact were REMOVED after
verification (do not re-accumulate the `.deploy_staging` cruft noted in [[disk-footprint-and-cruft]]).
