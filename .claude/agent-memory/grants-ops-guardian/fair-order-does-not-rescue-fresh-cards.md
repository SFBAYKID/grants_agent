---
name: fair-order-does-not-rescue-fresh-cards
description: MEASURED 2026-08-10 — rotation alone (a66f5d4) moved North Palos 26 -> 29 of 30; ranking cards by the LEAD (885ad88, CURRENT PROD) moved it to 0. Plus the award-brief tier hole
metadata:
  type: project
---

## RESOLVED IN `885ad88` — CURRENT PRODUCTION

**PRODUCTION IS `885ad88efe6c459e8e569e79a7194a376ae0a696`, schema 39, PID 66465.**
Deployed 2026-08-10 ~19:06 PT, 3 files, `.env`/crontab byte-identical, 26 rows untouched.

The rotation was not the error — **the sort key inside the kind was.** `priority_at` means
"how long has the PERSON waited", and **a card has no person waiting on it**, so applying it
to a lead buries every new arrival behind older, worse ones. Cards are now ranked by
`_lead_worth` = (tier, -amount, -freshness); every other kind keeps oldest-person-first.

| subject | strict age | rotation only (a66f5d4) | + lead ranking (885ad88) |
|---|---|---|---|
| North Palos `card_unengaged` 34 | 26 | 29 | **0** |
| North Palos `card_escalated` 34 | 27 | 27 | **1** |
| `offer_unanswered` (Jocelyn) | 28 | 3 | 5 |

**THE HEAD MOVED, deliberately.** Live head is now `card_unengaged` id=34, not
`capability_now_available` id=4 (which slid to 2). The previous round's check "head unchanged
⇒ fix is good" became the WRONG question once the goal was to surface a fresh card — the two
criteria are mutually exclusive. Restating a criterion after the goal changes is the lesson.
Tuesday should deliver North Palos `card_unengaged` (due 10:30, reachable) **and**
`capability_now_available` id=4 — the card is rescued without costing the oldest waiting person
their slot, because the `card_escalated` at position 1 is due 16:30, past the 14:45 last tick.

## `award-brief` IS NOT IN `_TIER_RANK` — 7 OF 10 LIVE CARDS SORT UNRANKED

Verified per-row on production rather than inferred (`_lead_worth` printed for every live card):

```
id=34 tier='gold'        amount=500000  -> (1, -500000, ...)
id=32 tier='gold'        amount=500000  -> (1, ...)
id=33 tier='gold'        amount=364891  -> (1, ...)
id=31..25 tier='award-brief' amount=500000 -> (9, ...)   # unranked, sorts LAST
```

`card_tier` is built as `str(row["style"] or row["kind"] or "")`, but `_TIER_RANK` holds
**style** vocabulary (`platinum/gold/rfp/silver/nugget`) while `kind` holds a DIFFERENT
vocabulary (`rich_award`, `award-brief`). **So the `or kind` fallback can only ever produce an
unrecognised tier — it is guaranteed rank 9 whenever `style` is empty.** Seven $500,000 awards
therefore rank below a $364,891 gold card. It fails SAFE (docstring: "anything unrecognised
sorts last rather than being guessed at") and it does not change North Palos, which wins on
all three keys — but the tier grading is only really operating on 3 of 10 cards.

**This is the [[row-get-wrong-column-false-null]] family**: a lookup miss and a real low rank
are indistinguishable from the output alone. Print the computed key per row before trusting
any ranking — that is the only reason this was visible.

---
## ORIGINAL FINDING (a66f5d4) — kept, because it is why the fix took two attempts

**The fix shipped, is healthy, and does not achieve its stated purpose.** Deployed
`a66f5d4b2a043fc3dde0bad17da7bf9cfab4d171` 2026-08-10 ~18:57 PT (PID 66149, schema 39,
`followup_nudges` unchanged at 26). This is about the OUTCOME, not the deploy.

## What was claimed vs what was measured

Claim: *"a fresh gold card now gets a slot the same day instead of queueing behind all of
them."* Measured against production, same filter both times (live + unrecorded, 30 subjects):

| subject | before (`priority_at`) | after (`_fair_order`) |
|---|---|---|
| North Palos `card_unengaged` id=34 | 26 of 30 | **29 of 30 — WORSE** |
| North Palos `card_escalated` id=34 | 27 | 27 — unchanged |
| `offer_unanswered` (Jocelyn) | 28 | **3 — much better** |
| oldest live card (`card_unengaged` 25) | 3 | **12 — WORSE** |

The head was unchanged (`capability_now_available` id=4, `U04ASV42UJD`, prio 07-23), which
was the coordinator's falsifiable check and it PASSED — but **an unchanged head is not
evidence the fix works.** Ask what the fix was FOR and measure that.

## WHY — the general lesson

`_fair_order` round-robins kinds (kind order = each kind's oldest member) and pops
**oldest-first WITHIN each kind**. So kind K's j-th member lands near position
`j x (active kinds)`.

**A round-robin helps the OLDEST member of a SMALL kind. It cannot help the NEWEST member
of the LARGEST kind — which is exactly what a freshly-posted card is.** `offer_unanswered`
jumped to 3 because it is a kind of ONE. North Palos sank to 29 because it is last inside a
10-member kind, and interleaving the small kinds ahead of the cards pushed every card back.

Cards got worse ACROSS THE BOARD (oldest live card 3 -> 12), because the small kinds now take
turns ahead of them. Round-robin traded card throughput for kind diversity.

**If the goal is "a fresh card is mentioned while it is still fresh", the lever is not the
order across kinds — it is the order WITHIN the card kind, or a per-kind quota instead of a
single global `MAX_NUDGES_PER_DAY=2`.** Oldest-first within a kind is precisely what buries a
new arrival.

## Practical consequence at the time of writing

North Palos `card_unengaged` sits 29th with 28 subjects ahead, against
`MAX_NUDGES_PER_DAY=2` and `MAX_NUDGES_PER_TARGET_PER_DAY=1` (many `card_escalated` share the
manager `U01DFJWQQJ3`, so only one of those drains per day). Its `drop_after` is
**Mon 2026-08-24 10:30**, 14 days from posting. It is still likely to age out unmentioned —
the original failure. Treat "position / 2 = days" as an UPPER bound: runtime suppressions do
not consume the delivery cap, and permanent ones shorten the queue, so it may drain faster.

## Measurement note that made this visible

Positions were taken on the LIVE+unrecorded subset both times, so they are comparable. The
raw `candidates()` list also contains the 35 stale subjects, which occupy the card kinds'
early round-robin slots and are burned on the first `--execute` walk — so the filtered
ordering is also the post-burn ordering. See [[deploy-0f62485-nudge-followups]] for the
read-only future-clock recipe.
