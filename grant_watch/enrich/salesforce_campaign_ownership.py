"""Requester ownership policy for organization-only Salesforce Leads."""

from __future__ import annotations

import sqlite3

from .. import persequor_client
from .salesforce_campaign_gateway import (
    SalesforceCampaignGateway,
    SalesforceRecordRef,
    validate_record_id,
)


def organization_lead_payload(
    row: sqlite3.Row,
    requester: str,
    action_id: str,
    owner: SalesforceRecordRef,
) -> dict[str, object]:
    """Build an honest organization-only Lead owned by the requesting Salesforce rep."""
    validate_record_id(owner.record_id, "User")
    entity = str(row["entity_name"] or "").strip()
    from .salesforce_contact_records import grant_summary, organization_fields

    payload: dict[str, object] = {
        "Company": entity,
        "LastName": entity,
        "OwnerId": owner.record_id,
        "Status": "New",
        "LeadSource": "Other",
        "Description": (
            f"{grant_summary(row)} "
            "Created by Grant as an organization-only lead — no individual contact "
            "has been verified yet, so the next step is to identify who runs "
            "technology or facilities there. "
            f"Grant lead {row['id']}; action {action_id}; "
            f"requested by Slack user {requester}; "
            f"source {row['detail_url'] or 'not provided'}."
        ),
    }
    # THE ORGANIZATION'S OWN FACTS DO NOT DEPEND ON HAVING FOUND A PERSON. This
    # payload used to carry only the name and the state, so a rep opening one of
    # these Leads saw an empty address, no website, no student count and no
    # industry — and had to go and research an organization Grant had already
    # researched. Everything here is evidenced and omitted when absent.
    payload.update(organization_fields(row))
    return payload


def campaign_lead_payload(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    requester: str,
    action_id: str,
    owner: SalesforceRecordRef,
) -> tuple[dict[str, object], str, str]:
    """Build the best Lead this organization can honestly get, and say which it is.

    Returns (payload, note, person_name) — `person_name` empty for an
    organization-only record.

    WHY THIS IS NOT ALWAYS ORGANIZATION-ONLY. A rep asked on 2026-08-11 to "load
    leads to Salesforce campaign with company name, POC title, name and contact
    information" and then had to ask again whether the preview actually contained
    any of that. It did not: the bulk path only ever built organization-only Leads,
    whose `LastName` is the organization and whose person fields are blank — so a
    campaign built this way could not contain a POC by construction, even for the
    organizations where Grant had already verified one.

    A person Lead is built only from a `verified` contact — a name and role read
    verbatim off the organization's own page. `linkedin_only` is deliberately
    excluded here even though the single-record approval path accepts it: that path
    shows one named person on a card a human reads, while this one creates up to a
    hundred at a time, and an unverified identity written a hundred times is a
    different risk.
    """
    from .. import db
    from .salesforce_contact_records import contact_lead_payload, split_person_name

    from ..enrich.zoominfo_enrichment import DECISION_MAKER_TITLES

    row = db.get_lead(conn, int(row["id"])) or row
    entity = str(row["entity_name"] or "")
    entity_key = db.canonical_entity_key(entity).partition("|")[0]

    def rank(contact: sqlite3.Row) -> tuple[int, int]:
        """Higher sorts better: a title Monarch sells to, then the fresher row.

        `contacts_for_lead` returns oldest id first, so taking the first verified
        row meant a 2019 import beat a contact found this morning. This is the same
        ranking `_best_linkedin_contact` already uses and for the same reason —
        a later row is a later reading of the organization's own page.
        """
        title = str(contact["title"] or "").strip().lower()
        relevant = any(word in title for word in DECISION_MAKER_TITLES)
        return (1 if relevant else 0, int(contact["id"]))

    verified = sorted(
        (
            contact
            for contact in db.contacts_for_lead(conn, int(row["id"]))
            if db.contact_is_page_verified(contact)
        ),
        key=rank,
        reverse=True,
    )
    for contact in verified:
        name = str(contact["name"] or "").strip()
        _first, last = split_person_name(name)
        # A contact row whose "name" is the organization is not a person, and would
        # produce exactly the nameless hybrid this is here to stop producing.
        if not last or db.canonical_entity_key(name).partition("|")[0] == entity_key:
            continue
        payload = contact_lead_payload(row, contact, requester, action_id, owner)
        title = str(payload.get("Title") or "").strip()
        return (
            payload,
            f"Verified contact {name}"
            + (f", {title}" if title else ", role not verified")
            + f"; owner is {owner.name}.",
            name,
        )
    return (
        organization_lead_payload(row, requester, action_id, owner),
        "No individual contact verified; organization name fills Company "
        f"and LastName; owner is {owner.name}.",
        "",
    )


def requester_owner(
    gateway: SalesforceCampaignGateway, requester: str
) -> tuple[SalesforceRecordRef, str]:
    """Resolve one Slack requester to exactly one active Salesforce user by email."""
    requester_email = persequor_client.rep_email_for(requester) or ""
    if not requester_email:
        raise ValueError(
            "The requesting Slack user is not mapped to an approved rep email"
        )
    owners = gateway.find_active_user_by_email(requester_email)
    if not owners:
        raise ValueError(
            f"No active Salesforce user matches requester email {requester_email}"
        )
    if len(owners) != 1:
        raise ValueError(
            f"Multiple active Salesforce users match requester email {requester_email}"
        )
    validate_record_id(owners[0].record_id, "User")
    return owners[0], requester_email
