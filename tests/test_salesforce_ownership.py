"""Trusted Slack requester to Salesforce Lead-owner resolution tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from grant_watch import persequor_client
from grant_watch.enrich import salesforce_campaign_gateway as gateway_mod
from grant_watch.enrich import salesforce_ownership as ownership

OWNER_ID = "005000000000001"
OTHER_OWNER_ID = "005000000000002"


class OwnerGateway:
    """Return configured active Salesforce users for one normalized email."""

    def __init__(self, users: list[gateway_mod.SalesforceRecordRef]) -> None:
        self.users = users
        self.lookups: list[str] = []

    def find_active_user_by_email(
        self, email: str
    ) -> list[gateway_mod.SalesforceRecordRef]:
        """Record the exact trusted email and return configured active users."""
        self.lookups.append(email)
        return self.users


def _user(
    record_id: str = OWNER_ID, email: str = "chase@example.test"
) -> gateway_mod.SalesforceRecordRef:
    """Return one valid active Salesforce User result."""
    return gateway_mod.SalesforceRecordRef(
        "User", record_id, "Chase Test", "https://salesforce.test/user", email=email
    )


def _roster(
    tmp_path: Path, reps: list[dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Install one isolated trusted Slack roster for a test."""
    path = tmp_path / "reps.json"
    path.write_text(json.dumps({"reps": reps}))
    monkeypatch.setattr(persequor_client, "REPS_PATH", path)


def test_exact_roster_user_resolves_one_active_salesforce_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the normalized trusted roster email reaches Salesforce."""
    _roster(
        tmp_path, [{"slack_id": "UCHASE", "email": " Chase@Example.Test "}], monkeypatch
    )
    gateway = OwnerGateway([_user()])
    result = ownership.resolve_requester_owner(gateway, "UCHASE")
    assert result == ownership.RequesterOwner(
        OWNER_ID, "Chase Test", "chase@example.test"
    )
    assert gateway.lookups == ["chase@example.test"]


@pytest.mark.parametrize(
    "reps",
    [
        [],
        [{"slack_id": "UCHASE", "email": "not-an-email"}],
        [
            {"slack_id": "UCHASE", "email": "chase@example.test"},
            {"slack_id": "UCHASE", "email": "other@example.test"},
        ],
    ],
)
def test_missing_malformed_or_duplicate_roster_fails_before_salesforce(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reps: list[dict[str, str]]
) -> None:
    """Untrusted or ambiguous roster state cannot prepare a Lead owner."""
    _roster(tmp_path, reps, monkeypatch)
    gateway = OwnerGateway([_user()])
    with pytest.raises(ValueError, match="exactly one valid"):
        ownership.resolve_requester_owner(gateway, "UCHASE")
    assert gateway.lookups == []


def test_missing_or_ambiguous_active_user_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Salesforce must return exactly one active user for the trusted email."""
    _roster(
        tmp_path, [{"slack_id": "UCHASE", "email": "chase@example.test"}], monkeypatch
    )
    for users in ([], [_user(), _user(OTHER_OWNER_ID)]):
        with pytest.raises(ValueError, match="exactly one active"):
            ownership.resolve_requester_owner(OwnerGateway(users), "UCHASE")


def test_confirmation_revalidation_rejects_changed_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A changed active Salesforce user cannot replace the frozen preview owner."""
    _roster(
        tmp_path, [{"slack_id": "UCHASE", "email": "chase@example.test"}], monkeypatch
    )
    frozen = ownership.RequesterOwner(
        OWNER_ID, "Chase Test", "chase@example.test"
    ).stored()
    changed = OwnerGateway([_user(OTHER_OWNER_ID)])
    with pytest.raises(ValueError, match="changed after preview"):
        ownership.require_frozen_requester_owner(changed, "UCHASE", frozen)


@pytest.mark.parametrize("owner_id", ["", "001000000000001", "not-a-user-id"])
def test_single_lead_gateway_rejects_invalid_owner_before_http(
    monkeypatch: pytest.MonkeyPatch, owner_id: str
) -> None:
    """The final all-or-none Lead boundary requires one valid User OwnerId."""
    gateway = gateway_mod.SalesforceCampaignGateway()
    posted: list[object] = []
    monkeypatch.setattr(
        gateway,
        "_post_fixed_composite",
        lambda *_args: posted.append(_args) or ({}, "unexpected"),
    )
    payload = {
        "Company": "Alpha School District",
        "LastName": "Alpha School District",
        "OwnerId": owner_id,
    }
    with pytest.raises(ValueError, match="Lead payload|valid User"):
        gateway.create_organization_lead_with_audit_bundle(
            payload,
            "11111111-1111-4111-8111-111111111111",
            "Research note",
            "System activity",
            "2026-07-16",
        )
    assert posted == []


def test_campaign_lead_gateway_rejects_person_fields_before_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bulk Campaign fallback can create only explicitly owned organization Leads."""
    gateway = gateway_mod.SalesforceCampaignGateway()
    posted: list[object] = []
    monkeypatch.setattr(
        gateway, "_create_many", lambda *_args: posted.append(_args) or []
    )
    payload = {
        "Company": "Alpha School District",
        "LastName": "Alpha School District",
        "OwnerId": OWNER_ID,
        "Email": "invented@example.test",
    }
    with pytest.raises(ValueError, match="organization Lead"):
        gateway.create_leads([payload])
    assert posted == []
