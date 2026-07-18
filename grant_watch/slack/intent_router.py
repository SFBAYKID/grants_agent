"""Deterministic pre-model routing for common Grant questions.

Why: capability questions ("what can I ask you?") and simple source-inventory
listings deserve instant, dependable answers instead of a free-form model turn
that can drift. This module recognizes a few tolerant phrasings BEFORE any
Anthropic call and answers them from a fixed capabilities script or validated
local discovery evidence. Anything unrecognized returns None and falls through
unchanged to the conversational model in conversation.respond().

It runs AFTER source_status.slack_source_status_reply, which already handles
the richer inventory phrasings and the paid-discovery refusal — this router
only catches what that pre-pass misses.
"""

from __future__ import annotations

import re

from .source_status import (
    _namespace_from_text,
    _state_from_text,
    source_inventory_status,
)

# Grant's fixed capability answer. Facts only: every line maps to a shipped
# workflow (search_leads, lead_stats, find_contact, source_inventory_status,
# Salesforce previews, and the Persequor handoff). No backticks (Slack renders
# them red); bullets and plain quotes only.
CAPABILITIES_REPLY = """Happy to help! Here's what I can do:
• Find schools and cities that recently won government security funding
• Search our grant leads by state, program, grade, amount, or date
• Break down our leads by state, program, or grade
• Track down the best contact at an awardee and check Salesforce for them
• Show which funding sources we've reviewed and how far research coverage has gotten
• Add qualified leads to Salesforce — you approve every change first
• Bring in Persequor to draft outreach email — a human always approves the send

Try one of these:
• "Find gold school leads in Michigan"
• "List the last 5 reviewed sources"
• "Who's the best contact at Mt. Morris Consolidated Schools?"

Just ask in plain English, right here in the thread."""

# Capability-help family: tolerant phrasings of "what can you do for me?".
_CAPABILITY_PATTERNS = (
    re.compile(r"\bwhat(?:\s+\w+){0,4}\s+can\s+i\s+ask\b"),
    re.compile(r"\bwhat\s+can\s+you\s+(?:do|help)\b"),
    re.compile(r"\bwhat\s+do\s+you\s+do\b"),
    re.compile(r"\bwhat\s+are\s+you\s+(?:capable\s+of|able\s+to\s+do|for)\b"),
    re.compile(r"\bwhat\s+are\s+your\s+capabilities\b"),
    re.compile(r"\bhow\s+(?:do|can|should)\s+i\s+use\s+(?:you|grant|this)\b"),
    re.compile(
        r"^\s*(?:@?grant[,:!]?\s*)?(?:hi|hey|hello)?[,!.\s]*"
        r"help(?:\s+me)?\s*[?!.]*\s*$"
    ),
)
# Reviewed-sources family: listing asks the source_status pre-pass doesn't
# recognize ("what sources…", "show me the sources you've reviewed"). The
# lookahead keeps figurative uses ("sources of funding") on the model path.
_REVIEWED_SOURCES_PATTERN = re.compile(
    r"\b(?:what|which|show|list|see)\b[^.?!]{0,40}\bsources\b(?!\s+of\b)"
    r"|\bsources\b[^.?!]{0,30}\breviewed\b"
)
# Discovery-status family: bare "coverage" or loose "…discovery…" wording.
_COVERAGE_PATTERN = re.compile(r"\bcoverage\b")
_DISCOVERY_PATTERN = re.compile(r"\bdiscovery\b")
_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}
_LIMIT_PATTERN = re.compile(
    r"\b(?:top|last|latest|show|first)\s+(\d{1,2}|"
    + "|".join(_NUMBER_WORDS)
    + r")\b"
)


def _requested_limit(lowered_text: str, default: int = 10) -> int:
    """Read an explicit small count ("last 5", "top three") from the message."""
    match = _LIMIT_PATTERN.search(lowered_text)
    if match is None:
        return default
    raw = match.group(1)
    count = int(raw) if raw.isdigit() else _NUMBER_WORDS[raw]
    return min(25, max(1, count))


def deterministic_reply(
    user_text: str, thread_context: list[str] | None = None
) -> str | None:
    """Answer a recognized routine question without constructing a model client.

    Returns the finished Slack reply text, or None when the message belongs on
    the conversational model path. Routing keys on the current human message
    only; thread context is accepted for signature parity but unused.
    """
    del thread_context
    lowered = user_text.lower()
    if any(pattern.search(lowered) for pattern in _CAPABILITY_PATTERNS):
        return CAPABILITIES_REPLY
    if _REVIEWED_SOURCES_PATTERN.search(lowered):
        return source_inventory_status(
            view="reviewed_sources",
            state=_state_from_text(user_text),
            namespace=_namespace_from_text(user_text),
            limit=_requested_limit(lowered),
        )
    if _COVERAGE_PATTERN.search(lowered):
        return source_inventory_status(
            view="coverage",
            state=_state_from_text(user_text),
            namespace=_namespace_from_text(user_text),
        )
    if _DISCOVERY_PATTERN.search(lowered):
        return source_inventory_status(
            view="summary", state=_state_from_text(user_text)
        )
    return None
