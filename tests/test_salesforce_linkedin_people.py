"""LinkedIn-only person context, enrichment, and singular Salesforce safety tests."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from grant_watch import db, linkedin_candidates
from grant_watch.enrich import finder
from grant_watch.enrich import salesforce_campaign_gateway as gateway_mod
from grant_watch.enrich import salesforce_campaigns as campaigns
from grant_watch.enrich import salesforce_linkedin_actions as actions
from grant_watch.enrich import salesforce_record_actions as record_actions
from grant_watch.enrich.organization_profile import OrganizationProfile
from grant_watch.models import (
    FundingEventType,
    Lead,
    LeadGrade,
    RawItem,
    VerificationStatus,
)
from grant_watch.slack import conversation

LEAD_ID = "00Q000000000001"
PRIOR_ACTION = "11111111-1111-4111-8111-111111111111"
PROFILE = "https://www.linkedin.com/in/vic-chalabian"
COMPANY = "Birmingham Community Charter High School"


def _grant_lead(tmp_path: Path) -> tuple[sqlite3.Connection, sqlite3.Row]:
    """Persist one verified award and the exact thread-bound LinkedIn candidate."""
    conn = db.connect(tmp_path / "linkedin.db")
    db.upsert_lead(conn, Lead(RawItem(
        source="usaspending:16.071", item_id="award", title="SVPP award",
        entity=COMPANY, state="CA", program="SVPP", amount=500_000,
        start="2025-10-01", end="2028-09-30",
        url="https://www.usaspending.gov/award/example", raw={},
        event_type=FundingEventType.AWARD_OBLIGATED,
        verification_status=VerificationStatus.VERIFIED,
        evidence_excerpt="Published award record"), LeadGrade.GOLD,
        entity_type="school"))
    row = db.get_lead(conn, 1)
    assert row is not None
    return conn, row


def _candidate(conn: sqlite3.Connection) -> linkedin_candidates.LinkedInCandidate:
    """Save Vartan's exact no-email search-result evidence in one Slack thread."""
    return linkedin_candidates.save_candidate(
        conn, 1, "TWORK", "CGRANTS", "1.1", "UCHASE", COMPANY,
        finder.LinkedInPerson(
            'Vartan "Vic" Chalabian', "IT Systems Manager", PROFILE,
            'Vartan "Vic" Chalabian - IT Systems Manager - Birmingham Community Charter High School'),
    )


@pytest.fixture(autouse=True)
def write_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable only singular person/update and required audit writes for these tests."""
    monkeypatch.setenv("GRANT_SALESFORCE_WRITE_CHANNEL_IDS", "CGRANTS")
    monkeypatch.setenv("SALESFORCE_PERSON_LEAD_WRITES_ENABLED", "1")
    monkeypatch.setenv("SALESFORCE_LEAD_ENRICHMENT_UPDATES_ENABLED", "1")
    monkeypatch.setenv("SALESFORCE_GRANT_AUDIT_RECORDS_ENABLED", "1")


class PlaceholderGateway:
    """Fake one exact Grant-created placeholder and record one atomic update."""

    def __init__(self) -> None:
        self.updated: dict[str, object] | None = None

    def exact_linkedin_person_leads(
            self, _profile: str, _company: str,
            _last_name: str) -> list[gateway_mod.SalesforceRecordRef]:
        """Return no person duplicate before the repair."""
        return []

    def linkedin_person_lead_snapshot(
            self, _lead_id: str) -> gateway_mod.LinkedInPersonLeadSnapshot:
        """Return placeholder fields before update and exact person fields after it."""
        fields = self.updated or {}
        return gateway_mod.LinkedInPersonLeadSnapshot(
            LEAD_ID, COMPANY, str(fields.get("FirstName") or ""),
            str(fields.get("LastName") or COMPANY), "",
            str(fields.get("Title") or ""), str(fields.get("LinkedIn__c") or ""),
            str(fields.get("Description") or (
                f"Created by Grant as an organization-only Lead. Action {PRIOR_ACTION}.")),
            "CA", "stamp", "https://salesforce.test/lead",
        )

    def lead_enrichment_snapshot(
            self, _lead_id: str) -> gateway_mod.LeadEnrichmentSnapshot:
        """Return blank organization details eligible for fill-blank enrichment."""
        values = {key: None for key in (
            "Website", "Phone", "Street", "City", "State", "PostalCode", "Country",
            "Industry", "Description", "LinkedIn__c", "Number_of_Students__c")}
        return gateway_mod.LeadEnrichmentSnapshot(
            LEAD_ID, COMPANY, "", "stamp", values, "https://salesforce.test/lead")

    def attach_linkedin_person_with_audit_bundle(
            self, _lead_id: str, delta: dict[str, object], _stamp: str,
            _action_id: str, _note: str, _task: str,
            _date: str) -> gateway_mod.LeadAuditResult:
        """Record one exact update and return its all-or-none audit IDs."""
        self.updated = delta
        return gateway_mod.LeadAuditResult(
            True, "069000000000001", "06A000000000001", "00T000000000001",
            lead_id=LEAD_ID)

    def verify_lead_audit_bundle(self, *_args: object) -> bool:
        """Accept the deterministic fake Note/link/Task readback."""
        return True

    def lead_audit_snapshot(
            self, _lead_id: str, _action_id: str) -> gateway_mod.LeadAuditSnapshot:
        """Return one complete existing audit bundle for local reconciliation."""
        return gateway_mod.LeadAuditSnapshot(
            "069000000000001", "06A000000000001", "00T000000000001")


def test_this_guy_routes_to_exact_candidate_not_organization(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The selected LinkedIn identity wins over the organization-only fallback."""
    conn, row = _grant_lead(tmp_path)
    candidate = _candidate(conn)
    monkeypatch.setattr(conversation.db, "connect", lambda: conn)
    calls: list[str] = []
    monkeypatch.setattr(
        conversation.tools, "salesforce_linkedin_person_preview",
        lambda candidate_id, *_args: calls.append(candidate_id) or (
            '<grant-crm-action>{"action_id":"a","nonce":"n","preview":"p",'
            '"expires_at":"e"}</grant-crm-action>'))
    monkeypatch.setattr(
        conversation.tools, "salesforce_organization_lead_create_preview",
        lambda *_args: (_ for _ in ()).throw(AssertionError("organization fallback")))
    result = conversation.respond(
        "Yes can we add this guy to Salesforce?", row, requester_slack="UCHASE",
        workspace="TWORK", channel="CGRANTS", thread_ts="1.1")
    assert calls == [candidate.candidate_id]
    assert "Vartan" in result["reply"] and "email is still unverified" in result["reply"]
    assert len(result["pending_crm_actions"]) == 1


