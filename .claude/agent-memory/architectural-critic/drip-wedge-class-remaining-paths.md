---
name: drip-wedge-class-remaining-paths
description: 85295d7 fixed the reserve-collision wedge but two paths in the SAME class remain — an unrenderable candidate crashes every tick forever, and a deterministic Slack rejection now silently burns inventory
metadata:
  type: project
---

`85295d7` fixed the C1 wedge (see [[drip-wedge-on-ambiguous-send]], now RESOLVED) by excluding
`notification_outbox` leads from all three candidate queries. Two paths in the same "one bad top-ranked
lead stops the product" class survive it. Both reproduced 2026-07-22 against a temp DB at `85295d7`.

**1. Render-exception wedge (NOT fixed).** `run_drip` calls `build_nugget`/`build_rfp_alert`/
`build_bulletin` BEFORE `reserve_notification`. Those raise `ValueError` on an empty entity or title —
and `plain_fragment`/`display_entity_name` strip `<>@\`*_~|` and punctuation, so an entity of `***`
sanitizes to `""`. `cli.cmd_drip` has no handler. Reproduced: a gold lead with `entity="***"` and the
highest amount wins `_best_nugget`, `run_drip` raises on EVERY tick, no `posts` row and no
`notification_outbox` row is written, so the new exclusion never engages and the next tick picks the
same row. Permanent, ~26 tracebacks/weekday in cron.log, nothing else in the channel.
The `amount > 0` guard in `nugget_candidates` is the same class already patched once, by query filter —
proof the pattern is known. Remaining unguarded preconditions: entity (all three builders) and title
(bulletin).

**2. Deterministic Slack rejection now BURNS leads (regression in economics, new at 85295d7).**
`except Exception` treats every failure as ambiguous and writes state `unknown`. But
`slack_sdk.SlackResponse.validate` raises `SlackApiError` with `status_code == 200` for
`channel_not_found` / `not_in_channel` / `invalid_auth` / `msg_too_long` — Slack answered, the message
provably did NOT land. Pre-85295d7 that cost a wedge (recoverable by deleting one row).
Post-85295d7 the lead is excluded forever, so a misconfigured channel or a revoked token silently
destroys ~1–2 gold leads per weekday (bounded by `DAILY_CAP`/`ABSOLUTE_CAP`, which count reservations)
while posting nothing. Reproduced with a client raising `SlackApiError`: three ticks, three leads gone,
zero messages. The returned string "delivery could not be confirmed" is also untrue in that case.
Discriminator: `SlackApiError` + `response.status_code == 200` -> definitively not delivered, safe to
release the reservation. 5xx / 429 / `TimeoutError` / `SlackRequestError` stay ambiguous.

**3. Nothing surfaces a burned lead.** `cli slack-failures` reads `slack_event_receipts` (inbound
turns), NOT `notification_outbox`. There is still no operator-visible list of outbox rows in state
`sending`/`unknown`. Silent inventory loss has no reporting surface.
