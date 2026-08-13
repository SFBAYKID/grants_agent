---
name: firecrawl-paid-call-surface
description: Every droplet path that spends Firecrawl credits — the RFP poller and rich-prepare cron (scheduled) plus the Slack search_leads with_contacts path, which can fire ~1,000 calls from ONE message; re-audited read-only 2026-08-12
metadata:
  type: project
---

First audited 2026-07-25; **re-audited read-only 2026-08-12 at `9fb6813`, and two of the
2026-07-25 conclusions were STALE. Both retractions are below — do not trust the old
version of this note.**

## THE BIG SPENDER IS SLACK, NOT CRON

`slack/search_enrichment.py` is the path that can spend four figures of calls from a
single Slack message, and it is the one to check first:

- `MAX_ENRICH_ROWS = 100` (raised from 10 on 2026-08-11, commit `85bec38`, on `main`).
- `ENRICH_WORKERS = 8` (`GRANT_ENRICH_WORKERS`), a `ThreadPoolExecutor`.
- `ENRICH_TIME_BUDGET_S` bounds only when a NEW lookup may **start**, not total spend.
- Chain: `search_leads(with_contacts=true, limit=N)` → `search.py` →
  `search_enrichment._enrich_contacts` → `tools.enrich_lead_contact` →
  `slack/contact_enrichment.py:127` (writes `paid_enrichment_attempts` with
  `operation='legacy_contact_enrichment'`, `request_key='legacy-contact:<lead_id>'`) →
  `enrich/finder.find_contact`.
- **Cost per organization:** `find_contact` walks `_angles_for(entity)` = **4** angles
  (`_SCHOOL_ANGLES` / `_CITY_ANGLES`), each a `_search(limit=4)`, plus scrapes bounded by
  `max_pages=6`, plus a further `_search(limit=5)` later in the pipeline. So **≤10
  Firecrawl HTTP calls per organization**, ×100 organizations ≈ **≤1,000 calls per Slack
  message**.
- `finder` has **no backoff and no 429 handling** — `resp.raise_for_status()` only
  (lines 347, 366). Firecrawl's per-account ceiling remains UNVERIFIED.

**Measured example (2026-08-11 PT evening):** 109 `legacy_contact_enrichment` attempts
across 86 distinct leads, in two clusters — 23:06–23:07 PT (11) and 23:44–23:51 PT (98).
**37 failed `SourceUnreachable`**, and a `SourceUnreachable` attempt is deliberately NOT
cached, so re-asking re-spends. `bot.log`'s last two tool-turns are
`search_leads {"grade":"silver","limit":100,"state":"CA","with_contacts":true}` and its
mtime (23:51:21 PT) matches the final attempt's finish (23:51:12 PT). That is a tight
correlation, not proof — bot.log lines carry no timestamps, so they can only ever be
bounded by mtime.

## RETRACTIONS from the 2026-07-25 audit

- **`GRANT_RICH_CARD_ENABLED` is `=1` on the droplet, NOT unset.** The old note said
  `campaign/preparation.py` "cannot run". It runs daily. `45 7 * * 1-5 rich-prepare
  --execute` → `campaign/prepare_worker.py` → `campaign/contact_evidence.py:157` writes
  `operation='contact_refresh'`. Typical 8–25 attempts per weekday run; 2026-08-12 it was
  15 between 07:45 and 07:53 PT.
- **`paid_enrichment_attempts` is NOT 0 rows.** It held **229** rows on 2026-08-12,
  spanning 2026-07-27 → 2026-08-12. **This table is the only real per-organization spend
  ledger that exists — use it, not log greps.** Columns: `id, lead_id, operation,
  request_key, attempt_no, state, started_at, finished_at, error`. (No `vendor` and no
  `created_at` — query those and you get an `OperationalError`, which at least fails
  loudly.)

## Still true

- Scheduled spenders: `sources/rfp.py` inside the `0 7 * * 1-5` poll (gated by
  `RFP_DISCOVERY_ENABLED=1`, ≤40 calls/run) and the `45 7` rich-prepare cron.
- Live modules reading `FIRECRAWL_API_KEY`: `cli.py`, `enrich/finder.py`,
  `slack/tools.py`, `source_discovery_batch.py`. Modules hitting `api.firecrawl.dev`:
  `enrich/finder.py`, `firecrawl_client.py`, `slack/tools.py`.
  `enrich/organization_profile.py` spends indirectly via finder's `_search`/`_scrape`.
- **Log greps for `402`/`429`/`billing` are worthless** (award dollar amounts; a real
  BILLINGS SCHOOL DISTRICT). Strict patterns only —
  `HTTPError|requests\.exceptions\.[A-Za-z]+|\[rfp\]`. On 2026-08-12 that returned only
  the 13 known OregonBuys 404s and 4 benign `[rfp]` lines.
- **The laptop holds the same `FIRECRAWL_API_KEY`** and is a second consumer a
  droplet-side audit cannot see. No credit-total question is answerable without the
  Firecrawl account's own usage page.

## Stray-code sweep (2026-08-12, whole `/home/grantwatch/` tree)

Clean where it matters: the live checkout's only executable is `run_bot.sh`; no stray
`.sh`/`.py` in the home root; **no `.lock` files anywhere**; `poll_locks` = 0; no attempt
rows with a NULL `finished_at`; `firecrawl_batches/` still holds only the July
`20260716T004633Z` dir; `.venv` contains **no** Firecrawl SDK (the code calls the HTTP API
via `requests`).

**But ~410 files under ~30 `.grants_agent.previous.pre-*` and `.deploy_staging/` snapshot
dirs still carry `FIRECRAWL_API_KEY`**, including whole `.env` copies — the known
credential sprawl in [[env-credential-sprawl]]. They are **inert as spend paths**:
nothing in the crontab, `run_bot.sh`, or any user systemd unit references them (checked,
0 matches; there are no user units at all). They remain a secrets problem, not a billing
one.

## Deployed-bytes check

At `9fb6813` all nine Firecrawl-touching modules were **byte-identical** to the commit's
blobs (sha256 compared against `git cat-file blob`), and `9fb6813` is an ancestor of
`origin/main`. Verify the ground, not the map — a tracked-file deletion does not
propagate to the droplet (see the `deploy_rsync.sh` lesson).

See [[tenant-and-layout]], [[rfp-poll-populate]], [[utc-day-vs-pacific-day-trap]],
[[env-credential-sprawl]], [[prod-state-9fb6813-verified]].
