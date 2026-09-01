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

from .. import lead_claims, roster
from ..record_semantics import semantics_for


def window_label(row: sqlite3.Row) -> str:
    """Describe stored dates according to the row's verified record meaning."""
    start = row["funds_start"] or "?"
    end = row["funds_end"] or "?"
    # Meaning comes from the EVENT, never the grade. The grade fallbacks that used to
    # live here rendered a silver AWARD as "posted …; response due …" — a false claim in
    # every export's date_context column and every Slack search line.
    return semantics_for(row).date_context(
        start, end, str(row["current_event_occurred_on"] or "")
    )


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
    return semantics_for(row).entity_role


def contact_suffix(cell: list[object]) -> str:
    """Render one enriched contact cell as a short inline suffix for the summary —
    honest about not_found / unreachable, never fabricated.

    The cell is [name, title, email, status, phone, org_phone]. The two phone fields
    stay separate all the way to the rendered line: `phone` is the named person's own
    verified number, `org_phone` is the organization's switchboard, and the wording
    below never lets the second be read as the first."""
    name, title, email, status, phone, org_phone = (list(cell) + [""] * 6)[:6]
    direct = f" · direct {phone}" if phone else ""
    main_line = f" · main line {org_phone}" if org_phone else ""
    if status == "verified":
        who = f"{name} ({title})".strip()
        # AN ORGANIZATION MAILBOX BESIDE A PERSON'S NAME READS AS THEIRS. Grant
        # printed "Rabbi Yossi Gross, Executive Director — office@ygla.org" in
        # production on 2026-08-11: true that the page carried both, false that the
        # address is his. `choose_email` was fixed for the Salesforce write path the
        # same day; this renderer had the identical gap, because the label depended
        # on which column held the address rather than on the address itself.
        from ..enrich.salesforce_contact_fields import email_is_general

        shown = (
            f"{email} (organization mailbox, not their own)"
            if email and email_is_general(str(email))
            else email
        )
        return f" · contact: {who} {shown}{direct}{main_line}".rstrip()
    if status == "linkedin_org_email":
        who = f"{name} ({title})".strip() if title else str(name)
        return f" · contact: {who} via LinkedIn; org mailbox {email}{main_line}"
    if status == "linkedin_only":
        who = f"{name} ({title})".strip() if title else str(name)
        return f" · contact: {who} via LinkedIn (no email verified){main_line}"
    if status == "org_email":
        return (
            f" · contact: general mailbox {email} (no named person verified){main_line}"
        )
    if status == "not_found":
        return " · contact: none found (site, LinkedIn, and org mailbox all checked)"
    if status == "unreachable":
        return " · contact: source unreachable — retry"
    if status == "error":
        return " · contact: lookup error"
    if status == "needs_operator_retry":
        # NOT "none found". Nothing was checked: an earlier paid attempt cannot be
        # proven spent or unspent, so re-spending is refused until a human clears it.
        return " · contact: not checked — an earlier lookup needs clearing by hand"
    if status:
        # A status with no branch above is an internal slug, and this string is read
        # by a rep. Say what is true — that there is nothing to show — rather than
        # leaking the identifier, which this project bans in replies.
        return " · contact: no result"
    return ""


def claimed_phrases(
    connection: sqlite3.Connection, lead_ids: list[int]
) -> dict[int, str]:
    """Who holds each of these leads, rendered for a human — "" when nobody does.

    THIS IS THE SURFACE WHERE A SECOND REP FINDS OUT A LEAD IS TAKEN, which is why
    the lookup and the rendering live together: a caller that fetched the claim and
    then formatted it itself is a caller that can leak the wrong thing.

    NEVER A RAW SLACK ID. An id is meaningless in a spreadsheet a rep forwards on,
    and in Slack's link form it notifies somebody who is not part of the exchange.
    A claimant the reviewed roster cannot name renders as the DATE alone — Grant
    knows the lead was taken and honestly cannot say by whom, which is a different
    fact from nobody having taken it.

    DEGRADES TO SILENCE ON A MISSING TABLE, DELIBERATELY. `search_leads` opens its
    connection `mode=ro`, so it never applies migrations and the table is simply
    absent on a database predating migration 48. An unmarked row is exactly how
    search behaved before this feature; letting the error out would instead turn
    every search into "ERROR: search failed", which is a far worse answer to a
    question that had nothing to do with claims.
    """
    if not lead_ids:
        return {}
    try:
        held = lead_claims.live_claims(connection, lead_ids)
    except sqlite3.Error:
        return {}
    rendered: dict[int, str] = {}
    for lead_id, claim in held.items():
        who = roster.display_name_for_slack(claim.slack_user)
        stamp = str(claim.claimed_at)[:10]
        rendered[lead_id] = f"{who} ({stamp})" if who else f"claimed {stamp}"
    return rendered
