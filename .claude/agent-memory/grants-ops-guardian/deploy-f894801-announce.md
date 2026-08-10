---
name: deploy-f894801-announce
description: Deploy cadfefe→f894801 on 2026-08-10 — schema 37→38 (announcements), PID 32750, 0.48s outage, 13th cron line; and the two findings it surfaced (corrections never re-seeded; the email line false for every rep but Chase), BOTH FIXED by 2159d67
metadata:
  type: project
---

> **SUPERSEDED AS CURRENT PROD by `2159d67` — see [[deploy-2159d67-resend-test-email]].**
> Both findings below are **CLOSED**: `recipient_for()` now reads its own `RESEND_TEST_EMAIL`
> (unset on the droplet), so all six reps resolve to their own mailbox; and `record()` now
> UPDATEs `correction` on a duplicate, so the re-seed took Kerry's two from 118/177 chars to
> **58/57**, and Monday's head message from 236 to **176**. `ask_text` still never moves.
> The measurements below are the ORIGINAL ones, kept because they are why the fix exists.

**LIVE 2026-08-10T04:34:55Z (droplet Sun 21:34:55 PT).** `cadfefe1ba0356495a2db16331d45186e1d54874`
→ `f894801e9d078547b74655059b40d7af152d99a4` (3 commits). Schema **37 → 38**. Listener
**31756 → 32750**, **0.481 s outage**. Crontab 12 → 13 lines.

## Fingerprints now

Revision `f894801…d99a4` (41 B *with* trailing newline — this box's `.deployed_revision` has carried
a newline since the cadfefe deploy, so `printf '%s\n'` is what keeps it stable here; the 40-B
no-newline convention in [[deploy-b4a8046-reminders-email]] is superseded on this machine).
Schema **38**, `schema_migrations` count 38, **47 tables** (was 46). `integrity_check` ok;
`foreign_key_check` exactly the two approved orphans (10642, 11892). leads 10715.
`.env` **unchanged** `9b68bc18…c634` / 67 lines / 33 keys / 600. `run_bot.sh` `07773019…06bb`.
crontab `8b4dd525…d994c` (12 lines, 1270 B) → **`cd38cc6ead025def4dd93e1aa16ef340cfcaa1a6dd3994466095366ec6086cc5`**
(13 lines, 1375 B). Disk 68%, 16 G.

Delta 20 paths = **16 deployable** + 4 `.claude/agent-memory/**`. 13 mods + **3 adds**
(`grant_watch/announce.py`, `data/announcements/2026-08-10.json`, `tests/acceptance_questions.py`),
zero deletes/renames. Pre-image: all 13 mods hashed to the `cadfefe` blobs, all 3 adds absent ⇒
clean base. Archive `c65195d9…b5d7`, 73,385 B, member set asserted EQUAL to the delta, upload
byte-exact. `rsync -cai --no-times --no-perms --files-from` → 13 `>fcsT` + 3 `>f+++` + 1 `cd+++`
(`data/announcements/`), **0 deleting**, catch-all empty, second pass 0 lines, `find -cnewer`
exactly 16, post-image 16/16 == `f894801` blobs. Import closure **115/115, 0 failures**.

Migration 38 applied with the bot **DOWN** ([[deploy-mechanism]]: a restart does not migrate).
Additive only: `CREATE TABLE announcements` + `ix_announcements_pending`. Landed empty.
Post-restart log region = exactly the 2 boot lines, **0 tracebacks**, PID_COUNT 1, 53 venv maps.
`run_bot.sh` logs its own `status=restart_attempt` line into `cron.log` at the relaunch instant —
that is *my* `nohup`, not the `*/5` keepalive, and it is the cheap proof the relaunch half ran.

Rollback artifacts (700/600) — `~/backups/deploy-f894801-20260810T043151Z/`:
- `grant_watch.db.vacuum` 25,096,192 B sha `0bec2cf8244c97642a48688fa53e8dcf9c8b851e08e07070e459e0577ac6c986`
  (schema 37 copy, integrity ok). **Fifth consecutive deploy with this identical sha** — the same
  cheap "no DB content change in between" corroboration.
- `code_at_cadfefe.tar.gz` 66,873 B sha `78f6d609b0d4bcbcc155216549de25e3993b29e73b0582b64af56d6d95bdb329`, 13 members, `gzip -t` OK
- `env.bak`, `crontab.bak`, `crontab.pre-announce.bak` (both == pre-edit live), `deployed_revision.bak`

