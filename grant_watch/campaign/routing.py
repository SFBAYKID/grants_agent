"""Exact, fail-closed rich-card recipient routing.

Priority is a proven recent call owner, exact Account owner, exact open-Opportunity
owner, then verified territory. Salesforce names are never identities: only Owner
email resolved through the approved roster may produce a Slack mention, and the user
must be a current member of the configured channel. Nationwide leads without a safe
owner remain eligible but render explicitly as unassigned territory (Chase A2).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .. import roster, territory


class RoutingReason(str, Enum):
    """Frozen explanation for the selected recipient."""

    SF_CALL_OWNER = "sf_call_owner"
    SF_ACCOUNT_OWNER = "sf_account_owner"
    SF_OPP_OWNER = "sf_opp_owner"
    TERRITORY = "territory"
    UNASSIGNED = "unassigned"


@dataclass(frozen=True)
class OwnerEvidence:
    """Exact Salesforce owner identity for one relationship source."""

    owner_user_id: str = ""
    owner_email: str = ""


@dataclass(frozen=True)
class Route:
    """One safe routing result ready to freeze into a card snapshot."""

    reason: RoutingReason
    slack_user_id: str = ""


def _active_roster_member(owner: OwnerEvidence, members: frozenset[str]) -> str:
    """Resolve an exact Owner email and require configured-channel membership."""
    if not owner.owner_user_id or not owner.owner_email:
        return ""
    slack_id = roster.slack_for_email(owner.owner_email) or ""
    return slack_id if slack_id in members else ""


def resolve(
    *,
    call_owner: OwnerEvidence = OwnerEvidence(),
    account_owner: OwnerEvidence = OwnerEvidence(),
    opportunity_owner: OwnerEvidence = OwnerEvidence(),
    state: str = "",
    state_source: str = "",
    channel_members: frozenset[str],
) -> Route:
    """Apply the reviewed routing priority and return unassigned when none is safe."""
    priorities = (
        (RoutingReason.SF_CALL_OWNER, call_owner),
        (RoutingReason.SF_ACCOUNT_OWNER, account_owner),
        (RoutingReason.SF_OPP_OWNER, opportunity_owner),
    )
    for reason, owner in priorities:
        slack_id = _active_roster_member(owner, channel_members)
        if slack_id:
            return Route(reason, slack_id)
    if territory.state_is_verified(state_source):
        territory_owner = territory.owner_for_state(state) or ""
        if territory_owner in channel_members:
            return Route(RoutingReason.TERRITORY, territory_owner)
    return Route(RoutingReason.UNASSIGNED)
