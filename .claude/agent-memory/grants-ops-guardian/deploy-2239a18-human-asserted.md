---
name: deploy-2239a18-human-asserted
description: Deploy 3cf9df0→2239a18 on 2026-08-09 (CURRENT PROD) — migration 32 schema 31→32, PID 15679, ~1s outage; the tool-count claim was 18 but is really 17, and the backfill's step-3 was a no-op
metadata:
  type: project
---

**LIVE 2026-08-09T22:23:49Z.** Production moved `3cf9df0fc4b53820eea237279a59a4218731132e` →
`2239a1833edd42fc499420600b9f9fce6982f9e3` (3 commits). Ships `record_contact_fact` — a rep can
give Grant a phone/email/name and have it RECORDED (own contact row, `contact_status`/provenance
`human_asserted`, attributed to the Slack user + dated) instead of refused. Schema **31 → 32**,
listener **14494 → 15679**, outage **~1 s**. `.env` and crontab untouched. All `verified`.

**Shape:** 25-path delta minus 4 `.claude/agent-memory/*` = **21 deployable** (17 mods + 4 adds:
`grant_watch/db_contacts.py`, `grant_watch/migrations_human_facts.py`,
`grant_watch/slack/research_tools.py`, `tests/test_human_asserted_contacts.py`). Pinned
`git archive 2239a18` artifact (sha256 `cfa5deb0…a1ff`, 1099 members / 956 files / 144 dirs /
0 symlinks), `diff -r`-proven identical to a pristine `git worktree` checkout (**956/956 files
compared**, 0 diffs; the worktree's extra file is git's own `.git` pointer, which `git archive`
correctly omits). Droplet-local checksum rsync `-cai --no-times --no-perms`, no `--delete`
(previewed `-cain --delete` → 0 deletions). Real run itemized **exactly 21 `>f` lines, 4 of them
`>f+++++++++`**, catch-all EMPTY, second dry-run 0 lines. `find -cnewer` = exactly those 21; all
21 live sha256 == the pinned blobs (compared programmatically, 21/21).

**A MISSED ADD WOULD HAVE BEEN FATAL, not subtle.** `db.py` shrank 110 lines because
`save_contact` / `save_vendor_contact` / `save_linkedin_contact` / `mark_contact_not_found` moved
to the new `db_contacts.py` and are RE-EXPORTED from `db.py`. A partial deploy = ImportError on
boot and a dead bot. The import smoke asserted all four re-exports PLUS the new
`save_human_asserted_contact` on both `db` and `db_contacts` — asserting the NEW symbol is what
proves the file on disk is the new one.

**THE STATED TOOL COUNT WAS WRONG — 17, not 18.** The task predicted `TOOL_SCHEMAS` 16 → **18**.
Measured three independent ways, it is 16 → **17**: the deployed 3cf9df0 code imported live on the
droplet = 16; the pinned artifact imported at `cd /private/tmp` with `PYTHONPATH=<extract>` = 17;
and a set-diff of the two commits' registries = `ADDED ['record_contact_fact']`, `REMOVED []`.
There is exactly ONE registry (`tool_schemas.TOOL_SCHEMAS`, re-exported by `tools.py`, consumed by
`conversation.py:812`) — no second dispatch table to make up the difference. The thing that
mattered (`record_contact_fact` present) was true, so this was a reporting error, not a deploy
fault. Same family as the `.env` line-count lesson in [[env-zoominfo-20260809]]: **a stated count
is a DERIVED claim; measure it, report the corrected number, and never adjust the measurement to
match the prediction.** Note also `grep -c '^        "name":'` on the raw file gives 13→14 —
indentation-sensitive and NOT the registry count; import it instead.

**MIGRATION 32 — the predicted SPLIT was right, the predicted MECHANISM was not.** The task
expected the 2 vendor rows to be re-derived from `contact_status` in the migration's third step.
They were not: pre-migration `contact_provenance` was ALREADY complete for every row carrying a
contact fact (`page_verified` 19, `linkedin_claimed` 36, `vendor_licensed` 2, NULL 26), so all 57
were picked up by step 2 (the copy), and step 3 could only see the 26 `not_found` rows, whose
status maps to nothing. **Step 3 updated ZERO rows in production.** This was deduced from the
measured pre-state BEFORE running it, and the post-state confirms it. Consequence worth keeping:
the migration's own docstring rationale for step 3 ("rows created between migration 29 and this
one have BOTH columns NULL") describes a class that does not exist in prod — `save_vendor_contact`
evidently does set `contact_provenance`.

