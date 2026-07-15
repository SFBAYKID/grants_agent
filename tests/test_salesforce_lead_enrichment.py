"""Blank-only, evidence-backed existing Salesforce Lead enrichment tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from grant_watch import db
from grant_watch.enrich import organization_profile
from grant_watch.enrich import salesforce_campaigns as campaigns
from grant_watch.enrich import salesforce_campaign_gateway as gateway_mod
from grant_watch.enrich import salesforce_record_actions as record_actions
from grant_watch.models import Lead, LeadGrade, RawItem

LEAD_ID = "00Q000000000001"


class FakeGateway:
    """Exact Lead snapshot/update boundary with deterministic readback."""

    def __init__(self, company: str = "Dinuba Unified School District") -> None:
        self.company = company
        self.values: dict[str, str | float | None] = {
            key: None for key in gateway_mod._LEAD_ENRICHMENT_FIELDS}
        self.values["State"] = "CA"
        self.stamp = "2026-07-15T22:00:00.000+0000"
        self.calls: list[dict[str, object]] = []
        self.audit_actions: set[str] = set()
        self.audit_create_count = 0
        self.fail_error = ""

    def lead_enrichment_snapshot(self, lead_id: str) -> gateway_mod.LeadEnrichmentSnapshot:
        """Return the current fake Lead state."""
        assert lead_id == LEAD_ID
        return gateway_mod.LeadEnrichmentSnapshot(
            LEAD_ID, self.company, "andrew@district.test", self.stamp,
            dict(self.values), "https://writer.test/lead")

    def enrich_lead_with_audit_bundle(
            self, lead_id: str, delta: dict[str, object],
            expected_system_modstamp: str, action_id: str, _note_body: str,
            _task_description: str, _activity_date: str) -> gateway_mod.LeadAuditResult:
        """Apply one all-or-none fake Lead/audit transaction."""
        assert lead_id == LEAD_ID and expected_system_modstamp == self.stamp
        if self.fail_error:
            return gateway_mod.LeadAuditResult(False, lead_id=lead_id, error=self.fail_error)
        self.calls.append(delta)
        self.values.update(delta)  # type: ignore[arg-type]  # test fake mirrors CRM JSON
        self.stamp = "2026-07-15T22:01:00.000+0000"
        self.audit_create_count += 1
        self.audit_actions.add(action_id)
        return gateway_mod.LeadAuditResult(
            True, "069000000000001", "06A000000000001", "00T000000000001",
            lead_id=lead_id)

    def lead_audit_snapshot(self, _lead_id: str,
                            action_id: str) -> gateway_mod.LeadAuditSnapshot:
        """Return a complete fake audit trail only after creation."""
        if action_id in self.audit_actions:
            return gateway_mod.LeadAuditSnapshot(
                "069000000000001", "06A000000000001", "00T000000000001")
        return gateway_mod.LeadAuditSnapshot()

    def create_lead_audit_bundle(
            self, _lead_id: str, action_id: str, _note_body: str,
            _task_description: str, _activity_date: str) -> gateway_mod.LeadAuditResult:
        """Create one deterministic all-or-none fake audit bundle."""
        self.audit_create_count += 1
        self.audit_actions.add(action_id)
        return gateway_mod.LeadAuditResult(
            True, "069000000000001", "06A000000000001", "00T000000000001")

    def verify_lead_audit_bundle(
            self, _lead_id: str, _action_id: str, _note_body: str,
            _task_description: str, _result: gateway_mod.LeadAuditResult) -> bool:
        """Accept the deterministic fake audit readback."""
        return True


@pytest.fixture(autouse=True)
def config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable only Lead enrichment in the test channel."""
    monkeypatch.setenv("GRANT_SALESFORCE_WRITE_CHANNEL_IDS", "CGRANTS")
    monkeypatch.setenv("SALESFORCE_WRITE_MY_DOMAIN_URL", "https://writer.test")
    monkeypatch.setenv("SALESFORCE_LEAD_ENRICHMENT_UPDATES_ENABLED", "1")
    monkeypatch.setenv("SALESFORCE_GRANT_AUDIT_RECORDS_ENABLED", "1")
    monkeypatch.setenv("SALESFORCE_CAMPAIGN_WRITES_ENABLED", "0")


