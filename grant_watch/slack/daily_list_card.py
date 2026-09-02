"""Rendering for the daily list: one card per award, approved by Chase 2026-09-01.

Split from `daily_list.py` because these are pure functions over rows — no client, no
connection, no state — so a change here cannot alter what gets reserved or recorded.
The same separation `approval_blocks.py` already makes for the same reason.

WHAT A ROW MAY CLAIM. Only what the stored evidence supports: the organization, the
state (omitted rather than printing a bare two-letter code), a positive amount, the
program, the award date at its stored precision, and the AGE. Never a contact, a
website or a Salesforce state — those live behind the frozen-snapshot evidence gates
and putting an unfrozen one on a list row is the fabrication rule 1 forbids.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date

from ..campaign.card import safe_text, safe_url
from ..presentation import award_age_phrase, display_entity_name, state_display_name

# Slack refuses a message over 50 blocks. Two blocks a card plus three of preamble.
MAX_BLOCKS = 48

# What KIND of organization this is, from the PROGRAM — a stored fact with published
# eligibility rules. `leads.entity_type` is the empty string on every row in production
# and is never written, so it cannot answer; guessing from the name would be an
# inference printed as a fact, and NSGP funds religious day schools as well as
# congregations. A program with no entry prints no segment rather than a guess.
SEGMENT = {
    "NSGP": "nonprofit / faith community",
    "SVPP": "school district",
    "CSSGP": "school district",
    "PCCD": "school district",
}

# A record number welded onto a name — "SAINT ANNES EPISCOPAL SCHOOL_443031012" is
# real. `plain_fragment` strips the underscore and GLUES the digits on, which is worse
# than leaving it, and CLAUDE.md bans internal identifiers from anything Grant says.
_ID_SUFFIX = re.compile(r"[_\s]\d{4,}$")


def clean_entity_name(value: object, limit: int = 90) -> str:
    """The organization's name, humanised, with any trailing record number removed."""
    return display_entity_name(_ID_SUFFIX.sub("", str(value or "").strip()), limit)


def _money(amount: object) -> str:
    """Whole dollars, or "" when the source published no usable figure."""
    try:
        value = float(amount)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    return f"${value:,.0f}" if value > 0 else ""


def _award_date(value: str, precision: str) -> str:
    """Only the precision the stored evidence supports — month stays a month."""
    try:
        parsed = date.fromisoformat(str(value)[:10])
    except ValueError:
        return ""
    if precision == "month":
        return parsed.strftime("%b %Y")
    return f"{parsed:%b} {parsed.day}, {parsed:%Y}"


def _event_label(event_type: object) -> str:
    """Name the dated event in the source's own terms."""
    return "obligated" if str(event_type or "") == "award_obligated" else "announced"


def card(row: sqlite3.Row, today: date, index: int, total: int) -> list[dict]:
    """One award as its own card — ONE block, deliberately.

    A per-card divider made every card cost two blocks, so 25 cards needed 53 against
    Slack's hard ceiling of 50 and the renderer silently dropped the last three. The
    cap could not be raised to fix that; the divider had to go.
    """
    name = safe_text(clean_entity_name(row["entity_name"]), 90)
    where = state_display_name(row["state"])
    heading = f"*{name}*" + (f" — {where}" if where else "")

    occurred = str(row["current_event_occurred_on"] or "")
    shown = _award_date(occurred, str(row["current_event_date_precision"] or ""))
    age = award_age_phrase(occurred, today)
    when = ""
    if shown:
        when = f"{_event_label(row['current_event_type'])} {shown}"
        if age:
            when = f"{when}, {age}"

    program = safe_text(row["program"], 24)
    facts = " · ".join(
        bit
        for bit in (
            _money(row["amount"]),
            program,
            SEGMENT.get(str(row["program"] or "").upper(), ""),
            when,
        )
        if bit
    )
    url = safe_url(row["current_event_source_url"] or row["detail_url"] or "")
    record = f"\n<{url}|Federal award record>" if url else ""
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{heading}\n{facts}{record}\n_{index} of {total}_",
            },
        }
    ]


def build_blocks(
    rows: list[sqlite3.Row], today: date
) -> tuple[list[dict], list[sqlite3.Row]]:
    """The list, bounded to Slack's ceiling, AND the rows it actually rendered.

    RETURNING THE RENDERED ROWS IS THE WHOLE POINT OF THE SIGNATURE. Truncating is
    right — `invalid_blocks` is a content error that releases the entire list, so
    posting 22 cards beats posting none. Truncating SILENTLY is not: the first version
    of this returned only blocks, the caller marked all 25 leads delivered, and three
    real leads were consumed and could never be shown to anyone again. They did not
    even look wrong — a dropped lead is indistinguishable from one that was never
    selected. The count in the header and in each "n of N" is now the number a reader
    can actually see, so the message cannot claim 25 while showing 22.
    """
    shown = rows[: max(0, (MAX_BLOCKS - 3))]
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"Freshest funding — {len(shown)} newest awards",
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        "Newest first, and nothing here has been posted before. "
                        "Reply in this thread to take one, or ask me for contacts."
                    ),
                }
            ],
        },
        {"type": "divider"},
    ]
    for index, row in enumerate(shown, start=1):
        blocks.extend(card(row, today, index, len(shown)))
    return blocks, list(shown)


def notification_text(rows: list[sqlite3.Row]) -> str:
    """The lock-screen line: names the count and the freshest organization."""
    first = safe_text(clean_entity_name(rows[0]["entity_name"], 60), 60) if rows else ""
    return f"Freshest funding: {len(rows)} newest awards, starting with {first}"
