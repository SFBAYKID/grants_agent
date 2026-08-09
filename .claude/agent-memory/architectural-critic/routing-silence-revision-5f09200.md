---
name: routing-silence-revision-5f09200
description: 2026-08-06 read-only review of deployed 5f09200 (unmapped state renders NO routing line) — clean except a silent mention-drop when conversations_members fails, plus stale routing.py docstring; block order is now conditional (route block may be absent)
metadata:
  type: project
---

Review of the production range 359c1e3→5f09200 (deployed 2026-08-06, first live tick
11:00 PT that day). Verdict: Approved — no Critical/High. Companion closure notes in
[[rich-daily-fallback-wiring]].

**What 5f09200 changed:** `territory.routing_line` is now just `return mention_line(state,
source)` (empty for unmapped AND inferred); `campaign/card.py fallback_text` route piece is
`"Assigned to <@id>. "` or `""`; `card.render` OMITS the route section when no
`route.slack_user_id`. So **the rich card's block order is conditional**: route is blocks[1]
only when an owner is mapped; otherwise the award section is blocks[1]. Verified 2026-08-06:
the ONLY runtime consumers of `RenderedCard.blocks` pass the whole tuple
(delivery.py:229/243); button handlers (`slack/proactive_actions.py`) never touch blocks;
thread replies use `snapshot.lead_context` FACTS, not blocks; `card.render` has exactly ONE
call site (delivery.run, on the freshly-frozen snapshot — old frozen snapshots are never
re-rendered because `_snapshot_key` fingerprints include `fallback_text`, so changed wording
mints a new snapshot row). Anything NEW that indexes `blocks[1]` for the route will break on
unmapped cards — check this on every future blocks consumer.

**Medium, still open — silent mention drop on a members-API failure:**
`delivery.channel_members` swallows EVERY exception into `frozenset()` with no log
(delivery.py:70), and `routing.resolve` requires `territory_owner in channel_members`. So a
transient `conversations_members` failure makes a PA/CA/WA/TX/OR rich card freeze
`routing_reason='unassigned'` and render with NO mention. Pre-5f09200 that day's card showed
a visibly WRONG "Unassigned territory" label a human would question; now the symptom is
invisible — indistinguishable from a genuinely unmapped state, and frozen permanently into
the snapshot. Not an honesty violation (nothing false is asserted) but an observability
regression. Remedy when touched next: one stderr line in `channel_members`' except handler,
and/or log when a verified-source mapped-state card resolves unassigned. Note the asymmetry:
the DAILY fallback card's `routing_line` needs no channel membership, so it still tags on
such a day.

**Low:** `campaign/routing.py` module docstring line 7 still says unassigned leads "render
explicitly as unassigned territory (Chase A2)" — false since 5f09200. Fix on next touch.

**Confinement verified:** the string "unassigned; no verified owner mapped"
(`snapshot.lead_context`'s `snapshot_routing`) reaches ONLY `conversation.lead_facts`'s
FACTS block for thread replies (grant.py:480) — never the card face. The model could repeat
it in a thread reply if asked; that is the permitted operator surface.

**Also verified this review:** the range's `4a4d550` "ruff format" commit is behavior-neutral
by AST comparison of all 15 touched runtime files; `fallback_text` spacing is clean by
execution across all 16 route×crm×contact×mode combinations; daily `render_blocks` shapes
(header+sentence minimum) are valid for empty routing/source; drip.py's
`_CONTENT_SLACK_ERRORS` still quarantines `invalid_blocks` rather than wedging; the
rewritten tests STRENGTHENED the pins (unmapped → exactly `""`; rich unassigned → no
"Unassigned" and no "<@" on text AND blocks; inferred-state silence test intact).
