"""Row-to-human presentation helpers for Grant's lead search results.

Split from search.py to honor the 1000-line module cap. Everything here turns
one lead row into honest display strings: date-window labels that match the
verified event meaning, record-kind/entity-role phrasing, contact suffixes that
never fabricate, and per-record verification links ("the link keeps the data
honest") that pin a URL to THE award being shown whenever the source allows.
"""

from __future__ import annotations

import re
import sqlite3
import urllib.parse


def window_label(row: sqlite3.Row) -> str:
    """Describe stored dates according to the row's verified record meaning."""
    start = row["funds_start"] or "?"
    end = row["funds_end"] or "?"
    event_type = str(row["current_event_type"] or "")
    event_date = str(row["current_event_occurred_on"] or "")
    if event_type in {"award_announced", "award_obligated"}:
        prefix = f"award event {event_date}; " if event_date else ""
        return f"{prefix}spend window {start} through {end}"
    if event_type == "application_window_opened":
        return f"applications open {start}; close {end}"
    if event_type == "rfp_posted":
        return f"posted {event_date or start}; response due {end}"
    if row["lead_grade"] == "gold":
        return f"spend window {start} through {end}"
    if row["source"] == "grants.gov":
        return f"applications open {start}; close {end}"
    if row["lead_grade"] == "silver":
        return f"posted {start}; response due {end}"
    return f"recorded window {start} through {end}"


# California's portal stores one dataset URL for every award row; the datastore
# API can address the single record by its PortalID, which is what a rep needs
# to verify one specific dollar amount. Verified live 2026-07-18 (PortalID 73146
# returns exactly one record with TotalAwardAmount $2,548,407).
_CA_RESOURCE_RE = re.compile(
    r"data\.ca\.gov/dataset/[0-9a-f\-]+/resource/([0-9a-f\-]{36})"
)


def record_link(row: sqlite3.Row) -> str:
    """Best per-record verification URL — deep-linked when the source allows.

    USAspending/Grants.gov/SAM rows already store per-record pages and pass
    through unchanged. CA portal rows get a datastore query pinned to their
    PortalID so the link proves THAT award, not just the dataset."""
    url = str(row["detail_url"] or "")
    item_id = str(row["source_item_id"] or "")
    match = _CA_RESOURCE_RE.search(url)
    if match and item_id and str(row["source"] or "").startswith("ca-grants"):
        filters = urllib.parse.quote(f'{{"PortalID":"{item_id}"}}')
        return (
            "https://data.ca.gov/api/3/action/datastore_search"
            f"?resource_id={match.group(1)}&filters={filters}"
        )
    return url


def entity_role_for_row(row: sqlite3.Row) -> str:
    """Distinguish a funding/posting agency from an actual award recipient."""
    event_type = str(row["current_event_type"] or "")
    if event_type == "application_window_opened":
        return "funding agency"
    if event_type == "rfp_posted":
        return "posting organization"
    if event_type in {"award_announced", "award_obligated"}:
        return "award recipient"
    return "organization"


def contact_suffix(cell: list[object]) -> str:
    """Render one enriched contact cell [name, title, email, status] as a short inline
    suffix for the summary — honest about not_found / unreachable, never fabricated."""
    name, title, email, status = (list(cell) + ["", "", "", ""])[:4]
    if status == "verified":
        who = f"{name} ({title})".strip()
        return f" · contact: {who} {email}".rstrip()
    if status == "not_found":
        return " · contact: none found"
    if status == "unreachable":
        return " · contact: source unreachable — retry"
    if status == "error":
        return " · contact: lookup error"
    if status:
        return f" · contact: {status}"
    return ""
