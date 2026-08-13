---
name: pkill-f-self-match-kills-your-session
description: pkill -f "grant_watch.slack.grant" over ssh matches your OWN command line and kills the session mid-deploy; use the [g] bracket trick
metadata:
  type: project
---

**`pkill -f "grant_watch.slack.grant"` RUN OVER SSH KILLS YOUR OWN SESSION.**

**Why:** the remote command arrives as `bash -c '<your whole script>'`, and that script
CONTAINS the literal string `grant_watch.slack.grant`. `-f` matches the full command line,
so pkill matches the listener *and* the shell running pkill. The shell dies, ssh returns
**255**, and every step after the `pkill` in that script silently never runs. Happened
2026-08-13 mid-deploy: the listener stopped correctly but the verification loop, the
"REMAINING=" count and the process listing all vanished, so the transcript looked like the
kill had failed when it had actually succeeded.

This is the same self-match family as the `pgrep -f` note in [[env-zoominfo-20260809]],
but with teeth: `pgrep` gives a wrong number, `pkill` ends the session.

**How to apply:** never let the literal process pattern appear unescaped in a remote
command that also greps or kills on it.

```bash
# Counting - bracket the first character so the pattern cannot match itself:
ps -u grantwatch -o cmd | grep -c "[g]rant_watch.slack.grant"

# Killing - the launcher's own regex form is already bracket-safe:
pkill -u grantwatch -f "grant_watch[.]slack[.]grant"
```

`run_bot.sh` gets this right already (`pgrep -f "grant_watch[.]slack[.]grant"`) - copy its
pattern rather than writing a fresh one.

**Scope was never at risk:** `-u grantwatch` bounded the kill to the tenant's own
processes, so the blast radius was one ssh shell, not another tenant. Keep the `-u
grantwatch` on every pkill for exactly that reason.

**Recovery:** reconnect and measure before assuming anything - the listener WAS down, the
crontab WAS still paused, and the correct action was to carry on, not to re-run the kill.
Same discipline as [[verify-the-premise-not-the-claim]]: an ssh exit 255 tells you the
session died, not that the work failed.