def test_existing_placeholder_is_updated_once_without_email_or_duplicate(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """One approval repairs the exact placeholder and never creates another Lead."""
    conn, _row = _grant_lead(tmp_path)
    candidate = _candidate(conn)
    gateway = PlaceholderGateway()
    plan = campaigns.MemberPlan(
        1, "birmingham", COMPANY, "CA", "create_standalone_org_lead",
        proposed_lead={"Company": COMPANY, "LastName": COMPANY})
    campaigns._store_action(
        conn, "create_organization_lead", "TWORK", "CGRANTS", "1.1", "UCHASE",
        {"lead": {"Company": COMPANY, "LastName": COMPANY}}, plans=[plan],
        action_id=PRIOR_ACTION)
    with conn:
        conn.execute("UPDATE crm_actions SET state='complete' WHERE id=?", (PRIOR_ACTION,))
        conn.execute(
            """UPDATE crm_action_items SET state='lead_created',salesforce_id=?
                 WHERE action_id=?""", (LEAD_ID, PRIOR_ACTION))
    duplicate = campaigns.salesforce.SFMatch(
        "Lead", LEAD_ID, COMPANY, COMPANY, "Chase",
        "https://salesforce.test/lead", "high", state="CA")
    monkeypatch.setattr(actions, "duplicate_organization", lambda *_args: [duplicate])
    prepared = actions.prepare_linkedin_person(
        conn, gateway, "TWORK", "CGRANTS", "1.1", "UCHASE", candidate.candidate_id)
    stored = conn.execute(
        "SELECT action_type,payload_json FROM crm_actions WHERE id=?",
        (prepared.action_id,)).fetchone()
    assert stored["action_type"] == "attach_linkedin_person_to_lead"
    payload = json.loads(str(stored["payload_json"]))
    assert payload["lead_id"] == LEAD_ID
    assert "Email" not in payload["delta"]
    assert payload["delta"]["FirstName"] == 'Vartan "Vic"'
    assert payload["delta"]["LastName"] == "Chalabian"
    assert payload["delta"]["LinkedIn__c"] == PROFILE
    result = campaigns.confirm_action(
        conn, gateway, prepared.action_id, prepared.nonce,
        "TWORK", "CGRANTS", "1.1", "UCHASE")
    assert result.added == 1 and gateway.updated is not None
    assert "Email" not in gateway.updated
    assert conn.execute(
        "SELECT status FROM linkedin_person_candidates WHERE id=?",
        (candidate.candidate_id,)).fetchone()[0] == "consumed"


def test_create_payload_fills_verified_org_fields_and_never_guesses_email(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A new person Lead includes supported profile data and leaves email blank."""
    conn, _row = _grant_lead(tmp_path)
    candidate = _candidate(conn)
    conn.execute(
        """INSERT INTO contacts
             (lead_id,source_url,contact_status,official_domain)
           VALUES (1,'https://www.birminghamcharter.com/contact','unverified',
                   'birminghamcharter.com')""")
    conn.commit()
    monkeypatch.setattr(actions, "duplicate_organization", lambda *_args: [])
    monkeypatch.setattr(actions, "fetch_profile", lambda *_args: OrganizationProfile(
        website="https://www.birminghamcharter.com/", main_phone="818-758-5200",
        street="17000 Haynes Street", city="Van Nuys", state="CA",
        postal_code="91406", country="US",
        source_url="https://www.birminghamcharter.com/contact"))

    class NewGateway(PlaceholderGateway):
        """Gateway with no placeholder; only preview payload inspection is needed."""

    prepared = actions.prepare_linkedin_person(
        conn, NewGateway(), "TWORK", "CGRANTS", "1.1", "UCHASE",
        candidate.candidate_id)
    stored = conn.execute(
        "SELECT action_type,payload_json FROM crm_actions WHERE id=?",
        (prepared.action_id,)).fetchone()
    payload = json.loads(str(stored["payload_json"]))["lead"]
    assert stored["action_type"] == "create_linkedin_person_lead"
    assert "Email" not in payload
    assert payload["Website"] == "https://www.birminghamcharter.com/"
    assert payload["Phone"] == "818-758-5200"
    assert payload["Street"] == "17000 Haynes Street"
    assert payload["Industry"] == "K-12 Schools"
    assert payload["LinkedIn__c"] == PROFILE
    assert "No email was found or verified" in payload["Description"]


def test_unknown_org_action_reconciles_by_reads_without_another_write(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An exact existing Lead/audit bundle repairs the ledger without Salesforce POST."""
    conn, _row = _grant_lead(tmp_path)
    payload = {
        "Company": COMPANY, "LastName": COMPANY, "State": "CA",
        "Description": (
            "Created by Grant as an organization-only Lead. No individual contact or "
            f"email was verified. Action {PRIOR_ACTION}."),
    }
    plan = campaigns.MemberPlan(
        1, "birmingham", COMPANY, "CA", "create_standalone_org_lead",
        proposed_lead=payload)
    campaigns._store_action(
        conn, "create_organization_lead", "TWORK", "CGRANTS", "1.1", "UCHASE",
        {"lead": payload}, plans=[plan], action_id=PRIOR_ACTION)
    with conn:
        conn.execute(
            "UPDATE crm_actions SET state='unknown',external_write_started=1 WHERE id=?",
            (PRIOR_ACTION,))
    duplicate = campaigns.salesforce.SFMatch(
        "Lead", LEAD_ID, COMPANY, COMPANY, "Chase",
        "https://salesforce.test/lead", "high", state="CA")
    monkeypatch.setattr(
        actions, "duplicate_organization", lambda *_args: [duplicate])
    monkeypatch.setattr(record_actions, "duplicate_organization", lambda *_args: [duplicate])
    gateway = PlaceholderGateway()
    result = record_actions.reconcile_unknown_organization_lead(
        conn, gateway, PRIOR_ACTION)
    assert result.already_present == 1
    row = conn.execute(
        """SELECT a.state,i.state,i.salesforce_id
             FROM crm_actions a JOIN crm_action_items i ON i.action_id=a.id
            WHERE a.id=?""", (PRIOR_ACTION,)).fetchone()
    assert tuple(row) == ("complete", "lead_created", LEAD_ID)
    assert gateway.updated is None


def test_candidate_is_isolated_by_thread_user_and_tenant(tmp_path: Path) -> None:
    """A pronoun cannot select a candidate from another thread, user, or workspace."""
    conn, _row = _grant_lead(tmp_path)
    _candidate(conn)
    assert linkedin_candidates.active_candidate(
        conn, 1, "TWORK", "CGRANTS", "1.1", "UCHASE") is not None
    assert linkedin_candidates.active_candidate(
        conn, 1, "OTHER", "CGRANTS", "1.1", "UCHASE") is None
    assert linkedin_candidates.active_candidate(
        conn, 1, "TWORK", "CGRANTS", "2.2", "UCHASE") is None
    assert linkedin_candidates.active_candidate(
        conn, 1, "TWORK", "CGRANTS", "1.1", "UOTHER") is None
