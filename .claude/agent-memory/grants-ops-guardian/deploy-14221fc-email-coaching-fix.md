---
name: deploy-14221fc-email-coaching-fix
description: Deploy b4a8046→14221fc on 2026-08-09 (CURRENT PROD) — code-only, schema stayed 35, PID 24507, ~3s outage; and the .env repair that made `source .env` stop aborting, proven by RESEND_FROM_EMAIL appearing in the live environ
metadata:
  type: project
---

**LIVE 2026-08-10T00:50Z (droplet 17:50 PT).** Production moved
`b4a8046dfa8f73d59d0175ae4d2f506ecfa9ebc9` → `14221fc0cacb824ecb6da8b36e1306ff290e8444`
(8 commits). **Schema stayed 35** — verified before AND after. Listener **22742 → 24507**.
Ships the `model_note`/`for_human` sentinel split and `grant_watch/lead_digest.py`, the one
renderer for lead results that reach a person with no model in between.

## THE .env REPAIR — the armed trap from [[deploy-b4a8046-reminders-email]], defused

That entry predicted this and it was exactly right. `sh -n .env` on the LIVE file failed:

    .env: 69: Syntax error: newline unexpected

and the live process showed the diagnostic asymmetry: `RESEND_API_KEY` present in
`/proc/<pid>/environ`, **`RESEND_FROM_EMAIL` absent** — same append, one has a space.

