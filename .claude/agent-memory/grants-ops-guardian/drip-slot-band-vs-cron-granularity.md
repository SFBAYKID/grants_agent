---
name: drip-slot-band-vs-cron-granularity
description: The drip slot band cannot be finer than the 30-min cron — a 10:30–11:00 band posts at 11:00 on 19 of 20 weekdays, killing the intended day-to-day variation
metadata:
  type: project
---

`drip.daily_slot()` picks a per-day target time inside `DRIP_SLOT_START_PT`–`DRIP_SLOT_END_PT`,
and `pacing_ok` then holds until the first cron tick **at or after** it. So the actual post time is
quantized to the drip cron, which is `*/30` — only `:00` and `:30` exist. **A band narrower than or
equal to 30 minutes therefore collapses to a single clock time.**

Measured on the droplet 2026-07-22 with the live `DRIP_SLOT_*` band 10:30–11:00, over the next 20
weekdays: **19 days post at 11:00 PT, 1 day at 10:30 PT.** `daily_slot` draws `randint(0, span)` =
31 possible offsets, and only offset 0 (exactly 10:30) lands on the 10:30 tick; offsets 1–30 all
wait for 11:00. The slot values themselves do vary (10:32, 10:41, 10:46, 10:52, 10:55, 10:58…) —
that variation is real but invisible, because it is rounded away.

**Why:** The commit's own docstring sells the feature as "still sporadic day to day (9:12, 8:34,
10:47…), but never before the team is at their desks." The first half of that promise is not
achievable with a 30-minute band on a 30-minute cron. The second half works perfectly — 11:00 PT is
squarely in the workday, which was the actual problem being fixed (cards were landing 04:00–05:00 PT
to an empty office).

**How to apply:** When Chase asks why the card always arrives at the same time, this is why — it is
arithmetic, not a bug, and NOT worth hunting in the DB or cron log. To restore real variance the
band must span several ticks (e.g. `09:00`–`13:00` → 9 candidate ticks) — a `.env`-only change, no
deploy. Rule of thumb: **distinct possible post times ≈ band minutes ÷ 30.** Do not "fix" this by
tightening the cron to `*/5` without asking; that multiplies drip invocations 6× and each one opens
the DB. See [[drip-pacing-and-cap]] and [[tenant-and-layout]].
