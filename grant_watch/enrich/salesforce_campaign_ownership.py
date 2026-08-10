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