def _contact(tmp_path: Path) -> tuple[sqlite3.Connection, int]:
    """Persist one verified contact and enriched Grant lead."""
    conn = db.connect(tmp_path / "enrich.db")
    db.upsert_lead(conn, Lead(RawItem(
        source="test", item_id="dinuba", title="award",
        entity="Dinuba Unified School District",
        state="CA", program="SCG", amount=3_000_000, start="2025-01-01",
        end="2026-09-30", url="https://source.test", raw={}), LeadGrade.GOLD))
    lead_id = int(conn.execute("SELECT id FROM leads").fetchone()[0])
    conn.execute(
        "UPDATE leads SET enrollment=6600,entity_type='school_district' WHERE id=?",
        (lead_id,))
    conn.commit()
    contact_id = db.save_contact(
        conn, lead_id, "Andrew Popp", "Principal", "andrew@district.test", "",
        "https://dinuba.k12.ca.us/directory", "high", "dinuba.k12.ca.us",
        {"name": True, "email": True, "title": True, "phone": False})
    return conn, contact_id


def _profile() -> organization_profile.OrganizationProfile:
    """Return official-site facts for offline tests."""
    return organization_profile.OrganizationProfile(
        "https://dinuba.k12.ca.us/", "1327 E El Monte Way", "Dinuba", "CA",
        "93618", "", "559-595-7200", "https://dinuba.k12.ca.us/directory", "")