Fixed by QUOTING IN PLACE. The value was never retyped: the script reads everything after
the first `=`, asserts it is unquoted / contains a space / contains no `"`, `\`, `$` or
backtick, and re-emits it wrapped in double quotes. **That makes "the value is unchanged" a
property of the construction, not a claim.** Proof captured: `VALUE_SHA` of the inner text,
`sh -n` silent afterwards, `dotenv_values` old vs new equal for that key and for every other
key, and post-restart `RESEND_FROM_EMAIL_NONEMPTY=1` in the live environ (was 0).

**A line DELETION breaks the prefix-sha trick, so use a line-set proof instead.** The
append recipe in [[env-zoominfo-20260809]] proves an untouched prefix with
`head -c <presize> | sha256`. That cannot work when a line is removed from the middle
(dead flag was line 25, the quoted line was 68). What replaced it: drop both target indices
from old and new, and assert the two remaining 66-line lists are equal **as ordered lists**,
hashing each side (`13c88db8…` both). Plus key-set delta == exactly one dropped name, zero
added, and zero dotenv values changed.

## The dead flag: `SALESFORCE_LEAD_ENRICHMENT_UPDATES_ENABLED` (deleted)

Gated nothing. `git grep` at BOTH b4a8046 and 14221fc: zero hits anywhere in the tree, and
`grep -rI` over the deployed tree: zero files. Not dynamically constructed either — every
env name under `grant_watch/` is a hardcoded literal (`os.environ["LITERAL"]` /
`os.environ.get("LITERAL", …)`), no `f"{x}_ENABLED"` pattern exists.

`git log -S --all` shows it DID exist once, on the SIDE lineage (`310df37`, `e2b2038`,
`94482c9`, `d254d3a`, `3387254`) — the same lineage that gave us
[[migration-version-collision]]. So it is a retired flag whose code never reached main,
copied into the droplet `.env` and left behind. That is the shape to expect: **not a typo, a
fossil from the other lineage.**

**THREE MORE FOSSILS ARE STILL IN THE DROPLET `.env`, unreferenced at 14221fc and NOT
removed (only one deletion was authorized):** `SALESFORCE_PERSON_LEAD_WRITES_ENABLED`,
`SALESFORCE_OPPORTUNITY_WRITES_ENABLED`, `SALESFORCE_GRANT_AUDIT_RECORDS_ENABLED`. Each
reads as an armed feature flag and gates nothing. Worth a follow-up authorization.
Counter-example that stops the same grep from over-reaching: `SALESFORCE_CLIENT_ID` /
`SALESFORCE_CLIENT_SECRET` ARE live — they are read via `os.environ[...]` **subscript**, so
a regex written only for `getenv("…")` misses them. Use a plain literal `git grep -l`, never
a getenv-shaped regex, before calling a variable dead.

## Proving a behaviour fix on the DEPLOYED bytes, before the restart

The whole point of this deploy is that a rep got emailed the model-facing string. So the
smoke test did not just import — it CALLED the deployed code:

    coached = "Nearby alternatives - x." + model_note(" Offer these to the user …")
    human   = for_human(coached)
    # MODEL_TEXT_STILL_CARRIES_COACHING= True
    # HUMAN_TEXT_CARRIES_COACHING= False
    # HUMAN_TEXT= 'Nearby alternatives - x.'

Two assertions, not one: the coaching must SURVIVE for the model and DISAPPEAR for the
human. Asserting only the second would pass on code that deleted the hint entirely.
Run it BEFORE the kill — a failure then costs zero downtime.

## Schema-stayed-35 proof (no migration, despite a migrations file in the delta)

`grant_watch/migrations_nudges.py` IS in the delta, which by itself proves nothing
(same trap as [[deploy-b4a8046-reminders-email]]). Diff the REGISTRY: extracted
`^def migration_N_x` from every `migrations*.py` at both revisions → **22 functions, byte-
identical lists**. The file's real change is two new members of `NUDGE_SUBJECT_KINDS`
(`card_escalated`, `thread_abandoned`) — and that tuple is deliberately **validated in
Python, not by a CHECK constraint** (comment at line 29), so a new kind ships without a
migration. Confirmed live: `SCHEMA_MAX=35` before, after the sync, and after the restart.

## Deploy shape (worked cleanly, nothing surprising)

Path-limited `git archive 14221fc -- <25 paths>` → 522,240 B, sha
`4790d851f969a5d42935b13b947953a7be5646235f742e91e6a4d60b7b00ba60`, member set asserted
EXACTLY equal to the intended list and all 25 hashed against `git show 14221fc:<path>`
before it left the laptop. Staged to `~/.deploy_stage_14221fc`, re-gated on the droplet
(25 files, 0 forbidden), then `rsync -cai --no-times --no-perms` staging → live.

**`--delete` is NEVER valid with a path-limited staging tree** — staging holds only the
delta, so `--delete` would remove every other file in those directories. Omitted by
construction (without it rsync cannot delete), and that reasoning belongs in the log rather
than a `-n --delete` "preview" whose output would be thousands of alarming lines. This is a
real divergence from the full-tree recipe in [[deploy-mechanism]]; do not copy the
delete-preview step across.

Itemize: 23 `>fcsT……` + 2 `>f+++++++++` (`grant_watch/lead_digest.py`,
`tests/test_salesforce_writer_scope.py`), 0 deleting, catch-all empty both passes, second
pass 0 lines, `find -cnewer` exactly 25. All 25 droplet sha256 == the 14221fc blobs.
**Pre-image check first:** all 23 overwritten files hashed byte-exact at b4a8046 → no
out-of-band drift, and OLDPID 22742 == the PID this file's predecessor recorded.

Delta classification: 31 paths = 25 deployable + 5 `.claude/agent-memory/**` (never
deployed) + `.env.example` (excluded by the standing `.env.*` rule; the droplet copy is
knowingly stale) + **0 under `data/**`**.

`config/reps.json` was pretty-printed AND gained `manager: true` on Anthony. Parsed both
revisions and diffed the `{slack_id: (email, name)}` maps — IDENTICAL, 6 rows both sides,
one added key on one row. **No new mailbox ⇒ no prod Salesforce `User.Email` probe needed**
([[roster-deploy-4c6a543]] requires the probe only for a NEW row).

## Postflight

PID 24507 uid 1001, cwd `/home/grantwatch/grants_agent`, 53 venv maps, PID_COUNT 1.
Outage **~3 s** to process start; **Bolt line at 9 s** — noticeably faster than the 30–45 s
this bot usually takes, so do not treat a fast boot as suspicious. Fresh log region exactly
2 lines ("Grant is listening (Socket Mode)…" + "⚡️ Bolt app is running!"), 0 tracebacks,
0 errors. `remind --dry-run` → **"remind: skip: nothing due"**. `nudge --dry-run` →
**"nudge: skip: outside business hours"** (Sunday 17:5x PT). `nudge --execute` NOT run, no
nudge cron line added, `capability-seed` never run.

`ENVIRON_TOTAL` stayed **51** across the restart — lost the dead flag, gained
`RESEND_FROM_EMAIL`. A net-zero count that changes composition is a nice cheap cross-check.

**Fingerprints now:** revision `14221fc…8444` (40 B, `printf '%s'`, no trailing newline);
schema 35, 46 tables, `integrity_check` ok, `foreign_key_check` still EXACTLY the two
approved orphans (10642, 11892), leads 10715; `.env` sha
`9b68bc18850800e15841a041fa8446dfdcac124f1264fe02f097c82341bdc634`, **67 lines, 33 keys**,
600, and `sh -n .env` now **SILENT**; crontab sha
`0ba78a3b79f826ffac3f71bb8539a62806bf7a83347a65558b60d38fcf248d6f` byte-identical, 11 lines
= 6 active + 5 comment, 0 nudge lines; `run_bot.sh` `07773019…06bb` unchanged. Disk 67%, 16 G.

**Rollback artifacts (retained, dir 700 / files 600):**
`~/backups/deploy-14221fc-20260810T004615Z/` —
`grant_watch.db.vacuum` (25,051,136 B, sha `56012a42e980f13d89cf6ef2fd2fe665612b428a28573415c70dffb62ca3b6c1`,
COPY verified integrity ok / schema 35 / 46 tables / leads 10715 / same 2 FK orphans),
`code_at_b4a8046.tar.gz` (127,800 B, sha `f971d60b0b5604df096556c24b88fd12e27c26eef075d502e245a7fa4199b3c9`,
23 members, `gzip -t` OK), `env.bak` (sha == pre-image `dda87de9…`), `crontab.bak`
(sha == `0ba78a3b…`), `deployed_revision.bak` (40 B, b4a8046).
Rollback = restore the tar, `rm` the 2 added files, restore `env.bak`, re-stamp b4a8046,
restart. No DB rollback needed — nothing wrote to it. `VACUUM INTO` on a `mode=ro`
connection again left the source's size AND mtime byte-identical (26894336 / 1786320159).
`~/backups` is now **205 M** — worth a retention decision before it becomes the next
disk story ([[disk-footprint-and-cruft]]).
