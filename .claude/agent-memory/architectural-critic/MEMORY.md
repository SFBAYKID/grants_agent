# Memory index — architectural-critic (grants_agent)

- [Persequor handoff is a no-op](persequor-handoff-noop.md) — shipped approve flow writes contacted/sent_at but Persequor verifiably drops bot mentions
- [Workflow design review 2026-07](workflow-design-review-2026-07.md) — verdict + required changes for docs/workflow_design.md (request_id pinning, build-order reorder, stuck-state semantics)
- [Grant on-chat search weak spots](grant-onchat-search-weakspots.md) — durable failure modes in search.py/conversation.py/tools.py/finder.py: prose-only turn state, non-deterministic top-N, not_found honesty gap, missing lead_id, sync handler, untested state machine
