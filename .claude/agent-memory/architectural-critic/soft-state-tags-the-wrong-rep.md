---
name: soft-state-tags-the-wrong-rep
description: Territory @mentions inherit leads.state, and the wired RFP aggregator derives state by matching 5 state NAMES anywhere in the row — homonym places (Oregon OH, Texas Twp MI, Pennsylvania Ave) now @-tag the wrong Monarch rep
metadata:
  type: project
---

Proven 2026-07-22 by calling `rfp_aggregator._row_state` and `territory.mention_line` directly on
realistic row text.

`drip.run_drip` appends `territory.mention_line(row["state"])`. `territory.py` is careful in its own
right (validated Slack ids, never guessed, unmapped state → no tag), but it trusts `leads.state`
absolutely — and `leads.state` is only as good as the source that set it.

`rfp_aggregator._row_state` matches the five TARGET state names (`washington|oregon|california|
pennsylvania|texas`) anywhere in the row block and drops the row only when TWO OR MORE target states
appear. Non-target states are not in the map, so a homonym place in a non-target state reads as
unambiguous:

| row text | state | rep now @-mentioned |
|---|---|---|
| "Washington Township School District, New Jersey" | WA | Kerry Hilligus |
| "City of California, Missouri" | CA | Anthony Dambrosio |
| "Texas Township, Michigan" | TX | Kerry Hilligus |
| "Mount Washington School District, Kentucky" | WA | Kerry Hilligus |
| "Oregon City Schools, Ohio" | OR | Kerry Hilligus |
| "GSA … 1600 Pennsylvania Avenue NW" | PA | Brett D'Ambrosio |

Before territory tagging the blast radius was a wrong two-letter label on a card. Now it is a
notification on a named rep's phone asserting "<State> is your territory" for a deal in another
state — exactly what `territory.py`'s own docstring calls worse than tagging nobody, because it
silently reassigns revenue. The earlier dict-order misfile (Washington County, Pennsylvania → WA) WAS
fixed by the ambiguity rule; this residual class was not.

`usaspending` (state comes from the `recipient_locations` query filter) and `ca_grants` (hardcoded
"CA") are safe. `sources/rfp.py::state_from` trusts any clean 2-letter value the LLM emits — also
soft, but that module is NOT wired.

**Fix direction:** require a positional trailing ", <State>"/USPS code near the buyer, or bind to the
detail-page host; and/or gate the @mention on a state provenance tier so only query-derived or
hardcoded states may tag a rep.

**How to apply:** whenever a field starts driving a HUMAN-directed action (a mention, an assignment,
an email), re-audit every source that populates it. A field that was merely displayed is held to a
much lower bar than one that routes work to a person.

Related: [[rfp-aggregator-and-staleness-fragilities]], [[drip-slot-and-gold-pool]].
