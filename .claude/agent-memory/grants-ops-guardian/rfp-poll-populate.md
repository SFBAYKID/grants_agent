---
name: rfp-poll-populate
description: How to populate the droplet DB with CURRENT open RFP leads for a live test — targeted RFP poll, verified live
metadata:
  type: project
---

To make the tenant DB show real, current OPEN RFP leads (e.g. before a live Slack demo), run a single
targeted poll of only the wired RFP aggregator. Verified live end-to-end on the droplet 2026-07-19.

**Command (grantwatch venv, app dir, flag in env):**
`cd ~/grants_agent && RFP_DISCOVERY_ENABLED=1 .venv/bin/python -m grant_watch.cli poll --source RFP`

**Why it is safe/correct:**
- `--source RFP` filters by substring against poller *names* (`cli.cmd_poll`: `only_source.lower() not in
  name.lower()`). Only the aggregator's poller is named "Security RFP discovery" (contains "rfp"); NO other
  poller name (ca_grants, grants_gov, oregon_buys, sam_gov, usaspending, webs, SAM.gov) contains "rfp", so
  exactly one source runs — no accidental fan-out, one Firecrawl scrape.
- The aggregator (`sources/rfp_aggregator.py`) scrapes the Starbridge physical-security listing via the
  existing FIRECRAWL_API_KEY, cherry-picks WA/OR/CA/PA/TX rows whose prose *names* a target state, and only
  emits rows with a parseable Close date >= today (`end=due_iso`). So every ingested `source='rfp'` lead is
  OPEN by construction; it writes `funds_end`=close date, `state`, `url`→`detail_url`. Grade is scored
  downstream (RFP_POSTED → GOLD when recent, else SILVER). Upsert is idempotent (content id = entity+title+due).
- Poll has its own overlap guard ("poll already running"); the daily 07:00 PT cron poll won't collide midday.
  WAL mode = the poll's writes are safe while the Grant bot runs.

**What the live run actually returned (2026-07-19, before=0/0):** just **2 open rows** — the aggregator only
surfaces the currently-open target-state listings, and they CHURN FAST. Both closed within ~3-4 days
(CA gold due 2026-07-22 = California Dept of General Services access-control RFP; PA silver due 2026-07-23 =
PA Dept of Corrections SCI Pine Grove cameras). Expect a small, short-fused set, not a big backlog — a live
test that needs a specific state should run BEFORE those close dates. RFP_DISCOVERY_ENABLED has been ==1 on
the droplet .env since 2026-07-18 (see [[tenant-and-layout]]).

**Read-only verify (SELECT-only, safe on live WAL DB):**
`SELECT lead_grade, COUNT(*) FROM leads WHERE source='rfp' AND date(funds_end) >= date('now') GROUP BY lead_grade;`
and example rows `SELECT entity_name,state,lead_grade,funds_end,detail_url FROM leads WHERE source='rfp'
AND date(funds_end) >= date('now') ORDER BY state;`. See [[deploy-mechanism]] and [[tenant-db-write-safety]].
