---
name: deploy-mechanism
description: How code reaches the droplet — NOT git; file-copy deploy with .deployed_revision + broken pip wrapper
metadata:
  type: project
---

The droplet checkout is NOT a git working tree — `~/grants_agent` has no `.git` and `git` commands fail there. Do not assume `git pull` works to deploy.

**A BOT RESTART DOES NOT APPLY SQLite MIGRATIONS (verified 2026-07-20).** The bot entrypoint
`grant_watch.slack.grant:main()` does `load_dotenv → create_app → sweep_orphaned_spinners (Slack API
ONLY, no DB) → SocketModeHandler.start()`; there is NO module-level or startup `db.connect()`. The
MIGRATING `db.connect()` (which runs `apply_migrations`) is called only INSIDE Slack event handlers and
by fresh `cli drip`/`cli poll` processes. So a healthy restart with "⚡️ Bolt app is running!" can leave a
new migration PENDING (`schema_migrations` MAX unchanged) — exactly what happened deploying aa09dca's
migration 13 (code on disk, MAX still 12 after restart). It is self-healing/safe (connect() applies the
migration BEFORE any dependent insert in that process, so e.g. a platinum/rfp post can't hit the old
narrow CHECK), but to VERIFY a migration deploy you must either trigger a DB event or run a one-shot
migrating connect: `cd ~/grants_agent && .venv/bin/python -c "from grant_watch import db; db.connect().close()"`
(that WRITES — needs per-run authorization). NEVER report a migration "applied" just because the bot
restarted cleanly — read `schema_migrations` MAX + the actual schema. Also note Chase's manual deploys may
skip stamping `.deployed_revision` (it read stale `ba0a7b7` while aa09dca code was actually on disk) —
confirm the CODE on disk (grep a known new symbol), not just the revision file. See [[migration-version-collision]].

**Why:** Deploys are done by copying the tree from the laptop, not by pulling. Evidence: a `.deployed_revision` file at repo root holds the deployed commit hash (was `3f35a26974...` = commit 3f35a26 on 2026-07-14); the prior deploy left a backup `~/.grants_agent.previous.pre-3f35a269` (tree renamed to `.previous.pre-<oldhash>` before laying down the new one). `.env`, `.venv`, `grant_watch.db*`, and logs are preserved across deploys (they are not in git and differ from the laptop).

**How to apply:**
- To deploy a new commit, do NOT `git pull` on the droplet. Either use Chase's laptop-side deploy tooling, or (if approved) surgically `scp` only the files changed between `.deployed_revision` and the target — computed locally with `git diff --name-status <deployed>..<target>` — while preserving `.env`/`.venv`/`grant_watch.db`. Then update `.deployed_revision` to match, to keep the deploy state coherent.
- Committed `deploy/` dir only contains `run_bot.sh` (the keepalive launcher) — it is NOT a deploy script.
- **`.venv/bin/pip` is BROKEN** (wrong shebang / relocated venv → "cannot execute: required file not found"). Use `.venv/bin/python -m pip …` for all installs; that works (pip 24.0).
- When the repo state on the droplet does not match the instructed deploy method, STOP and confirm with Chase before improvising — a full rsync would clobber the divergent prod `.env` and live SQLite db. See [[tenant-and-layout]].

**Proven full-tree rsync recipe (2026-07-16: 3d653c6 → 25513bc; re-proven 2026-07-17: 25513bc → 9db96d0, 9db96d0 → 36d2470, 36d2470 → 6ea70f2, 6ea70f2 → c714b01, and c714b01 → 50acadd, and 2026-07-17 ed261ff → e6df182 = 14 files [15 delta minus `.env.example`, which the `.env.*` exclude correctly skips], zero deletions each time; Chase-approved, all verified):**

**2026-07-22 15263d2 → 264b0e2 (8-file, all verified) — code + a 2-key `.env` append, no migration:**
Second add-shaped deploy; the f4d6237→15263d2 entry below is the template and it held exactly.
`git diff --name-status` listed 13 paths, 5 were `.claude/agent-memory/*` (excluded) ⇒ deployable
delta = 8: 6 mods `<fcst....` (CLAUDE.md, db_engagement.py, presentation.py, slack/drip.py,
tests/test_drip.py, tests/test_salesforce_contact_records.py) + **2 ADDS `<f+++++++`**
(`grant_watch/territory.py`, `tests/test_territory.py`, both verified ABSENT beforehand). `-cain
--delete` preview = 0 deleting lines ⇒ real run with plain `-cai`. `find -cnewer` = exactly the 8;
all 8 remote sha256 == the 264b0e2 blobs. `.env`(1784571508)/`run_bot.sh`(1784192756)/
`grant_watch.db`(1784729302) mtimes unchanged by the rsync. Restart: OLDPID 515819 (== the PID this
file recorded for the 15263d2 deploy ⇒ no out-of-band restart) dead in 1s → single NEWPID 1859872,
Bolt pair, NO_TRACEBACK, PID_COUNT=1. crontab still 4 lines (NOT touched — the new slot logic is
app-side); schema_migrations MAX still 13.
- **Import smoke should assert REMOVED symbols too, not just new ones.** This commit deleted
  `drip.POST_PROBABILITY` and `drip.DAILY_AIM`; asserting `not hasattr(drip, …)` proves the file on
  disk is the NEW one, which a "does the new function exist" check alone cannot (a half-applied or
  stale-cached file could satisfy both). Cheap, and it caught nothing here only because the deploy
  was clean.
