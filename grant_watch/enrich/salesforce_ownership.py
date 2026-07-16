"""Resolve and revalidate requester-bound Salesforce ownership for Lead creates.

Ownership is derived only from the trusted Slack-to-rep roster and an exact active
Salesforce User lookup. Model arguments never supply an owner. The frozen owner is
revalidated immediately before a write so routing changes fail closed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .. import persequor_client
from .salesforce_campaign_gateway import (
    SalesforceCampaignGateway,
    validate_record_id,
)


@dataclass(frozen=True)
class RequesterOwner:
    """One exact active Salesforce user resolved from the requesting Slack user."""

    record_id: str
    name: str
    email: str

    def stored(self) -> dict[str, str]:
        """Return immutable owner metadata stored inside the hashed approval payload."""
        return asdict(self)


def resolve_requester_owner(
    gateway: SalesforceCampaignGateway, requester_slack: str
) -> RequesterOwner:
    """Resolve one Slack requester to exactly one active Salesforce User."""
    email = persequor_client.rep_email_for(requester_slack)
    if not email:
        raise ValueError(
            "your Slack user does not map to exactly one valid Salesforce owner"
        )
    matches = gateway.find_active_user_by_email(email)
    if len(matches) != 1:
        raise ValueError(
            "your Slack user does not map to exactly one active Salesforce owner"
        )
    match = matches[0]
    record_id = validate_record_id(match.record_id, "User")
    if not match.name.strip() or match.email.strip().casefold() != email.casefold():
        raise ValueError("Salesforce returned an invalid requester owner")
    return RequesterOwner(record_id, match.name.strip(), email)


def require_frozen_requester_owner(
    gateway: SalesforceCampaignGateway, requester_slack: str, frozen: object
) -> RequesterOwner:
    """Require the current exact requester owner to equal the frozen preview owner."""
    if not isinstance(frozen, dict):
        raise ValueError("Salesforce owner metadata is missing from the approval")
    expected = RequesterOwner(
        str(frozen.get("record_id") or ""),
        str(frozen.get("name") or ""),
        str(frozen.get("email") or "").strip().lower(),
    )
    validate_record_id(expected.record_id, "User")
    if not expected.name or not expected.email:
        raise ValueError("Salesforce owner metadata is invalid")
    current = resolve_requester_owner(gateway, requester_slack)
    if current != expected:
        raise ValueError(
            "the requesting rep's Salesforce ownership changed after preview"
        )
    return current
