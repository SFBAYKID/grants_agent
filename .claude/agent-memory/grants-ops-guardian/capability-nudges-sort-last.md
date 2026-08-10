---
name: capability-nudges-sort-last
description: Declaring a capability does NOT put the reopened asks next in the nudge queue — stalled_at is the DECLARATION time, so they sort LAST behind every eligible subject (measured 2026-08-09: positions 14-18 of 19, ~7 days out)
metadata:
  type: project
---

**The "declare the capabilities, then fire a nudge and Kerry/Jocelyn/Nelly hear from Grant"
plan does not work, and the reason is in the sort key.**

`_capability_asks` sets `stalled_at = available_since` — deliberately, and the docstring
explains why (the thing worth reporting is *when the capability shipped*, not when the
person asked, otherwise every historical ask is born stale). But `candidates()` sorts
**oldest `stalled_at` first**, and `run()` takes the first non-suppressed candidate. So a
freshly declared ask has `stalled_at = now`, which is the **newest** timestamp in the
queue — it sorts to the **back**, behind every card and CRM subject already waiting.

**Measured read-only on prod 2026-08-09 (a718066, schema 37), with the 5 seeded asks
rendered as if declared at that moment:**

```
 0-13  the 14 already-eligible subjects (all C01DGT9D11D)
14     capability_now_available  U01E908206M   <-- first capability ask
15     capability_now_available  U01E908206M
16     capability_now_available  U06RXJKRXSR
17     capability_now_available  U04ASV42UJD
18     capability_now_available  U04ASV42UJD
```

`ELIGIBLE_AHEAD_OF_FIRST_CAPABILITY = 14`. At `MAX_NUDGES_PER_DAY = 2` per audience and
**one nudge per invocation**, that is roughly **seven more days** of nudges before the
first capability message is even chosen. Declaring is therefore not the last step before
those three colleagues hear anything — the 14 subjects in front of them are.

**So `capability --execute` immediately followed by `nudge --execute` sends a
`card_unengaged` channel reply, NOT an apology to Kerry.** Anyone predicting "the next
nudge reaches <person>" from the fact that an ask was reopened is predicting the wrong
message. Check the merged sort order, never the ask list.

**Corollary for `--force` demos:** forcing more runs does not skip the queue either;
`--force` waives only the business-hours window ([[nudge-queue-state-20260809]]), and the
2/day + 4h-gap caps still apply, so the capability asks cannot be pulled forward at all
without either sending everything ahead of them or changing the sort.

## Also confirmed on the deployed bytes

- **A/B is still inert for `capability_now_available`**: variants "a" and "b" render
  **character-identical** text for all 5 asks. Matches [[nudge-variant-ab-is-inert]]; the
  local fix (`3829948`, "the last two kinds were still A/B testing a sentence against
  itself") is **NOT deployed**.
- All 5 asks have a non-empty `thread_ts`, so all 5 *would* enter the queue, and
  `_capability_is_live` is **True** for all four capabilities (`email_results` included —
  the Resend key is present on the droplet), so none would be held back as
  `capability_not_ready`.
- The dry run `cli capability <name>` (no `--execute`) uses `connect_readonly()` and
  returns **before** `mark_available`, printing the exact reopen count and the ask list.
  It answers "how many would this reopen" with zero risk — use it instead of guessing.

Related: [[relayed-consent-is-not-consent]], [[deploy-d664548-followups-live]].
