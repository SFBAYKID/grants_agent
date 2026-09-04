"""The drip card's TEXT: one short factual sentence per card kind, and its source line.

Split from `drip.py` on 2026-09-04 when the newest-first ordering and the six-month
ceiling pushed it to 1016 lines (CLAUDE.md rule 4). This is the seam with the least
risk: every function here is a pure function over an already-selected row and a
clock — no Slack client, no database handle, no pacing state — so a change here
cannot alter WHICH lead is chosen or WHETHER a post happens. `drip.py` re-exports
these names, because 25 call sites across the tests, the CLI and the delivery path
reach them as `drip.build_nugget` and friends.

The rules the wording follows are Chase's: one sentence, no links or buttons inline,
the source link on its own line, an award's age stated in words, and never a
"received" or "just got" phrase for an event the source did not date.
"""

from __future__ import annotations

import math
import re
import sqlite3
from datetime import date

from ..presentation import (
    award_age_phrase,
    display_entity_name,
    plain_fragment,
    state_display_name,
)
from .search_presentation import record_link
from .source_status import _safe_url


def _fmt_amount(amount: float | None) -> str:
    """Format a finite positive source amount without silently dropping cents."""
    if amount is None or not math.isfinite(amount) or amount <= 0:
        return ""
    return f"${amount:,.2f}".removesuffix(".00")


def _award_facts(row: sqlite3.Row) -> tuple[str, str, str, str]:
    """Validate + extract the persisted award facts shared by nugget and platinum.

    Returns (entity, location, amount, program_text); raises on any unverified/missing
    fact so a proactive card is never built on incomplete evidence."""
    if str(row["current_event_verification_status"] or "") != "verified":
        raise ValueError("proactive award must be verified")
    if str(row["current_event_type"] or "") not in {
        "award_announced",
        "award_obligated",
    }:
        raise ValueError("proactive award has unsupported event type")
    entity = display_entity_name(row["entity_name"])
    if not entity:
        raise ValueError("proactive award requires an entity")
    # Nationwide polling means the code may be any state; an unrecognized code yields
    # no location rather than printing a bare abbreviation at a rep.
    state = plain_fragment(state_display_name(row["state"]))
    amt = _fmt_amount(row["amount"])
    if not amt:
        raise ValueError("proactive award requires a finite positive amount")
    location = f" in {state}" if state else ""
    program = plain_fragment(row["program"])
    program_text = f" {program}" if program else ""
    return entity, location, f" {amt}", program_text


def _award_when(row: sqlite3.Row, today: date | None = None) -> str:
    """ ", federal funds obligated October 10, 2025 — about 11 months ago" — or "".

    A CLAUSE, NOT A SENTENCE. The card is "one short factual sentence" by Chase's
    design and three tests pin exactly that, so the age is folded into the sentence
    the card already had rather than appended as a second one.

    THE LEGACY CARD CARRIED NO DATE AT ALL, and 34 of the 44 award cards ever posted
    were this shape: "X in Y has a verified $500,000 SVPP funding award." under a
    GOLD header, with no temporal content anywhere in the payload. A rep could not
    tell a three-week-old award from a three-year-old one, which is exactly what it
    cost when one phoned a district ten months after the obligation and was told the
    replacement was already finishing with a competitor.

    RETURNS "" ON AN UNREADABLE DATE rather than raising. Every gold lead should carry
    one — `scoring.grade` sends an undated award to SILVER — but three cards were
    posted in July from events with no date, before the pollers stored them, and a
    renderer that raised would quarantine a real lead over a missing nicety.
    """
    occurred = str(row["current_event_occurred_on"] or "")
    shown = _event_date(occurred, str(row["current_event_date_precision"] or ""))
    if not shown:
        return ""
    age = award_age_phrase(occurred, today)
    return f", {_event_label(row).lower()} {shown}{f' — {age}' if age else ''}"


def _event_label(row: sqlite3.Row) -> str:
    """Name the dated event in the source's own terms, never a generic 'awarded'."""
    return (
        "Federal funds obligated"
        if str(row["current_event_type"] or "") == "award_obligated"
        else "Award announced"
    )


def _event_date(value: str, precision: str) -> str:
    """Render only the precision the stored evidence actually supports."""
    try:
        parsed = date.fromisoformat(value[:10])
    except ValueError:
        return ""
    if precision == "month":
        return parsed.strftime("%B %Y")
    return parsed.strftime("%B %d, %Y").replace(" 0", " ")


def build_nugget(row: sqlite3.Row, today: date | None = None) -> tuple[str, str]:
    """Build one minimal award sentence using only persisted source facts."""
    entity, location, amount, program_text = _award_facts(row)
    return (
        f"{entity}{location} has a verified{amount}{program_text} funding award"
        f"{_award_when(row, today)}.",
        "award-brief",
    )


def build_platinum(row: sqlite3.Row, today: date | None = None) -> tuple[str, str]:
    """The cream: a security grant awarded in the last few days — the buyer is about to
    spend, so the card is timely and action-oriented (Chase: 'contact them now'). Facts
    only — same verified award data as a nugget, just worded for urgency."""
    entity, location, amount, program_text = _award_facts(row)
    return (
        f"{entity}{location} just landed a verified{amount}{program_text} security "
        f"award and is about to spend it — worth reaching out now"
        f"{_award_when(row, today)}.",
        "platinum",
    )


