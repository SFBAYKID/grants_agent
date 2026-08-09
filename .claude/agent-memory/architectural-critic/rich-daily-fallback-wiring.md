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
1. **CLOSED (verified 2026-08-06 against deployed 5f09200):** module-level `_SKIP_*`
   constants in delivery.py are now the same objects at every `run()` return site
   (lines 181/192/201/235) AND inside `_FALLBACK_EXACT`, so wording drift between the
   producer and the classifier is structurally impossible; `tests/test_drip_card.py`'s
   literal copies now actively pin the exact strings. The cutoff string stays derived
   from `pacing.HARD_CUTOFF_PT` on both sides.
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

**Full outcome-tree enumeration (2026-08-06, for Chase's "will the card fire" ask):** the
ONLY leaf that ends a day cardless with routine exit-0 is legacy `pick()` returning None →
"skip: nothing new worth saying" — reachable solely when gold+silver-RFP+bulletin pools are
ALL empty (honest silence, data-unreachable while the ~500-lead gold backlog exists). Every
other no-card leaf either recovers at a later same-day tick (backoff holds, legacy
slot-target later than the current tick — the 11:30 rich-cutoff tick always falls back and
satisfies any band target; clamp guarantees targets ≤16:30 are reachable) or exits non-zero
(blocked/error/unknown/quarantined). Consumed-attempt leaves (quarantine/rejected/unknown)
cap the REST of the day silent at exit 0, but only after one loud non-zero tick — by design.
`quarantine_lead` writes a lead_id-bearing outbox row, so it counts toward
`delivery_attempts_today` and burns the day's cap.

No consumer reads `notification_outbox.payload_json` (grepped 2026-08-05), so the new
"blocks" payload key is inert. Header labels ("GOLD · Verified award" etc.) are backed by
the candidate queries' WHERE clauses (gold+verified award / silver+rfp_posted); bulletin
claims no tier. Stale docs found: rich_award_card_design.md §7 and grant_message_catalog.md
still said 11:00 cutoff, and the catalog's "otherwise it posts nothing" is false post-fallback.
