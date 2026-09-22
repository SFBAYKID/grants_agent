"""The rep-facing reasons a Campaign batch is waiting on a decision.

Split out of `salesforce_campaign_batch` at the 1000-line cap (rule 4). This text is
what Grant relays to a rep before any button exists, so every sentence has to be
true of the specific organizations it counts.

WHY IT COUNTS INSTEAD OF ASSERTING. On 2026-09-22 Grant told Kerry that all 77
unmatched Oregon organizations would be "organization-only" Leads, when 26 of them
had a ZoomInfo contact on file and would be created as named people. It also said a
name clash between Grant's own rows "matches multiple Oregon records" in Salesforce.
Both sentences came from this text, which assumed every new Lead was nameless and
every ambiguity was Salesforce's. Neither has been true since contacts and Grant-row
collisions were handled.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict

from .. import db

# The note `_prepare_campaign_batch` writes when the clash is between GRANT rows.
GRANT_ROW_COLLISION_NOTE = "Multiple Grant rows share only a name/state identity."


def has_named_contact(conn: sqlite3.Connection, lead_id: int) -> bool:
    """Whether a new Lead for this organization would name a person.

    Mirrors `campaign_lead_payload`: a page-verified or ZoomInfo contact makes a
    person Lead. It is a count for the rep, not the payload — the payload is still
    built by that function at confirmation-card time.
    """
    return any(
        db.contact_is_page_verified(contact)
        or str(contact["contact_status"] or "") == "vendor_licensed"
        for contact in db.contacts_for_lead(conn, lead_id)
    )


def blocked_remedies(
    conn: sqlite3.Connection, pending: list[dict[str, object]]
) -> list[str]:
    """Name the exact decision each waiting organization needs, with true counts."""
    counts: dict[str, int] = defaultdict(int)
    missing_with_person = 0
    grant_row_clashes: list[str] = []
    for item in pending:
        state = str(item["resolution_state"])
        if state == "ambiguous" and item.get("note") == GRANT_ROW_COLLISION_NOTE:
            grant_row_clashes.append(str(item["entity_name"]))
            continue
        counts[state] += 1
        if state == "missing" and has_named_contact(
            conn, int(item["representative_lead_id"])
        ):
            missing_with_person += 1
    remedies: list[str] = []
    if counts["missing"]:
        nameless = counts["missing"] - missing_with_person
        remedies.append(
            f"{counts['missing']} have no Salesforce record at all — approve creating "
            f"them as new Leads so I can add them: {missing_with_person} with a named "
            f"contact Grant has on file and {nameless} organization-only, with no "
            "person on them yet"
        )
    if counts["ambiguous"]:
        remedies.append(
            f"{counts['ambiguous']} match more than one Salesforce record — "
            "you can tell me to leave those out, or fix them in Salesforce; "
            "Grant never picks between duplicates"
        )
    if grant_row_clashes:
        remedies.append(
            f"{len(grant_row_clashes)} ({', '.join(sorted(grant_row_clashes))}) have "
            "several grants on file under the same name and Grant cannot prove they "
            "are one organization — this is Grant's own data, not Salesforce; you can "
            "tell me to leave them out"
        )
    if counts["account_only"]:
        remedies.append(
            f"{counts['account_only']} exist only as an Account, which cannot be "
            "a Campaign Member — you can tell me to leave those out, or add a "
            "Lead/Contact in Salesforce"
        )
    return remedies
