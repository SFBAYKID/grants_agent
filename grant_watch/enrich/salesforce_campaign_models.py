"""Typed immutable models for Salesforce Campaign previews and executions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .salesforce_campaign_gateway import SalesforceRecordRef


class CampaignActionState(str, Enum):
    """Durable states for one externally mutating action."""

    READY = "ready"
    COMMITTING = "committing"
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"
    UNKNOWN = "unknown"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    DRY_RUN = "dry_run"


@dataclass(frozen=True)
class CampaignDraft:
    """Fields Grant may create on a new Campaign after a human preview."""

    name: str
    campaign_type: str = "Other"
    status: str = "Planned"
    is_active: bool = True
    owner_id: str = ""
    owner_label: str = "Salesforce integration user"
    start_date: str = ""
    end_date: str = ""
    description: str = ""

    def payload(self, action_id: str, requester: str) -> dict[str, object]:
        """Return the exact Salesforce create fields shown in the preview."""
        provenance = (
            f"Created by Grant. Action {action_id}. "
            f"Requested by Slack user {requester}."
        )
        description = f"{self.description.strip()}\n{provenance}".strip()
        payload: dict[str, object] = {
            "Name": self.name.strip(),
            "Type": self.campaign_type,
            "Status": self.status,
            "IsActive": self.is_active,
            "Description": description,
        }
        if self.owner_id:
            payload["OwnerId"] = self.owner_id
        if self.start_date:
            payload["StartDate"] = self.start_date
        if self.end_date:
            payload["EndDate"] = self.end_date
        return payload


@dataclass(frozen=True)
class MemberPlan:
    """How one canonical organization will become a Campaign Member."""

    lead_id: int
    canonical_entity_key: str
    entity_name: str
    state: str
    operation: str
    salesforce_ref: SalesforceRecordRef | None = None
    proposed_lead: dict[str, object] | None = None
    note: str = ""


@dataclass(frozen=True)
class PreparedAction:
    """Stored action plus the one-time nonce returned only to its requester."""

    action_id: str
    nonce: str
    preview: str
    expires_at: str


@dataclass(frozen=True)
class ActionExecution:
    """Human-readable aggregate of a confirmed action's exact outcomes."""

    state: CampaignActionState
    message: str
    campaign_id: str = ""
    added: int = 0
    already_present: int = 0
    unresolved: int = 0
    failed: int = 0
    unknown: int = 0
