---
name: grant-onchat-search-weakspots
description: Durable architectural weak spots in Grant's Slack on-demand search / contact-enrichment path (search.py, conversation.py, tools.py, finder.py)
metadata:
  type: project
---

Durable weak spots found stress-testing the "list-then-offer-contacts" on-chat search design (2026-07-14 review of the with_contacts/confirm-first plan). These recur for ANY multi-turn or enrichment feature on Grant, so re-check them on future designs.

**Why:** these are structural, not one-off bugs — they bite any feature built on the same primitives.

**How to apply:** when reviewing any new Grant conversational/enrichment feature, check each of these first.

1. **Multi-turn state is prose-only.** Grant has NO structured dialogue-state store. `_thread_history` (grant.py) rebuilds context from visible Slack messages, `_thread_history` returns `lines[-10:]`, and `conversation.respond` feeds the model only `thread_context[-6:]`. So any "confirm first, act on the next reply" flow depends on the model re-parsing its own prior prose, and the original request can scroll out of the 6-line window. There is no deterministic gate forcing confirm-before-act — it is 100% prompt-encoded.

2. **`search_leads` top-N is non-deterministic.** ORDER BY clauses in `_date_clause`/`search_leads` have no unique tiebreaker (e.g. default `datetime(first_seen) DESC, amount DESC`; date searches order only by `date(...)` granularity). Real GOLD/SVPP rows share funds_start (all `2025-10-01` in fixtures), so LIMIT N is unstable across two identical queries. Fix pattern: append `, id ASC` to every order_sql. Matters because the design runs the search TWICE (list, then enrich).

3. **`contacts` table cannot express "we couldn't reach the source."** Schema `contact_status` is only unverified|verified|not_found; no 'error'/'unreachable'. `finder.find_contact` returns None on Firecrawl RequestException (per-angle `continue`), so a transient Firecrawl/Anthropic outage gets persisted as substantive `not_found` — a Constitution rule-1 honesty violation (a human later trusts not_found as final). Also no UNIQUE constraint → re-running enrichment appends duplicate verified rows and multiple not_found rows per lead.

4. **`_SEARCH_COLUMNS` omits `leads.id`.** Search result rows carry source_item_id but not the PK, so contact persistence can't tie a row back to its lead without ambiguous entity-name matching (entity names demonstrably collide across sources — see `db.reconcile_seed_duplicates`).

5. **Slack event handlers run the full LLM+tool loop synchronously with no idempotency guard.** `_handle_drip_thread` blocks on `conversation.respond`. No dedup on Slack event_id/client_msg_id, so a slow handler (enrichment is minutes) risks Slack redelivery → duplicate concurrent runs, duplicate spinners, duplicate cost. Long work belongs in a background job, not the event handler.

6. **The dialogue state machine is untested.** No test exercises `conversation.respond` (Anthropic is live; test_slack.py is "pure/offline"). CI cannot catch regressions in prompt-encoded flow. Regression-testing requires stubbing the Anthropic client with scripted tool_use turns.
