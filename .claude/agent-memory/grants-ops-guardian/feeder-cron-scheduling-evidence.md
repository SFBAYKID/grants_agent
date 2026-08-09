---
name: feeder-cron-scheduling-evidence
description: Measured facts for scheduling the rich-card evidence feeders — poll really takes 9m10s, salesforce-sync's unordered LIMIT 500 churns the lowest rowids forever, and most NCES-pending leads can never bind
metadata:
  type: project
---

Measured 2026-08-06 while reviewing Chase's proposed feeder crontab (b22ed55). All read-only.

**INSTALLED 2026-08-06T21:38Z — crontab is now 10 lines, sha
`575fbc7ce7c7dc24dc2e806fe15ff37c9db98808d269b31ff1eb67aed0041a72`** (was 5 lines,
`70e309aa…876f`; the original 5 remain byte-identical, proven by `diff` of the first 5 lines
against the backup `~/crontab.bak.20260806T213818Z`). Added exactly ONE active line plus a 4-line
comment: `20 7 * * 1 … nces-bind --limit-states 12 --execute`. Active (non-comment) lines went
4 → 5. **No `salesforce-sync` line was added** — 03ab7bb made `prepare_worker` refresh CRM state
for its own targets, which closes the evidence chain without it. First fire Monday
2026-08-10 07:20 PT, visiting AZ, IL, KY, AR, MO, OK, MI, WI, AL, IN, NC, NM.
Install shape that worked: build the new crontab locally from the fetched bytes, `scp` it, then
`crontab ~/crontab.proposed` — safer than an inline heredoc because the comment contains backticks.
NOTE an earlier attempt at this same task was classifier-denied and correctly abandoned; it was
re-attempted only after Chase re-authorized it.

**The 07:00 poll takes 9m10s, not "a few minutes."** `runs` rows for the 14:00Z cron across 10
days: 534–563 s (min 2026-07-30, max 2026-07-24), 7 sources, dead consistent. So it finishes
~07:09:23 PT worst-observed. **Any feeder scheduled at 07:10 has a 37-second margin** — schedule
07:20 or later. (OregonBuys 404s every run and lands `state='failed'`; pre-existing, not a deploy
artifact.)

**`salesforce_sync._candidates` has an unordered `LIMIT 500`** — the SQL takes an arbitrary 500 of
the stale pool (effectively lowest rowids), and only THEN sorts by `lead_score` in Python and takes
`limit`. Measured: the stale pool is **10,627 leads spanning ids 76..10705**, and
`_candidates(conn, 50)` picks **ids 229..378**. Because a refreshed lead goes stale again after
`STALE_HOURS = 24`, the same low-rowid band re-enters the pool daily and is re-picked forever; high
rowids are never reached. Concretely: of the 8 leads `preparation.preparable_lead_ids` targets
(7784, 239, 7787, 7789, 235, 243, 241, 238), sync reached **5** (235/238/239/241/243) and never
reached 7784/7787/7789 — including 7789 Bartlett ISD, the one lead recorded as draft-ready-capable.

**UPDATE 2026-08-06 after deploying 79db6e1 (the `LIMIT 500` removal): the fix is correct but does
NOT make salesforce-sync a viable feeder — it makes the pipeline case WORSE.** With global ranking
live, `_candidates(conn, 50)` now picks ids 229..8466 (ranking proven global), but the 8 pipeline
targets rank **51, 110, 113, 118, 128, 129, 146, 165** by `lead_score` among the 10,627 stale
leads, so `--limit 50` reaches **0 of 8** and `--limit 100` reaches **1 of 8**. The old arbitrary
low-rowid slice had been reaching 5 of 8 *by accident*. Worse, `_candidates` ends with
`return rows[: max(1, min(limit, 100))]` — a **hard cap of 100** — so with the deepest target at
rank 165 **no `--limit` value can ever reach all 8**. Feeding the rich card requires targeting
`preparable_lead_ids` (or a CRM refresh inside `prepare_worker`), not a global `lead_score` rank.
Do not schedule the sync line until that exists.

**`salesforce-sync --dry-run` is NOT free.** `sync()` calls `salesforce.lookup()` for every
candidate and only skips `_persist` when `dry_run` — so a `--limit 50 --dry-run` still spends
~150–350 PRODUCTION Salesforce GETs. To answer "which leads would it pick", simulate
`_candidates(conn, N)` against `db.connect_readonly()` instead: same answer, zero API calls.

**CRM freshness is structurally fragile:** `CRM_FRESH_HOURS = 24` with a once-daily sync at time T
and a post window later in the day means any single missed sync makes the age at the next day's
window `24h + (window - T) > 24h` — guaranteed stale. One failed sync silently downgrades the day
to the legacy card via `delivery.fallback_to_daily`; there is no alarm and no MAILTO. A second daily
sync does NOT fix this — because refreshed leads drop out of the pool, the later run advances to
different leads rather than retrying the same ones.

**NCES pending list starves.** 176 unbound gold+new leads across **36 states**; `cmd_nces_bind`
orders states by pending DESC. Measured against live NCES for the top 5 (AZ/IL/KY/AR/MO): 69
pending, **19 would bind, 50 can never bind** (AZ is 20 pending / 1 bindable — names like
`AJO UNIFIED SCHOOL DISTRICT 15`, `SCHOOL DIST 103`, `FRANKFORT INDEPENDENT BOARD OF EDUCATION`).
Unmatchable leads keep their state permanently at the top of the pending list, so after ~2 runs the
top slots are occupied by exhausted states and the other 31 are never reached. Daily scheduling is
wrong until unmatchable leads are marked; weekly with a wider `--limit-states` is the safe interim.
Latent bug: the state-selection query treats `nces_id=''` as pending but `enrich_state_leads` filters
`nces_id IS NULL` only — inert today (0 rows have `''`) but it would re-queue a state forever.

**Timing/concurrency:** `nces-bind` fetch measured **16.6 s for 5 states** (2 paged ArcGIS queries
each, 1 s `polite_get` sleep dominates). SQLite is WAL with `busy_timeout=10000` and network I/O
sits OUTSIDE the write transactions, so overlap is tolerable — but `salesforce_sync.sync` has no
try/except around `_persist`, so a lock error aborts the whole run, and `cmd_salesforce_sync`
returns 1 if ANY lead is partial/unavailable, making its exit code a noisy health signal.
`cron.log` is 1.7 MB with no rotation. Related: [[drip-slot-band-vs-cron-granularity]],
[[nces-binding-blocks-rich-card]], [[firecrawl-paid-call-surface]].
