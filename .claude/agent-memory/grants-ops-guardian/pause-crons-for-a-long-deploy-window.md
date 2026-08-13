---
name: pause-crons-for-a-long-deploy-window
description: A migration window longer than ~4 min needs the tenant crontab paused - the */5 keepalive relaunches the listener and 10 jobs apply migrations under you
metadata:
  type: project
---

**FOR ANY DEPLOY THAT KEEPS THE BOT DOWN LONGER THAN A RESTART, PAUSE THE TENANT
CRONTAB FIRST.**

**Why:** the droplet has **10 active cron jobs** and in business hours there is no gap
wider than ~4 minutes. Two of them actively fight a deploy:
- `*/5 * * * * run_bot.sh` - the keepalive **relaunches the listener**, so a killed bot
  comes back within 5 minutes, possibly onto a half-synced tree.
- `3-59/10 * * * * watchdog --execute` - a fresh CLI process calls the MIGRATING
  `db.connect()`, so **cron applies migrations**, not the restart
  ([[deploy-mechanism]]). Plus `nudge` `*/15 8-14`, `remind`/`drip` `*/30`.

Running a 6-migration data cutover against that is a race with a live writer.

**How to apply:** this is the grants tenant's OWN user crontab, so it is in scope. Pause,
deploy, restore from the exact backup bytes, verify the sha.

```bash
BK=~/crontab.backup.pre-<hash>.<UTC>
[ -e "$BK" ] && exit 1          # never clobber a backup
crontab -l > "$BK"; chmod 600 "$BK"
sed 's/^\([^#]\)/#PAUSED# \1/' "$BK" > ~/crontab.paused.<UTC>
crontab ~/crontab.paused.<UTC>
crontab -l | grep -cE '^[^#]'   # must be 0
# ... deploy ...
crontab "$BK"                   # restore from BYTES, never by un-commenting
crontab -l | sha256sum          # must equal the preflight baseline
rm -f ~/crontab.paused.<UTC>
```

**Restore from the backup file, not by reversing the `sed`.** Reversing an edit is a second
transformation that can differ; replaying the captured bytes cannot. Verified byte-identical
2026-08-13: sha `34002d4b…`, 10 active job lines, 25 total.

**Order the stopped window so it can be aborted cheaply.** Run the migrations and the
ledger cutover from a **staging tree** (`~/deploy_staging_<hash>`, a full `git archive` of
the pinned commit) against the LIVE database, and sync the 139 files into the live tree
only afterwards. Then a failure at any earlier step leaves the live tree on the old
revision and the old `.env`, which still boots. Syncing first would strand the listener on
new code it cannot start.

**Cost accepted:** ticks missed during the window (here one `watchdog` and one keepalive)
simply do not run. Both are idempotent repair jobs, so a skipped tick is harmless - but
say so in the report rather than letting the gap look like a fault.

**If the session dies while paused, the crons stay off and Grant stays down.** Keep the
window tight and restore as soon as the listener is verified up. See
[[prod-state-58b3e24-verified]].

**`crontab -r` is a fine way to pause, and simpler than the `sed` dance** — provided the
backup bytes exist in TWO places first. On 2026-08-13 (87d4e00) I captured `crontab -l`
to the laptop **and** to `~/crontab.backup.pre-87d4e00.<UTC>` before `crontab -r`, then
restored with `crontab <backup>` and proved it byte-identical with `cmp` against the
laptop copy, not just a sha recomputed on the droplet. Two independent copies means a
failed restore is recoverable from the side that did not fail.

**Cost the window against the tick calendar before choosing it — a pause is not
automatically expensive.** The 87d4e00 window ran 14:04:04 → 14:06:49 PDT and suppressed
**exactly one tick**: the `*/5` keepalive at 14:05, which was the very job replaced by the
manual `nohup bash run_bot.sh`. Watchdog (`3-59/10`) next fired 14:13, `nudge`
(`*/15 8-14`) 14:15, `drip`/`remind` (`*/30`) 14:30 — all after the restore. The gaps
between `:06` and `:13` past the hour are the cheap ones on a weekday afternoon. Compute
which ticks fall inside the window and report them by name.
