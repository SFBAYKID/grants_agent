# Memory index — architectural-critic (grants_agent)

- [Persequor handoff is a no-op](persequor-handoff-noop.md) — shipped approve flow writes contacted/sent_at but Persequor verifiably drops bot mentions
- [Workflow design review 2026-07](workflow-design-review-2026-07.md) — verdict + required changes for docs/workflow_design.md (request_id pinning, build-order reorder, stuck-state semantics)
- [Grant on-chat search weak spots](grant-onchat-search-weakspots.md) — durable failure modes in search.py/conversation.py/tools.py/finder.py: prose-only turn state, non-deterministic top-N, not_found honesty gap, missing lead_id, sync handler, untested state machine
- [RFP discovery source fragilities](rfp-discovery-source-fragilities.md) — Firecrawl+LLM "rfp" poller: verbatim-but-wrong dates, entity-vs-vendor, multi-RFP cross-contamination, item_id namespacing, relevance block-list, coverage honesty, drip boundary
- [RFP aggregator + staleness fragilities](rfp-aggregator-and-staleness-fragilities.md) — wired Starbridge aggregator: state-from-substring misfile+recall loss, content item_id over-merge, frozen-grade "closed reads as open", VERIFIED overload, unvalidated LinkedIn name
- [posts.kind CHECK vs new drip kinds](posts-kind-check-vs-drip-kinds.md) — CRITICAL: CHECK allows only nugget/bulletin; live platinum/rfp post crashes at record_post AFTER Slack send; every live run_drip test uses nugget so suite stays green
