---
name: slack-workspace-id-missing
description: SLACK_WORKSPACE_ID has never existed in the droplet .env, so every rich-card button click refuses; plus the proof techniques (run_bot.sh exports .env, so /proc/environ is authoritative)
metadata:
  type: project
---

**`SLACK_WORKSPACE_ID` was NEVER set in the droplet tenant `.env`** (verified read-only 2026-08-06:
0 lines matching `^SLACK_WORKSPACE_ID=`, 0 matching the name anywhere, and 0 in all six
`~/.env.bak.*` pre-images going back to 2026-07-15). It is also absent from the running bot's
`/proc/633555/environ`. Consequence: `campaign/actions.py:_authorized_snapshot` fails its FIRST
gate (`if not expected_workspace or workspace != expected_workspace`) on **every** rich-card
button click, so both `rich_not_relevant` and `rich_persequor_draft` are dead in production until
the key is added AND the bot is restarted.

**Why:** the key was added to `.env.example` with the rich-card feature but never propagated to the
droplet during the 2026-07-24 (`e8ecf0c`) or 2026-08-05 enable. Nothing tested a live button click,
so the gap survived the enable checklist.

**How to apply:** the fix is `.env` append + bot restart — an `.env` edit alone is NOT enough
(long-lived bot). When enabling any feature whose code reads a NEW env var, diff the code's
`os.environ.get(...)` key set against the droplet `.env` key NAMES before declaring it live.
See [[rich-card-enable-20260805]] and [[rich-card-deploy-e8ecf0c]].

### Proof techniques worth reusing

- **`run_bot.sh` does `set -a; source .env; set +a` before `nohup .venv/bin/python -u -m
  grant_watch.slack.grant`.** So `/proc/<bot pid>/environ` IS authoritative for what the bot got
  from `.env` at start, even though `grant.py:main()` also calls `load_dotenv()`. Without that
  `source` line, `/proc/environ` would prove nothing (setenv from dotenv does not update it).
  Re-check `run_bot.sh` before trusting an environ-based conclusion.
- The real workspace/team id is recoverable READ-ONLY, two independent ways that agree:
  `SELECT DISTINCT workspace FROM slack_event_receipts` (also `slack_conversation_threads`,
  `crm_actions`) and a `WebClient(token).auth_test()` call. Both are read-only; auth.test posts
  nothing. Report shape+prefix only.
- **A one-off `load_dotenv()` with no argument CRASHES when the script is fed on stdin**
  (`assert frame.f_back is not None` in `dotenv/main.py:find_dotenv`). Always pass the explicit
  path: `load_dotenv("/home/grantwatch/grants_agent/.env")`. Extends [[oneoff-scripts-need-load-dotenv]].

### The refusal is invisible in bot.log

`proactive_actions.py` catches `(PermissionError, ValueError)` and replies in-thread; it never
logs. `bot.log` had ZERO occurrences of the refusal string, the snapshot id, `PermissionError`, or
any traceback, and its mtime was two hours BEFORE the click. Another instance of
[[grant-bot-silent-llm-fallback]]: a clean bot.log is not evidence a click succeeded — the
in-thread reply and the DB are the only ground truth.

### Redaction trap I hit

`rich_card_snapshots.render_inputs_json` embeds the contact email, name, and website in cleartext.
A column-NAME-based redaction filter does not catch it. When dumping a snapshot row, exclude
`render_inputs_json` and `fallback_text` explicitly.
