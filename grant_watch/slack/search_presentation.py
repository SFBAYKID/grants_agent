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


def grade_phrases(
    record_value: str, rows: list[sqlite3.Row] | None = None
) -> dict[str, str]:
    """Grade-tier wording that stays TRUE for the record kind actually shown.

    The old fixed wording called every gold "award won, money to spend" and every silver
    "open solicitation" — wrong when the results are RFPs (a gold RFP is a fresh posting,
    not an award; a past-due silver RFP is not open). Use the explicit record_kind filter
    when given; otherwise infer it from the shown rows' event types so a plain award
    search keeps the helpful "money to spend" phrasing while an all-RFP set never claims
    an award or unverified openness. Mixed/unknown stays generic. The literals mirror
    RecordKind.*.value (kept here to avoid a search.py import cycle)."""
    kind = record_value
    if not kind and rows:
        kinds = {str(r["current_event_type"] or "") for r in rows}
        if kinds and kinds <= {"award_announced", "award_obligated"}:
            kind = "award"
        elif kinds == {"rfp_posted"}:
            kind = "solicitation"
        elif kinds == {"application_window_opened"}:
            kind = "funding_opportunity"
    if kind == "award":
        return {
            "gold": "gold (award won, money to spend)",
            "silver": "silver (funding in progress)",
            "watch": "watch (worth monitoring)",
        }
    if kind == "solicitation":
        return {
            "gold": "gold (recently posted RFP)",
            "silver": "silver (RFP posted earlier — check the due date)",
            "watch": "watch (check the due date)",
        }
    if kind == "funding_opportunity":
        return {
            "gold": "gold (top-fit opportunity)",
            "silver": "silver (open opportunity)",
            "watch": "watch (worth monitoring)",
        }
    return {
        "gold": "gold (freshest, top priority)",
        "silver": "silver (solid lead)",
        "watch": "watch (worth monitoring)",
    }


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
    if status == "linkedin_org_email":
        who = f"{name} ({title})".strip() if title else str(name)
        return f" · contact: {who} via LinkedIn; org mailbox {email}"
    if status == "linkedin_only":
        who = f"{name} ({title})".strip() if title else str(name)
        return f" · contact: {who} via LinkedIn (no email verified)"
    if status == "org_email":
        return f" · contact: general mailbox {email} (no named person verified)"
    if status == "not_found":
        return " · contact: none found (site, LinkedIn, and org mailbox all checked)"
    if status == "unreachable":
        return " · contact: source unreachable — retry"
    if status == "error":
        return " · contact: lookup error"
    if status:
        return f" · contact: {status}"
    return ""
