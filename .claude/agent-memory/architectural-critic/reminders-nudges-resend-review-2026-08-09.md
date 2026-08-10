---
name: reminders-nudges-resend-review-2026-08-09
description: Review of 60c8907..b4a8046 + uncommitted escalation work — four REPRODUCED defects (parked-lead escalation, stop_followups false confirmation, poison-spec queue wedge, drip ignores opt-out) and what genuinely holds
metadata:
  type: project
---

Review of the reminders / Resend / capability-asks / nudge-escalation surface on
`review/rich-award-card-campaign-20260723` (commits `60c8907`, `f62017e`, `b4a8046`
plus UNCOMMITTED escalation work in `nudges.py`, `roster.py`, `reps.json`).
Verdict: **Rejected — Requires Rework.** Four defects were EXECUTED, not theorised.

**Why:** three of them produce a false sentence in a message to a named colleague,
which is rule 1 pointed at a person; the fourth silently stops the whole feature.

**How to apply:** when any of these areas is touched again, check these four first.

## The four reproduced defects

1. **One-shot suppression keyed on a literal subject_kind string.**
   `nudges.suppress_reason` gates `lead_parked` / engagement re-checks on
   `subject_kind == "card_unengaged"`. `card_escalated` shares the SAME post and the
   same `observed` dict but matches none of those branches, so a lead the rep marked
   `not_relevant` suppresses the channel nudge and STILL DMs the manager "went to
   <@rep> and nothing's come back here since". Reproduced end to end.
   LESSON: a new subject kind that derives from an existing one inherits its
   producer but NOT its suppression. Derive the checks from the SUBJECT (the post),
   not from the kind label.

2. **The freeze validates, the thaw coerces.** `reminders.create` filters a
   model-written spec to `SPEC_KEYS` but never type-checks it; `search_kwargs` does
   `float(value)` at DELIVERY time. `{"amount_min": "$500,000"}` stores fine and then
   raises inside `reminder_worker.run` OUTSIDE any try, before the per-reminder loop
   can continue. One malformed spec wedges the entire reminder queue forever —
   including unrelated reminders with no spec at all. Same wedge class as
   [[drip-wedge-class-remaining-paths]].
   LESSON: validate a frozen call's arguments at FREEZE time, and isolate each item
   in a worker loop.

3. **A narrow opt-out returns the broad confirmation.** `stop_followups` with
   `scope="nudges"` (the schema's own enum invites it) inserts a nudges-only row —
   `reminders.set_optout` only cancels reminders for `all`/`reminders` — then returns
   the unconditional "I've stopped following up with you, and cancelled the reminders
   you had running." Reproduced: the reminder is still `active` and still due.
   Note the schema description ("only narrow it if they were specific") CONTRADICTS
   the system prompt ("do not ask which kind").

4. **Two of four proactive senders never see the opt-out.** `is_opted_out` is called
   only by `nudges.py` and `reminder_worker.py`. `drip.py` still emits
   `territory.routing_line` → a literal `<@U…>` ping, and `salesforce_followups.py`
   posts unconditionally. `stop_followups` promises "ALL of Grant's proactive
   messages" and cannot deliver that.

## What genuinely holds (do not re-litigate)

- **No double-send.** Reserve-before-send is correct in both workers; the UNIQUE keys
  arbitrate, `advance()` is reached even when `reserve` returns None, and a lost
  `complete()` self-heals on the next tick. Two concurrent runs cannot duplicate.
- **The Resend recipient really is unrepresentable.** No parameter anywhere accepts an
  address; `requester_slack` is a `run_tool` keyword, never model `args`. The only
  redirect is the operator-set `OUTREACH_TEST_EMAIL`, and it is disclosed in the
  returned outcome.
- **`search_kwargs` allowlist holds.** `db_path`, `requester_slack`, `export`,
  `result_scope`, `with_contacts` are all excluded, and `search.py` clamps
  `max(1, min(limit, …))` so a negative limit cannot become SQLite's `LIMIT -1`.
- **The migrations 14-22 move is clean.** Bodies are byte-identical except
  `_add_column` → `_add_col` (behaviourally identical helpers); `Migration` name
  strings for 1-28 are unchanged; only migration 15 mutates data and its body is
  unchanged. The sqlite_master diff missed nothing that matters here.
- **The seeded `unmet_asks_20260809.json` is self-consistent** — every `asked_at`
  equals its `message_ts` to the second, and every `thread_ts <= message_ts`.

## The structural gap behind all of it

`capability_asks.record()` has NO live caller — only `cmd_capability-seed`. Migration
34's docstring claims "a row is written at the moment of refusal, by the code that
knows exactly which capability was missing." No refusal path writes one. The ledger is
a historical seed, so the NEXT unmet ask is lost exactly as Kerry's was.
`capability_asks.close()` has zero callers, so no ask is ever marked answered.
