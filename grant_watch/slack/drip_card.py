"""Block Kit rendering for the legacy daily drip card.

Chase's rule (2026-08-05): every proactive card renders in the NEWEST visual layout —
the rich campaign look — even on days when the strict rich-evidence gates cannot be
met. This module restyles the proven one-sentence daily card into that layout WITHOUT
adding a single claim: the sentence, routing line, and source link are the exact
strings the text-only card already posts, rearranged into blocks. No contact, website,
Salesforce state, or action button ever appears here — those exist only on the rich
path, where each is backed by frozen evidence (grant_watch/campaign/card.py).

The plain `text` of the Slack message remains the full concatenated card (sentence +
routing + source), so notifications and screen readers carry every fact and the blocks
can never disagree with the text — they are the same strings.
"""

from __future__ import annotations

from typing import Any

# Matches the rich card's bound (campaign/card.py MAX_SECTION); the sentences here are
# a few hundred characters at most, so this is a defensive ceiling, not a truncation
# that could hide a fact.
MAX_SECTION = 3000

# Header labels per pick() kind. A bulletin is program NEWS, not a verified award, so
# its header deliberately carries no tier or verification claim.
_HEADERS = {
    "platinum": "PLATINUM · Verified award",
    "nugget": "GOLD · Verified award",
    "rfp": "SILVER · Open RFP",
    "bulletin": "PROGRAM BULLETIN",
}


def render_blocks(
    kind: str, sentence: str, routing: str, source: str
) -> list[dict[str, Any]]:
    """Arrange the daily card's exact existing strings into the rich-card layout.

    `sentence` is the builder's verified one-liner, `routing` is
    territory.routing_line's output (may be empty — an inferred state asserts no
    territory), and `source` is source_line's output (may be empty — an unsafe or
    missing URL yields no link). Empty pieces drop their block rather than rendering a
    placeholder. An unknown kind raises KeyError, matching run_drip's builder lookup:
    both can only disagree with pick() through a programming error, which must be loud.
    """
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": _HEADERS[kind], "emoji": True},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": sentence.strip()[:MAX_SECTION]},
        },
    ]
    route = routing.strip()
    if route:
        # The only markup here is the roster-validated rep mention that
        # territory.routing_line already emits (an unmapped state emits nothing).
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": route[:MAX_SECTION]}}
        )
    src = source.strip()
    if src:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": src[:MAX_SECTION]}],
            }
        )
    return blocks