Result, matching the prediction EXACTLY: `provenance` = **page_verified 19, linkedin_claimed 36,
vendor_licensed 2, NULL 26, human_asserted 0** (total 83). A cross-tab confirmed each provenance
maps to exactly its expected `contact_status` and nothing else. `contacts` columns **14 → 17**
(`provenance`, `asserted_by_slack_user`, `asserted_at` all present); legacy `contact_provenance`
UNCHANGED (frozen, nothing reads it); tables stayed **42** (columns only, no new table).

**Applied with the bot DOWN** via the explicit one-shot writable connect
(`.venv/bin/python -c "from grant_watch import db; db.connect().close()"`) — same as
[[deploy-fe56807-stage3]]; a restart does NOT migrate ([[deploy-mechanism]]). Kill → migrate →
restart ran inside ONE ssh session, so the whole outage was ~1 s and no concurrent writer existed
during the data-mutating step.

**Postflight (all verified):** revision stamped full hash; `integrity_check` ok; `foreign_key_check`
EXACTLY the two known `source_observations` orphans (10642, 11892), no new ones. Listener uid 1001,
cwd `/home/grantwatch/grants_agent`, argv `.venv/bin/python -u -m grant_watch.slack.grant`, 53
`/proc/PID/maps` hits under the tenant `.venv`, PID_COUNT 1, same PID at 60 s (etimes 110, then 131).
Exactly ONE boot: `bot.log` 920 → 922, one "Grant is listening" + one "Bolt app is running",
0 tracebacks. `.env` sha `f4abd546…2a99` / 66 lines / 32 keys AND mtime unchanged; crontab sha
`575fbc7c…1a72` / 10 lines; `run_bot.sh` sha `07773019…06bb` / mode 755 / mtime untouched;
`secrets/` 1 file intact; `grant_watch.db` mtime unchanged by the sync. `drip --dry-run` →
`drip: skip: weekend` (rc 0), `nudge --dry-run` → `nudge: skip: outside business hours` (rc 0);
schema re-read **32** after both, so the dry runs neither migrated nor wrote. Disk 67% / 17G free,
unchanged.

**Rollback artifacts (mode 700 dir, mode 600 files, retained):**
`~/backups/deploy-2239a18-20260809T221928Z/` — `grant_watch.db.pre32` (24,985,600 B, sha256
`37e50bce…6e81`, `VACUUM INTO` from a read-only connection, source proven byte-unchanged; the COPY
verified: integrity ok, schema 31, 42 tables, the same two FK orphans, contacts 83, leads 10,715) +
`code_at_3cf9df0.tar.gz` (4,132,443 B, sha256 `862bc153…4126`, 1050 members, `gzip -t` OK, audited
to contain ZERO `.env`/`secrets`/`*.db`/`.venv`/`.git`/`__pycache__`/logs) + `.deployed_revision.bak`.
**Migration 32 mutates data, so a DB rollback is restore-from-backup**, and a code rollback must
also `rm` the 4 added files and purge `__pycache__` or `db.connect()` silently re-applies 32.

**CHASE COMMITTED TWICE DURING THE DEPLOY** — local HEAD went `2239a18` → `9c4acf7` → `d176206`
("a shifted selection refuses instead of skipping an organization"; "repeated Slack refusals stop
draining the lead pool") while the sync was running. Both are clean descendants, so production is
now 2 commits BEHIND local, deliberately. This is the second consecutive deploy where pinning by
hash was load-bearing rather than ceremonial ([[deploy-3cf9df0-campaign-status]] caught a dirty
tree carrying an unfinished migration). Treat "the working tree is clean, I checked" as true only
at the instant it was said.

`needs-testing`: `record_contact_fact` has never been invoked from live Slack — the first real call
is the first real test. Nothing has yet written a `human_asserted` row (count is 0).

**Pre-existing cruft, deliberately NOT touched:** `~/.deploy_staging` still holds **12 stale dirs
from 2026-07-15, 274 MB** (my own staging dir was removed; the parent `rmdir` failed only because of
those). Same set flagged in [[disk-footprint-and-cruft]] — needs per-path approval, and at 17G free
it is not urgent.
