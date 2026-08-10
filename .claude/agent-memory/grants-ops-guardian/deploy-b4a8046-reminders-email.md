---
name: deploy-b4a8046-reminders-email
description: Deploy 26153bd→b4a8046 on 2026-08-09 (SUPERSEDED by 14221fc) — schema 32→35, PID 22742, 6th cron line; and the .env line that BREAKS `source .env` because an unquoted display-name value contains a space and angle brackets (REPAIRED in the 14221fc deploy)
metadata:
  type: project
---

**LIVE 2026-08-09T00:02Z (droplet 17:02 PT).** Production moved
`26153bd57d6499baf31fb7208add6df48da07b0d` → `b4a8046dfa8f73d59d0175ae4d2f506ecfa9ebc9`
(4 commits). Schema **32 → 35**. Listener **19225 → 22742**. Ships the reminder system,
the capability-ask register, and Grant's first outbound EMAIL transport (Resend).

## THE FINDING THAT MATTERS: a `.env` value with a space breaks `source .env`

`run_bot.sh` line 15 is `set -a; source .env; set +a`. The authorized append added
`RESEND_FROM_EMAIL=<display name> <addr@domain>` — **unquoted, containing a space, `<` and
`>`**. bash parses that as an assignment plus a redirection with no target:

    .env: line 68: syntax error near unexpected token `newline'   ← real cron.log line

Consequences, all measured:
- `RESEND_FROM_EMAIL` is **ABSENT from `/proc/<pid>/environ`** while `RESEND_API_KEY`
  (same append, no space) IS present. A boolean environ check on the pair therefore
  disagrees with itself — that asymmetry is the tell.
- **`source` ABORTS AT THAT LINE.** It is currently the LAST line (68), so nothing was
  lost. **Any future `.env` append lands after it and will never be shell-exported.**
  Treat this as armed for the next env deploy.
- The feature still works: `grant.py:873` and `cli.py:566` both call `load_dotenv()`, and
  python-dotenv parses the unquoted spaced value correctly (`dotenv_values` sees 34 keys,
  `is_configured()` True under both the bot and the cron/CLI path). Nothing on the droplet
  depends on the shell export for this var — cron entries do not source `.env` at all.
- **Recommended fix (NOT applied — the instruction was byte-identical append):** quote it,
  `RESEND_FROM_EMAIL="Name <addr@domain>"`, in the LOCAL repo `.env` first, then re-append.
  python-dotenv strips the quotes, so the value is unchanged.
  **APPLIED 2026-08-09 in the 14221fc deploy — see [[deploy-14221fc-email-coaching-fix]].**
  `sh -n .env` is now silent and `RESEND_FROM_EMAIL` IS in the live environ. The
  prediction above held exactly; the fingerprints in this file are now superseded.

**Lesson: before appending any `.env` value, test it as shell syntax, not just as dotenv.**
`printf '%s\n' "$line" | sh -n /dev/stdin` catches it in one command. A value that dotenv
loves can still be a shell syntax error, and this repo's launcher sources the file.

## Verification shapes that earned their keep

- **A restart does NOT apply migrations** ([[deploy-mechanism]] still true). Applied them
  with the bot **DOWN**: pkill → confirm 0 listeners → one-shot
  `PYTHONPATH=… .venv/bin/python -c "from grant_watch import db; db.connect().close()"`
  → schema 32→35 → then relaunch. No handler can hold a writable connection mid-migration.
  The `*/5` keepalive can relaunch during the gap, so keep the window to seconds.
- **`.deployed_revision` is 40 bytes with NO trailing newline.** `printf '%s\n'` writes 41
  and silently drifts from convention; use `printf '%s'`. Nothing in `grant_watch/` reads
  the file (`git grep deployed_revision -- grant_watch` is EMPTY) — it is a deploy marker
  only, but keep it byte-consistent.
- **Path-limited `git archive <rev> -- <delta paths>` beats a full-tree archive.** 26
  members, 122,771 B, sha `c7a51fd0…68a7`; every member hashed against `git show
  b4a8046:<path>` (26/26). Memory then cannot reach the droplet even in a temp staging
  dir, so the `.claude` exclude stops being load-bearing at all.
- Preview → real → second pass: 16 changed `>fcsT` / 10 adds `>f+++` / 2 new dirs `cd+++`
  / **0 deletions** / 0 unexpected; second pass 0 lines; `find -cnewer` exactly 26. Sha
  ladder checked BOTH ways (16 live files == 26153bd before, all 26 == b4a8046 after).

## Migrations 33/34/35 — additive only, verified by reading them

33 creates `reminders`, `reminder_deliveries`, `followup_optouts` (+3 indexes); 34 creates
`capability_asks` (+1 index); 35 is a guarded `ALTER TABLE capability_asks ADD COLUMN
correction`. No existing table altered, no row rewritten. All four new tables landed
**EMPTY**; tables 42 → 46; `integrity_check` ok; `foreign_key_check` still EXACTLY the two
approved orphans (10642, 11892).

**Migrations 14–22 MOVED from `migrations.py` into `migrations_rich.py` in this commit**
and were renamed `_migration_N_x` → `migration_N_x`. Their bodies differ only by the helper
rename `_add_column` → `_add_col` (diffed line by line). Inert for prod — 14–22 are long
applied, so they never re-run — but it means **`migrations.py` changing is not by itself
evidence of a schema change.** Diff the registry, not the file.

## The new email surface (security shape worth remembering)

`notify/resend_client.send_to_rep(slack_user, subject, text_body, …)` — **there is no
address parameter at all**; the recipient is resolved from the Slack id through
`config/reps.json`. Verified live by `inspect.signature`: zero params matching
email/address/to. So no prompt, tool arg, or scraped page can aim it off-roster. None of
the 5 new tools is in `_ACTION_PRODUCING_TOOLS`, so the bb4e0c9 marker trust boundary
still strips CRM markers from their output. Live tool count **22**.
**`OUTREACH_TEST_EMAIL` is set on the droplet and `recipient_for` honours it**, so every
rep email currently REDIRECTS to that test mailbox — check it before telling anyone their
mail went to them.

## Postflight (all verified)

PID 22742 uid 1001, cwd `/home/grantwatch/grants_agent`, 53 venv maps, PID_COUNT 1, stable
at 267 s. Post-restart log region = exactly 2 lines ("Grant is listening (Socket Mode)…" +
"⚡️ Bolt app is running!"), **0 tracebacks**; the 5 tracebacks in the tail sit at lines
801–870, BELOW the 948 restart boundary — always compare against the offset before blaming
a deploy. Boot again took ~30–45 s, not 6.
`remind --dry-run` → **"skip: nothing due"**. `nudge --dry-run` → **"skip: outside business
hours"** (Sunday 17:0x PT) — NOT executed, no nudge cron line added, `capability-seed`
never run. 18/18 changed modules import; full bot closure loads.

**Fingerprints now:** revision `b4a8046…ebc9` (40 B); schema 35; `.env` sha
`dda87de9352e38af3e9bc6c43f012e93b9b4084a702e11f8e24bb475dcc48dea`, **68 lines, 34 keys**,
600 (was `f4abd546…2a99` / 66 / 32 — the first 66 lines still hash to the old whole-file
sha, which is the cheap proof the append touched nothing); crontab sha
`0ba78a3b79f826ffac3f71bb8539a62806bf7a83347a65558b60d38fcf248d6f`, **11 lines = 6 active +
5 comment** (was `575fbc7c…1a72` / 10 / 5 active; the 10 pre-existing lines are byte-
identical, proven by prefix sha); `run_bot.sh` `07773019…06bb` unchanged. Disk 67%, 16 G.

The 6th cron line: `*/30 8-16 * * 1-5 … cli remind --execute >> cron.log 2>&1`. Safe to add
before any reminder exists — `reminder_worker.run` iterates `reminders.due()` and returns
"skip: nothing due" on an empty table; `MAX_PER_RUN=1`; it only ever posts into the
`thread_ts` the reminder was created in, and emails only the requester.

**Rollback artifact (retained, dir 700 / files 600):**
`~/backups/deploy-b4a8046-20260809T235615Z/` —
`grant_watch.db.vacuum` (25,006,080 B, sha `2dc1962d8f19a261c2f55effd181a72b281c8417754e14f31df151d7eed71e51`,
COPY verified integrity ok / schema 32 / 42 tables / leads 10715),
`code_at_26153bd.tar.gz` (90,195 B, sha `813f4140dba92be314b2bc9a0f80f8a2c7e93f1a8a28a140cba069a5f03ebbd3`,
16 members, `gzip -t` OK), `env.bak` (sha == pre-image `f4abd546…`), `crontab.bak`
(sha == pre-image `575fbc7c…`), `.deployed_revision.bak` (40 B).
Rollback = restore the tar, `rm` the 10 added files, drop the 4 new tables (or restore the
DB copy with `-wal`/`-shm` deleted first), remove the cron line, remove the 2 `.env` lines.
`VACUUM INTO` on a `mode=ro` connection again proved read-only: source mtime **and** size
byte-identical before and after.
