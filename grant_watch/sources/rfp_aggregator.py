"""Aggregator RFP source — OPEN physical-security solicitations from a bid aggregator.

Why (Chase, 2026-07-18/19): individual city/school RFP pages are mostly already closed
and many don't scrape; the OPEN opportunities are aggregated on sites like Starbridge,
whose physical-security listing scrapes cleanly — each row carries the buyer, a posted
("Release") date, a due ("Close") date, a status, and a detail link. So instead of
hunting dead `.gov` pages (see sources/rfp.py, which finds ~0 open), read the aggregator
listing directly. Cherry-picked to WA/OR/CA/PA/TX for now (backfill by widening states);
all rows land as normal RFP leads so everything stays queryable/exportable.

Honesty: fields are copied verbatim from the scraped listing, and the drip alert links
to the Starbridge detail page so the rep confirms the actual RFP. The state cherry-pick
only accepts a row whose text explicitly names a target state — a row with no clear
state is skipped rather than mis-filed (no guessed state). All trust-bearing parsing is
pure (parse_starbridge) and fixture-tested; poll() is the only live I/O.
"""

from __future__ import annotations

import re
from datetime import date

from ..models import (
    DatePrecision,
    FundingEventType,
    RawItem,
    VerificationStatus,
)
from ..enrich.finder import SourceUnreachable, _scrape
from . import rfp_parse

STARBRIDGE_URL = "https://starbridge.ai/catalog/rfp/physical-security"
TARGET_STATES: tuple[str, ...] = ("WA", "OR", "CA", "PA", "TX")

# Full state name -> USPS code. The row names the state in prose ("City of Joliet,
# Illinois"); a bare 2-letter code is too ambiguous in free text to trust.
_STATE_NAMES = {
    "washington": "WA",
    "oregon": "OR",
    "california": "CA",
    "pennsylvania": "PA",
    "texas": "TX",
}
_ROW_TITLE_RE = re.compile(r"\[\*\*(.+?)\*\*\]\((https://starbridge\.ai/rfp/[^)]+)\)")
_BUYER_RE = re.compile(r"\[([^\]]+)\]\(https://starbridge\.ai/buyer/[^)]+\)")
_RELEASE_RE = re.compile(r"Release:\s*([A-Za-z0-9,/\s]+?)(?:\n|$)")
_CLOSE_RE = re.compile(r"Close:\s*([A-Za-z0-9,/\s]+?)(?:\n|$)")
# "Washington" is ambiguous — never accept it as the state when the row is about DC.
_DC_RE = re.compile(r"\bd\.?c\.?\b|district of columbia", re.IGNORECASE)


def _row_state(block: str) -> str:
    """A target state's USPS code ONLY when the row's prose names EXACTLY ONE target
    state, else '' (drop the row).

    Conservative on purpose: returning the first dict-order match let a block that names
    two target states — e.g. a Pennsylvania buyer in "Washington County, Pennsylvania" —
    mis-file as WA simply because 'washington' is checked first. A guessed state is worse
    than no state (module docstring; Constitution rule 1), so any ambiguity (zero or more
    than one distinct target state named) drops the row rather than picking one."""
    found: set[str] = set()
    for name, code in _STATE_NAMES.items():
        if re.search(rf"\b{name}\b", block, re.IGNORECASE):
            if code == "WA" and _DC_RE.search(block):
                continue  # 'Washington' here reads as Washington, D.C. — not the state
            found.add(code)
    return next(iter(found)) if len(found) == 1 else ""


def parse_starbridge(
    page_text: str, today: date, states: tuple[str, ...] = TARGET_STATES
) -> list[RawItem]:
    """Pure parser: aggregator listing -> open, target-state, physical-security RFPs.

    Each row is dropped unless it is (a) in a cherry-picked state named in its text,
    (b) still OPEN (a future Close date, status not 'Unavailable'), and (c) actually
    physical security. Fields are taken verbatim from the row; a Release (posting) date,
    when present, drives GOLD (fresh) vs SILVER in scoring — otherwise SILVER.
    """
    out: list[RawItem] = []
    seen_ids: set[str] = set()
    matches = list(_ROW_TITLE_RE.finditer(page_text))
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(page_text)
        block = page_text[start:end]
        title = match.group(1).strip()
        detail_url = match.group(2).strip()

        state = _row_state(block)
        if state not in states:
            continue  # cherry-pick: only a clearly-target-state row (backfill later)
        # status: the word right after the title link. Only a status word IN THAT
        # POSITION is trusted as closed — checking the prose would false-positive on
        # "closed-circuit" cameras. Starbridge uses "Unavailable"; the others are a
        # cheap safety net if it ever surfaces a future-dated closed/awarded listing.
        after_title = page_text[match.end() : match.end() + 24]
        if re.match(
            r"\s*(?:Unavailable|Closed|Awarded|Cancelled|Canceled|Withdrawn)",
            after_title,
            re.IGNORECASE,
        ):
            continue

        close_match = _CLOSE_RE.search(block)
        close_raw = close_match.group(1) if close_match else ""
        due_iso = rfp_parse.parse_iso_date(close_raw)
        if not due_iso or date.fromisoformat(due_iso) < today:
            continue  # no verifiable future deadline -> not a live, datable open RFP

        buyer_match = _BUYER_RE.search(block)
        entity = (buyer_match.group(1).strip() if buyer_match else "").strip()
        if not entity:
            continue

        description = block
        if not rfp_parse.is_relevant(f"{title} {description}"):
            continue

        release_raw = _RELEASE_RE.search(block)
        posted_iso = (
            rfp_parse.parse_iso_date(release_raw.group(1)) if release_raw else None
        )
        # Content-based id (entity + title + due) so the same RFP listed twice with
        # different slugs/case collapses to one lead.
        item_id = rfp_parse.rfp_item_id(entity, "", title, due_iso, detail_url)
        if item_id in seen_ids:
            continue
        seen_ids.add(item_id)
        out.append(
            RawItem(
                source="rfp",
                item_id=item_id,
                title=title[:200],
                entity=entity,
                state=state,
                program="RFP:security",
                amount=None,  # a solicitation has no awarded dollars
                start=posted_iso or "",
                end=due_iso,  # the Close/submission deadline; SILVER/GOLD when >= today
                url=detail_url,
                raw={
                    "buyer": entity,
                    "close_printed": close_raw.strip(),
                    "release_printed": (release_raw.group(1).strip() if release_raw else ""),
                    "aggregator": "starbridge",
                },
                event_type=FundingEventType.RFP_POSTED,
                event_date=posted_iso or "",  # posting date -> GOLD when recent
                date_precision=DatePrecision.DAY,
                application_portal="Starbridge",
                source_locator=item_id,
                evidence_excerpt=(
                    f"{title} — closes {due_iso} — via Starbridge listing "
                    f"({entity}, {state})"
                )[:300],
                verification_status=VerificationStatus.VERIFIED,
            )
        )
    return out


def poll() -> list[RawItem]:
    """Scrape the aggregator listing and parse open target-state physical-security RFPs.

    Raises SourceUnreachable when the listing could not be read (Constitution rule 1:
    'could not look' is never recorded as 'no open RFPs')."""
    page_text = _scrape(STARBRIDGE_URL)
    if len(page_text) < 300:
        raise SourceUnreachable("aggregator listing could not be read")
    return parse_starbridge(page_text, date.today())
