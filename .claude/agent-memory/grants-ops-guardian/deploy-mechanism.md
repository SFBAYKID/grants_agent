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

**Proven surgical-deploy recipe (used 2026-07-14 to go 3f35a26 → 604069d, all verified):**
1. Backup full tree first: `cp -a ~/grants_agent ~/.grants_agent.previous.pre-<currentfullhash>` (Chase's convention = name the backup after the CURRENT/outgoing hash; NOTE the older `.previous.pre-3f35a269` backup was named after the *incoming* hash — the two conventions differ, so read a backup's own `.deployed_revision` to know what it holds). Disk is 48G/`/`, ~20G free, repo ~131M incl `.venv` — a full copy is <1% of free space.
2. Sync ONLY the changed files, drift-proof, from the laptop: `git archive --format=tar <target> -- "${(@f)$(git diff --name-only <deployed>..<target>)}" | ssh <scoped> 'tar -xvf - -C /home/grantwatch/grants_agent'`. Touches only those paths; never `.env`/`.venv`/`.db`/`secrets/`. (zsh: MUST split the file list with `${(@f)...}` — unquoted `$FILES` does not word-split in zsh.)
3. Deps: `.venv/bin/python -m pip install -r requirements.txt` (pip wrapper broken — use `python -m pip`).
4. `printf '%s\n' '<targetfullhash>' > .deployed_revision`.
5. Restart: `kill $(pgrep -f 'grant_watch[.]slack[.]grant')` then `~/grants_agent/run_bot.sh` (idempotent pgrep guard; new proc reparents to PID 1, survives disconnect).

**Healthy-deploy verification (all must pass):** `pgrep -f 'grant_watch[.]slack[.]grant'` returns a stable PID; `bot.log` tail shows "⚡️ Bolt app is running!" with no traceback; `.venv/bin/python -c "import grant_watch.slack.grant, grant_watch.google_sheets"` prints OK; `.venv/bin/python -m pytest -q` (baseline **107 passed** as of 604069d, isolated tmp_path DBs + mocked Slack — safe on prod, does not touch `grant_watch.db` or post to Slack).
