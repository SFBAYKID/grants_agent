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

## IT ALSO SILENTLY REORDERS THE NUDGE QUEUE — 2026-08-09

The worst instance yet, because nothing failed and the wrong answer looked authoritative.
A bare-python walk of `nudges.candidates()` put **`U06RXJKRXSR` at eligible #0**, while
`cli nudge --dry-run --force` (stable across three consecutive runs) named **Kerry
`U01E908206M`**. Neither errored. The cause: `suppress_reason` calls
`_capability_is_live('email_results')` → `resend_client.is_configured()`, which reads
`RESEND_API_KEY`. Without `.env` that is **False**, so Kerry's ask is suppressed
`capability_not_ready` and the head of the queue silently becomes a different colleague.
With `load_dotenv()` the two agree exactly (ELIGIBLE 19, PERMANENT 25).

So a bare one-off does not merely degrade — it can produce a **confident, plausible,
wrong answer about which human a proactive message would reach**. Reporting that walk
would have been fabrication of exactly the kind the Constitution forbids.

Two mechanical traps hit while fixing it: `load_dotenv()` with no argument **raises
`AssertionError`** under `python - <<'PY'` (its `find_dotenv` walks `frame.f_back`, which
is None from stdin), and writing the script to `/tmp` puts it outside the repo so
`import grant_watch` fails — write it inside `~/grants_agent/` and delete it after.

**How to apply:** before running any one-off on the tenant, confirm it calls `load_dotenv()`
itself, or supply the env without editing it (`set -a && . ./.env && set +a && .venv/bin/python …`).
**Whenever a one-off's answer disagrees with the CLI's, believe the CLI and suspect the env
first** — and never report a queue/eligibility result from a walk that did not load `.env`.
Order the script's steps so the cheapest credential check fails FIRST — a script that
validates `os.environ` up front cannot write a misleading `unavailable` snapshot. Related:
[[firecrawl-paid-call-surface]], [[tenant-db-write-safety]].
