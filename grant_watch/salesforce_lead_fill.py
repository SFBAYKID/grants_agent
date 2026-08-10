"""Fill in the Salesforce Leads Grant put on a campaign but could not complete.

WHY THIS IS NEEDED AND WHY IT IS AWKWARD. Chase created a campaign, 13 of 14 leads
matched records that ALREADY existed in Salesforce, and he opened one to find no
title, no mobile and no notes — one had been imported in 2019 and never touched
since. Grant had researched those organizations and had nowhere to put what it knew,
because it is create-only by design.

The create-only guarantee is worth keeping: it is why "delete that campaign" is
structurally impossible rather than merely refused. So this does not add a general
update path. It adds exactly one operation — fill a field that is EMPTY — and the
gateway enforces that by READING the record first (see `fill_lead_blanks`). Grant can
add what it knows and can never remove or contradict what a human put there.

WHAT IT SENDS is only what Grant actually holds: the organization's own address,
website and phone from its published site, the student count from NCES, and the best
contact's title, email and mobile. Anything Grant does not have is simply absent.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .enrich.salesforce_campaign_gateway import SalesforceCampaignGateway
from .enrich.salesforce_contact_records import organization_fields


@dataclass(frozen=True)
class FillOutcome:
    """What one sweep actually changed — counted, never estimated."""

    considered: int
    filled: int
    already_complete: int
    failed: int

    def summary(self) -> str:
        """One honest line for the operator."""
        return (
            f"considered {self.considered}, filled {self.filled}, "
            f"already complete {self.already_complete}, errored {self.failed}"
        )


def linked_leads(conn: sqlite3.Connection, limit: int = 25) -> list[sqlite3.Row]:
    """Grant leads that are known to correspond to a real Salesforce Lead.

    `crm_action_items.salesforce_id` is written when a campaign action verifies, so
    it is the only place Grant honestly knows which CRM record a lead became. Only
    `Lead` ids are eligible — a Contact is a different object with different fields
    and is deliberately out of scope.

    ONE ROW PER GRANT LEAD. `DISTINCT` on the PAIR is not the same thing: lead #231
    maps to two Salesforce records, so it appeared twice and the identical values
    would have been written to both — and `--limit 25` would have bounded rows rather
    than leads. Where a lead has several CRM records, the lowest id is taken and the
    rest are left alone; writing the same organization's details into two records is
    a merge decision, and a merge is a human's call.
    """
    return list(
        conn.execute(
            """SELECT i.lead_id, MIN(i.salesforce_id) AS salesforce_id
                 FROM crm_action_items i
                WHERE i.salesforce_id IS NOT NULL
                  AND i.salesforce_id <> ''
                  AND i.salesforce_id LIKE '00Q%'
                  AND i.lead_id IS NOT NULL
                GROUP BY i.lead_id
                ORDER BY i.lead_id
                LIMIT ?""",
            (max(1, limit),),
        )
    )


def proposed_fields(conn: sqlite3.Connection, lead_id: int) -> dict[str, object]:
    """Everything Grant knows about this lead that a Salesforce Lead has a home for.

    Organization facts come first because they need no contact to have been found —
    that asymmetry is what left the org-only records so thin. The person's details
    are added only when Grant actually has a contact, and the CONTACT'S OWN number
    is used rather than the organization switchboard, which would read on the record
    as their direct line.
    """
    from . import db

    lead = db.get_lead(conn, lead_id)
    if lead is None:
        return {}
    fields: dict[str, object] = dict(organization_fields(lead))
    # Gated on the profile's own verdict for the same reason `organization_fields`
    # is: a `not_found` lookup leaves these columns holding whatever the search
    # landed on, and the fill path can only write into an EMPTY field — so a wrong
    # value here seals that field against every later correction.
    if str(lead["org_profile_status"] or "") == "found" and str(
        lead["org_phone"] or ""
    ):
        fields["Phone"] = str(lead["org_phone"])

    # A LINKEDIN CLAIM MUST NOT BECOME A SALESFORCE FIELD. `linkedin_only` means
    # exactly "we found a profile and ownership is unproven" — this repo says so
    # everywhere it renders one. A Salesforce `Title` carries no provenance, so
    # writing an unverified title there launders a guess into a CRM fact that outlives
    # every thread and that nobody downstream can tell apart from a checked one.
    # Grant still surfaces LinkedIn findings in Slack, where they are labelled.
    contacts = db.contacts_for_lead(conn, lead_id)
    usable = [
        row
        for row in contacts
        if str(row["contact_status"]) in {"verified", "vendor_licensed"}
    ]
    verified = [row for row in usable if str(row["contact_status"]) == "verified"]
    best = verified[0] if verified else (usable[0] if usable else None)
    if best is not None:
        for column, field in (
            ("title", "Title"),
            ("email", "Email"),
            ("mobile_phone", "MobilePhone"),
        ):
            try:
                value = str(best[column] or "").strip()
            except (IndexError, KeyError):
                value = ""
            if value:
                fields[field] = value
    return fields


def run(
    conn: sqlite3.Connection,
    gateway: SalesforceCampaignGateway | None = None,
    *,
    limit: int = 25,
    dry_run: bool = True,
) -> FillOutcome:
    """Fill blanks on every Salesforce Lead Grant can honestly map to one of its own."""
    rows = linked_leads(conn, limit)
    if dry_run:
        for row in rows:
            fields = proposed_fields(conn, int(row["lead_id"]))
            print(f"  lead #{row['lead_id']} -> {row['salesforce_id']}")
            if not fields:
                print("      (nothing to offer)")
            # PRINT THE VALUES, NOT JUST THE FIELD NAMES. A preview that lists
            # "Website" tells an operator nothing about whether that Website is the
            # district's or the state education department's — and on production it
            # was cde.ca.gov for two leads. The names looked perfect while the
            # payload was wrong, so the review step could not do its job.
            for name, value in sorted(fields.items()):
                print(f"      {name}: {value}")
        return FillOutcome(len(rows), 0, 0, 0)

    client = gateway or SalesforceCampaignGateway()
    filled = complete = failed = 0
    for row in rows:
        fields = proposed_fields(conn, int(row["lead_id"]))
        if not fields:
            complete += 1
            continue
        try:
            result = client.fill_lead_blanks(str(row["salesforce_id"]), fields)
        except Exception as exc:  # noqa: BLE001 — one bad record cannot end the sweep
            failed += 1
            print(f"  #{row['lead_id']}: {type(exc).__name__}")
            continue
        if not result.success:
            failed += 1
            print(f"  #{row['lead_id']}: {result.error}")
        elif result.error and result.error.startswith("filled"):
            filled += 1
            print(f"  #{row['lead_id']} {row['salesforce_id']}: {result.error}")
        else:
            complete += 1
    return FillOutcome(len(rows), filled, complete, failed)
