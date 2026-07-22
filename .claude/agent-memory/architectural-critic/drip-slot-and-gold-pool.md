---
name: drip-slot-and-gold-pool
description: Daily-slot timing collapses to 3 clock times and has no window clamp or missed-slot fallback; the unblocked gold pool is ~195 same-day SVPP rows then ~347 undated CA rows
metadata:
  type: project
---

Measured 2026-07-22 against `264b0e2` / `0a83d73` (`drip.daily_slot`, `slot_band`, `scoring.grade`,
`db_engagement.nugget_candidates`), by running the real code over 400 simulated days.

**Slot timing.**
- The band is 10:00–11:30 PT and cron ticks are `*/30`, so the card lands at exactly **10:30, 11:00
  or 11:30** — 3 distinct clock times, ~1/3 each over 400 days. The `264b0e2` commit message cites
  "11:24, 10:14, 10:04, 11:01, 10:09" as the spread; those are the drawn TARGETS, not delivery times.
  A narrow band plus a coarse tick grid destroys the "sporadic" property the design exists for.
- `slot_band()` is NOT clamped to `in_window()`. `DRIP_SLOT_START_PT=16:59`/`END=17:30` yields a
  17:13 PT slot, which `in_window` (`pt.hour < 17`) can never admit — the daily card is silenced
  forever by a config typo, logging only "holding for today's 17:13 PT slot".
- No missed-slot fallback. Previously 28 tick chances at 45%; now effectively 3. A bot/cron/droplet
  outage across a 90-minute band silently costs the whole day's card.
- DST itself is safe (band is far from the 2 AM transition), but the cron's own timezone handling is
  unverified from the repo — confirm with the guardian, not by assumption.

**Gold pool composition (what unblocking `suppressed` actually surfaces).**
- Re-grading DOES work: `usaspending` re-polls from `TIME_FLOOR=2018-10-01` daily and `upsert_lead`
  rewrites `lead_grade`, so the FY25 SVPP cohort flips GOLD→SILVER and leaves the pool. Verified:
  same item graded at 2026-10-06 stores `silver`, pool 1 → 0.
- BUT the ~195 `usaspending:16.071` rows all obligated 2025-10-10 die on the SAME day (~2026-10-05).
  At 1 card/day only ~50 post first; the rest expire unposted. "544 leads = 211 business days of
  runway" is false.
- `_best_nugget` ranks fresh-dominant, so all SVPP ($500K, fit 1.0, score ~0.90) precede all CA rows
  (undated → fresh 0.3, fit 0.6 → score ~0.14). Result: ~50 consecutive near-identical cards
  ("X in <state> has a verified $500,000 SVPP funding award"), then ~347 CA cards. That is the
  "same message every morning" complaint returning in a new shape.
- The ~347 `ca-grants-award:*` rows have `event_date=""` (deliberate — PublishDate is not an award
  date) and `program=<ProjectTitle>`, so the program field is free text, never matches `PROGRAM_FIT`,
  and never aggregates in `program_outcome_points`. They DO eventually expire, via
  `grade()`'s `end < today` on ProjectEndDate — not via freshness.
- `nugget_candidates` has NO `last_seen` filter: a lead the source stopped returning is never
  re-graded and stays postable as "verified" indefinitely.
- `nugget_candidates` takes no channel argument, so a post in the playground channel permanently
  removes that lead from the production pool (and both channels draw their own slot, so both post).

**How to apply:** when a suppression/eligibility filter is removed, characterize the pool that
appears — its date variance, its rendering fields, its drain order and its expiry mechanism — before
judging whether surfacing it is honest. "The pool went 0 → 544" says nothing about what 544 cards
will read like on day 60.

Related: [[drip-wedge-on-ambiguous-send]], [[rfp-aggregator-and-staleness-fragilities]].
