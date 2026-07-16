"""Standalone organization-only Salesforce Lead safety and approval tests."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from grant_watch import db
from grant_watch.enrich import salesforce_campaign_gateway as gateway_mod
from grant_watch.enrich import salesforce_campaigns as campaigns
from grant_watch.enrich import salesforce_record_actions as record_actions
from grant_watch.enrich import salesforce_ownership as ownership
from grant_watch.enrich import finder
from grant_watch.enrich.organization_profile import OrganizationProfile
from grant_watch.models import (
    FundingEventType,
    Lead,
    LeadGrade,
    RawItem,
    VerificationStatus,
)

LEAD_ID = "00Q000000000001"
OWNER_ID = "005000000000001"
OWNER = ownership.RequesterOwner(OWNER_ID, "Chase Test", "chase@example.test")


class FakeGateway:
    """A singular organization Lead boundary that records every create attempt."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def create_organization_lead_with_audit_bundle(
            self, payload: dict[str, object], _action_id: str, _note_body: str,
            _task_description: str, _activity_date: str) -> gateway_mod.LeadAuditResult:
        """Return one deterministic all-or-none Lead/audit result."""
        self.calls.append(payload)
        return gateway_mod.LeadAuditResult(
            True, "069000000000001", "06A000000000001", "00T000000000001",
            lead_id=LEAD_ID)

    def get_record(self, _sobject: str, _record_id: str
                   ) -> gateway_mod.SalesforceRecordRef:
        """Return the exact organization identity for readback."""
        return gateway_mod.SalesforceRecordRef(
            "Lead", LEAD_ID, "Corning Union Elementary School District",
            "https://salesforce.test/lead", company="Corning Union Elementary School District",
            state="CA")

    def lead_creation_snapshot(
            self, _lead_id: str) -> gateway_mod.LeadCreationSnapshot:
        """Return the exact approved identity and owner after creation."""
        payload = self.calls[-1]
        return gateway_mod.LeadCreationSnapshot(
            LEAD_ID, str(payload["Company"]), "", str(payload["LastName"]), "",
            str(payload.get("State") or ""), str(payload["OwnerId"]),
            "https://salesforce.test/lead")

    def verify_lead_audit_bundle(
            self, _lead_id: str, _action_id: str, _note_body: str,
            _task_description: str, _result: gateway_mod.LeadAuditResult) -> bool:
        """Accept the deterministic fake audit readback."""
        return True


@pytest.fixture(autouse=True)
def config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable only the organization-only Lead and required audit gates."""
    monkeypatch.setenv("GRANT_SALESFORCE_WRITE_CHANNEL_IDS", "CGRANTS")
    monkeypatch.setenv("SALESFORCE_ORGANIZATION_LEAD_WRITES_ENABLED", "1")
    monkeypatch.setenv("SALESFORCE_GRANT_AUDIT_RECORDS_ENABLED", "1")
    monkeypatch.setenv("SALESFORCE_CAMPAIGN_WRITES_ENABLED", "0")
    monkeypatch.setattr(
        ownership, "resolve_requester_owner", lambda *_args: OWNER)
    monkeypatch.setattr(
        ownership, "require_frozen_requester_owner", lambda *_args: OWNER)
    monkeypatch.setattr(finder, "find_official_site", lambda *_args: finder.OfficialSite(
        "corningelementary.org", "https://www.corningelementary.org/contact",
        "Corning Union Elementary School District official site"))
    monkeypatch.setattr(record_actions, "fetch_profile", lambda *_args: OrganizationProfile(
        website="https://www.corningelementary.org/", street="1590 South Street",
        city="Corning", state="CA", postal_code="96021", main_phone="530-824-7700",
        source_url="https://www.corningelementary.org/contact"))


def _lead(tmp_path: Path, verified: bool = True) -> tuple[sqlite3.Connection, int]:
    """Persist one current funding event with an exact source URL."""
    conn = db.connect(tmp_path / "organization.db")
    item = RawItem(
        source="ca-grants-portal", item_id="corning", title="Stronger Connections award",
        entity="Corning Union Elementary School District", state="CA",
        program="Stronger Connections", amount=2_226_321,
        start="2025-07-01", end="2026-09-30",
        url="https://www.grants.ca.gov/award/corning", raw={},
        event_type=FundingEventType.AWARD_ANNOUNCED,
        verification_status=(VerificationStatus.VERIFIED if verified
                             else VerificationStatus.NEEDS_TESTING),
        evidence_excerpt="Published award record",
    )
    db.upsert_lead(conn, Lead(item, LeadGrade.GOLD, entity_type="school_district"))
    lead_id = int(conn.execute("SELECT id FROM leads").fetchone()[0])
    return conn, lead_id


def test_preview_has_no_person_fields_and_uses_current_event_source(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The preview freezes only verified organization facts and the exact event source."""
    conn, grant_lead_id = _lead(tmp_path)
    monkeypatch.setattr(record_actions, "duplicate_organization", lambda *_args: [])
    action = campaigns.prepare_organization_lead_creation(
        conn, "TWORK", "CGRANTS", "1.1", "UCHASE", grant_lead_id)
    row = conn.execute("SELECT action_type,payload_json FROM crm_actions").fetchone()
    payload = json.loads(str(row["payload_json"]))["lead"]
    assert row["action_type"] == "create_organization_lead"
    assert payload["Company"] == payload["LastName"] == (
        "Corning Union Elementary School District")
    assert payload["OwnerId"] == OWNER_ID and "Owner: Chase Test" in action.preview
    assert not ({"FirstName", "Email", "Title", "LinkedIn__c"} & set(payload))
    assert "https://www.grants.ca.gov/award/corning" in payload["Description"]
    assert "no verified person or email" in action.preview
    assert "organization name because Salesforce requires it" in action.preview
    assert "does not represent a person" in payload["Description"]
    assert payload["Website"] == "https://www.corningelementary.org/"
    assert payload["Phone"] == "530-824-7700"
    assert payload["Street"] == "1590 South Street"
    assert "No Campaign membership or Opportunity" in action.preview