def test_preview_fills_only_blank_fields_and_preserves_identity(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Populated State and protected identity fields are never overwritten."""
    conn, contact_id = _contact(tmp_path)
    monkeypatch.setattr(record_actions, "fetch_profile", lambda *_args: _profile())
    gateway = FakeGateway()
    action = campaigns.prepare_lead_enrichment(
        conn, gateway, "T", "CGRANTS", "1.1", "U", contact_id,
        f"https://writer.test/lightning/r/Lead/{LEAD_ID}/view")
    assert "Website" in action.preview and "State:" not in action.preview
    payload = conn.execute("SELECT payload_json FROM crm_actions").fetchone()[0]
    assert '"Email"' not in payload and '"OwnerId"' not in payload


def test_preview_rejects_wrong_salesforce_identity(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An exact Lead link still cannot cross organizations."""
    conn, contact_id = _contact(tmp_path)
    monkeypatch.setattr(record_actions, "fetch_profile", lambda *_args: _profile())
    with pytest.raises(ValueError, match="does not match"):
        campaigns.prepare_lead_enrichment(
            conn, FakeGateway("Other District"), "T", "CGRANTS", "1.1", "U",
            contact_id, f"https://writer.test/lightning/r/Lead/{LEAD_ID}/view")


def test_confirm_updates_once_and_reads_back(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Confirmation performs one allowlisted update and verifies every field."""
    conn, contact_id = _contact(tmp_path)
    monkeypatch.setattr(record_actions, "fetch_profile", lambda *_args: _profile())
    gateway = FakeGateway()
    action = campaigns.prepare_lead_enrichment(
        conn, gateway, "T", "CGRANTS", "1.1", "U", contact_id,
        f"https://writer.test/lightning/r/Lead/{LEAD_ID}/view")
    result = campaigns.confirm_action(
        conn, gateway, action.action_id, action.nonce, "T", "CGRANTS", "1.1", "U")
    assert result.added == 1 and len(gateway.calls) == 1
    assert set(gateway.calls[0]) <= gateway_mod._LEAD_ENRICHMENT_FIELDS


def test_feature_flag_off_prevents_update(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Campaign/Lead creation flags cannot silently enable Lead PATCH."""
    conn, contact_id = _contact(tmp_path)
    monkeypatch.setattr(record_actions, "fetch_profile", lambda *_args: _profile())
    gateway = FakeGateway()
    action = campaigns.prepare_lead_enrichment(
        conn, gateway, "T", "CGRANTS", "1.1", "U", contact_id,
        f"https://writer.test/lightning/r/Lead/{LEAD_ID}/view")
    monkeypatch.setenv("SALESFORCE_LEAD_ENRICHMENT_UPDATES_ENABLED", "0")
    result = campaigns.confirm_action(
        conn, gateway, action.action_id, action.nonce, "T", "CGRANTS", "1.1", "U")
    assert result.state is campaigns.CampaignActionState.FAILED and gateway.calls == []


def test_composite_failure_preserves_internal_reconciliation_detail(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A rolled-back Salesforce error stays internal while the action fails closed."""
    conn, contact_id = _contact(tmp_path)
    monkeypatch.setattr(record_actions, "fetch_profile", lambda *_args: _profile())
    gateway = FakeGateway()
    action = campaigns.prepare_lead_enrichment(
        conn, gateway, "T", "CGRANTS", "1.1", "U", contact_id,
        f"https://writer.test/lightning/r/Lead/{LEAD_ID}/view")
    gateway.fail_error = "grantAuditTask HTTP 400: INVALID_FIELD"
    result = campaigns.confirm_action(
        conn, gateway, action.action_id, action.nonce, "T", "CGRANTS", "1.1", "U")
    row = conn.execute("SELECT state,last_error FROM crm_actions").fetchone()
    assert result.state is campaigns.CampaignActionState.FAILED
    assert row["state"] == "failed" and "INVALID_FIELD" in row["last_error"]
    assert gateway.calls == []


def test_gateway_rejects_identity_or_routing_field_updates() -> None:
    """The atomic boundary cannot modify identity, owner, status, or arbitrary fields."""
    gateway = gateway_mod.SalesforceCampaignGateway()
    with pytest.raises(ValueError, match="forbidden"):
        gateway.enrich_lead_with_audit_bundle(
            LEAD_ID, {"Email": "other@example.com"}, "stamp",
            "11111111-2222-3333-4444-555555555555", "note", "task", "2026-07-15")


def test_missing_audit_repair_creates_no_second_lead_update(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Repair adds only Note/Activity artifacts for the exact completed Lead action."""
    conn, contact_id = _contact(tmp_path)
    monkeypatch.setattr(record_actions, "fetch_profile", lambda *_args: _profile())
    gateway = FakeGateway()
    action = campaigns.prepare_lead_enrichment(
        conn, gateway, "T", "CGRANTS", "1.1", "U", contact_id,
        f"https://writer.test/lightning/r/Lead/{LEAD_ID}/view")
    campaigns.confirm_action(
        conn, gateway, action.action_id, action.nonce, "T", "CGRANTS", "1.1", "U")
    gateway.audit_actions.clear()
    gateway.audit_create_count = 0
    update_count = len(gateway.calls)

    repair = campaigns.prepare_lead_audit_repair(
        conn, gateway, "T", "CGRANTS", "1.1", "U",
        f"https://writer.test/lightning/r/Lead/{LEAD_ID}/view")
    assert "No Lead fields" in repair.preview and "no customer outreach" in repair.preview
    result = campaigns.confirm_action(
        conn, gateway, repair.action_id, repair.nonce, "T", "CGRANTS", "1.1", "U")

    assert result.added == 1
    assert len(gateway.calls) == update_count
    assert gateway.audit_create_count == 1
    item = conn.execute(
        "SELECT state,salesforce_id FROM crm_action_items WHERE action_id=?",
        (repair.action_id,)).fetchone()
    assert tuple(item) == ("audit_repaired", LEAD_ID)
