---
name: grant-bot-silent-llm-fallback
description: bot.log silence is NOT health — the LLM-path fallback reply logs nothing; how to diagnose Grant LLM failures on the droplet
metadata:
  type: project
---

The Grant bot's "I couldn't finish that request safely" Slack reply comes ONLY from
`_parse_final` in `grant_watch/slack/conversation.py` (~line 323) and that path writes
NOTHING to `bot.log` — bot.log only ever contains startup pairs ("Grant is listening" /
"Bolt app is running!"). Verified live 2026-07-16 during an all-LLM-requests-failing
incident: zero log lines were produced by hours of failing requests.

**Why:** API exceptions re-raise out of `respond()` and grant.py replies with a
DIFFERENT text ("I'm having trouble thinking right now (ExcName)…" or
`_fallback_answer`). So the "finish that request safely" string specifically means the
Anthropic call returned 200 and the model's final message failed the strict
`{"intent","reply"}` JSON-envelope parse or the `_contains_internal_language` filter
(rejects snake_case tokens, braces, *Error names — including URLs with underscores).

Re-verified 2026-07-16 22:13Z, and the gap is WIDER than LLM-path: tool-execution
failures (find_contact + salesforce_lookup erroring on a live request) also wrote
ZERO bytes. Proof method that works: `run_bot.sh` launches with
`nohup ... >> bot.log 2>&1`, so ALL bot stdout+stderr goes to bot.log — and bot.log's
mtime stayed frozen at the process start time through the failing window. mtime-vs-
process-lstart is the fastest "nothing was logged anywhere" proof. The only record of
a tool error is the text Grant itself posts in Slack; capturing real tracebacks needs
a code change (owner-side), not an ops action.

**How to apply:**
- Never read bot.log silence as "no errors". Diagnose by symptom text: exact fallback
  string = 200-but-unparseable model output; "trouble thinking right now (X)" = raised
  exception X (bad key / 404 model / egress).
- Presence-only env checks that worked: `grep -c '^KEY=..*' .env` and, for the LIVE
  process, `tr "\0" "\n" < /proc/<pid>/environ | grep -c '^KEY=..*'` (counts only,
  never values). Egress probe without the key: keyless curl to
  https://api.anthropic.com/v1/messages returns 405 when egress is fine.
- `GRANT_MODEL` was NOT set in droplet .env (2026-07-16); code default is
  `DEFAULT_MODEL` in conversation.py (`claude-sonnet-5` then).
- The deployed revision can be a side-lineage commit NOT on local main — always
  compare droplet files to `git show <deployed_rev>:path` by sha256, not to HEAD.
- Also seen 2026-07-16 and flagged: droplet .env has SALESFORCE_CAMPAIGN_WRITES_ENABLED=1
  while CLAUDE.md said Campaign writes stay disabled pending approval — confirm with
  Chase whenever touching Salesforce config. See [[tenant-and-layout]] and
  [[deploy-mechanism]].
