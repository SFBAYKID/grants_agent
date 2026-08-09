---
name: deploy-70afa75-refusal-ceiling
description: Deploy 2239a18→70afa75 on 2026-08-09 (CURRENT PROD) — code-only, schema stayed 32, PID 16804, 2s outage; the mmin-20 protected-path audit over-reached into the PREVIOUS deploy
metadata:
  type: project
---

**LIVE 2026-08-09T22:36:49Z.** Production moved `2239a1833edd42fc499420600b9f9fce6982f9e3` →
`70afa75daa67487008ba32ecc167866025d42eba` (3 commits). Listener **15679 → 16804**, outage **2 s**.
`.env` and crontab untouched. All `verified`.

Ships: campaign batch 2+ passes back batch 1's organization count and refuses on mismatch
(`CampaignTargetRequest.expected_total_organizations`, default 0 = "first batch"); a ceiling of
**3** Slack-refused deliveries per day per channel (`drip.MAX_REJECTIONS_PER_DAY = 3`,
`db.rejections_today`) so the drip HOLDS instead of quarantining a gold lead every 30 min; plus a
migration comment correction.

**NO MIGRATION — proven three ways before touching prod, not assumed from the commit message.**
`migrations.py` and `migrations_rich.py` byte-IDENTICAL across the delta; `migrations_human_facts.py`
differs but is **AST-identical** (`ast.dump` equal, 6761 chars both sides) — a pure comment rewrite;
max registered version **32** in both commits. Schema read 32 before, after the sync, and again
after both dry runs. See [[deploy-5f09200-fallback-routing]] for the same AST-diff discipline.

**Shape:** 12-path delta minus 3 `.claude/agent-memory/*` = **9 deployable, ALL modifications,
zero adds, zero deletions.** Pinned `git archive 70afa75` artifact (sha256 `6fa6a0ba…f451`,
1100 members / 957 files / 143 dirs / 0 symlinks), `diff -r`-proven identical to a pristine
`git worktree` checkout (**957/957 files compared**, 0 diffs). Artifact sha verified again after
upload. Droplet-local `rsync -cai --no-times --no-perms`, no `--delete` (`-cain --delete` preview
= 0 deletions). Real run itemized **exactly 9 `>f` lines, 0 `>f+++`**, catch-all EMPTY, and
TOTAL_PREVIEW_LINES was 9 — not even a `.d..t` dir touch, which is what `--no-times` buys.
`find -cnewer` = exactly those 9; second dry-run 0 lines; all 9 live sha256 == the pinned blobs,
compared **programmatically with an `assert len==9` guard** (the 0-files-compared PASS trap).

**`db.py` re-exports `rejections_today` from `db_engagement`** (both `import` block and `__all__`),
so a half-applied sync = ImportError on boot and a dead bot — same fatal-if-missed shape as
[[deploy-2239a18-human-asserted]]. Import smoke asserted the symbol on BOTH modules, in `db.__all__`,
`drip.MAX_REJECTIONS_PER_DAY == 3`, and the new dataclass field + its default — run BEFORE the kill.

**THE `-mmin -20` PROTECTED-PATH AUDIT PRINTED 29 PATHS, NOT 9 — AND IT WAS FINE.** The previous
deploy (2239a18) landed at **22:21:07Z**, ~18 minutes earlier, so a 20-minute mtime window swept in
its whole file set (`db_contacts.py`, `migrations.py`, `conversation.py`, `tools.py`, `conftest.py`,
CLAUDE.md…). Resolved by printing actual mtimes: two clean clusters, 22:21:07Z (previous deploy) and
22:36:48–49Z (my 9 + the revision stamp). **Lesson: on a same-day second deploy, size the audit
window to THIS deploy or use the `-cnewer` marker, which is anchored to the sync itself and stayed
authoritative at exactly 9.** Do not report such an audit "clean" without explaining every extra row.

**Postflight (all verified):** revision stamped full hash; `integrity_check` ok; `foreign_key_check`
EXACTLY the two known `source_observations` orphans (10642, 11892); tables 42. Listener uid 1001,
cwd `/home/grantwatch/grants_agent`, argv `.venv/bin/python -u -m grant_watch.slack.grant`, 53
`/proc/PID/maps` hits under the tenant `.venv`, PID_COUNT 1, same PID at 60 s. Exactly ONE boot: one
"Grant is listening" + one "Bolt app is running", 0 tracebacks. `.env` sha `f4abd546…2a99` / 66 lines
/ 32 keys AND mtime unchanged; crontab sha `575fbc7c…1a72` / 10 lines; `run_bot.sh` sha `07773019…06bb`;
`secrets/` 1 file. `drip --dry-run` → `drip: skip: weekend` (rc 0), `nudge --dry-run` →
`nudge: skip: outside business hours` (rc 0); **DB mtime byte-identical across both dry runs** and
schema still 32 — inert proven by mtime, not just by exit code. Disk 67% / 16G free.

**Rollback artifacts (mode 700 dir, 600 files, retained):**
`~/backups/deploy-70afa75-20260809T223557Z/` — `code_at_2239a18.tar.gz` (51,360 B, sha256
`b8aaf26c…86a1`, 9 members, `gzip -t` OK, audited 0 forbidden paths) + `.deployed_revision.bak`.
**No DB backup taken and none needed** — nothing migrates and the deploy writes no rows; rollback is
re-rsync the 9 files (all modifications, so no `rm` step) + purge `__pycache__` + restamp + restart.

Own staging dir and artifact tar removed after the run; the **12 pre-existing stale
`.deploy_staging` dirs from 2026-07-15 remain untouched** ([[disk-footprint-and-cruft]]).

`needs-testing`: neither shipped behavior has fired in production — no batch-2 campaign has run
against the new count check, and no channel has hit 3 Slack refusals in a day.
