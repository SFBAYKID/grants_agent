---
name: restart-means-relaunch
description: A deploy restart is pkill THEN `nohup bash run_bot.sh` — waiting for the */5 cron keepalive instead turned a sub-second outage into 47 seconds on 2026-08-09
metadata:
  type: feedback
---

**A restart is two steps. `pkill` alone is an outage, not a restart.**

```bash
pkill -f 'grant_watch[.]slack[.]grant'
nohup bash run_bot.sh >> cron.log 2>&1 < /dev/null &     # <-- this half is not optional
```

**Why:** [[deploy-mechanism]] records a sanctioned path phrased as "pkill, then let the */5
cron keepalive relaunch the bot". Read literally that is true — `run_bot.sh` has an
idempotent pgrep guard and cron does fire it — but it only recovers at the next 5-minute
boundary. On the 0716a17 deploy I wrote a restart script that pkill'd and then merely
*polled* for a new PID; the poll timed out, `PID_COUNT=0`, and Grant sat dead for **~47
seconds** until I relaunched by hand. Every deploy before it explicitly ran
`nohup bash run_bot.sh` and measured 0.2–4 s. The keepalive is the crash safety net; it is
not the deploy's relaunch mechanism.

**How to apply:** in any restart helper, the relaunch line must be present and must be
*asserted* — after the wait loop, if `pgrep -c` is 0, relaunch immediately rather than
proceeding to the log check. Two symptoms name this failure instantly and should be printed
unconditionally: **`PID_COUNT=0`** and an **empty fresh log region**. A verifier that only
greps for "Bolt app is running" reports a vague failure; printing the PID count and the raw
post-offset log region says *why* in one look.

Related: [[deploy-0716a17-org-profile-gate]], [[deploy-mechanism]].