- **Backup shape used (reusable):** DB as a set (`.db`/`-wal`/`-shm` → `~/grant_watch.db.bak.<UTC>`)
  + `PRAGMA integrity_check` on the COPY (not the live file) to prove the backup is restorable, not
  merely present + `cp` of `.deployed_revision` → `~/.deployed_revision.bak.<UTC>` + a ~30KB
  `tar.gz` of just the files about to be overwritten. Git is the real rollback source for tracked
  files, so rolling back = re-rsync the old commit AND `rm` the two added files.
- **DISK IS AT 97% (46G/48G used, ~1.6G free) on the shared droplet root as of 2026-07-22.** The
  ~28MB backup fit easily, but this is droplet-wide (`/dev/vda1 /`), NOT just the grants tenant, so
  the guardian cannot fix it — it is Chase's admin call. Flag it before any large operation.

**2026-07-21 f4d6237 → 15263d2 (4-file, all verified) — FIRST deploy that ADDS new files:**
Every prior entry here was "ALL modifications, zero add/delete", so this is the shape to copy when a
commit introduces modules. `db.py` crossed the 1000-line cap and was split, so the delta was 2 mods
(`grant_watch/db.py` 989→880 lines, `tests/test_rfp.py`) + **2 ADDS** (`grant_watch/db_common.py` 56,
`grant_watch/db_engagement.py` 183). `git diff --name-status` listed 9 paths but 5 were
`.claude/agent-memory/*` (excluded), so deployable delta = 4. In `-cain` itemize an ADD shows as
**`<f+++++++`** (vs `<fcst....` for a modification) — confirm one `<f+++++++` per new file or the add
silently didn't ship. **A missed add here is fatal, not subtle:** `db.py` now re-exports `_now`,
`_LEAD_EVENT_SELECT`, `_CRM_CONTEXT_SELECT` FROM `db_common`, so a partial deploy = ImportError on boot
and a dead bot. Verified the new modules were ABSENT beforehand (proving they're real adds).
**Dropped `--delete` for this run:** the delta had zero deletions, so `--delete` was pure risk. Ran a
`-cain --delete` **delete-preview** first purely to inspect (0 deleting lines), then the real run with
plain `-cai`. Keep that pattern: preview deletions, then omit `--delete` when the delta has none.
IMPORT_OK before kill covered db_common + db_engagement + db explicitly (plus grant/scoring/drip/
rfp_parse) AND asserted `db.py` still exposes the re-exported names + the new `_adopt_drifted_lead`.
`find -cnewer` = exactly the 4; all 4 remote sha256 == local blobs; 2nd dry-run fully idempotent.
`.env`(1784571508)/`run_bot.sh`(1784192756)/`grant_watch.db`(1784576126) mtimes identical before AND
after restart. Restart: OLDPID 4148415 dead in 2s → single NEWPID 515819, Bolt pair present,
IMPORT_ERROR_FOUND=NO, NO_TRACEBACK, PID_COUNT=1. crontab still 4 lines; schema_migrations MAX still 13
(commit adds no migration). Fresh target-specific helpers written (`deploy_rsync_15263d2.sh`,
`restart_verify_15263d2.sh`) — never reuse target-specific stamp/verify across deploys.

**2026-07-19 190b097 → ba0a7b7 (6-file, all verified) — routine clean forward + drip-cron realign:**
CODE-ONLY (2 commits: f7dfddc "RFPs Silver not Gold" + ba0a7b7 "drip window opens 7am ET"). Live
`.deployed_revision` read first = 190b097 (matched task; no drift). Ancestry clean. `git diff
--name-status 190b097..ba0a7b7` = exactly 6 tracked files, ALL modifications, zero add/delete: 3 source
(scoring.py, slack/drip.py, slack/intent_router.py) + 3 tests (test_drip, test_rfp, test_rfp_aggregator).
Working tree clean at HEAD except the 4 `.claude/agent-memory/*` files (excluded). `-cain` dry-run = the
6 as `<fcst....` + 3 benign `.d..t....` dir touches, ZERO deletions; post-run dry-run FULLY EMPTY
(idempotent, even dir touches gone). `find -cnewer` = the 6 (+ live cron.log, excluded); all 6 remote
sha256 == ba0a7b7 blobs. `.env`(1784436291)/`run_bot.sh`(1784192756) mtimes UNCHANGED before+after;
db(1784481352) excluded. IMPORT_OK (grant+scoring+drip+intent_router) BEFORE kill; `.deployed_revision`
stamped full hash. Restart (pkill + `nohup bash run_bot.sh`): OLDPID 755817 (== the PID memory recorded
for the 190b097 deploy → confirms no out-of-band restart) dead → new single PID 1989352, "Grant is
listening (Socket Mode)…" + "⚡️ Bolt app is running!", NO_TRACEBACK, PID_COUNT=1 stable. THEN realigned
drip crontab 5-17→4-17 (see [[tenant-and-layout]] for the fail-closed recipe + the pipefail/diff gotcha).
Reused persisted target-agnostic helpers deploy_rsync.sh/set_marker.sh/restart_verify.sh/final_check.sh;
wrote fresh stamp_ba0a7b7.sh + verify_ba0a7b7.sh (never reuse target-specific stamp/verify across deploys).

**2026-07-19 eabf6e5 → 190b097 (4-file, all verified) — routine clean forward, nothing surprising:**
CODE-ONLY grading change (usaspending.py captures 'Base Obligation Date' as award date; scoring.py adds
gold-fresh/silver-older award split) — changes FUTURE grading only, writes nothing itself. Live
`.deployed_revision` read first = eabf6e5 (matched memory + task; no drift). Ancestry clean (eabf6e5 IS
ancestor of 190b097). `git diff --name-status eabf6e5..190b097` = exactly 4 tracked files, ALL
modifications, zero add/delete: 2 source (scoring.py, sources/usaspending.py) + 2 tests (test_scoring,
test_sources). `-cain` dry-run = the 4 as `<fcst....` + 3 benign `.d..t....` dir touches (grant_watch/,
grant_watch/sources/, tests/), ZERO deletions; 2nd dry-run after real run FULLY EMPTY (idempotent).
`find -cnewer` = exactly the 4 (no stray/live files this run); all 4 remote sha256 == local 190b097 blobs.
`.env`(1784436291)/`run_bot.sh`(1784192756) mtimes UNCHANGED before AND after restart; `grant_watch.db`
(1784481352) excluded. IMPORT_OK (grant + scoring + sources.usaspending) BEFORE kill; `.deployed_revision`
stamped full hash. Restart (pkill + `nohup bash run_bot.sh`): OLDPID 372564 dead → new single PID 755817,
"Grant is listening (Socket Mode)…" + "⚡️ Bolt app is running!", NO_TRACEBACK, PID_COUNT=1 stable after
20s. crontab still 4 lines (3 active + disabled salesforce-followups comment). No DB/migration/.env/poll
touched. NOTE: stale scratchpad helpers from the PRIOR deploy (stamp.sh, verify_after.sh, local/remote_sha
.txt) still target eabf6e5's 10-file list — do NOT reuse target-specific helpers across deploys; only
deploy_rsync.sh / set_marker.sh / restart_verify.sh / final_check.sh are target-agnostic, write fresh
stamp+verify per target.

**2026-07-19 170abba → eabf6e5 (10-file, all verified) — routine clean forward, nothing surprising:**
CODE-ONLY honesty/correctness bug sweep ("fix: honesty + correctness bugs from RFP/export bug sweep").
Live `.deployed_revision` read first = 170abba (matched memory; no out-of-band drift). Ancestry clean
(170abba IS ancestor of eabf6e5). `git diff --name-status 170abba..eabf6e5` = exactly 10 tracked files,
ALL modifications, zero add/delete: 6 source (enrich/finder.py, enrich/salesforce_contact_records.py,
slack/search.py, slack/search_presentation.py, sources/rfp_aggregator.py, sources/rfp_parse.py) + 4 tests
(test_enrich, test_rfp_aggregator, test_salesforce_contact_records, test_search). `-cain` dry-run = the 10
as `<fcst....` + benign `.d..t....` dir touches (`./`, enrich/, slack/, sources/, tests/), ZERO deletions;
2nd dry-run after real run was FULLY EMPTY (idempotent). `find -cnewer` = exactly the 10; all 10 remote
sha256 == local eabf6e5 blobs. `.env`(epoch 1784436291)/`run_bot.sh`(epoch 1784192756) mtimes UNTOUCHED
before AND after restart; DB excluded. IMPORT_OK (grant + all 6 changed modules) BEFORE kill;
`.deployed_revision` stamped full hash. Restart (pkill + `nohup bash run_bot.sh`): OLDPID 1830839 dead →
new single PID 372564, "Grant is listening (Socket Mode)…" + "⚡️ Bolt app is running!", NO_TRACEBACK,
PID_COUNT=1 stable after 20s. crontab still 4 lines (3 active + disabled salesforce-followups comment).
No DB/migration/.env touched (task was explicit code-only). GOTCHA (cost one round-trip): a plain `ssh
… -n 'bash -s' <<'HEREDOC'` runs NOTHING — `-n` points ssh stdin at /dev/null, so the remote `bash -s`
gets no script; drop `-n` on the ssh that IS the heredoc/`bash -s` stdin consumer (inverse of the
existing add-`-n` note for ssh that doesn't need stdin).

**2026-07-19 c3d3ea7 → 170abba (4-file, all verified) — routine clean forward, nothing surprising:**
via `deploy_rsync.sh` (bash). Live `.deployed_revision` read first = c3d3ea7 (matched memory; no
out-of-band drift this time). `git diff --name-status c3d3ea7..170abba` = exactly 4 tracked files, ALL
modifications, zero add/delete (slack/conversation.py, slack/search.py, slack/tools.py, tests/test_search.py
— "on-demand open-RFP export via open_only search filter"). `-cain` dry-run = the 4 as `<fcst....` + benign
`.d..t....` dir touches (`./`, `grant_watch/slack/`, `tests/`), ZERO deletions. `find -cnewer` = exactly the
4; all 4 remote sha256 == local. `.env`(07-18T21:44:51)/`run_bot.sh`(07-16T02:05) mtimes UNTOUCHED (DB mtime
advanced = live tenant writes, `*.db` excluded). IMPORT_OK (grant + slack.search/conversation/tools) BEFORE
kill; `.deployed_revision` stamped full hash. Restart (pkill + `nohup bash run_bot.sh`): OLDPID 3926720 dead
→ new single PID 1830839, "⚡️ Bolt app is running!", NO_TRACEBACK, PID stable. RFP_DISCOVERY_ENABLED already
==1 (append step was a no-op), crontab still 4 lines.

**2026-07-19 d9713d9 → c3d3ea7 (7-file, all verified) — droplet was AHEAD of the task's stated base:**
routine clean forward deploy via `deploy_rsync.sh` (bash). Task said droplet was at 194d364 with a 2-commit
delta; live `.deployed_revision` was actually **d9713d9** — some deploy advanced 194d364→d9713d9 OUTSIDE
guardian memory (not recorded here; the prior memory entry ends at the d11d5db→194d364 deploy). Measured
from the REAL base, `git diff --name-status d9713d9..c3d3ea7` = exactly the 7 tracked files the task listed
(cli.py, slack/drip.py, sources/__init__.py + NEW sources/rfp_aggregator.py, tests/test_drip.py, NEW
tests/test_rfp_aggregator.py, NEW tests/fixtures/rfp/starbridge_physical_security.md). The two "extra"
files that appear in 194d364..c3d3ea7 (enrich/salesforce_campaigns.py + slack/grant.py) are byte-IDENTICAL
at d9713d9 and c3d3ea7 — they landed in the unrecorded d9713d9 deploy, so already correct on the droplet;
checksum rsync correctly skipped them. Ancestry checked clean-forward first (194d364 IS ancestor of
c3d3ea7). `-cain` dry-run = the 7 as `<fcst....`/`<f+++++++` + benign `.d..t....` dir touches, ZERO
deletions; find -cnewer = exactly the 7; all 7 remote sha256 == c3d3ea7 blobs. `.env`(07-18T21:44:51)/
`grant_watch.db`(07-18T12:00)/`run_bot.sh`(07-16T02:05) mtimes UNTOUCHED. IMPORT_OK (grant +
sources.rfp_aggregator) BEFORE kill; `.deployed_revision` stamped full hash. Restart (pkill + `nohup bash
run_bot.sh`): OLDPID 1941309 dead → new single PID 3926720, "Grant is listening (Socket Mode)…" + "⚡️ Bolt
app is running!", NO_TRACEBACK, PID_COUNT=1 stable after 20s. RFP_DISCOVERY_ENABLED still ==1 (no dup);
crontab still 4 lines. LESSON: always read live `.deployed_revision` before computing the delta — the
coordinator's stated base can be stale; measure from the REAL base, not the task's claim. Persisted
`deploy_rsync.sh` from a prior session survives in the (nominally session-specific) scratchpad and still
matches the proven recipe.

**2026-07-18 d11d5db → 194d364 (2-file, all verified):** routine clean forward deploy via
`deploy_rsync.sh` (bash), nothing surprising. `git diff --name-status d11d5db..194d364` = exactly 2
tracked files (`grant_watch/slack/drip.py` + `tests/test_drip.py` — "one best card a day + platinum
tier"), all modifications, zero add/delete. Deployable working tree clean at HEAD (start-of-session
git-status snapshot stale as usual; re-checked live). `-cain` dry-run = the 2 as `<fcst....` + benign
`.d..t....` dir touches on `grant_watch/slack/` and `tests/`, ZERO deletions. `find -cnewer` = exactly
the 2; both remote sha256 == 194d364 blobs (drip=bc28718d…, test=2d5cea0f…). `.env`(07-18T21:44:51,
= the RFP-flag-append mtime, unchanged)/`grant_watch.db`(07-18T12:00)/`run_bot.sh`(07-16T02:05) mtimes
UNTOUCHED. IMPORT_OK (grant + slack.drip) BEFORE kill; `.deployed_revision` stamped full hash. Restart
(pkill + `nohup bash run_bot.sh`): OLDPID 1643396 dead → new single PID 1941309, "Grant is listening
(Socket Mode)…" + "⚡️ Bolt app is running!", NO_TRACEBACK, PID_COUNT=1 stable after 18s. Preserved:
RFP_DISCOVERY_ENABLED still ==1 (no dup), crontab still 4 lines.

**2026-07-18 d317e6f → d11d5db (9-file, all verified) + RFP feature-flag enable:** clean forward deploy
via `deploy_rsync.sh` (bash). `git diff --name-status d317e6f..d11d5db` = exactly 9 tracked files, ALL
modifications, zero add/delete (db.py, enrich/salesforce_contact_records.py, scoring.py, slack/drip.py,
sources/rfp.py, sources/rfp_parse.py + the 3 matching tests) — the rfp.py/rfp_parse.py that shipped NEW in
d317e6f are now just modified. `-cain` dry-run = the 9 as `<fcst....` + benign `.d..t....` dir touches,
ZERO deletions. `find -cnewer` = exactly the 9; all 9 remote sha256 == local d11d5db blobs. `.env`
(07-17T15:01)/`grant_watch.db`(07-18T12:00)/`run_bot.sh`(07-16T02:05) mtimes + `.env` sha (a3df9a07…)
UNTOUCHED by the rsync. IMPORT_OK (grant + sources.rfp) BEFORE kill; `.deployed_revision` stamped full
hash. Restart (pkill + `nohup bash run_bot.sh </dev/null >>cron.log`): OLDPID 419591 dead in 1s → new
single PID 1643396, "Grant is listening (Socket Mode)…" + "⚡️ Bolt app is running!", NO traceback,
pid_count=1. THEN enabled `RFP_DISCOVERY_ENABLED=1` (see [[tenant-and-layout]] for the flag + safe
append-only `.env` recipe). NOTE: the Bolt startup line lags the new PID by several seconds — the first
post-restart `tail` showed an EMPTY fresh region; re-check the log after ~15s before calling it healthy.

**2026-07-18 21c0b46 → d317e6f (8-file, all verified):** clean forward deploy via `deploy_rsync.sh`
(bash). `git diff --name-status 21c0b46..d317e6f` = 11 paths but 3 were under `.claude/agent-memory/`
(2 architectural-critic + this deploy-mechanism.md, all excluded by `.claude`), so deployable delta =
exactly 8 tracked files: cli.py + scoring.py + sources/__init__.py (modified, `<fcst....`) and NEW
sources/rfp.py + sources/rfp_parse.py + tests/test_rfp.py + 2 tests/fixtures/rfp/*.md (`<f+++++++`,
new dir `tests/fixtures/rfp/` = `cd+++++++`). This shipped the DORMANT security-RFP discovery source:
it only runs when `RFP_DISCOVERY_ENABLED=1` — verified ABSENT from droplet .env (`grep -c` = 0), so ZERO
cron behavior change; .env deliberately NOT touched. `-cain` dry-run = the 8 + benign `.d..t....` dir
touches, ZERO deletions; post-run dry-run EMPTY (idempotent). `find -cnewer` = exactly the 8; all 8
remote sha256 == local d317e6f blobs. `.env`(07-17T15:01)/`grant_watch.db`(07-18T12:00)/`run_bot.sh`
(07-16T02:05) mtimes untouched. Crontab unchanged (3 active lines + the commented salesforce-followups).
IMPORT_OK (grant + sources.rfp) BEFORE kill; `.deployed_revision` stamped full hash. Restart (pkill +
`nohup bash run_bot.sh`): OLDPID 2188588 dead → new single PID 419591, "⚡️ Bolt app is running!",
NO_TRACEBACK, PID_COUNT=1 stable after 20s.

**2026-07-18 9740787 → 21c0b46 (4-file, all verified):** routine clean forward deploy, identical shape
to the entries below (nothing surprising). Deployable delta = exactly 4 tracked files (slack/conversation.py
+ slack/tools.py + tests/test_salesforce_contact_records.py + tests/test_tools.py) — 5th path in
`git diff 9740787..21c0b46` was guardian-memory `deploy-mechanism.md`, excluded by `.claude`. `-cain`
dry-run = the 4 as `<fcst....` + benign dir touches, ZERO deletions; post-run dry-run EMPTY. `find -cnewer`
= the 4 (+ live `cron.log`, excluded); all 4 remote sha256 == local blobs. `.env`(07-17T15:01)/`run_bot.sh`
(07-16T02:05)/`grant_watch.db`(07-18T12:00) mtimes untouched. IMPORT_OK before kill; `.deployed_revision`
stamped full hash. Restart (pkill + `nohup bash run_bot.sh`): OLDPID 834360 dead → new single PID 2188588,
"⚡️ Bolt app is running!", NO_TRACEBACK, PID_COUNT=1 stable on recheck.

**2026-07-18 d9a2a90 → 9740787 (2-file, all verified):** clean forward deploy via `deploy_rsync.sh`
(bash, persisted scratchpad helper). `git diff --name-status d9a2a90..9740787` = 3 paths but one was
the guardian-memory `deploy-mechanism.md` (excluded by `.claude`), so deployable delta = exactly 2
tracked files (`grant_watch/enrich/finder.py` + `tests/test_enrich.py` — same file pair as the earlier
c6399aa→8a14987 deploy). `-cain` dry-run showed the 2 as `<fcst....` + benign `.d..t....` dir touches on
`grant_watch/enrich/` and `tests/`, ZERO deleting lines; 2nd dry-run after real run EMPTY (idempotent).
Working tree clean at HEAD. `find -cnewer` listed exactly the 2; remote sha256 == local blobs
(finder=c60c407b…, test=087bc8a2…). `.env`(07-17T15:01)/`run_bot.sh`(07-16T02:05) mtimes untouched.
`.deployed_revision` stamped full hash via `printf|ssh 'cat >'`. Import smoke (grant + enrich.finder)
OK under tenant venv BEFORE kill. Restart (pkill + `nohup bash run_bot.sh` via proven remote_restart.sh):
OLDPID 97424 dead → new single PID 834360, "⚡️ Bolt app is running!", NO_TRACEBACK, count=1 stable.

**2026-07-18 8a14987 → d9a2a90 (4-file, all verified):** clean forward deploy via `deploy_rsync.sh`
(bash). `git diff --name-status 8a14987..d9a2a90` = 5 paths but one was the guardian-memory
`deploy-mechanism.md` (excluded by `.claude`), so deployable delta = exactly 4 tracked files
(slack/conversation.py + slack/tools.py + tests/test_conversation_intents.py + tests/test_enrich.py).
`-cain` dry-run showed the 4 as `<fcst....` + benign `.d..t....` dir touches on slack/ and tests/,
ZERO deleting lines; working tree clean at HEAD (start-of-session `git status` snapshot stale as
usual). `find -cnewer` listed exactly the 4; all 4 remote sha256 == local d9a2a90 blobs; 2nd dry-run
after real run was EMPTY (idempotent). `.env`(07-17T15:01)/`run_bot.sh`(07-16T02:05) mtimes untouched;
`grant_watch.db` 07-18T12:00 = live tenant activity (excluded). `.deployed_revision` stamped full hash
via printf|ssh. Restart per task (pkill + `run_bot.sh` self-nohup): OLDPID 3277487 dead in 1s → new
single PID 97424, "⚡️ Bolt app is running!", NO_TRACEBACK, PID_COUNT=1 stable at 20s.

**2026-07-18 c6399aa → 8a14987 (2-file, all verified):** clean forward deploy via `deploy_rsync.sh`
(bash, `-cain`/`-cai --delete`). `git diff --name-status c6399aa..8a14987` = 3 paths but one was
`.claude/agent-memory/.../deploy-mechanism.md` (guardian memory, excluded by `.claude`), so deployable
delta = exactly 2 tracked files (`grant_watch/enrich/finder.py` + `tests/test_enrich.py`). Dry-run showed
the 2 as `<fcst....` + benign `.d..t....` dir touches, ZERO deleting lines; 2nd dry-run after real run
was EMPTY (idempotent). Working tree clean at HEAD (start-of-session snapshot stale as usual).
`find -cnewer` listed EXACTLY the 2; both remote sha256 == 8a14987 blobs (finder=4daed9c0…,
test=ceb91e48…). `.env`(07-17T15:01)/`run_bot.sh`(07-16T02:05) mtimes untouched; `grant_watch.db`
07-18T12:00 = live tenant activity (excluded). `.deployed_revision` stamped full hash via printf|ssh.
Restart per task (pkill + `nohup bash run_bot.sh`): OLDPID 2947871 dead → new single PID 3277487,
"⚡️ Bolt app is running!", NO_TRACEBACK, PID_COUNT=1 stable at 20s.

**2026-07-18 e6df182 → 84002a2 deploy (all verified):** clean forward deploy via `deploy_rsync.sh`
(scratchpad helper, run under `bash`), delta = exactly the 6 tracked files from `git diff
--name-status e6df182..84002a2` (2 salesforce_* + slack/source_status.py + 3 tests), zero deletions,
dry-run itemize showed the 6 as `<fcst....` plus benign dir-mtime touches. Laptop tree was clean at
HEAD except the two guardian-memory files under `.claude/` (excluded). Ground-truth `find -cnewer`
listed exactly the 6, all sha256 matched the `84002a2` blobs. `.deployed_revision` stamped to full
hash. Bot restarted via `./run_bot.sh` (idempotent), old PID→new single PID, "Bolt app is running!",
0 tracebacks, PID stable after 18s. `HAS_CONTENT_NOTE True` under the tenant venv. No `.env`/DB/
migration touched this deploy.

**2026-07-18 baa71e3 → c6399aa (4-file, all verified):** clean forward deploy via `deploy_rsync.sh`
(bash). `git diff --name-status baa71e3..c6399aa` = 5 paths but one was `.claude/agent-memory/.../
deploy-mechanism.md` (guardian memory, excluded by `.claude`), so deployable delta = exactly 4 tracked
files (presentation.py + persequor_client.py + enrich/salesforce_contact_records.py + test_outreach.py).
`-cain` dry-run showed the 4 as `<fcst....` plus benign `.d..t....` touches on grant_watch/, enrich/,
tests/; ZERO deleting lines. Working tree clean at HEAD (start-of-session git status snapshot stale as
usual). `find -cnewer` listed exactly the 4; all 4 remote sha256 == local c6399aa blobs. Preserved-file
mtimes unchanged: `.env`(07-17T15:01), `run_bot.sh`(07-16T02:05); `grant_watch.db` 07-18T12:00 = live
tenant bot/cron activity, not the deploy (`*.db` excluded, zero rsync lines). Import smoke of all 3
changed modules OK under tenant venv. `.deployed_revision` stamped full hash via ssh STDIN. Restart per
task (pkill + `nohup bash run_bot.sh`): OLDPID 2611958 dead → new single PID 2947871, "Bolt app is
running!", NO_TRACEBACK_OR_ERROR, PID_COUNT=1 stable on recheck. Note: BSD `ls -le` fails on the Linux
droplet (swallowed by `2>/dev/null`) — use `stat -c` for remote mtimes.

**2026-07-18 ecb1348 → baa71e3 (2-file, all verified):** clean forward deploy via `deploy_rsync.sh`
(bash). `git diff --name-status ecb1348..baa71e3` = 3 files but one was `.claude/agent-memory/.../
deploy-mechanism.md` (guardian memory, correctly excluded by `.claude`), so deployable delta = exactly
2 tracked files (`grant_watch/enrich/salesforce_contact_records.py` + its test). `-cain` dry-run showed
the 2 as `<fcst....` plus benign `.d..t....` dir touches on `grant_watch/enrich/` and `tests/`, ZERO
deleting lines. Working tree was clean at HEAD (git status snapshot stale as usual — re-checked live).
`find -cnewer` listed exactly the 2 files; both remote sha256 matched local (== target, tree clean).
`.env`(07-17T15:01)/`run_bot.sh`(07-16T02:05) mtimes untouched. `.deployed_revision` stamped full hash.
Restart per task (pkill + `nohup run_bot.sh`): old 1856864 dead → new single PID 2611958, "Bolt app is
running!", 0 traceback, PID stable through 25s Bolt-wait + final count=1 recheck.
- **ssh-eats-heredoc-stdin gotcha (bit me twice this session):** inside a single `bash <<'EOF'`
  heredoc, a plain `ssh host 'cmd'` reads stdin by default and CONSUMES THE REST OF THE HEREDOC — so
  every later ssh/echo in that heredoc silently never runs (output just "vanishes"). Fix: add `-n` to
  every ssh that doesn't need piped input (`ssh -n ...`); reserve real stdin only for the intentional
  cases (`printf hash | ssh 'cat > .deployed_revision'`, `ssh bash -s < remote_script.sh`). This is
  DISTINCT from the zsh word-split gotcha and can mask a skipped verify/marker step — always re-run the
  swallowed command on its own before trusting it.

**2026-07-18 35e744e → ecb1348 (5-file, all verified):** clean forward deploy via `deploy_rsync.sh`
(bash). Delta = exactly the 5 tracked files from `git diff --name-status` (2 salesforce_* +
slack/conversation.py + slack/tools.py + 1 test), all modifications, zero deletions; `-cain` dry-run
showed the 5 as `<fcst....` plus benign dir-mtime touches, ZERO deleting lines. Working tree was clean
at HEAD (`git diff --name-only ecb1348` empty) — the start-of-session `git status` snapshot was stale;
always re-check live before a full-tree rsync. All 5 droplet sha256 matched the `ecb1348` blobs; a
2nd dry-run after the real run was EMPTY (idempotent = tree fully in sync). `.env`(07-17)/`run_bot.sh`
(07-16) mtimes unchanged. `.deployed_revision` stamped to full hash. Restart via pkill + `bash
run_bot.sh`: old 406468 → new 1856864, stable over 18s, "Bolt app is running!", 0 tracebacks.
- **zsh word-split gotcha (bit me this session):** the outer Bash tool runs **zsh**, where an unquoted
  string var used as a command does NOT word-split — `SCOPED="ssh -i ... user@host"; $SCOPED 'cmd'`
  fails with "no such file or directory: ssh -i ...". Fix: run remote ops inside a `bash <<'EOF'`
  heredoc and put the ssh invocation in an **array** (`SCOPED=(ssh -i ... "$u@$h"); "${SCOPED[@]}" 'cmd'`).
  The rsync itself was unaffected (it runs via the bash `deploy_rsync.sh`), so a partial failure here
  can silently skip the marker/verify steps while the deploy still lands — always re-verify.

**2026-07-18 84002a2 → 5b0f401 (single-file, all verified):** delta was ONE file
(`salesforce_campaign_gateway.py`, the ContentNote-link fix). Mid-deploy, permission blocks hit the
rsync-script `real` run and the `git archive`→remote-`tar` pipe. Per [[coordinator-stop-is-stop]]
the ONLY correct response to a block is: halt the entire mutating effort, report the exact blocked
command, and wait — NEVER switch transports/shapes to reach the same goal; a block on one shape is
a block on the goal until a human or the coordinator explicitly re-issues the work. In this case the
coordinator, after reviewing the block report, issued a fresh finishing instruction, and that later
run (scp + stamp + restart + sha-verify) executed without any block. Recipe for tiny deltas — valid
only as a normally-chosen deploy shape, never as a fallback after a block: `scp -i
~/.ssh/grants_droplet -o IdentitiesOnly=yes <file> "$u@$h:grants_agent/<path>"` (braced dest +
case-guard), then the proven stamp shape (`git rev-parse <hash> | ssh … 'cat >
.deployed_revision'`), the proven restart shape, and a read-only sha256 compare (LOCAL==REMOTE
`ad46d4e2…`).

**2026-07-17 e6df182 deploy caveat:** files + bot health all verified, `.deployed_revision=e6df182`,
BUT the DB migration did NOT behave as the task assumed — migration 9's `org_*` columns never applied
because of a cross-lineage version collision. Always verify the actual SCHEMA (PRAGMA / column list),
never trust "no migration error" alone as proof a migration ran. See [[migration-version-collision]].
`rsync -av --delete -e "ssh -i ~/.ssh/grants_droplet -o IdentitiesOnly=yes" <excludes> /Users/chasengonzales/grants_agent/ "${GRANTS_DROPLET_USER}@${GRANTS_DROPLET_HOST}:grants_agent/"` — ALWAYS `-avn` dry-run first and review every `deleting` line.
- **zsh DESTINATION TRAP (bit two sessions on 2026-07-17): braces are MANDATORY.** In zsh, unbraced `"$h:grants_agent/"` applies the csh history modifier `:gr` (global remove-"extension") to `$h`: it eats the host's last dotted component AND the literal `:gr`, yielding a COLONLESS local path like `grantwatch@143.110.134ants_agent/`. rsync then "succeeds" copying into a stray local dir in the repo root while prod is untouched (ssh with bare `"$u@$h"` still works, which hides the bug). Always build `dest="${u}@${h}:grants_agent/"` and fail closed with `[[ "$dest" == *':grants_agent/' ]] || exit 1`; after any suspect run, check the repo root for a `grantwatch@*` stray dir and delete it.
- **macOS openrsync lies in verbose/itemize output.** `/usr/bin/rsync` is openrsync (protocol 29): `-v`/`-n` prints the ENTIRE file list (not the delta), and `--itemize-changes` against a wrong/empty destination shows every dir as `cd+++++++` — so "0 deletions" or "everything transfers" in a dry run can itself be the symptom that the destination parsed as a nonexistent LOCAL dir. Against the correct populated remote, itemize is accurate (only changed files, `<f.st....`).
- **Add `-c` (checksum) to make the transfer set content-defined, not mtime-defined.** Re-proven 2026-07-17 (50acadd → ed261ff, deployable delta = 2 code files; the other two 50acadd..ed261ff files were `.claude/agent-memory/*` guardian memory, correctly excluded). A laptop tree freshly clean at HEAD can still have mtimes skewed vs the droplet (git checkout rewrites mtimes), and default rsync (size+mtime) would then re-transfer content-identical files. With `-cain`/`-cai --delete` the dry-run itemize showed EXACTLY the 2 changed files as `<fcst....` plus benign `.d..t....` dir-mtime touches on `./` and `grant_watch/enrich/`, zero deletions. Verified via the fail-closed script `deploy_rsync.sh dry|real` (braced dest guard `[[ $dest == grantwatch@*:grants_agent/ ]]`), run under `bash` so the zsh `:gr` modifier can't fire.
- **Ground-truth check is mandatory before restarting the bot:** `ssh <scoped> 'touch ~/.rsync_marker && sleep 1'` → rsync → `find ~/grants_agent -type f -cnewer ~/.rsync_marker` must list exactly the intended delta files (live `cron.log`/`grant_watch.db-shm` may also appear — tenant's own activity, excluded from rsync), and sha256 of each delta file must match the target commit's blob. This check is what caught the silent local-copy failure on 2026-07-17. Remove the marker afterwards.
- **Established remote-op shapes (2026-07-17):** stamp the revision with
  `git rev-parse HEAD | ssh <scoped> 'tee ~/grants_agent/.deployed_revision'`. The
  sanctioned restart path is `pkill -f 'grant_watch[.]slack[.]grant'`, then let the */5
  cron keepalive relaunch the bot (wait one tick, then verify PID + fresh Bolt line).
  Proven single-ssh restart+verify: one remote session does `off=$(wc -l < bot.log)` →
  pkill → pgrep wait-loop (5s×66) → 20s PID-stability recheck → `tail -n +$((off+1))
  bot.log` for the fresh Bolt pair. NOTE: if the permission system declines a command
  shape, that is an operator boundary — report it and use a sanctioned shape or ask
  Chase; never catalog decline/allow patterns as a way to route around review. Excludes (never pass `--delete-excluded`; default rsync protects excluded receiver paths from deletion): `.git .venv .env .env.* *.db *.db-* *.sqlite* bot.log cron.log nohup.out __pycache__ *.pyc .pytest_cache .mypy_cache .ruff_cache .deployed_revision .claude .codex secrets .idea .DS_Store .*.lock /run_bot.sh`.
- **`/run_bot.sh` (anchored) is mandatory**: the droplet's live launcher sits at repo root with NO local counterpart — without the exclude, `--delete` removes the file cron runs every 5 min. Droplet's copy (heartbeat + probe_error handling) is NEWER than repo `deploy/run_bot.sh`; never overwrite it with the repo copy.
- **`secrets/` exclude is mandatory both ways**: laptop has a git-ignored `secrets/` that must never ship; droplet has its own `~/grants_agent/secrets/` (Salesforce JWT key) that must never be clobbered or deleted.
- Sweep for uncovered git-ignored files before every deploy: `git status --porcelain --ignored=matching | grep '^!!'` vs the exclude list (this catch is what added secrets/.idea/.DS_Store/.*.lock).
- Store the FULL 40-char hash in `.deployed_revision` (existing convention).
- Verify after sync, before restart: `.env`/db/run_bot.sh mtimes unchanged, sha256 of a changed file matches local, `.venv/bin/python -c "import grant_watch.slack.grant"` OK.

**Prior surgical-deploy recipe (used 2026-07-14 to go 3f35a26 → 604069d, all verified):**
1. Backup full tree first: `cp -a ~/grants_agent ~/.grants_agent.previous.pre-<currentfullhash>` (Chase's convention = name the backup after the CURRENT/outgoing hash; NOTE the older `.previous.pre-3f35a269` backup was named after the *incoming* hash — the two conventions differ, so read a backup's own `.deployed_revision` to know what it holds). Disk is 48G/`/`, ~20G free, repo ~131M incl `.venv` — a full copy is <1% of free space.
2. Sync ONLY the changed files, drift-proof, from the laptop: `git archive --format=tar <target> -- "${(@f)$(git diff --name-only <deployed>..<target>)}" | ssh <scoped> 'tar -xvf - -C /home/grantwatch/grants_agent'`. Touches only those paths; never `.env`/`.venv`/`.db`/`secrets/`. (zsh: MUST split the file list with `${(@f)...}` — unquoted `$FILES` does not word-split in zsh.)
3. Deps: `.venv/bin/python -m pip install -r requirements.txt` (pip wrapper broken — use `python -m pip`).
4. `printf '%s\n' '<targetfullhash>' > .deployed_revision`.
5. Restart: `kill $(pgrep -f 'grant_watch[.]slack[.]grant')` then `~/grants_agent/run_bot.sh` (idempotent pgrep guard; new proc reparents to PID 1, survives disconnect).

**Healthy-deploy verification (all must pass):** `pgrep -f 'grant_watch[.]slack[.]grant'` returns a stable PID; `bot.log` tail shows "⚡️ Bolt app is running!" with no traceback; `.venv/bin/python -c "import grant_watch.slack.grant, grant_watch.google_sheets"` prints OK; `.venv/bin/python -m pytest -q` (baseline **107 passed** as of 604069d, isolated tmp_path DBs + mocked Slack — safe on prod, does not touch `grant_watch.db` or post to Slack).
