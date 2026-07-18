---
name: deploy-mechanism
description: How code reaches the droplet — NOT git; file-copy deploy with .deployed_revision + broken pip wrapper
metadata:
  type: project
---

The droplet checkout is NOT a git working tree — `~/grants_agent` has no `.git` and `git` commands fail there. Do not assume `git pull` works to deploy.

**Why:** Deploys are done by copying the tree from the laptop, not by pulling. Evidence: a `.deployed_revision` file at repo root holds the deployed commit hash (was `3f35a26974...` = commit 3f35a26 on 2026-07-14); the prior deploy left a backup `~/.grants_agent.previous.pre-3f35a269` (tree renamed to `.previous.pre-<oldhash>` before laying down the new one). `.env`, `.venv`, `grant_watch.db*`, and logs are preserved across deploys (they are not in git and differ from the laptop).

**How to apply:**
- To deploy a new commit, do NOT `git pull` on the droplet. Either use Chase's laptop-side deploy tooling, or (if approved) surgically `scp` only the files changed between `.deployed_revision` and the target — computed locally with `git diff --name-status <deployed>..<target>` — while preserving `.env`/`.venv`/`grant_watch.db`. Then update `.deployed_revision` to match, to keep the deploy state coherent.
- Committed `deploy/` dir only contains `run_bot.sh` (the keepalive launcher) — it is NOT a deploy script.
- **`.venv/bin/pip` is BROKEN** (wrong shebang / relocated venv → "cannot execute: required file not found"). Use `.venv/bin/python -m pip …` for all installs; that works (pip 24.0).
- When the repo state on the droplet does not match the instructed deploy method, STOP and confirm with Chase before improvising — a full rsync would clobber the divergent prod `.env` and live SQLite db. See [[tenant-and-layout]].

**Proven full-tree rsync recipe (2026-07-16: 3d653c6 → 25513bc; re-proven 2026-07-17: 25513bc → 9db96d0, 9db96d0 → 36d2470, 36d2470 → 6ea70f2, 6ea70f2 → c714b01, and c714b01 → 50acadd, and 2026-07-17 ed261ff → e6df182 = 14 files [15 delta minus `.env.example`, which the `.env.*` exclude correctly skips], zero deletions each time; Chase-approved, all verified):**

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
