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
from typing import Mapping

from . import db
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


@dataclass(frozen=True)
class LeadFillPlan:
    """Fields and compliance state derived from one exact selected contact."""

    fields: Mapping[str, object]
    do_not_call: bool


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


def build_fill_plan(conn: sqlite3.Connection, lead_id: int) -> LeadFillPlan:
    """Build one typed CRM plan from the organization and one selected contact.

    Organization facts come first because they need no contact to have been found —
    that asymmetry is what left the org-only records so thin. The person's details
    are added only when Grant actually has a contact, and the CONTACT'S OWN number
    is used rather than the organization switchboard, which would read on the record
    as their direct line.
    """
    lead = db.get_lead(conn, lead_id)
    if lead is None:
        return LeadFillPlan({}, False)
    fields: dict[str, object] = dict(organization_fields(lead))
    # Gated on the profile's own verdict for the same reason `organization_fields`
    # is: a `not_found` lookup leaves these columns holding whatever the search
    # landed on, and the fill path can only write into an EMPTY field — so a wrong
    # value here seals that field against every later correction.
    try:
        org_phone = str(lead["evidenced_org_phone"] or "")
    except (IndexError, KeyError):
        org_phone = ""
    if org_phone:
        fields["Phone"] = org_phone

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
        if str(row["contact_status"]) == "vendor_licensed"
        or db.contact_is_page_verified(row)
    ]
    verified = [row for row in usable if db.contact_is_page_verified(row)]
    best = verified[0] if verified else (usable[0] if usable else None)
    do_not_call = False
    if best is not None:
        try:
            do_not_call = bool(int(best["do_not_call"] or 0))
        except (IndexError, KeyError, TypeError, ValueError):
            do_not_call = False
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
    return LeadFillPlan(fields, do_not_call)


def proposed_fields(conn: sqlite3.Connection, lead_id: int) -> dict[str, object]:
    """Compatibility view of the ordinary fields in :func:`build_fill_plan`."""
    return dict(build_fill_plan(conn, lead_id).fields)


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
            plan = build_fill_plan(conn, int(row["lead_id"]))
            print(f"  lead #{row['lead_id']} -> {row['salesforce_id']}")
            if plan.do_not_call:
                print("      Description: prepend fixed DO NOT CALL marker")
            if not plan.fields and not plan.do_not_call:
                print("      (nothing to offer)")
            # PRINT THE VALUES, NOT JUST THE FIELD NAMES. A preview that lists
            # "Website" tells an operator nothing about whether that Website is the
            # district's or the state education department's — and on production it
            # was cde.ca.gov for two leads. The names looked perfect while the
            # payload was wrong, so the review step could not do its job.
            for name, value in sorted(plan.fields.items()):
                print(f"      {name}: {value}")
        return FillOutcome(len(rows), 0, 0, 0)

    client = gateway or SalesforceCampaignGateway()
    filled = complete = failed = elsewhere = 0
    for row in rows:
        plan = build_fill_plan(conn, int(row["lead_id"]))
        if not plan.fields and not plan.do_not_call:
            complete += 1
            continue
        marker_changed = False
        if plan.do_not_call:
            try:
                marker = client.mark_lead_do_not_call(str(row["salesforce_id"]))
            except Exception as exc:  # noqa: BLE001 — never fill after ambiguous DNC
                failed += 1
                print(f"  #{row['lead_id']}: do-not-call {type(exc).__name__}")
                continue
            if marker.error == "not in this org":
                elsewhere += 1
                print(f"  #{row['lead_id']}: skipped, {marker.error}")
                continue
            if not marker.success:
                failed += 1
                print(f"  #{row['lead_id']}: {marker.error}")
                continue
            marker_changed = marker.error == "marked do-not-call"
        if not plan.fields:
            if marker_changed:
                filled += 1
            else:
                complete += 1
            continue
        try:
            result = client.fill_lead_blanks(
                str(row["salesforce_id"]), dict(plan.fields)
            )
        except Exception as exc:  # noqa: BLE001 — one bad record cannot end the sweep
            failed += 1
            print(f"  #{row['lead_id']}: {type(exc).__name__}")
            continue
        if result.error == "not in this org":
            # NOT A FAILURE. `crm_action_items.salesforce_id` holds ids from both the
            # production org and the monarchdev sandbox, so a sweep legitimately meets
            # records that do not exist here and correctly skips them. Counting those
            # as errors made `fill-leads` exit 1 on a run where every single real
            # record was written perfectly — harmless today, and exactly the kind of
            # thing that reads as a broken job the moment this goes in cron.
            elsewhere += 1
            print(f"  #{row['lead_id']}: skipped, {result.error}")
        elif not result.success:
            failed += 1
            print(f"  #{row['lead_id']}: {result.error}")
        elif result.error and result.error.startswith("filled"):
            filled += 1
            print(f"  #{row['lead_id']} {row['salesforce_id']}: {result.error}")
        elif marker_changed:
            filled += 1
        else:
            complete += 1
    if elsewhere:
        print(f"  ({elsewhere} skipped as belonging to another Salesforce org)")
    return FillOutcome(len(rows), filled, complete, failed)
