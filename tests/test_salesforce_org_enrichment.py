"""Contact-independent, blank-only Salesforce organization enrichment tests."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from grant_watch import db
from grant_watch.enrich import salesforce_campaign_gateway as gateway_mod
from grant_watch.enrich import salesforce_campaigns as campaigns
from grant_watch.enrich import salesforce_org_enrichment as enrichment
from grant_watch.enrich import salesforce
from grant_watch.enrich import salesforce_record_actions as record_actions
from grant_watch.enrich.organization_profile import OrganizationProfile
from grant_watch.models import FundingEventType, Lead, LeadGrade, RawItem, VerificationStatus
from grant_watch.slack import conversation

LEAD_ID = "00Q000000000001"


class FakeGateway:
    """Exact blank organization Lead boundary with one audited update call."""

    def __init__(self, company: str = "Birmingham Community Charter High School") -> None:
        self.company = company
        self.values: dict[str, str | float | None] = {
            key: None for key in gateway_mod._LEAD_ENRICHMENT_FIELDS}
        self.values["State"] = "CA"
        self.stamp = "2026-07-15T22:00:00.000+0000"
        self.calls: list[dict[str, object]] = []

    def lead_enrichment_snapshot(self, lead_id: str) -> gateway_mod.LeadEnrichmentSnapshot:
        """Return the current exact organization-only Lead state."""
        assert lead_id == LEAD_ID
        return gateway_mod.LeadEnrichmentSnapshot(
            LEAD_ID, self.company, "", self.stamp, dict(self.values),
            "https://writer.test/lead")

    def enrich_lead_with_audit_bundle(
            self, lead_id: str, delta: dict[str, object],
            expected_system_modstamp: str, action_id: str, _note_body: str,
            _task_description: str, _activity_date: str) -> gateway_mod.LeadAuditResult:
        """Apply the one immutable update and its audit records."""
        assert lead_id == LEAD_ID and expected_system_modstamp == self.stamp
        self.calls.append(delta)
        self.values.update(delta)  # type: ignore[arg-type]  # fake mirrors Salesforce JSON
        self.stamp = "2026-07-15T22:01:00.000+0000"
        return gateway_mod.LeadAuditResult(
            True, "069000000000001", "06A000000000001", "00T000000000001",
            lead_id=lead_id)

    def verify_lead_audit_bundle(
            self, _lead_id: str, _action_id: str, _note_body: str,
            _task_description: str, _result: gateway_mod.LeadAuditResult) -> bool:
        """Accept the deterministic audit readback."""
        return True


@pytest.fixture(autouse=True)
def config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable only exact Lead enrichment and its audit bundle."""
    monkeypatch.setenv("GRANT_SALESFORCE_WRITE_CHANNEL_IDS", "CGRANTS")
    monkeypatch.setenv("SALESFORCE_WRITE_MY_DOMAIN_URL", "https://writer.test")
    monkeypatch.setenv("SALESFORCE_LEAD_ENRICHMENT_UPDATES_ENABLED", "1")
    monkeypatch.setenv("SALESFORCE_GRANT_AUDIT_RECORDS_ENABLED", "1")


def _lead(tmp_path: Path) -> tuple[sqlite3.Connection, int]:
    """Persist one verified Birmingham award with no contact rows."""
    conn = db.connect(tmp_path / "org-enrichment.db")
    item = RawItem(
        source="usaspending:16.071", item_id="birmingham",
        title="School Violence Prevention Program award",
        entity="Birmingham Community Charter High School", state="CA",
        program="SVPP", amount=500_000, start="2025-10-01", end="2028-09-30",
        url="https://www.usaspending.gov/award/ASST_NON_15JCOPS25GG01291SSIX_015",
        raw={}, event_type=FundingEventType.AWARD_OBLIGATED,
        verification_status=VerificationStatus.VERIFIED,
        evidence_excerpt="Published award record")
    db.upsert_lead(conn, Lead(item, LeadGrade.GOLD, entity_type="school"))
    return conn, int(conn.execute("SELECT id FROM leads").fetchone()[0])


def _profile() -> OrganizationProfile:
    """Return complete official-site organization data for offline tests."""
    return OrganizationProfile(
        website="https://www.bcchs.net/", street="17000 Haynes St.",
        city="Lake Balboa", state="CA", postal_code="91406",
        country="United States", main_phone="(818) 758-5200",
        source_url="https://www.bcchs.net/contact")


