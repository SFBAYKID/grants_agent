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
    payload: dict[str, object] = {
        "Company": entity,
        "LastName": entity,
        "OwnerId": owner.record_id,
        "Status": "New",
        "LeadSource": "Other",
        "Description": (
            "Created by Grant as an organization-only lead. No individual contact "
            f"has been verified. Grant lead {row['id']}; action {action_id}; "
            f"requested by Slack user {requester}; "
            f"source {row['detail_url'] or 'not provided'}."
        ),
    }
    if row["state"]:
        payload["State"] = str(row["state"])
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
