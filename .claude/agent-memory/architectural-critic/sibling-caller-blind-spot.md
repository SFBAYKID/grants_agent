---
name: sibling-caller-blind-spot
description: Recurring pattern in this repo — a defect is found and fixed in ONE caller of a class while the sibling caller (often the one production actually runs) is left broken. Three live instances found 2026-08-09.
metadata:
  type: project
---

Every cross-cutting invariant in this repo has been fixed **one caller at a time**, and
the caller that got missed is repeatedly the one production actually exercises.

**Why:** the fix is written from the incident (one thread, one message, one worker), so
the patch lands where the evidence was. Nothing enumerates the other call sites.

**How to apply:** when any of these invariants is touched, grep for EVERY caller and
name them in the commit message. Do not accept "fixed" until the sibling list is empty.

## The three instances confirmed 2026-08-09 (audit of `771a68f`)

1. **`presentation.for_human` — unmediated tool text.** Its own docstring says
   "Anything that reaches a rep unmediated must come through here." Callers:
   `reminder_worker.py:51,65` only. `slack/reminder_tools.py:203` (`email_results`)
   runs `search_leads` and mails the raw body to a rep's INBOX — so a no-match search
   delivers the literal `<model-note>Offer these to the user (with counts)…</model-note>`
   (emitted at `slack/search.py:658`) to a colleague. Same defect class as `2cf8058`,
   different caller, and `email_results` also has no broadening salvage, so it dead-ends.

2. **`reminders.is_opted_out` — proactive senders.** `stop_followups(scope="all")`
   promises "I've stopped following up with you". `drip.py:765` now drops its routing
   mention on opt-out. `campaign/card.py:156,221` — the RICH card, which is the card
   production posts (`GRANT_RICH_CARD_ENABLED=1` since 2026-08-06) — still emits
   `<@user>` with no opt-out check. `slack/salesforce_followups.py` never checks either.

3. **Who the card tagged.** `slack/nudges.py:232` RECOMPUTES the tagged rep with
   `territory.owner_for_state`. The rich card routes through
   `campaign/routing.py` (SF call owner → account owner → opportunity owner → territory).
   For any relationship-routed card the nudge names a DIFFERENT person, and
   `_escalation_message` then tells the manager "went to <@X>" about someone who was
   never tagged. The correct value is already persisted:
   `posts.snapshot_id → rich_card_snapshots.slack_user_id`.

## Related structural fact

`capability_asks.record()` still has NO live caller (only `cli capability-seed`), and
`capability_asks.close()` has ZERO callers anywhere including tests — dead code under
rule 5. `migrations_nudges.py:94` claims "A row is written at the moment of refusal, by
the code that knows exactly which capability was missing." That sentence is false.
See [[reminders-nudges-resend-review-2026-08-09]].