def test_preview_needs_no_contact_and_fills_complete_blank_address(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No contact row or email is needed for verified organization-only fields."""
    conn, lead_id = _lead(tmp_path)
    monkeypatch.setattr(
        enrichment.finder, "find_official_site",
        lambda *_args: enrichment.finder.OfficialSite(
            "bcchs.net", "https://www.bcchs.net/contact", "official site"))
    monkeypatch.setattr(enrichment, "fetch_profile", lambda *_args: _profile())
    action = campaigns.prepare_organization_lead_enrichment(
        conn, FakeGateway(), "T", "CGRANTS", "1.1", "U", lead_id,
        f"https://writer.test/lightning/r/Lead/{LEAD_ID}/view")
    delta = json.loads(conn.execute(
        "SELECT payload_json FROM crm_actions").fetchone()[0])["delta"]
    stored = json.loads(conn.execute(
        "SELECT payload_json FROM crm_actions").fetchone()[0])
    assert {"Street", "City", "PostalCode", "Country", "Website", "Phone"} <= set(delta)
    assert not ({"FirstName", "LastName", "Email", "Title"} & set(delta))
    assert "Grant research summary for Birmingham" in delta["Description"]
    assert "salesforce_lead_enrichment_preview" not in delta["Description"]
    assert "No person, email, Campaign" in action.preview
    assert stored["approval_preview"] == action.preview
    assert stored["note_body"] in action.preview
    assert stored["task_description"] in action.preview
    assert "research notes" in stored["task_description"]
    assert "Description" not in stored["task_description"]
    assert "requested by Slack user" not in stored["note_body"]
    assert "Birmingham Community Charter High School" in action.preview
    assert "BIRMINGHAM COMMUNITY" not in action.preview


def test_wrong_salesforce_company_fails_before_preview(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An exact Lead link cannot cross organization identity boundaries."""
    conn, lead_id = _lead(tmp_path)
    monkeypatch.setattr(enrichment, "_profile", lambda *_args: _profile())
    with pytest.raises(ValueError, match="does not match"):
        campaigns.prepare_organization_lead_enrichment(
            conn, FakeGateway("Other School"), "T", "CGRANTS", "1.1", "U", lead_id,
            f"https://writer.test/lightning/r/Lead/{LEAD_ID}/view")


def test_confirmation_updates_one_exact_lead_and_creates_no_record(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Confirmation reuses the existing single-record audited update boundary."""
    conn, lead_id = _lead(tmp_path)
    monkeypatch.setattr(enrichment, "_profile", lambda *_args: _profile())
    gateway = FakeGateway()
    action = campaigns.prepare_organization_lead_enrichment(
        conn, gateway, "T", "CGRANTS", "1.1", "U", lead_id,
        f"https://writer.test/lightning/r/Lead/{LEAD_ID}/view")
    result = campaigns.confirm_action(
        conn, gateway, action.action_id, action.nonce, "T", "CGRANTS", "1.1", "U")
    assert result.added == 1 and len(gateway.calls) == 1
    assert conn.execute("SELECT state FROM crm_action_items").fetchone()[0] == "lead_enriched"


def test_confirmation_rechecks_state_and_immutable_preview(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A changed identity or tampered approval envelope fails before any write."""
    conn, lead_id = _lead(tmp_path)
    monkeypatch.setattr(enrichment, "_profile", lambda *_args: _profile())
    gateway = FakeGateway()
    action = campaigns.prepare_organization_lead_enrichment(
        conn, gateway, "T", "CGRANTS", "1.1", "U", lead_id,
        f"https://writer.test/lightning/r/Lead/{LEAD_ID}/view")
    gateway.values["State"] = "NV"
    result = campaigns.confirm_action(
        conn, gateway, action.action_id, action.nonce,
        "T", "CGRANTS", "1.1", "U")
    assert result.state == campaigns.CampaignActionState.FAILED
    assert "nothing was submitted" in result.message
    assert "ValueError" not in result.message
    assert gateway.calls == []

    tamper_path = tmp_path / "tamper"
    tamper_path.mkdir()
    conn2, lead_id2 = _lead(tamper_path)
    action2 = campaigns.prepare_organization_lead_enrichment(
        conn2, FakeGateway(), "T", "CGRANTS", "1.1", "U", lead_id2,
        f"https://writer.test/lightning/r/Lead/{LEAD_ID}/view")
    conn2.execute(
        "UPDATE crm_actions SET payload_json='{}' WHERE id=?", (action2.action_id,))
    conn2.commit()
    with pytest.raises(ValueError, match="payload changed"):
        campaigns.confirm_action(
            conn2, FakeGateway(), action2.action_id, action2.nonce,
            "T", "CGRANTS", "1.1", "U")


def test_exact_salesforce_lead_selection_requires_one_state_bound_match() -> None:
    """Blank state, low confidence, another object, or ambiguity cannot select a Lead."""
    def match(*, state: str = "CA", confidence: str = "high",
              sobject: str = "Lead", record_id: str = LEAD_ID) -> salesforce.SFMatch:
        return salesforce.SFMatch(
            sobject, record_id, "Record", "Birmingham Community Charter High School",
            "Owner", f"https://writer.test/{record_id}", confidence, state=state)

    assert enrichment.select_exact_lead(
        [match()], "Birmingham Community Charter High School", "CA").record_id == LEAD_ID
    for matches in (
        [match(state="")], [match(confidence="possible")],
        [match(sobject="Account")], [match(), match(record_id="00Q000000000002")],
    ):
        with pytest.raises(ValueError, match="one exact matching Lead"):
            enrichment.select_exact_lead(
                matches, "Birmingham Community Charter High School", "CA")


def test_industry_uses_reviewed_entity_type_not_organization_name() -> None:
    """A word such as Schoolcraft in a county name cannot imply K-12 industry."""
    assert record_actions._verified_industry("school") == "K-12 Schools"
    assert record_actions._verified_industry("school_district") == "K-12 Schools"
    assert record_actions._verified_industry("county") == ""


def test_pending_preview_followup_is_plain_english_and_uses_frozen_delta(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A follow-up explains the exact pending preview without another model turn."""
    conn, lead_id = _lead(tmp_path)
    monkeypatch.setattr(enrichment, "_profile", lambda *_args: _profile())
    campaigns.prepare_organization_lead_enrichment(
        conn, FakeGateway(), "T", "CGRANTS", "1.1", "U", lead_id,
        f"https://writer.test/lightning/r/Lead/{LEAD_ID}/view")
    database = tmp_path / "org-enrichment.db"
    real_connect = db.connect
    monkeypatch.setattr(conversation.db, "connect", lambda: real_connect(database))
    result = conversation.respond(
        "Does this preview require an email, and exactly what will change if confirmed?",
        None, requester_slack="U", workspace="T", channel="CGRANTS", thread_ts="1.1")
    reply = result["reply"]
    assert "does not require or add an email" in reply
    assert "• Street" in reply and "• Postal code" in reply
    assert "• Research notes" in reply
    assert "salesforce_" not in reply and "payload" not in reply
    assert result["pending_crm_actions"] == []


def test_pending_preview_followup_rejects_tampered_payload(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A read-only explanation still refuses an altered approval envelope."""
    conn, lead_id = _lead(tmp_path)
    monkeypatch.setattr(enrichment, "_profile", lambda *_args: _profile())
    campaigns.prepare_organization_lead_enrichment(
        conn, FakeGateway(), "T", "CGRANTS", "1.1", "U", lead_id,
        f"https://writer.test/lightning/r/Lead/{LEAD_ID}/view")
    conn.execute("UPDATE crm_actions SET payload_json='{}'")
    conn.commit()
    database = tmp_path / "org-enrichment.db"
    real_connect = db.connect
    monkeypatch.setattr(conversation.db, "connect", lambda: real_connect(database))
    monkeypatch.setattr(
        conversation, "Anthropic",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("model should not run")))
    assert conversation._pending_org_enrichment_reply(
        "What changes if this preview is confirmed?", "T", "CGRANTS", "1.1", "U") is None


def test_reconciled_legacy_placeholder_notes_are_safely_replaced(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Grant may replace only its exact completed legacy placeholder description."""
    conn, lead_id = _lead(tmp_path)
    monkeypatch.setattr(enrichment, "_profile", lambda *_args: _profile())
    prior_id = "11111111-2222-3333-4444-555555555555"
    plan = campaigns.MemberPlan(
        lead_id, "birmingham", "Birmingham Community Charter High School", "CA",
        "create_standalone_org_lead")
    campaigns._store_action(
        conn, "create_organization_lead", "T", "CGRANTS", "old-thread", "U",
        {"lead": {}}, plans=[plan], action_id=prior_id)
    with conn:
        conn.execute("UPDATE crm_actions SET state='complete' WHERE id=?", (prior_id,))
        conn.execute(
            """UPDATE crm_action_items
                  SET state='lead_created',salesforce_id=? WHERE action_id=?""",
            (LEAD_ID, prior_id))
    gateway = FakeGateway()
    gateway.values["Description"] = (
        "Created by Grant as an organization-only Lead. No individual contact or email "
        f"was verified. Action {prior_id}.")
    campaigns.prepare_organization_lead_enrichment(
        conn, gateway, "T", "CGRANTS", "1.1", "U", lead_id,
        f"https://writer.test/lightning/r/Lead/{LEAD_ID}/view")
    payload = json.loads(conn.execute(
        "SELECT payload_json FROM crm_actions WHERE id!=?", (prior_id,)).fetchone()[0])
    assert payload["delta"]["Description"].startswith("Grant research summary for Birmingham")
    assert "Created by Grant as an organization-only Lead" not in payload["delta"]["Description"]


def test_research_note_title_leads_with_organization_name() -> None:
    """A salesperson sees the organization before the internal audit reference."""
    action_id = "11111111-2222-3333-4444-555555555555"
    title = gateway_mod._research_note_title(
        "Grant research summary for Birmingham Community Charter High School", action_id)
    assert title.startswith("Grant research — Birmingham Community Charter High School — ")
    assert title.endswith(action_id)
