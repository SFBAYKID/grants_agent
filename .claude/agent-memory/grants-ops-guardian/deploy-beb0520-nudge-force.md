---
name: deploy-beb0520-nudge-force
description: Deploy 70afa75→beb0520 on 2026-08-09 (CURRENT PROD) — code-only, schema stayed 32, PID 17737, ~1s outage; the full-tree rsync preview that would have shipped .claude memory, touched .codex, and DELETED run_bot.sh
metadata:
  type: project
---

**LIVE 2026-08-09T22:48:33Z.** Production moved `70afa75daa67487008ba32ecc167866025d42eba` →
`beb0520ceebcd5ed9aaeb52c4f7b2371099b34a0` (2 commits: one docs-only, one feature). Listener
**16804 → 17737**, outage **~1 s** (kill 22:48:32Z, new PID up 22:48:33Z). `.env` and crontab
untouched. All `verified`.

Ships `nudge --force`: an operator override that skips **only** `in_window`. Read the code, not the
commit message — `pacing_reason(..., force=True)` short-circuits exactly one branch
(`if not force and not in_window(now)`); the one-shot `followup_nudges` lookup, `suppress_reason`,
`MAX_NUDGES_PER_DAY=2`, `MAX_NUDGES_PER_TARGET_PER_DAY=1` and `MIN_GAP=4h` all still run, and the
one-shot + suppression checks happen BEFORE pacing is even consulted.

**NO MIGRATION — proven, not assumed.** `git diff --name-only 70afa75..beb0520` = exactly 4 paths
(`CLAUDE.md`, `grant_watch/cli.py`, `grant_watch/slack/nudges.py`, `tests/test_nudges.py`), all
**M**, zero adds/deletes, zero `.claude/` paths, and **no file matching `migrat`** anywhere in the
delta. Schema read **32** before and after.

## THE BIG ONE: a full-tree rsync preview here was NOT safe, and the preview is what caught it

Prior deploys used a full-tree staging→live rsync with an exclude list. Run that way today, the
`-cain --delete` preview showed **83 lines**, and three of them were disqualifying:

1. **`*deleting run_bot.sh`.** In git the launcher lives at **`deploy/run_bot.sh`**; the droplet's
   live keepalive sits at the **repo root** as `run_bot.sh` and is what the `*/5` cron invokes. A
   full-tree sync with `--delete` would have **deleted the thing that restarts the bot** — the bot
   would die at the next restart and never come back. Omitting `--delete` (standing practice) is the
   only reason this has never fired. Do not ever add `--delete` to a full-tree run.
2. **61 `>f+++` adds under `.claude/agent-memory/**`** — guardian + critic memory (rollback paths,
   PIDs, verification recipes) shipped to the tenant. Confirms [[roster-deploy-4c6a543]]: the droplet
   holds only `.claude/agents/*.md` + 3 architectural-critic files (5 files total); the
   `grants-ops-guardian/` subdir has never existed there. The exclude is load-bearing, not cosmetic.
3. **`>fcsT .codex/config.toml`** — the OTHER toolchain's config, tracked in git and **drifted** on
   the droplet (live sha `b839550d…4a99`). Not in this delta. Shipping it would have silently
   reverted Codex's config. See [[codex-parallel-writer-forensics]].

**So: for a small delta, use `--files-from` and stop reasoning about excludes.**
`rsync -cai --no-times --no-perms --files-from=<list of exactly the delta paths> STAGE/ LIVE/`
with a fail-closed guard (`changed==4`, `adds==0`, `deletions==0`, `other_lines==0`) before the real
run. Blast radius then equals the stated delta *by construction* rather than by an exclude list you
have to get right. Preview 4, real 4, second pass 0 (idempotent), `find -cnewer` exactly 4.

**Both directions of the sha ladder were checked, which is what makes "clean forward move" real:**
the 4 live files BEFORE the sync hashed identical to `git show 70afa75:<path>` (no prior drift), and
AFTER hashed identical to `git show beb0520:<path>`. Compared programmatically with `assert n==4`
(the 0-files-compared PASS trap).

**Artifact:** pinned `git archive beb0520` (sha256 `5cb66b75…dc5e`, 12,728,320 B, 1100 members /
957 files / 144 dirs / 0 symlinks), `diff -r --exclude=.git` rc=0 / 0 lines against a pristine
`git worktree` of the same hash. NOTE: a python walk comparing the two trees reported "MISMATCH"
purely because a **worktree's `.git` is a FILE, not a directory**, so a `dn`-only exclude misses it —
957/957 content hashes matched. Exclude `.git` by name for both files and dirs.

**Import smoke BEFORE the kill** (0 failures): `inspect.signature` showed `force` on `cmd_nudge`,
`nudges.run` and `nudges.pacing_reason`, and `nudge --help` really advertises `--force` via a
subprocess — behavioral proof the new bytecode was loaded, not a grep of the source.

**Postflight (all verified):** revision stamped full hash (40 B); exactly ONE boot in the new log
region (1 "Grant is listening" + 1 "Bolt app is running", **0** tracebacks at 60 s); PID 17737 uid
1001, cwd `/home/grantwatch/grants_agent`, 53 venv maps, PID_COUNT 1, stable at 60 s and 204 s;
`integrity_check` ok; `foreign_key_check` EXACTLY the two known orphans (10642, 11892); tables 42;
`.env` sha `f4abd546…2a99` / 66 lines / 32 keys / mtime 1786309948 unchanged; crontab sha
`575fbc7c…1a72` / 10 lines; `run_bot.sh` sha `07773019…06bb`. Disk 67% / 16–17 G free.

**Rollback artifact (mode 700 dir, 600 files, retained):**
`~/backups/deploy-beb0520-20260809T224533Z/` — `code_at_70afa75.tar.gz` (27,994 B, sha256
`a7e36fed…9d17f`, 4 members, `gzip -t` OK, 0 forbidden paths) + `.deployed_revision.bak`.
No DB backup taken or needed: nothing migrates and the deploy writes no rows. **`ls -l` hides
`.deployed_revision.bak`** — use `ls -la` or you will think the backup is incomplete.

My own staging dir + tar were removed afterward; the ~10 pre-existing `.deploy_staging/*` dirs are
the known cruft from [[disk-footprint-and-cruft]], untouched.
