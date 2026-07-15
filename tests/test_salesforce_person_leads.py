"""Standalone Salesforce person-Lead approval and duplicate safety tests."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from grant_watch import db
from grant_watch.enrich import salesforce_campaigns as campaigns
from grant_watch.enrich import salesforce_campaign_gateway as gateway_mod
from grant_watch.enrich import salesforce_record_actions as record_actions
from grant_watch.enrich.organization_profile import OrganizationProfile
from grant_watch.models import Lead, LeadGrade, RawItem

LEAD_ID = "00Q000000000001"


class FakeGateway:
    """A singular Lead boundary that records every external create attempt."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def create_lead(self, _payload: dict[str, object]) -> gateway_mod.CreateResult:
        """Return one deterministic Lead ID."""
        self.calls.append("create_lead")
        return gateway_mod.CreateResult(True, LEAD_ID)


@pytest.fixture(autouse=True)
def config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable only the explicitly scoped standalone Lead test path."""
    monkeypatch.setenv("GRANT_SALESFORCE_WRITE_CHANNEL_IDS", "CGRANTS")
    monkeypatch.setenv("SALESFORCE_PERSON_LEAD_WRITES_ENABLED", "1")
    monkeypatch.setenv("SALESFORCE_CAMPAIGN_WRITES_ENABLED", "0")


def _contact(tmp_path: Path, evidence: dict[str, bool] | None = None
             ) -> tuple[sqlite3.Connection, int]:
    """Persist one Grant lead and its official-page contact."""
    conn = db.connect(tmp_path / "person.db")
    db.upsert_lead(conn, Lead(RawItem(
        source="test", item_id="person", title="award",
        entity="Dinuba Unified School District", state="CA", program="SVPP",
        amount=100_000, start="2026-01-01", end="2027-01-01",
        url="https://source.test/award", raw={}), LeadGrade.GOLD))
    lead_id = int(conn.execute("SELECT id FROM leads").fetchone()[0])
    contact_id = db.save_contact(
        conn, lead_id, "Andrew Popp", "Principal", "andrew@district.test",
        "5551212", "https://district.test/staff", "high", "district.test",
        evidence if evidence is not None else {
            "name": True, "email": True, "title": True, "phone": True})
    return conn, contact_id


def test_preview_requires_field_evidence(monkeypatch: pytest.MonkeyPatch,
                                         tmp_path: Path) -> None:
    """A legacy verified label cannot replace name/email page evidence."""
    conn, contact_id = _contact(tmp_path, {"name": True, "email": False})
    monkeypatch.setattr(campaigns, "_duplicate_person", lambda *_args: [])
    with pytest.raises(ValueError, match="current source evidence"):
        campaigns.prepare_person_lead_creation(
            conn, "TWORK", "CGRANTS", "1.1", "UCHASE", contact_id)


def test_preview_freezes_exact_contact_without_write(monkeypatch: pytest.MonkeyPatch,
                                                      tmp_path: Path) -> None:
    """Preview stores exact source facts and performs no Salesforce create."""
    conn, contact_id = _contact(tmp_path)
    monkeypatch.setattr(campaigns, "_duplicate_person", lambda *_args: [])
    action = campaigns.prepare_person_lead_creation(
        conn, "TWORK", "CGRANTS", "1.1", "UCHASE", contact_id)
    row = conn.execute("SELECT action_type,state,payload_json FROM crm_actions").fetchone()
    payload = json.loads(str(row["payload_json"]))["lead"]
    assert (row["action_type"], row["state"]) == ("create_person_lead", "ready")
    assert payload["LastName"] == "Andrew Popp" and "FirstName" not in payload
    assert "No Campaign membership" in action.preview


def test_confirmation_creates_one_and_reads_back(monkeypatch: pytest.MonkeyPatch,
                                                  tmp_path: Path) -> None:
    """A confirmed action creates exactly one Lead and verifies its identity."""
    conn, contact_id = _contact(tmp_path)
    monkeypatch.setattr(campaigns, "_duplicate_person", lambda *_args: [])
    action = campaigns.prepare_person_lead_creation(
        conn, "TWORK", "CGRANTS", "1.1", "UCHASE", contact_id)
    match = campaigns.salesforce.SFMatch(
        "Lead", LEAD_ID, "Andrew Popp", "Dinuba Unified School District", "Chase",
        "https://salesforce.test/lead", "high", state="CA")
    monkeypatch.setattr(campaigns.salesforce, "exact_email_matches", lambda _email: [match])
    gateway = FakeGateway()
    result = campaigns.confirm_action(
        conn, gateway, action.action_id, action.nonce,
        "TWORK", "CGRANTS", "1.1", "UCHASE")
    assert result.added == 1 and gateway.calls == ["create_lead"]
    item = conn.execute("SELECT state,salesforce_id FROM crm_action_items").fetchone()
    assert tuple(item) == ("lead_created", LEAD_ID)


def test_confirmation_duplicate_prevents_create(monkeypatch: pytest.MonkeyPatch,
                                                 tmp_path: Path) -> None:
    """A duplicate appearing between preview and confirmation prevents POST."""
    conn, contact_id = _contact(tmp_path)
    duplicate = campaigns.salesforce.SFMatch(
        "Lead", LEAD_ID, "Andrew Popp", "Dinuba Unified School District", "Chase",
        "https://salesforce.test/lead", "high")
    outcomes: list[list[campaigns.salesforce.SFMatch]] = [[], [duplicate]]
    monkeypatch.setattr(campaigns, "_duplicate_person", lambda *_args: outcomes.pop(0))
    action = campaigns.prepare_person_lead_creation(
        conn, "TWORK", "CGRANTS", "1.1", "UCHASE", contact_id)
    gateway = FakeGateway()
    result = campaigns.confirm_action(
        conn, gateway, action.action_id, action.nonce,
        "TWORK", "CGRANTS", "1.1", "UCHASE")
    assert result.already_present == 1 and gateway.calls == []


def test_create_preview_includes_verified_organization_fields(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Official profile and NCES facts populate only their exact Salesforce fields."""
    conn, contact_id = _contact(tmp_path)
    conn.execute(
        "UPDATE leads SET entity_type='school_district',enrollment=6600")
    conn.commit()
    monkeypatch.setattr(campaigns, "_duplicate_person", lambda *_args: [])
    monkeypatch.setattr(record_actions, "fetch_profile", lambda *_args: OrganizationProfile(
        "https://district.test/", "1327 E El Monte Way", "Dinuba", "CA", "93618",
        "", "559-595-7200", "https://district.test/contact", ""))
    campaigns.prepare_person_lead_creation(
        conn, "TWORK", "CGRANTS", "1.1", "UCHASE", contact_id)
    payload = json.loads(conn.execute(
        "SELECT payload_json FROM crm_actions").fetchone()[0])["lead"]
    assert payload["Website"] == "https://district.test/"
    assert payload["Street"] == "1327 E El Monte Way"
    assert payload["Industry"] == "K-12 Schools"
    assert payload["Number_of_Students__c"] == 6600
