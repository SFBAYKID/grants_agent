"""Pure routing tests for exact CRM priority, membership, and nationwide fallback."""

from __future__ import annotations

from grant_watch.campaign.routing import OwnerEvidence, RoutingReason, resolve

MEMBERS = frozenset({"U01DFJWQQJ3", "U01E908206M", "U08C1NBH875"})
ANTHONY = OwnerEvidence("005ACCOUNT", "anthony@monarchconnected.com")
KERRY = OwnerEvidence("005CALL", "kerry@monarchconnected.com")
BRETT = OwnerEvidence("005OPP", "brett@monarchconnected.com")


def test_recent_call_owner_wins_relationship_and_territory() -> None:
    """An exact rostered call owner outranks Account, Opportunity, and territory."""
    route = resolve(
        call_owner=KERRY,
        account_owner=ANTHONY,
        opportunity_owner=BRETT,
        state="CA",
        state_source="usaspending:16.071",
        channel_members=MEMBERS,
    )
    assert route.reason is RoutingReason.SF_CALL_OWNER
    assert route.slack_user_id == "U01E908206M"


def test_account_owner_wins_open_opportunity_and_territory() -> None:
    """Exact Account ownership wins when there is no qualifying call."""
    route = resolve(
        account_owner=ANTHONY,
        opportunity_owner=BRETT,
        state="PA",
        state_source="usaspending:16.071",
        channel_members=MEMBERS,
    )
    assert route.reason is RoutingReason.SF_ACCOUNT_OWNER
    assert route.slack_user_id == "U01DFJWQQJ3"


def test_opportunity_owner_wins_territory() -> None:
    """An exact open-Opportunity owner is the third relationship priority."""
    route = resolve(
        opportunity_owner=BRETT,
        state="TX",
        state_source="usaspending:16.071",
        channel_members=MEMBERS,
    )
    assert route.reason is RoutingReason.SF_OPP_OWNER
    assert route.slack_user_id == "U08C1NBH875"


def test_unmapped_crm_owner_falls_back_to_verified_territory() -> None:
    """Unknown Salesforce email cannot tag; a verified territory may safely route."""
    route = resolve(
        account_owner=OwnerEvidence("005X", "stranger@example.com"),
        state="WA",
        state_source="usaspending:16.071",
        channel_members=MEMBERS,
    )
    assert route.reason is RoutingReason.TERRITORY
    assert route.slack_user_id == "U01E908206M"


def test_owner_not_in_channel_falls_through_safely() -> None:
    """Even an exact roster owner cannot be mentioned outside channel membership."""
    route = resolve(
        call_owner=KERRY,
        account_owner=ANTHONY,
        state="PA",
        state_source="usaspending:16.071",
        channel_members=frozenset({"U08C1NBH875"}),
    )
    assert route.reason is RoutingReason.TERRITORY
    assert route.slack_user_id == "U08C1NBH875"


def test_nationwide_unmapped_state_is_explicitly_unassigned() -> None:
    """Nationwide commercial eligibility does not manufacture an owner."""
    route = resolve(
        state="AZ",
        state_source="usaspending:16.071",
        channel_members=MEMBERS,
    )
    assert route.reason is RoutingReason.UNASSIGNED
    assert route.slack_user_id == ""


def test_inferred_state_cannot_route_by_territory() -> None:
    """A state inferred from prose never tags even when its code is mapped."""
    route = resolve(state="CA", state_source="rfp", channel_members=MEMBERS)
    assert route.reason is RoutingReason.UNASSIGNED
