---
name: nudge-replies-are-silently-dropped
description: A plain threaded reply to ANY follow-up nudge is dropped at grant.py's `post is None and not general_thread` gate — nudges write no posts row and no conversation-thread row, so every nudge thread is a dead end unless the rep @-mentions Grant
metadata:
  type: project
---

Diagnosed read-only 2026-08-12 after Anthony Dambrosio replied to an 11:45 nudge and
Grant never answered.

## The mechanism, proven end to end

`cli nudge --execute` posts a **new top-level channel message** and records it in
`followup_nudges.slack_ts`. It does **not** write a `posts` row and does **not** write a
`slack_conversation_threads` row. `on_message` (deployed `grant_watch/slack/grant.py`,
line 323 at `9fb6813`) then does:

```python
post = db.find_post_by_ts(conn, event["channel"], thread_ts)
general_thread = db.is_conversation_thread(conn, workspace, channel, thread_ts)
if post is None and not general_thread:
    return          # <- silent, no receipt, no log line, no message
```

Both lookups miss, so the reply is discarded **before** `claim_slack_event`. That is why
there is no `slack_event_receipts` row and nothing in `bot.log`: the drop is upstream of
every recording mechanism.

**It is not specific to one nudge.** All six delivered nudges checked on 2026-08-12 had
`posts_row=0` AND `convo_thread_row=0` — `capability_now_available` x3, `card_unengaged`
x2, `card_escalated` x1. Every nudge Grant has ever sent is a thread a rep cannot reply
into.

**Why it looked like it sometimes works:** `@Grant` in the reply routes through
`on_app_mention` (line ~217), a different handler with no posts-row requirement. Kerry's
"Yes" on 2026-08-10 landed because of that. So the rule is: **@-mention works, plain
threaded reply does not** — and the nudge wording invites a plain reply ("Want me to find
a contact?").

## Second-order damage

`followup_nudges.engaged_at` stayed **NULL** for the reply. The ledger therefore believes
the rep never answered, so escalation/`offer_unanswered` logic will keep chasing a person
who *did* answer. That is the most embarrassing failure mode this feature has.

## Ruled out, with evidence — do not re-litigate these

- **subtype / `thread_broadcast`** (the `nudge_silence._is_human` blind spot): Anthony's
  message carried `subtype: None`, keys `['blocks','client_msg_id','parent_user_id',
  'team','text','thread_ts','ts','type','user']`. Read from `conversations_replies`, not
  inferred. The listener's subtype gate is a real latent bug but it did **not** fire here.
- **Wrong channel**: `C01DGT9D11D` is first (primary) in `SLACK_CHANNEL_ID`.
- **Restart mid-flight**: listener PID continuous, `0` keepalive restart lines that day,
  288 healthy ticks, 12/hour across the whole window.
- **Stuck `processing` receipt**: the only two are 2026-07-18 and 2026-08-10, both in the
  playground channel, unrelated threads.

## Identity — and a correction to this note's own first version

**`U01DFJWQQJ3` is Anthony Dambrosio, who is BOTH a rep AND the manager.** This note
first said CLAUDE.md was wrong to call him "the manager". **That was an over-correction
and CLAUDE.md is right.** `users_info` establishes the person, not their role here, and
the role lives in `config/reps.json`: his row carries `"manager": true`, with Chase's
own words in the file's comment — *"Since Anthony is the manager"* (2026-08-09). Exactly
one row may carry it, and `roster.manager_slack_id()` fails closed if zero or several do.

So "the escalation goes to the manager" is accurate. **The real thing worth knowing is
narrower and still true:** for a lead in Anthony's own territory, the manager and the
rep are the SAME person, so an escalation about a rep's silence would be addressed to
the person it is about. It did not arise here — the North Palos card was
`routing_reason='unassigned'` with no rep tagged, so there was no rep to name and
escalating to the manager was correct.

**The lesson is about method, not about Anthony:** a live API answers who somebody IS;
the repo's reviewed config answers what ROLE they hold. Reading the first and
contradicting the second is how a correct record gets "fixed" into a wrong one.

## How to apply

Before concluding "Grant ignored someone", check **which** message they replied under:
`SELECT * FROM followup_nudges WHERE slack_ts=?` vs `SELECT * FROM posts WHERE ts=?`.
A hit in the first and a miss in the second IS this defect. Always run the
`posts`-lookup control against a known card ts in the same query batch — a zero from a
lookup you have never seen return one is not evidence.

Related: [[readonly-db-forensics-recipe]], [[prod-state-9fb6813-verified]],
[[grant-slack-event-flow]], [[nudge-queue-state-20260809]].
