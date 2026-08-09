---
name: oneoff-scripts-need-load-dotenv
description: cwd does NOT load the tenant .env — only cli.py/grant.py/source_discovery_batch.py call load_dotenv(), so any one-off script runs with a bare env and silently degrades
metadata:
  type: project
---

A one-off script on the droplet that imports `grant_watch.*` directly gets **no `.env` at all**,
no matter what its cwd is. `load_dotenv()` is called in exactly three entrypoints
(`grant_watch/cli.py:411`, `grant_watch/slack/grant.py:874`,
`grant_watch/source_discovery_batch.py:490`), all inside their `main()`. Importing
`grant_watch.db` / `grant_watch.campaign` / `grant_watch.enrich` triggers none of them.

**Why:** verified 2026-08-06 running a one-off rich-card preview for lead #1603 with
`cd ~/grants_agent && .venv/bin/python _oneoff_x.py`. Result: `FIRECRAWL_API_KEY not
configured`, Salesforce `unavailable` ("missing SALESFORCE_MY_DOMAIN_URL,
SALESFORCE_CLIENT_ID, SALESFORCE_CLIENT_SECRET"), then `KeyError: 'SLACK_BOT_TOKEN'`.
CLAUDE.md's older lore says "a background cwd silently skipped .env" — that framing is
misleading. cwd only matters *given* someone calls `load_dotenv()` (which walks up from cwd
to find the file). No call, no env, any cwd.

**The damage this shape causes is silent, not loud.** The paid/credentialed steps degrade
into honest-looking negatives instead of crashing early: the Salesforce lookup returned
`unavailable` and `salesforce_sync._persist` WROTE that row to prod, which suppresses that
lead from `_candidates` re-lookup for `STALE_HOURS = 24`. A missing-config run can therefore
leave real state behind before it ever reaches its first hard failure.

**How to apply:** before running any one-off on the tenant, confirm it calls `load_dotenv()`
itself, or supply the env without editing it (`set -a && . ./.env && set +a && .venv/bin/python …`).
Order the script's steps so the cheapest credential check fails FIRST — a script that
validates `os.environ` up front cannot write a misleading `unavailable` snapshot. Related:
[[firecrawl-paid-call-surface]], [[tenant-db-write-safety]].
