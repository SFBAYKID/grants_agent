---
name: grant-slack-event-flow
description: How Grant's Slack event handlers + slack_event_receipts work — key facts for diagnosing "mention answered but plain follow-up ignored"
metadata:
  type: project
---

Diagnosing why Grant answers an @mention but not a plain thread follow-up (esp. in PRIVATE channels).

**Both handlers exist in the deployed code** (`grant_watch/slack/grant.py`): `@app.event("app_mention")`
(on_mention) and `@app.event("message")` (on_message). So a missing-plain-follow-up is NOT a
missing-handler bug. on_message defers to on_mention when the text @mentions Grant, and (a1d2484)
stays SILENT if the message @mentions anyone ELSE.

**`slack_event_receipts` has NO event-type column** (cols: event_id, workspace, channel, thread_ts,
slack_user, state, received_at, finished_at, error, action_state, delivery_state, reviewed_at). You
CANNOT tell app_mention vs message receipts apart from the row. A row = an event that reached the app
AND passed enough gates to be "claimed".

**Receipt-recording asymmetry (critical when reasoning about absence):**
- on_mention records the receipt (`db.claim_slack_event`) right after the channel/bot/subtype gate — so
  EVERY @mention in a configured channel gets a receipt.
- on_message records the receipt ONLY AFTER a thread-ownership gate: the reply's thread must be a known
  drip post (`find_post_by_ts`) OR a registered conversation thread (`is_conversation_thread`); else it
  RETURNS before claiming. So a plain follow-up produces NO receipt if the event never arrived OR if it
  arrived in a thread Grant isn't tracking. Absence of a receipt is therefore CONSISTENT WITH, but not
  PROOF OF, non-arrival. (A thread becomes "registered" when Grant answers an @mention in it via
  `register_conversation_thread`; drip-post threads are matched by ts.)

**PRIVATE vs PUBLIC channel delivery (the usual root cause of the symptom):** Bolt only sees a `message`
event if Slack delivers it, which needs the Slack app's Event Subscriptions to include the right bot
event + scope: `message.channels`/`channels:history` for PUBLIC channels, `message.groups`/`groups:history`
for PRIVATE channels (also im/mpim variants). Socket Mode still requires these subscriptions. Classic
symptom: plain follow-ups work in the public playground (`C0B02721MNK`) but NOT in the private production
channel (`C01DGT9D11D`) because `message.groups` / `groups:history` is missing. This is a SLACK-SIDE app
config check (api.slack.com dashboard), OUTSIDE the droplet — the guardian can't verify it read-only from
the box without the bot token. See [[tenant-and-layout]] for the channel ids.
