---
name: roster-deploy-4c6a543
description: 5f09200→4c6a543 deployed 2026-08-06 (Nelly added to reps.json), config-only, NO restart, zero outage; the reusable pre-deploy Salesforce roster gate; reps.json is re-read per call; .claude/agent-memory is NOT on the droplet
metadata:
  type: project
---

**2026-08-06 production deploy 5f09200 → `4c6a543a39b0d765f9911b6f12c47be78a81474b`. Config-only:
one rep row added to `config/reps.json`. NO restart, NO outage, listener PID 633555 held.**

**A roster row is only half a rep — verify the Salesforce side BEFORE deploying.** Adding a row to
`config/reps.json` also authorizes creating Salesforce Leads owned by that person, and
`salesforce_campaign_ownership.requester_owner` resolves it as
`persequor_client.rep_email_for(slack_id)` → `gateway.find_active_user_by_email(email)` → **must be
exactly 1 active User**. That compares `User.Email` character-for-character, so a Slack-directory
email that differs from the Salesforce one deploys a row guaranteed to raise
"No active Salesforce user matches requester email". Gate every future roster addition on this
read-only probe first (run as `grantwatch`, GET/SELECT only, `gw._get("query", …)`):
- `SELECT Id,Name,IsSandbox,InstanceName FROM Organization LIMIT 1` — must be `00D41000002jIQ8EAM`
  "Monarch", `IsSandbox=false`. `gw.verify_write_scope()` is read-only and asserts the same, harder.
- `find_active_user_by_email(<email>)` == 1, plus a `chase@monarchconnected.com` control == 1 to prove
  the creds/query shape work, plus a `LIKE`-based name/email sweep to catch a near-miss address.
- Also query the exact email with NO `IsActive` filter — an inactive duplicate would break the ==1
  assumption later. Here `totalSize` was 1, so no shadow row.
Result this time: **Nelly's production `User.Email` IS exactly `marnelly@monarchconnected.com`**
(Id `0055d00000Ce6LAAAZ`, Name "Marnelly Santos", IsActive true). Her prod *Username* is
`marnelly_santos@monarchconnected.com` — username ≠ email, do not conflate them; the sandbox variant
carries the extra `.monarchdev` suffix. Only other `%Santos%` hit was Jho Santos, **inactive**.

**`config/reps.json` needs NO bot restart — proven two ways, not assumed.** `roster.identities()`
(roster.py:30) and `persequor_client.rep_email_for()` (persequor_client.py:92) each call
`json.loads(REPS_PATH.read_text())` *inside the function body*; `REPS_PATH` is a module-level `Path`
(a path, not content), there are no `lru_cache`/`@cache` decorators, and no column-0 assignment
anywhere in `grant_watch/` captures `identities()`/`rep_email_for()` at import time. Empirical proof
that beats the grep: in one already-running interpreter that had *already* imported and called
`identities()`, truncate the file in place → count went 5 → 2 → 5 on restore. **Caveat: that
experiment WRITES to a live production config file.** Do it only with a guaranteed restore + sha
re-verify, and prefer running it against a copy if any consumer could read mid-window; a truncated
roster fails *closed* (fewer authorizations, never a wrong one), which is why the risk was acceptable.

**`.claude/agent-memory/**` is NOT on the droplet and should stay that way.** The droplet's `.claude`
holds only `agents/*.md` and `agent-memory/architectural-critic/*` from some historical copy — the
`grants-ops-guardian/` subdir has never existed there, because `.claude` is in the standard rsync
exclude list. So a `git diff` delta that *counts* 5 files can have a *deployable* delta of 2. Ship the
docs subset only if explicitly asked; guardian memory documents rollback paths, PIDs and verification
recipes, which is mild but real information for anyone with a foothold in the tenant.

**Mechanism (unchanged and still correct):** pinned `git archive` at the target (sha256
`8fa8b20b8f7214880947423d18d85b20e29cc158af9e76ceb50f94bd33d4b985`, 912 files, 0 symlinks, only
`.env.example` matches `\.env`, no `.git`/db/secrets) → droplet staging → **Stage 1** full-tree
`rsync -cain --delete` drift audit (exactly 2 `>fcst` content-differing files, 0 deletions, 0 adds,
**861 `.f..t......` mtime-only touches** = the git-archive commit-date stamping that makes a full-tree
real run destroy mtime forensics) → **Stage 2** real `rsync -cai --files-from=<2 paths>`. Note the
laptop working tree was dirty at deploy time (uncommitted guardian-memory edits); `git archive <sha>`
reads the *commit* tree, so that is harmless — which is precisely why `deploy_rsync.sh` (pushes from
the working tree) is banned.

**Verified end state:** reps.json sha256 `c94b7318ec10c2ed9a9ee6fa70e5bfae2e42acfab5c6bb83ad71a0f443e6abd6`
(5 reps; rows 1-4 parsed-identical to the pre-deploy backup), CLAUDE.md `932f4ef4…f3ad`, `.env`
`b3f338ff…c3bff` UNCHANGED (47 lines), crontab `70e309aa…876f` UNCHANGED (5 lines), DB size+mtime
byte-identical (never opened), `run_bot.sh` untouched, `secrets/` intact, 5 stray `__pycache__` dirs
purged outside `.venv`. Disk 65% / 17G free.

**Rollback = one file copy:** `cp /home/grantwatch/reps.json.bak.20260806T181834Z
~/grants_agent/config/reps.json` (sha256 `b791d0ab…4a89`), plus
`~/.deployed_revision.bak.20260806T181834Z` and `~/pre-4c6a543-overwritten.20260806T181834Z.tar.gz`
(sha256 `fc401b97…0d5f`, 2 members). No restart needed to roll back either.

**Incidental live evidence:** today's card posted 18:00-18:05Z (11:00 PT), *before* this deploy —
`drip[rich]: skip: no rich award card satisfies every evidence rule; falling back to the daily card`
then `drip: posted nugget (award-brief) for lead #1603: HOXIE SCHOOL DISTRICT NO 46`. That is the
[[deploy-5f09200-fallback-routing]] fallback path firing unattended in production on a real weekday.
See also [[deploy-mechanism]], [[salesforce-connection-test]].
