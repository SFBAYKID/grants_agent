---
name: drip-pacing-and-cap
description: Why Grant posts only ONE drip card a day — DAILY_CAP=1 by design — plus the read-only recipe for auditing drip ticks and the gold-backlog suppression funnel
metadata:
  type: project
---

**Grant posts at most ONE drip card per weekday by design. "Only one lead today" is
almost never a bug.** Verified read-only on the droplet 2026-07-20 (revision f4d6237).

**PACING MODEL CHANGED 2026-07-22 (commit 264b0e2, deployed): the per-tick coin flip is
GONE.** `DAILY_AIM` and `POST_PROBABILITY` no longer exist — do not look for them, and
do not describe drip as "random per tick". `pacing_ok()` now holds until **today's slot**,
one target time per (date, channel) drawn inside `DRIP_SLOT_START_PT`–`DRIP_SLOT_END_PT`
(Pacific, `.env`-tunable, defaults 10:00–11:30; live droplet value is 10:30–11:00). The
skip line to expect before the slot is `skip: holding for today's HH:MM PT slot`.
`should_post`/`pacing_ok`/`run_drip` also LOST their `rng` parameter. Because the cron is
`*/30`, a ≤30-min band collapses to one clock time — see
[[drip-slot-band-vs-cron-granularity]].

`grant_watch/slack/drip.py`:
- `DAILY_CAP = 1`, `ABSOLUTE_CAP = 2` (the daily card + at most one
  `urgent` emergency card), `MIN_GAP_MINUTES = 90`.
- The `(N)` in the log line `drip: skip: daily cap reached (N)` is the **cap constant**,
  not the number posted today. Do not read `(1)` as "1 post so far".
- `in_window()` = `et.weekday() < 5 and et.hour >= 7 and pt.hour < 17`. Cron runs
  `*/30 4-17` PT = 28 ticks, but the app window closes at 17:00 PT, so the 17:00 and
  17:30 PT ticks ALWAYS log `outside Mon-Fri 7am ET – 5pm PT window`. Two deliberately
  wasted ticks/day — expected, not a fault.
- Set by Chase in commit **194d364** (2026-07-18, "one best card a day + platinum tier
  (don't overburden)"), which changed `DAILY_CAP` 3 → 1. Days before that legitimately
  show 2–8 posts, so older expectations of "2 a day" predate the cap change.

**Why:** Chase's own product rule — in-code comment "ONE card a day is plenty — too many
and people tune out." So a day with 27 `daily cap reached (1)` skips and one post is the
system working correctly.

**How to apply:** When asked "why did only one lead post today", check `DAILY_CAP` FIRST
before hunting for cron gaps or errors. Only an `urgent`/exceptional card can produce a
second post.

## Read-only tick audit recipe (proven, no writes)

`cron.log` has no per-line timestamps, but the `*/5` keepalive writes
`grant_keepalive status=healthy at=<ISO-UTC>` — use it as a timestamp anchor. Drip output
lands immediately after the same-minute keepalive, so the anchor == the tick minute:

```bash
awk '/^grant_keepalive status=/ { if (match($0, /at=[0-9T:\-]+Z/)) ts=substr($0,RSTART+3,RLENGTH-3); next }
     /^drip: / { print substr(ts,1,16) }' cron.log | sort | uniq -c
```
28 anchors, one line each = every tick fired. (2026-07-20: all 28 fired, zero gaps.)
Gotcha: a naive `grep -i error` over cron.log false-positives on the grants.gov agency
name "Bureau of Coun**terror**ism".

## Read-only DB probe (never `db.connect()` — that MIGRATES = writes)

Open `sqlite3.connect("file:grant_watch.db?mode=ro", uri=True)` and fail closed by
attempting `CREATE TABLE _ro_probe (x)` first — it must raise "attempt to write a readonly
database". Then the engine's own pure-SELECT helpers are safe to call directly:
`db.posts_today(conn, channel, now)`, `db.nugget_candidates(conn)`, `db.rfp_candidates(conn)`,
`db.bulletin_candidates(conn)`. Gotcha: running the script via `ssh … python -` breaks
`load_dotenv()` (find_dotenv walks the stack and asserts) — call `load_dotenv(".env")`.
The leads column is `entity_name`, not `org_name`.

## The real supply constraint (2026-07-20 snapshot)

`nugget_candidates` (GOLD awards) = **0**. Funnel: 639 gold leads → 547 gold+new → but
**546 are `suppressed=1, backfill=1`** (the deliberately suppressed backfilled award
backlog, an open product decision per CLAUDE.md, not a bug). So the daily card falls
through the ladder to a silver RFP. `rfp_candidates` was down to **3** (due 07-22/07-23),
i.e. the RFP pool nearly exhausted — once empty, drip falls to bulletins
(790 candidates, but heavily narrowed by the relevance regex).
Watch this: the cap is 1/day, so supply of 3 is only ~3 days of runway.

**Dedup:** an earlier version of this memory said the PA DOC leads "differed only by title
case" — that was WRONG (I inferred it from truncated poll-log lines). The real mechanism is
a dedup-key format migration. See [[rfp-dedup-key-drift]].

## 2026-07-22 LATE refresh (revision 264b0e2 deployed) — the gold pool is OPEN, supply solved

**The two snapshots below are now HISTORY.** Commit 264b0e2 deleted `AND e.suppressed=0`
from `nugget_candidates` (Chase: "gold is what we should really be serving users each
day") and added `AND l.id NOT IN (SELECT lead_id FROM posts …)`. Verified live on the
droplet immediately after deploy: **`nugget_candidates` = 544** (the identical query with
the old `suppressed=0` filter still returns **0** — proof the suppression flag was the
sole gag). Funnel: 638 gold → 546 gold+`status='new'` → 546 still after the verified and
`award_*` filters → **544** after excluding the 2 leads already in `posts`. So "~546" is
the pool and 544 is the postable count; the 2-lead gap is the new anti-repeat guard
working, not a loss. State mix of the 544: CA 354, AZ 22, IL 15, KY 14, AR 13, MO 12,
OK 10, WI/MI 8, TX/AL 7. `rfp_candidates` is still 0, but that no longer starves drip —
the ladder now stops at the gold rung. At 1 card/day, 544 candidates ≈ 2 years of runway.

## 2026-07-22 refresh (read-only, revision 15263d2) — HISTORICAL, superseded by the section above

`nugget_candidates` still **0**, and the reason hardened: **638/638 gold leads have
`suppressed=1, backfill=1`** — not one exception (whole-table: 1050 events non-suppressed,
10866 suppressed). Non-suppressed `award_*` events of ANY grade = 29, all `lead_grade='watch'`.
So the gold ladder rung in `drip.pick()` (platinum → gold → silver RFP → bulletin) can never
fire from current data; every daily card falls through to an RFP.

`rfp_candidates` = **0** as of 2026-07-22, because only 3 `source='rfp'` leads exist
(#9533, #9565, #9566) and **all three are now in `posts`** (07-20, 07-21, 07-22), so the
`NOT IN (SELECT lead_id FROM posts)` guard excludes them all. #9566 also expires today
(funds_end 2026-07-22) and #9533/#9565 tomorrow. Next tick therefore falls to bulletins,
or to `skip: nothing new worth saying` if the relevance regex rejects them. The 07-22 poll
logged `[Security RFP discovery] 3 items, 0 new` — the aggregator is returning the same 3
listings, not new supply. See [[rfp-poll-populate]] (supply is small and short-fused by
design) and [[identical-rfp-card-text]].
