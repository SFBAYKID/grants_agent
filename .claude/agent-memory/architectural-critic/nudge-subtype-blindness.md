---
name: nudge-subtype-blindness
description: REPRODUCED 2026-08-10 — a real human reply carrying a Slack `subtype` (thread_broadcast, file_share, me_message) is dropped by BOTH grant.py's listener and nudge_silence._is_human, so the reply gets no answer AND the person is then publicly reported to the manager as not having replied
metadata:
  type: project
---

# The subtype filter is a single point of failure that ends in a false accusation

`grant_watch/slack/nudge_silence.py:91` treats ANY message carrying a `subtype` as
non-human. `grant_watch/slack/grant.py:219` and `:297` apply the SAME rule to inbound
events. Slack sets a subtype on several shapes a real person produces:

| shape | subtype | result |
|---|---|---|
| reply with "Also send to channel" | `thread_broadcast` | invisible |
| reply with a screenshot/file | `file_share` | invisible |
| `/me` | `me_message` | invisible |
| reply with attachments, no file | (none) | correctly seen |
| edited reply | (none, `edited` field) | correctly seen |

Executed against `nudges.run` with a real migrated DB: each of the three produced
`nudged offer_unanswered in C0TEST` posting *"I offered to build that campaign for
<@Jocelyn> and nothing's come back here"* — while her reply sat in the thread.

**Why it compounds.** The listener drops the same message, so (a) Grant never answers
her, (b) no `slack_event_receipts` row is written, so `nudge_variants.mark_engagement`
never sets `engaged_at` and `_unanswered_offers` still selects the row. The three
"independent" signals in `nudge_silence`'s docstring are not independent — the local
table and the Slack read share one rule and fail together.

**Why the tests could not catch it.** `tests/test_nudge_followups.py:699` exercises only
`channel_join`, the one subtype that IS noise. Both `_Slack` stubs synthesise thread
payloads by hand and never emit a subtype, a `has_more`, or a `reactions` array — the
stub answers whatever the code asks, the same failure class as the ZoomInfo and
campaign-COUNT bugs already in CLAUDE.md.

**Fix shape:** allowlist the subtypes that are genuinely not a person
(`bot_message`, `channel_join`/`leave`, `channel_topic`, `message_changed`,
`message_deleted`, `tombstone`) rather than blocklisting the presence of the key. Same
edit needed in `grant.py`, or a rep replying with a file still gets silence.

Related: [[nudge-silence-verified-vs-unknown]]
