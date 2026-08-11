---
name: nudge-silence-verified-vs-unknown
description: REPRODUCED 2026-08-10 — nudge_silence.replied_since returns False ("verified silence") in three situations where it has NOT verified anything: a truncated 200-message page, a reaction instead of a reply, and a reply from anyone other than the excluded manager counting as the named person answering
metadata:
  type: project
---

# `False` is claimed in three places where the honest answer is `None`

The module's whole design is that it may say "I don't know" and every caller treats
that like "they replied". Three inputs collapse *unknown* into *verified silence*.

**1. Pagination.** `conversations_replies(channel, ts, limit=200)` with no
`has_more`/`next_cursor` follow-up. Slack returns OLDEST FIRST, so in a long thread the
truncated tail is exactly the recent part that matters. Measured: with 199 ordinary
messages ahead of the offer, both the offer and the reply fall off the page and
`replied_since` returns **False**, and `nudges.run` posts the escalation. Threshold is
201 messages in the thread. Slack SAYS it truncated (`has_more: true`) — the code never
looks. If the page is short, the correct answer is `None`.

**2. Reactions are not read.** A 👀 on the card is engagement everywhere else in this
codebase ("the cheapest +1 there is", `grant.py:379`) but `replied_since` only walks
messages. When the listener missed the reaction event there is no `engagement` row
either, so both guards say nothing came back. Executed: a card carrying
`reactions: [{name: eyes, users: [Jocelyn]}]` still produced *"went to <@Jocelyn> and
nothing's come back here"*. The reactions array is already in the payload that was
fetched.

**3. Wrong predicate.** `replied_since` answers "did ANY human other than
`exclude_user` post", not "did the NAMED PERSON reply". For `offer_unanswered` the
excluded user is the MANAGER, so an unrelated colleague's one-line comment in the
thread returns True and the subject is retired as `answered_since_offer` — which is in
`PERMANENT_SUPPRESSIONS`. Executed: Nelly says "is the CA campaign done?" and Jocelyn's
unanswered offer is burned forever. Errs safe on accusation, silently destroys the
feature's purpose. The kinds want two different questions: "did anyone pick this up"
(card) vs "did THIS person answer" (offer) — the second needs `only_user=silent_slack`.

Related: [[nudge-subtype-blindness]], [[nudge-pacing-per-audience]]
