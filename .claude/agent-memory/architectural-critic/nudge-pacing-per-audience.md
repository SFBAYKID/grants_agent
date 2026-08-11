---
name: nudge-pacing-per-audience
description: REPRODUCED 2026-08-10 — every nudge cap is scoped to one audience, so MAX_NUDGES_PER_DAY=2 delivered 4 and MAX_NUDGES_PER_TARGET_PER_DAY=1 delivered 2 to the same person; and `run` RETURNS on a per-candidate pacing reason, starving the whole queue behind it
metadata:
  type: project
---

# Two pacing defects in `nudges.py` that measurement, not reading, exposed

**1. The caps are per-channel, not per-day or per-person.** `_sent_today` filters
`WHERE audience=?`, and both cap checks are computed from its result. Executed with two
cards for the same rep in two channels: **4 messages delivered on one Pacific day
against `MAX_NUDGES_PER_DAY = 2`, and Jocelyn nudged twice against
`MAX_NUDGES_PER_TARGET_PER_DAY = 1`.** This is live shape, not a hypothetical:
production has `C01DGT9D11D`, the playground `C0B02721MNK`, and capability asks whose
audience is a DM (`D0BGW7EP3K5`). `daily_slots` is also seeded per (date, audience), so
each channel draws its own two slots. The per-person cap is the one that matters —
its constant name is a claim the code does not keep.

**2. `run` returns, where it should continue.** `if waiting: return f"skip: {waiting}"`.
Some pacing reasons are facts about the DAY (`daily nudge cap reached`, `too soon after
the last nudge`) and returning is right. Two are facts about ONE CANDIDATE:
`already nudged this person today` and `outside <rep>'s working hours`. Executed:
Kerry's card at the head of the queue at 19:00 PT (22:00 her time) returned
`skip: outside U01E908206M's working hours` and Brett's fully-sendable expired preview
behind it was never considered. The queue order is stable, so this repeats every tick.
It also mis-reports to the operator: the returned string names one rep's clock as the
reason nothing went out, which is not the whole truth.

**Capacity arithmetic worth keeping.** One card produces TWO subjects
(`card_unengaged` + `card_escalated`). At one card per weekday that is 10 subjects a
week from cards alone against a budget of 10 sends a week per channel — before any
capability ask, abandoned thread, or expired preview. `_fair_order`'s round-robin
shares the shortfall out rather than removing it; the tail ages out at `DROP_AFTER`
and is retired with a silent permanent `stale` row.

Related: [[nudge-silence-verified-vs-unknown]]
