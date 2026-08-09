---
name: edit-cards-in-place
description: Standing instruction from Chase — fix a posted Slack card with chat.update, never by posting a replacement
metadata:
  type: feedback
---

When a card already in the channel needs changing, **edit it in place with `chat.update`. Never
post a second card to supersede the first.** Chase, verbatim 2026-08-06: "whatever you do when you
change this just edit it in place".

**Why:** the channel is a feed a sales team reads, not a log. Two cards for one award reads as two
opportunities, and the team has already been burned by apparent-duplicate cards
([[identical-rfp-card-text]], [[rfp-dedup-key-drift]]). It also keeps `posts` /
`notification_outbox` honest — those rows record a *delivery*, and re-rendering the presentation of
an existing delivery is not a new delivery.

**How to apply:** re-render from the FROZEN snapshot and call `chat_update(channel, ts, text,
blocks)`. Do not write `posts` or `notification_outbox` rows, do not mutate
`rich_card_snapshots` (immutable evidence of what was true at approval time), and open the DB
`connect_readonly()`. Recompute `fallback_text` rather than reusing the frozen copy when the layout
changed — the frozen text carries the old wording — but take every FACT from the frozen draft so
nothing new is asserted. Gate the update on a `posts` row actually binding
`(snapshot_id, channel, ts)`, so a mistyped ts cannot rewrite an unrelated message.

Proven 2026-08-06 on ts `1786049660.891549`: `edited: True`, same ts, channel still showed exactly
two bot messages that day, and `posts`/`notification_outbox`/`rich_card_snapshots` were all
unchanged at 2/2/2. Related: [[first-rich-card-posted]].
