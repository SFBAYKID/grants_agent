---
name: drip-wedge-on-ambiguous-send
description: CRITICAL and unfixed at 0a83d73 — one ambiguous Slack send permanently wedges the whole drip; the reservation blocks re-post forever while pick() keeps choosing the same lead
metadata:
  type: project
---

Proven empirically 2026-07-22 against HEAD `0a83d73` with a real temp DB (migrations applied,
`db.upsert_lead`, `drip.run_drip` with a client whose `chat_postMessage` raises).

**The wedge.** `run_drip` reserves (`notification_outbox`, state `sending`), calls Slack, and on ANY
exception calls `finish_notification(..., 'unknown')` — deliberately not retrying, because a timeout
may mean Slack accepted the post. Correct as far as it goes. But the lead is then left
`status='new'`, absent from `posts`, and still top of `_best_nugget` (a deterministic `max()` over a
static pool). Every later tick: `pick()` returns THE SAME lead → `reserve_notification` hits the
existing `delivery_key` (`INSERT OR IGNORE` → rowcount 0) → returns None → `run_drip` returns early.

Measured output on consecutive days after one failure:
```
2026-07-23 -> skip: this funding event is already reserved or delivered
2026-07-24 -> skip: this funding event is already reserved or delivered
2026-07-27 -> skip: this funding event is already reserved or delivered
```
Zero messages sent, forever. The early return happens BEFORE the RFP and bulletin tiers, so one bad
gold lead blocks platinum, gold, rfp AND bulletin. `cmd_drip` exits 0 on a "skip:", so nothing alarms;
the symptom is "Grant went quiet", which is the exact condition that already cost a full day to
diagnose once.

Same wedge fires from the post-send bookkeeping path if BOTH `record_post` and the fallback
`mark_surfaced` fail (e.g. a full disk — the droplet sat at 95% on 2026-07-22).

**Nothing sweeps `notification_outbox`.** `reserve_notification`/`finish_notification` are the only
writers; no reaper, no TTL, no operator command. `salesforce_followups` has the identical shape in
`salesforce_followup_state`.

**Fix direction:** make the picker skip an already-reserved lead instead of re-selecting it — mirror
the existing `posts` exclusion with an outbox exclusion in `db_engagement.nugget_candidates` /
`rfp_candidates` / `bulletin_candidates`. That keeps "never blind-retry an ambiguous send" while
letting the queue advance. Pair with a liveness check (pool non-empty AND no post in N business days
=> loud health failure).

**How to apply:** any time a reservation/outbox pattern guards an external send, ask "what un-sticks
the reserved row, and what does the selector do on the next tick?" A reservation that is never swept
AND a deterministic selector is a permanent outage, not a safety mechanism.

Related: [[posts-kind-check-vs-drip-kinds]] (same wedge reached via a CHECK violation, since fixed by
migration 13), [[drip-slot-and-gold-pool]].
