---
name: deploy-7837cda-watchdog
description: Deploy cdfdaf9→7837cda on 2026-08-10 (CURRENT PROD) — code-only, schema 38, PID 36059, 0.153s outage; crontab 19→25 lines with a 24/7 watchdog; and the behaviour change that matters most — the bot now calls db.connect() AT BOOT, so a restart DOES apply migrations
metadata:
  type: project
---

> **SUPERSEDED BY `8cb557a` — the boot pass documented below was REMOVED the same night.**
> A restart is inert again: no `db.connect()` at boot, no migrations applied, no Slack edit.
> See [[deploy-8cb557a-watchdog-boot-revert]]. The cron tick and `watchdog.py` are unchanged.

**LIVE 2026-08-10T06:31:08Z (droplet Sun 23:31:08 PT).**
`cdfdaf917ae752f1de09b2b4b75991e0ed652285` → `7837cdad8bc5d5b8089cc5b72cd4d0d6398faa83`
(1 commit). **Schema stayed 38.** Listener **34654 → 36059**, **0.153 s outage**.

Delta 8 paths = **5 deployable** + 3 `.claude/agent-memory/**`. 3 mods + **2 adds**
(`grant_watch/slack/watchdog.py`, `tests/test_watchdog.py`). Pre-image 3/3 == `cdfdaf9`
blobs, adds proven absent. Archive `77906731…b7c3`, 26,285 B, member set == delta, upload
byte-exact. rsync itemize 3 `>fcsT` + 2 `>f+++`, 0 deleting, catch-all empty, second pass 0,
`find -cnewer` exactly 5, post-image 5/5. **Closure 120/120 byte-identical to `7837cda`.**
Import closure **117/117**. `.env` `9b68bc18…c634` and `run_bot.sh` `07773019…06bb`
unchanged. crontab `11b4a201…4964` (19 lines) → **`34002d4bc67e21f59d1ae6ba11054a9658caae5a2a10b0b5b8345909de6d7ab5`**
(25 lines, 2475 B, **10 active jobs**). Disk 68%, 16 G. `~/backups` **493 M**.
Rollback: `~/backups/deploy-7837cda-20260810T063004Z/` (db.vacuum `1cdf5117…`,
`code_at_cdfdaf9.tar.gz` `ae3d3012…` 3 members, crontab.bak, deployed_revision.bak).

## A RESTART NOW APPLIES MIGRATIONS — [[deploy-mechanism]] is partly superseded

`grant.py:main()` previously had **no** startup `db.connect()`, which is why that memory
says "A BOT RESTART DOES NOT APPLY SQLite MIGRATIONS". This deploy adds a boot-time
watchdog pass that opens the **migrating** `db.connect()`:

```python
_conn = _db.connect()
print(_watchdog.run(app.client, _conn, bot_id=..., dry_run=False))
```

So from `7837cda` onward a plain restart DOES run `apply_migrations`. That is convenient
but it removes the old separation — **a future migration deploy can no longer assume the
bot is only reading**. Keep applying migrations with the bot DOWN and verify
`schema_migrations` MAX directly; do not start relying on the restart to do it.

**The boot pass also runs `dry_run=False`** — it edits Slack. A restart is now a
message-mutating act, however narrowly. It is wrapped in a bare `except` that prints
`watchdog skipped at boot (<Error>)`, so it can never block boot.

## Verifying the retirement of `sweep_orphaned_spinners`

A combined `grep -c` over the retired symbols returns **1**, and that is NOT a failure —
line 818 is a prose comment that names `primary_channel_id()` while explaining why the
sweep was removed. Check each symbol individually (`def sweep_orphaned_spinners`,
`_SPINNER_RE`, `_ORPHAN_TEXT`, `_ORPHAN_MIN_AGE_S` → 0 each; the `..config` import line no
longer carries `primary_channel_id`) and show the comment as the one expected textual hit.
Same family as every other "never trust a count from a pattern you have not seen match".

## Watchdog behaviour, measured on the deployed bytes

`STUCK_AFTER` 20 min, `TOO_OLD` 3 days, `LIMIT 50`, requires `reviewed_at IS NULL` and
non-null channel+thread. It edits **only Grant's own last message in the thread** and only
when it still matches `^[/\\|—] \S.{0,80}…$`; anything else is classed "already answered"
and the receipt is closed with **no Slack write**.

Order matters when deploying this: **run the dry run BEFORE the restart**, because the boot
pass executes for real and consumes the state you wanted to observe.
- dry run (pre-restart): `[dry-run] watchdog: 0 stranded spinner(s) replaced, 1 already answered`, DB size+mtime identical ⇒ read-only proven.
- boot pass: `watchdog: 0 stranded spinner(s) replaced, 1 already answered` → set `reviewed_at=2026-08-10T06:31:09.760617+00:00` on `Ev0BP34QSCN6`, no `chat_update`.
- `watchdog --execute` after that: `watchdog: nothing stuck`.

**`_mark_reviewed` sets `reviewed_at` but NOT `state`.** Both rows still read
`state='processing'`, so a naive `SELECT state, COUNT(*)` monitor will show "2 stuck"
forever. Judge by `reviewed_at`, not by `state`.

**`Ev0BK2QATN5N` (2026-07-18, 22 days) is permanently unreachable** — beyond `TOO_OLD`, so
it is filtered before the loop and will never be reviewed or closed. Confirmed it does NOT
feed a follow-up: `thread_abandoned` candidates = **0**.

## Cron: 19 → 25 lines, and why the watchdog is at `3-59/10`

`3-59/10 * * * *` = every 10 minutes at :03,:13,…,:53. Offset deliberately: this is the
only job that runs 24/7, and :00/:15/:30/:45 already carry drip (`*/30`), remind (`*/30`),
nudge (`*/15`), announce (`0 8`) and the `*/5` keepalive on one SQLite file.
**Proven firing**: `cron.log` gained `watchdog: nothing stuck` between the 06:31:08
restart_attempt and the 06:35 keepalive ⇒ the 06:33 tick ran. Append-only edit; guards
included head-of-new == whole-of-old by sha, all 19 originals `grep -qxF`, all 9
pre-existing active jobs asserted, and `removed=0`.

## The transport trap bit me again — §1b of [[ssh-rate-limit-and-stdin-traps]]

`ssh host '… $(crontab -l | cut -d\" \" -f1) … pgrep -f \"pattern\"'` — hand-escaped double
quotes inside a single-quoted ssh argument. The remote shell received corrupted arguments
and printed **`PID=`** and **`CRONTAB_SHA=`** (empty), i.e. "the bot is dead", on a healthy
bot, plus `cut: '"': No such file or directory`. Re-ran from a script file: PID 36059,
count 1. **Any remote command containing quotes goes in a FILE run with `bash -s`.** The
rule was already written down; obeying it is the part that needs practice.

Related: [[deploy-cdfdaf9-threadscan]], [[deploy-mechanism]], [[restart-means-relaunch]],
[[ssh-rate-limit-and-stdin-traps]], [[verify-the-premise-not-the-claim]].
