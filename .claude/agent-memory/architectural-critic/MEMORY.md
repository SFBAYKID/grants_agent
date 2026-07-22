# Memory index — architectural-critic (grants_agent)

- [Persequor handoff is a no-op](persequor-handoff-noop.md) — shipped approve flow writes contacted/sent_at but Persequor verifiably drops bot mentions
- [Workflow design review 2026-07](workflow-design-review-2026-07.md) — verdict + required changes for docs/workflow_design.md (request_id pinning, build-order reorder, stuck-state semantics)
- [Grant on-chat search weak spots](grant-onchat-search-weakspots.md) — durable failure modes in search.py/conversation.py/tools.py/finder.py: prose-only turn state, non-deterministic top-N, not_found honesty gap, missing lead_id, sync handler, untested state machine
- [RFP discovery source fragilities](rfp-discovery-source-fragilities.md) — Firecrawl+LLM "rfp" poller: verbatim-but-wrong dates, entity-vs-vendor, multi-RFP cross-contamination, item_id namespacing, relevance block-list, coverage honesty, drip boundary
- [RFP aggregator + staleness fragilities](rfp-aggregator-and-staleness-fragilities.md) — wired Starbridge aggregator: item_id over-merge, frozen-grade "closed reads as open", VERIFIED overload, unvalidated LinkedIn name (state-misfile item 1 since fixed)
- [posts.kind CHECK vs new drip kinds](posts-kind-check-vs-drip-kinds.md) — RESOLVED by migration 13; kept for the test-topology lesson (every live run_drip test used the one allowed kind)
- [Drip wedges on an ambiguous send](drip-wedge-on-ambiguous-send.md) — RESOLVED by 85295d7 (outbox exclusion); kept for the "one top-ranked lead stops everything" pattern
- [Drip slot + gold pool reality](drip-slot-and-gold-pool.md) — slot collapses to 3 clock times, no missed-slot fallback (window clamp fixed 85295d7); pool = ~195 same-day SVPP then ~347 undated CA rows
- [Soft state tags the wrong rep](soft-state-tags-the-wrong-rep.md) — RESOLVED by 85295d7's VERIFIED_STATE_SOURCES allowlist; the underlying `_row_state` prose inference is still wrong
- [Drip wedge class: remaining paths](drip-wedge-class-remaining-paths.md) — after 85295d7: unrenderable candidate still crashes every tick forever; a deterministic Slack rejection now silently burns leads; nothing surfaces a burned lead