def build_bulletin(row: sqlite3.Row) -> tuple[str, str]:
    """Build truthful program news from an official opportunity record.

    The opportunity title is the news, with the posting agency as fallback.
    """
    what = plain_fragment(row["title"] or row["entity_name"])
    if not what:
        raise ValueError("proactive bulletin requires a title or entity")
    close = f" through {row['funds_end'][:10]}" if row["funds_end"] else ""
    text = f"{what} is listed as open{close}."
    return text, "bulletin-open"


def _short_title(value: object, limit: int = 88) -> str:
    """Sanitized solicitation title shortened from the MIDDLE, keeping head and tail.

    Tail-cutting is wrong here, and it shipped that way once. Sibling solicitations
    from one project share a long prefix and differ only at the end — the two real PA
    Corrections rows are 'Sci Pine Grove - Control Room, Security Cameras and Other
    Facility Upgrades - **General and HVAC Construction**' and '… - **Plumbing
    Construction \\*REBID\\***'. Trimming the tail cut off the only words that told them
    apart, so both cards still read identically (caught by Chase in the 2026-07-22
    playground preview). Keeping both ends preserves what the RFP is AND which package.

    plain_fragment already strips URLs, punctuation and every Slack control character
    (<>@`*_~|), so nothing here can inject a mention or a link.
    """
    text = plain_fragment(value, max_length=240)
    if len(text) <= limit:
        return text
    head_budget = limit // 2
    head = text[:head_budget].rsplit(" ", 1)[0].rstrip(" ,;:-")
    tail_budget = limit - len(head)
    tail = text[-tail_budget:].split(" ", 1)[-1].lstrip(" ,;:-")
    return f"{head} … {tail}" if tail else f"{head}…"


_RFP_CAMERA_RE = re.compile(r"camera|surveillance|cctv|\bvideo\b", re.IGNORECASE)
_RFP_ACCESS_RE = re.compile(
    r"access control|door (?:access|hardening)|card reader", re.IGNORECASE
)


def build_rfp_alert(row: sqlite3.Row) -> tuple[str, str]:
    """One human sentence for an OPEN physical-security RFP a rep can act on now.

    Chase (2026-07-18): an open camera/access-control RFP is an active buyer — Grant
    should flag it individually ('… just opened an RFP … anybody want to talk?'). Kept
    honest: it says the RFP is OPEN with its verified deadline, never a posting date we
    did not read. The subject is drawn from the verified title/evidence, not invented.
    """
    if str(row["current_event_verification_status"] or "") != "verified":
        raise ValueError("proactive RFP must be verified")
    if str(row["current_event_type"] or "") != "rfp_posted":
        raise ValueError("proactive RFP has unsupported event type")
    entity = display_entity_name(row["entity_name"])
    if not entity:
        raise ValueError("proactive RFP requires an entity")
    haystack = f"{row['title'] or ''} {row['current_event_evidence_excerpt'] or ''}"
    camera = bool(_RFP_CAMERA_RE.search(haystack))
    access = bool(_RFP_ACCESS_RE.search(haystack))
    if camera and access:
        subject = "security cameras and access control"
    elif access:
        subject = "access control"
    elif camera:
        subject = "security cameras"
    else:
        subject = "physical security"
    due = str(row["funds_end"] or "")[:10]
    due_text = f", responses due {due}" if due else ""
    # Name the solicitation. Chase reported "the same card every morning" on
    # 2026-07-22; the leads were in fact DIFFERENT (verified in production: #9533
    # "…General and HVAC Construction" and #9565 "…Plumbing Construction *REBID*", two
    # trade packages of one SCI Pine Grove project). Because this sentence printed only
    # the agency, the regex-derived subject and the shared deadline, two genuinely
    # distinct RFPs rendered as identical text — indistinguishable from a repeat, and
    # useless to a rep who cannot tell which package they are being asked about.
    project = _short_title(row["title"])
    project_text = f" — {project}" if project else ""
    return (
        f"{entity} has an open RFP for {subject}{due_text}{project_text}. "
        "Anybody want to talk?",
        "rfp-open",
    )


def source_line(row: sqlite3.Row) -> str:
    """A separate, hyperlinked source line for a proactive alert (Chase 2026-07-19:
    hyperlink the label, don't show the raw URL, and leave a blank line before it).

    Every funding claim carries its source. The URL comes ONLY from the stored,
    per-record detail link and is hardened through _safe_url — a missing or unsafe URL
    yields no line rather than a bad one. Rendered as a Slack `<url|label>` link (the
    post uses mrkdwn); the URL never comes from untrusted text, and the label is fixed,
    so nothing injectable reaches the link."""
    try:
        url = record_link(row)
    except (KeyError, IndexError):
        return ""
    if not url:
        return ""
    safe = _safe_url(url)
    if safe == "(URL unavailable)":
        return ""
    return f"\n\n<{safe}|View the source record>"
