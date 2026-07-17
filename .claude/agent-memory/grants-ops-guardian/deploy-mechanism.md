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

**Proven full-tree rsync recipe (2026-07-16: 3d653c6 → 25513bc; re-proven 2026-07-17: 25513bc → 9db96d0, 9db96d0 → 36d2470, 36d2470 → 6ea70f2, 6ea70f2 → c714b01, and c714b01 → 50acadd, zero deletions each time; Chase-approved, all verified):**
`rsync -av --delete -e "ssh -i ~/.ssh/grants_droplet -o IdentitiesOnly=yes" <excludes> /Users/chasengonzales/grants_agent/ "${GRANTS_DROPLET_USER}@${GRANTS_DROPLET_HOST}:grants_agent/"` — ALWAYS `-avn` dry-run first and review every `deleting` line.
- **zsh DESTINATION TRAP (bit two sessions on 2026-07-17): braces are MANDATORY.** In zsh, unbraced `"$h:grants_agent/"` applies the csh history modifier `:gr` (global remove-"extension") to `$h`: it eats the host's last dotted component AND the literal `:gr`, yielding a COLONLESS local path like `grantwatch@143.110.134ants_agent/`. rsync then "succeeds" copying into a stray local dir in the repo root while prod is untouched (ssh with bare `"$u@$h"` still works, which hides the bug). Always build `dest="${u}@${h}:grants_agent/"` and fail closed with `[[ "$dest" == *':grants_agent/' ]] || exit 1`; after any suspect run, check the repo root for a `grantwatch@*` stray dir and delete it.
- **macOS openrsync lies in verbose/itemize output.** `/usr/bin/rsync` is openrsync (protocol 29): `-v`/`-n` prints the ENTIRE file list (not the delta), and `--itemize-changes` against a wrong/empty destination shows every dir as `cd+++++++` — so "0 deletions" or "everything transfers" in a dry run can itself be the symptom that the destination parsed as a nonexistent LOCAL dir. Against the correct populated remote, itemize is accurate (only changed files, `<f.st....`).
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