## The 13th cron line

`0 8 * * 1-5 cd ~/grants_agent && .venv/bin/python -m grant_watch.cli announce --execute >> cron.log 2>&1`
Appended only. All 12 originals proven byte-identical **individually** with `grep -qxF`, plus
`head -12 | sha256` == the old whole-file sha, plus `diff` showing exactly 1 added / 0 removed.
08:00 PT now has **three** jobs on the same SQLite file (announce, `remind` `*/30 8-16`, `nudge`
`*/30 8-15`) — no contention observed, but it is the first triple overlap on this box.

## FINDING 1 — the shortened wording did NOT reach the two messages it was written for

`capability_asks.correction` is a **DB column**, seeded once from
`data/capability_asks/unmet_asks_20260809.json`. `capability_asks.record()` INSERTs and swallows
`IntegrityError` on a duplicate, so **re-running `capability-seed` skips existing rows and never
updates them.** Shipping the edited JSON therefore changed nothing for the 5 already-seeded asks.

Measured on prod at Mon 2026-08-10T17:00Z, ask 1 (Kerry, `email_results`):

| | length | text |
|---|---|---|
| **as deployed** | **236** | `…you asked: "Email those to kerry@monarchconnected.com" I couldn't send email then — and I talked about you in the third person while telling you so, which can't have helped. I can now — want me to send it?` |
| if re-seeded | 176 | `…I couldn't then, and I answered you clumsily on top of it. I can now — want me to send it?` |

So the brief's "255 → ~140" is wrong twice: it is **236 today**, and even a re-seed only reaches
**176**, because the ask_text quote (41 chars, under the new 70-char trim) and the mention and the
offer are all structural. Only variant **b** is short (126) — and b does **not** end in a question.
Asks 3/4/5 have an empty `correction` in both DB and file, so they are unaffected.
**Rule: when a "wording change" edits a `data/*.json` seed, check whether the string is already
in the DB — the deploy moves the file, not the row.**

## FINDING 2 — the announcement's email line is false for every rep except Chase

`OUTREACH_TEST_EMAIL` **is set** on the droplet and `resend_client.recipient_for()` honours it for
every caller. Measured by identity comparison (value never printed):
`mail_would_go_to_THEM` = **False** for Kerry, Nelly and Jocelyn, **True** only for Chase.
The announcement says *"I can email you a list of leads directly."* Resend is configured and the
send will succeed — it just lands in the test mailbox, and `email_results` then reports
`"Sent it to <test address>"` back to the rep. Compounding: the FIRST capability nudge to fire
(Monday 09:54 PT, Kerry, `email_results`) offers exactly that, so the person the announcement
reaches first is the one it is wrong for. Predicted by [[deploy-b4a8046-reminders-email]].

## Monday 2026-08-10, measured on the deployed bytes

Band is now 08:30–**14:30** PT (was 15:00). Drawn slots **C01DGT9D11D → 09:54 + 14:11 PT**
(playground `C0B02721MNK` → 08:32 + 13:51). Both are reachable on the `*/30 8-15` cron (ticks 10:00
and 14:30) **and** inside Kerry's Eastern gate — 14:30 PT = 17:30 ET < 18:00 — so the
Eastern-rep ceiling recorded in [[deploy-cadfefe-nudge-slots]] is genuinely closed.
Queue at 10:00 PT: **44 candidates**, 23 `card_unengaged` / 11 `crm_preview_expired` /
5 `capability_now_available` / 3 `card_escalated` / 1 blocked / 1 partial. The head sits at
**position 24** — the 24 ahead of it are all `stale`. Head: `capability_now_available` id 1,
`suppress_reason=''`, `pacing_reason(force=False)=''`, `in_window=True`, variant `a`.
`followup_nudges` still **0 rows**; `announcements` has exactly 1 row, `posted_at IS NULL`.

`capability_asks.available_since` was set for all 5 rows at **2026-08-10T02:08:2x UTC** (19:08 PT,
matching the DB mtime) — i.e. the capabilities were declared out of band earlier that evening, NOT
by this deploy. `announce.run()` does **not** call `mark_available`; the announcement's
`capabilities` list is stored but inert. Declaring stays a separate operator act.

Related: [[deploy-cadfefe-nudge-slots]], [[restart-means-relaunch]], [[deploy-mechanism]],
[[row-get-wrong-column-false-null]], [[nudge-variant-ab-is-inert]], [[ssh-rate-limit-and-stdin-traps]].
