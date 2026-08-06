---
name: firecrawl-paid-call-surface
description: Every code path on the droplet that spends Firecrawl credits — the RFP poller (scheduled, ≤40 calls/weekday) plus two Slack-triggered paths; audited read-only 2026-07-25
metadata:
  type: project
---

Audited read-only 2026-07-25 when Chase suspected something was draining Firecrawl credits.

**Exactly three deployed code paths spend Firecrawl credits** (`verified` — these are the only modules
that read `FIRECRAWL_API_KEY`):

1. **`sources/rfp.py` — the ONLY SCHEDULED spender.** Gated by `RFP_DISCOVERY_ENABLED=1` (set in the
   droplet `.env` 2026-07-18), it runs inside the weekday `0 7 * * 1-5` poll as the poller named
   **"Security RFP discovery"**. Cost shape: `_SEARCH_QUERIES` = **6** queries/run, `_RESULTS_PER_QUERY`
   = 4 pages scraped per query, hard ceiling `_MAX_FIRECRAWL_CALLS = 40` per run (search+scrape
   combined). So **≤40 calls per weekday ≈ ≤200/week**, and zero on weekends. Verified run history
   2026-07-20 → 07-24, all ~14:00–14:10 UTC (07:00 PT).
2. **`enrich/finder.py` `_search()` (find_contact)** — Slack/human-triggered, never scheduled.
3. **`slack/tools.py` `web_search`** → `https://api.firecrawl.dev/v1/search` — LLM-triggered inside a
   Slack conversation, never scheduled.

`source_discovery_batch.py` (the big paid batch worker) also reads the key but has NOT run since
**2026-07-15**; the only batch dir is `data/source_catalog/firecrawl_batches/20260716T004633Z`.

**`GRANT_RICH_CARD_ENABLED` is UNSET/absent on the droplet**, so `campaign/preparation.py` (the
rich-card possibly-paid contact discovery) cannot run. `paid_enrichment_attempts` table = 0 rows.

**Diagnostic gotchas learned:**
- **Grepping cron.log for `402` / `429` / `billing` is worthless — all false positives.** Award rows
  carry dollar amounts (`$402,336`, `$327,429`) and there is a real **BILLINGS SCHOOL DISTRICT**.
  Use strict patterns (`HTTPError|status_code|Traceback|requests\.exceptions|\[rfp\]`) instead.
- **`bot.log` lines carry NO timestamps**, so tool-call counts in it can never be dated — only bounded
  by the file mtime. Do not claim "N calls this week" from bot.log. See [[grant-bot-silent-llm-fallback]].
- The design's `in_flight` durable pre-HTTP attempt marker: searched the whole `data/` tree, **0 files
  contain it**, and there are no `.lock` files and `poll_locks`=0 — a clean way to prove no paid call
  is mid-flight or was interrupted.
- Socket check: the bot's only established connections go to **AWS us-west-2** (Slack; matches
  `wss-primary.slack.com`). `api.firecrawl.dev` resolves to **35.245.250.27** (GCP) — a different
  network, so a live Firecrawl call is easy to distinguish. `ss -tnp` without sudo shows host-wide
  sockets but attributes only our own PIDs; **never probe the unattributed ones — they belong to other
  tenants.**
- **The laptop holds the SAME `FIRECRAWL_API_KEY`** and is a second consumer that a droplet-side audit
  cannot see (the 2026-07-23 rich-card enrichment reruns were laptop-side). Any credit-drain question
  must consider both, and neither can be settled without the Firecrawl account's own usage page.

See [[tenant-and-layout]], [[rfp-poll-populate]].