def test_unverified_current_event_cannot_prepare_preview(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An unverified event cannot become Salesforce data through this workflow."""
    conn, grant_lead_id = _lead(tmp_path, verified=False)
    monkeypatch.setattr(record_actions, "duplicate_organization", lambda *_args: [])
    with pytest.raises(ValueError, match="verified current funding source"):
        campaigns.prepare_organization_lead_creation(
            conn, "TWORK", "CGRANTS", "1.1", "UCHASE", grant_lead_id)


def test_confirmation_creates_exactly_one_lead_and_audit_bundle(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """One approval makes one singular gateway call and verifies the exact Lead."""
    conn, grant_lead_id = _lead(tmp_path)
    monkeypatch.setattr(record_actions, "duplicate_organization", lambda *_args: [])
    action = campaigns.prepare_organization_lead_creation(
        conn, "TWORK", "CGRANTS", "1.1", "UCHASE", grant_lead_id)
    gateway = FakeGateway()
    result = campaigns.confirm_action(
        conn, gateway, action.action_id, action.nonce,
        "TWORK", "CGRANTS", "1.1", "UCHASE")
    assert result.added == 1 and len(gateway.calls) == 1
    assert gateway.calls[0]["Company"] == "Corning Union Elementary School District"
    item = conn.execute("SELECT state,salesforce_id FROM crm_action_items").fetchone()
    assert tuple(item) == ("lead_created", LEAD_ID)


def test_duplicate_at_confirmation_prevents_create(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A record appearing after preview blocks the singular Salesforce POST."""
    conn, grant_lead_id = _lead(tmp_path)
    duplicate = campaigns.salesforce.SFMatch(
        "Account", "001000000000001", "Corning Union Elementary School District", "",
        "Chase", "https://salesforce.test/account", "high", state="CA")
    outcomes: list[list[campaigns.salesforce.SFMatch]] = [[], [duplicate]]
    monkeypatch.setattr(
        record_actions, "duplicate_organization", lambda *_args: outcomes.pop(0))
    action = campaigns.prepare_organization_lead_creation(
        conn, "TWORK", "CGRANTS", "1.1", "UCHASE", grant_lead_id)
    gateway = FakeGateway()
    result = campaigns.confirm_action(
        conn, gateway, action.action_id, action.nonce,
        "TWORK", "CGRANTS", "1.1", "UCHASE")
    assert result.already_present == 1 and gateway.calls == []


def test_changed_requester_owner_fails_before_external_write(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A changed requester mapping cannot substitute a new owner at confirmation."""
    conn, grant_lead_id = _lead(tmp_path)
    monkeypatch.setattr(record_actions, "duplicate_organization", lambda *_args: [])
    action = campaigns.prepare_organization_lead_creation(
        conn, "TWORK", "CGRANTS", "1.1", "UCHASE", grant_lead_id)

    def changed(*_args: object) -> ownership.RequesterOwner:
        """Represent a roster or active-user change after the immutable preview."""
        raise ValueError("the requesting rep's Salesforce ownership changed after preview")

    monkeypatch.setattr(ownership, "require_frozen_requester_owner", changed)
    gateway = FakeGateway()
    result = campaigns.confirm_action(
        conn, gateway, action.action_id, action.nonce,
        "TWORK", "CGRANTS", "1.1", "UCHASE")
    assert result.state is campaigns.CampaignActionState.FAILED
    assert gateway.calls == []
    assert conn.execute(
        "SELECT external_write_started FROM crm_actions").fetchone()[0] == 0


def test_wrong_owner_readback_is_unknown_not_success(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A Lead created under the wrong owner is never reported as completed."""
    conn, grant_lead_id = _lead(tmp_path)
    monkeypatch.setattr(record_actions, "duplicate_organization", lambda *_args: [])
    action = campaigns.prepare_organization_lead_creation(
        conn, "TWORK", "CGRANTS", "1.1", "UCHASE", grant_lead_id)

    class WrongOwnerGateway(FakeGateway):
        """Return a mismatched owner after the external create began."""

        def lead_creation_snapshot(
                self, lead_id: str) -> gateway_mod.LeadCreationSnapshot:
            """Preserve identity but prove that Salesforce routed ownership wrongly."""
            snapshot = super().lead_creation_snapshot(lead_id)
            return gateway_mod.LeadCreationSnapshot(
                snapshot.record_id, snapshot.company, snapshot.first_name,
                snapshot.last_name, snapshot.email, snapshot.state,
                "005000000000099", snapshot.link)

    gateway = WrongOwnerGateway()
    result = campaigns.confirm_action(
        conn, gateway, action.action_id, action.nonce,
        "TWORK", "CGRANTS", "1.1", "UCHASE")
    assert result.state is campaigns.CampaignActionState.UNKNOWN
    assert len(gateway.calls) == 1


def test_unknown_reconciliation_rejects_wrong_owner(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An unknown organization Lead cannot reconcile under a different owner."""
    conn, grant_lead_id = _lead(tmp_path)
    monkeypatch.setattr(record_actions, "duplicate_organization", lambda *_args: [])
    action = campaigns.prepare_organization_lead_creation(
        conn, "TWORK", "CGRANTS", "1.1", "UCHASE", grant_lead_id)
    conn.execute(
        "UPDATE crm_actions SET state='unknown',external_write_started=1 WHERE id=?",
        (action.action_id,))
    conn.commit()
    match = campaigns.salesforce.SFMatch(
        "Lead", LEAD_ID, "Corning Union Elementary School District",
        "Corning Union Elementary School District", "Other Owner",
        "https://salesforce.test/lead", "high", state="CA")
    monkeypatch.setattr(
        record_actions, "duplicate_organization", lambda *_args: [match])

    class WrongOwnerReconciliationGateway(FakeGateway):
        """Return an exact Grant placeholder with an unapproved owner."""

        def linkedin_person_lead_snapshot(
                self, _lead_id: str) -> gateway_mod.LinkedInPersonLeadSnapshot:
            """Freeze all identity fields except the intentionally wrong owner."""
            return gateway_mod.LinkedInPersonLeadSnapshot(
                LEAD_ID, "Corning Union Elementary School District", "",
                "Corning Union Elementary School District", "", "", "",
                f"Action {action.action_id}", "CA", "stamp",
                "https://salesforce.test/lead", "005000000000099")

    with pytest.raises(ValueError, match="exact Grant placeholder"):
        record_actions.reconcile_unknown_organization_lead(
            conn, WrongOwnerReconciliationGateway(), action.action_id)


def test_disabled_gate_prevents_external_write(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The organization-only action has its own default-off production gate."""
    conn, grant_lead_id = _lead(tmp_path)
    monkeypatch.setattr(record_actions, "duplicate_organization", lambda *_args: [])
    action = campaigns.prepare_organization_lead_creation(
        conn, "TWORK", "CGRANTS", "1.1", "UCHASE", grant_lead_id)
    monkeypatch.setenv("SALESFORCE_ORGANIZATION_LEAD_WRITES_ENABLED", "0")
    gateway = FakeGateway()
    result = campaigns.confirm_action(
        conn, gateway, action.action_id, action.nonce,
        "TWORK", "CGRANTS", "1.1", "UCHASE")
    assert result.state == campaigns.CampaignActionState.FAILED
    assert gateway.calls == []
