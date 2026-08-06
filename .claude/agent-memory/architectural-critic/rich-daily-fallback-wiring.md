---
name: rich-daily-fallback-wiring
description: 2026-08-05 rich→daily fallback (delivery.fallback_to_daily) is exact-string classification — sound today by full enumeration, but 4/5 strings are literal-coupled with no drift test; legacy pacing_ok never counts proactive_daily_slots; legacy cards now reach the invalid_blocks quarantine codes
metadata:
  type: project
---

Chase 2026-08-05: with `GRANT_RICH_CARD_ENABLED=1`, `cli.cmd_drip` runs rich
`campaign/delivery.run` first and hands the tick to legacy `run_drip` only when
`delivery.fallback_to_daily(outcome)` is true — four exact skip strings (`_FALLBACK_EXACT`)
plus the cutoff string derived from `pacing.HARD_CUTOFF_PT` (moved 11:00→11:30 PT so slot
minutes 10:31–10:45 became reachable on the :00/:30 cron grid). Legacy cards now also post
Block Kit (`slack/drip_card.py`) — blocks are substrings of the proven text, no new claims.

**Why the classifier is sound (verified 2026-08-05 by full enumeration):** every fallback
string returns BEFORE `chat_postMessage`; rich `should_post` counts max(posts, outbox
lead-rows, slot rows) and that count check precedes even `force` — so ANY same-day Slack
attempt (posts row, 'sending'/'unknown'/'rejected'/'unrenderable' outbox row, or slot row)
forces "skip: daily cap reached (1)", which never falls back. Releases
(`release_notification` DELETEs the row) happen only on definitive non-delivery
(429 / systemic / unrecognized-200-code). The cap asymmetry (legacy `pacing_ok` counts
posts+outbox but NOT `proactive_daily_slots`) therefore fails CLOSED, not open.

**Durable weak spots — re-check these on any change to delivery.py / drip.py / followups:**
1. **String drift is untested for 4 of 5 strings.** `tests/test_drip_card.py` compares the
   classifier against ITS OWN literal copies; only the cutoff string is derivation-protected
   and integration-tested against real `should_post` output. Rewording e.g.
   "skip: no rich award card satisfies every evidence rule" in `run()` passes the whole
   suite and silently kills the primary fallback → permanent cardless flag-on days, exit 0.
   Fix: shared module-level outcome constants, or drive `run()` into each state and assert
   `fallback_to_daily(actual)`.
2. **Legacy pacing is blind to slot rows.** Safe sequentially (see above), but
   `slack/salesforce_followups.py` uses `reserve_daily_slot` as its ONLY arbitration vs the
   drip when rich is enabled and writes neither posts nor outbox. A followup run CONCURRENT
   with a fallback tick can double-post. Inert today (zero Campaign members, no followups
   cron) — must teach `pacing_ok` to count `_daily_slot_count` (or serialize followups into
   cmd_drip) BEFORE the followups job is ever scheduled.
3. **Legacy leads can now be burned by a renderer bug.** `_CONTENT_SLACK_ERRORS` includes
   `invalid_blocks`/`invalid_block_part`/`blocks_too_long`; pre-restyle those were
   unreachable on the legacy path. A systematic `drip_card.render_blocks` defect quarantines
   the TOP gold lead per weekday (loud exit 1, but inventory destroyed) — the code indicts
   the renderer, not the lead.
4. **Mention moved from rendered text into a section block** — that blocks-mentions notify
   the rep's phone is Slack-documented but `assumed` until the first live card is confirmed.
5. **Crash between `reserve_daily_slot` and `reserve_notification`** (delivery.py, one
   statement apart) orphans a slot row: every later tick caps out, no fallback, silent
   cardless day, exit 0. No sweeper exists. See [[rich-delivery-no-resume-path]].
6. The rich dry-run preview returns before veto/freeze/prior checks, so `--dry-run` can
   report "would post rich_award" on a tick whose real run would fall back to the daily card.

No consumer reads `notification_outbox.payload_json` (grepped 2026-08-05), so the new
"blocks" payload key is inert. Header labels ("GOLD · Verified award" etc.) are backed by
the candidate queries' WHERE clauses (gold+verified award / silver+rfp_posted); bulletin
claims no tier. Stale docs found: rich_award_card_design.md §7 and grant_message_catalog.md
still said 11:00 cutoff, and the catalog's "otherwise it posts nothing" is false post-fallback.
