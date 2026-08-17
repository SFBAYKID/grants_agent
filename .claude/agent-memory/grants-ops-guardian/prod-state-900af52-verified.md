---
name: prod-state-900af52-verified
description: CURRENT PROD 2026-08-17 - 900af52, schema 47 UNCHANGED, PID 198537, 6 files, 0.211s outage, no cron pause, DM venue live
metadata:
  type: project
---

**PRODUCTION IS `900af5297bfb9cb09dbe7831dc2406919d56586f`** (origin/main head,
"Merge branch 'fix/enrich-orgs-defects-20260813': Grant can be DMed, by the roster only").
Deployed 2026-08-17 by the guardian. Supersedes [[prod-state-87d4e00-verified]].

**Why:** Grant could not be DMed. The Slack App Home "messages tab" checkbox was off AND
the code refused DMs independently; Chase flipped the checkbox the same day, so shipping
this closed the window where a rep types into the DM and is silently ignored.
**How to apply:** this is the rollback fingerprint and the current-state reference.

## The numbers

- **6 deployable files** (11 delta paths − 5 `.claude/agent-memory/**`): 4 M + **2 A**
  (`grant_watch/slack/venues.py`, `tests/test_direct_messages.py`). All **6/6
  byte-identical** to the pinned commit's blobs; second rsync pass **empty**; delete
  preview showed **zero** deleting lines so `--delete` was omitted.
- **NO MIGRATION. Schema 47 → 47.** Tables 52, indexes 92, columns untouched.
- PID **124668 → 198537**, exactly one listener. OLDPID matched what
  [[prod-state-87d4e00-verified]] recorded ⇒ **no out-of-band restart since 2026-08-13.**
- **Outage 0.211 s measured** (kill 19:23:59.862Z → up 19:24:00.079Z). Sub-second,
  like the `0223c10` deploy — not the 116 s migration window of `87d4e00`.
- Tracebacks **13 → 13**. `bot.log` 1152 → 1154 (exactly the two boot lines).
- `.env` **byte-identical**, sha `e168372900965e…`, size 3953, mode 600 — same sha as
  87d4e00, so the env has not moved since 2026-08-13.
- Crontab **byte-identical**, sha `34002d4bc67e…`, 25 lines / 10 active — proven by
  `cmp` against a laptop-side copy, not a recomputed sha.
- `leads` 10781 → 10781, `followup_nudges` 35 → 35, `contacts` 178, `posts` 39,
  **FK orphans 2 → 2 compared**. `integrity_check=ok` on the COPY.
- Droplet pytest on the two shipped test files: **22 passed**.

## NO CRON PAUSE, and that was correct

Deliberately did not pause the crontab, reversing the 87d4e00 posture. The reasoning,
not the conclusion: the pause exists because a **long** window lets the `*/5` keepalive
relaunch onto a **half-synced tree** and lets the `*/10` watchdog apply migrations. Here
the sync completed and was byte-verified BEFORE any kill, so the keepalive could only
ever see a complete tree; the old process held old code in memory and stayed healthy;
`run_bot.sh` is pgrep-guarded so it cannot start a second listener. The only exposed
window is pkill→relaunch, issued as one remote command, and it measured 0.211 s.
**Pause for a migration window; do not pause for a restart.** Confirmed after the fact:
keepalive logged `status=restart_attempt at=19:23:59Z` (that was my own relaunch) then
`healthy` at every tick since, and watchdog/nudge/remind/drip all ticked clean on the
new code within 12 minutes.

## Backups taken (keep)

- `~/grant_watch.db.pre-900af52.20260817T191924Z` — SQLite backup API from a `mode=ro`
  source, `integrity_check=ok`, schema 47, leads 10781 verified **against the COPY**.
- `~/.deployed_revision.bak.pre-900af52.20260817T191924Z` (holds `87d4e00…`),
  `~/crontab.backup.pre-900af52.20260817T191924Z`,
  `~/pre-900af52-overwritten.20260817T191924Z.tar.gz` (4 members, 45 KB).
- `~/grant_watch.db.pre46.20260813T180041Z` confirmed **UNTOUCHED** — still Chase's
  migration-46 rollback.

## What the DM gate actually does, proven on the deployed bytes

`venues.py` (201 lines, new) moves the boundary from the ROOM to the PERSON. Measured
read-only on the droplet, both directions:

- `in_configured_channel({"channel":"D…","channel_type":"im"})` → **False** — the channel
  gate was NOT loosened; it still refuses every DM.
- `is_approved_sender(<real roster id>)` → **True** (a guard needs a TRUE before you
  trust its FALSEs); `is_approved_sender("U000000000")` → False; `(None)` → False.
- `may_converse(roster DM)` → **True**; `may_converse(stranger DM)` → **False**.
- `is_direct_message({"channel_type":"im","channel":"C01DGT9D11D"})` → **False** — a
  payload claiming `im` while naming a `C…` room cannot borrow the DM path.
- Roster loaded 6 rows from `config/reps.json` (mode 664, sha `9e16c3fb…`, unchanged by
  this deploy). A malformed roster authorizes nobody (bare `except` → False).

Bot token scopes read live from the `auth.test` `x-oauth-scopes` header (17 total):
`im:history`, `im:read`, `im:write`, `chat:write` all **GRANTED**. Bot user
`U0BH0ESRJ4W`, team `T01DFJLFKE3`.

**NOT verified: an actual DM end-to-end.** Requires a human to type at Grant; the
`message.im` event subscription is not readable through the bot token, and the guardian
sends no Slack messages without authorization.
