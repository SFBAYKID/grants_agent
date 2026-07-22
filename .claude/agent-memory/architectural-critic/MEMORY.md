# Memory index — architectural-critic (grants_agent)

- [Persequor handoff is a no-op](persequor-handoff-noop.md) — shipped approve flow writes contacted/sent_at but Persequor verifiably drops bot mentions
- [Workflow design review 2026-07](workflow-design-review-2026-07.md) — verdict + required changes for docs/workflow_design.md (request_id pinning, build-order reorder, stuck-state semantics)
- [Grant on-chat search weak spots](grant-onchat-search-weakspots.md) — durable failure modes in search.py/conversation.py/tools.py/finder.py: prose-only turn state, non-deterministic top-N, not_found honesty gap, missing lead_id, sync handler, untested state machine
- [RFP discovery source fragilities](rfp-discovery-source-fragilities.md) — Firecrawl+LLM "rfp" poller: verbatim-but-wrong dates, entity-vs-vendor, multi-RFP cross-contamination, item_id namespacing, relevance block-list, coverage honesty, drip boundary
- [RFP aggregator + staleness fragilities](rfp-aggregator-and-staleness-fragilities.md) — wired Starbridge aggregator: item_id over-merge, frozen-grade "closed reads as open", VERIFIED overload, unvalidated LinkedIn name (state-misfile item 1 since fixed)
- [posts.kind CHECK vs new drip kinds](posts-kind-check-vs-drip-kinds.md) — RESOLVED by migration 13; kept for the test-topology lesson (every live run_drip test used the one allowed kind)
- [Drip wedges on an ambiguous send](drip-wedge-on-ambiguous-send.md) — CRITICAL, unfixed at 0a83d73: one Slack timeout freezes the entire drip forever; proven empirically
- [Drip slot + gold pool reality](drip-slot-and-gold-pool.md) — slot collapses to 3 clock times, no window clamp, no missed-slot fallback; pool = ~195 same-day SVPP then ~347 undated CA rows
- [Soft state tags the wrong rep](soft-state-tags-the-wrong-rep.md) — territory @mentions inherit leads.state; the aggregator's name-substring state makes "Oregon, Ohio" ping the OR rep
