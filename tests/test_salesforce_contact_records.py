"""Contact-record write-gate tests; every test is offline and side-effect free."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import requests

from grant_watch import db
from grant_watch.enrich import salesforce
from grant_watch.enrich import salesforce_campaign_gateway as gateway_mod
from grant_watch.enrich import salesforce_campaigns as campaigns
from grant_watch.enrich import salesforce_contact_records as records
from grant_watch.models import Lead, LeadGrade, RawItem

LEAD_SF_ID = "00Q000000000777"
TASK_ID = "00T000000000001"
ACCOUNT_ID = "001000000000001"
USER_ID = "005000000000001"
CHASE_SLACK_ID = "U01DPJVURHU"


def _link(sobject: str, record_id: str) -> str:
    """Fake Lightning link on the configured test hostname."""
    return f"https://writer.salesforce.test/lightning/r/{sobject}/{record_id}/view"


@dataclass
class FakeGateway:
    """In-memory Salesforce boundary capturing every create for assertions."""

    lead_result: gateway_mod.CreateResult = field(
        default_factory=lambda: gateway_mod.CreateResult(True, LEAD_SF_ID)
    )
    task_result: gateway_mod.CreateResult = field(
        default_factory=lambda: gateway_mod.CreateResult(True, TASK_ID)
    )
    created_leads: list[dict[str, object]] = field(default_factory=list)
    created_tasks: list[dict[str, object]] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)

    def find_active_user_by_email(
        self, email: str
    ) -> list[gateway_mod.SalesforceRecordRef]:
        """Resolve the roster rep to one fake active Salesforce user."""
        if email == "chase@monarchconnected.com":
            return [
                gateway_mod.SalesforceRecordRef(
                    "User", USER_ID, "Chase Gonzales", _link("User", USER_ID)
                )
            ]
        return []

    def get_record(
        self, sobject: str, record_id: str
    ) -> gateway_mod.SalesforceRecordRef:
        """Read back the just-created fake Lead."""
        self.calls.append("get_record")
        return gateway_mod.SalesforceRecordRef(
            sobject, record_id, "Jane Smith", _link(sobject, record_id)
        )

    def create_lead(self, payload: dict[str, object]) -> gateway_mod.CreateResult:
        """Record one Lead create."""
        self.calls.append("create_lead")
        self.created_leads.append(payload)
        if isinstance(self.lead_result, Exception):
            raise self.lead_result
        return self.lead_result

    def create_task(self, payload: dict[str, object]) -> gateway_mod.CreateResult:
        """Record one Task create."""
        self.calls.append("create_task")
        self.created_tasks.append(payload)
        return self.task_result


class TimeoutGateway(FakeGateway):
    """Gateway whose Lead create times out after reaching the network."""

    def create_lead(self, payload: dict[str, object]) -> gateway_mod.CreateResult:
        """Simulate a network timeout after the request reached Salesforce."""
        self.calls.append("create_lead")
        raise requests.Timeout("simulated network timeout")


def _lead_row(
    conn: sqlite3.Connection, item_id: str, entity: str, state: str = "CA"
) -> int:
    """Insert one Grant lead and return its local id."""
    db.upsert_lead(
        conn,
        Lead(
            item=RawItem(
                source="test",
                item_id=item_id,
                title="security award",
                entity=entity,
                state=state,
                program="SVPP",
                amount=500_000,
                start="2026-01-01",
                end="2027-12-31",
                url=f"https://source.test/{item_id}",
                raw={},
            ),
            grade=LeadGrade.GOLD,
        ),
    )
    return int(
        conn.execute(
            "SELECT id FROM leads WHERE source='test' AND source_item_id=?", (item_id,)
        ).fetchone()[0]
    )


def _verified_contact(conn: sqlite3.Connection, lead_id: int, **overrides: str) -> int:
    """Insert one website-verified contact row."""
    values = {
        "name": "Jane Smith",
        "title": "Technology Director",
        "email": "jsmith@alpha.k12.ca.us",
        "phone": "555-123-4567",
        "source_url": "https://alphausd.org/staff",
        "official_domain": "alphausd.org",
    }
    values.update(overrides)
    return db.save_contact(
        conn,
        lead_id,
        values["name"],
        values["title"],
        values["email"],
        values["phone"],
        values["source_url"],
        "high",
        official_domain=values["official_domain"],
    )


def _no_match(*_args: object, **_kwargs: object) -> salesforce.SFResult:
    """A complete Salesforce search that provably found nothing."""
    return salesforce.SFResult(status=salesforce.SFResultStatus.NO_MATCH)


def _found(matches: list[salesforce.SFMatch]) -> object:
    """Build a lookup callable returning FOUND with the given matches."""

    def lookup(*_args: object, **_kwargs: object) -> salesforce.SFResult:
        """Return a canned FOUND lookup result."""
        return salesforce.SFResult(
            status=salesforce.SFResultStatus.FOUND, matches=matches
        )

    return lookup


def _account_match(company: str = "Alpha School District") -> salesforce.SFMatch:
    """One high-confidence Account match bound to the Grant org."""
    return salesforce.SFMatch(
        sobject="Account",
        record_id=ACCOUNT_ID,
        name=company,
        company=company,
        owner="Chase Gonzales",
        link=_link("Account", ACCOUNT_ID),
        confidence="high",
        state="CA",
    )


def _prepare(
    conn: sqlite3.Connection,
    gateway: FakeGateway,
    lead_id: int,
    contact_id: int | None = None,
    lookup: object = _no_match,
) -> campaigns.PreparedAction:
    """Prepare a contact record with the standard test Slack context."""
    return records.prepare_contact_record(
        conn,
        gateway,
        "TWORK",
        "CGRANTS",
        "123.4",
        CHASE_SLACK_ID,
        lead_id,
        contact_id,
        lookup=lookup,  # type: ignore[arg-type]
    )


def _confirm(
    conn: sqlite3.Connection,
    gateway: FakeGateway,
    action: campaigns.PreparedAction,
    requester: str = CHASE_SLACK_ID,
    dry_run: bool = False,
) -> campaigns.ActionExecution:
    """Confirm through the real wrapper so all shared gates run."""
    return campaigns.confirm_action(
        conn,
        gateway,
        action.action_id,
        action.nonce,
        "TWORK",
        "CGRANTS",
        "123.4",
        requester,
        dry_run=dry_run,
    )


@pytest.fixture(autouse=True)
def writer_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure one allowed Slack channel and fake Salesforce hostname."""
    monkeypatch.setenv("GRANT_SALESFORCE_WRITE_CHANNEL_IDS", "CGRANTS")
    monkeypatch.setenv(
        "SALESFORCE_WRITE_MY_DOMAIN_URL", "https://writer.salesforce.test"
    )
    monkeypatch.setenv("SALESFORCE_CAMPAIGN_WRITES_ENABLED", "1")


def test_preview_persists_without_write(tmp_path: Path) -> None:
    """Preparing a record stores an immutable preview and performs no create."""
    conn = db.connect(tmp_path / "t.db")
    lead_id = _lead_row(conn, "a1", "Alpha School District")
    _verified_contact(conn, lead_id)
    gateway = FakeGateway()
    action = _prepare(conn, gateway, lead_id)
    assert gateway.calls == []
    assert "Jane Smith" in action.preview
    assert "jsmith@alpha.k12.ca.us" in action.preview
    assert "Street, PostalCode, Industry: blank" in action.preview
    row = conn.execute("SELECT state,action_type FROM crm_actions").fetchone()
    assert tuple(row) == ("ready", records.ACTION_TYPE)


def test_preview_discloses_blanks_and_never_guesses(tmp_path: Path) -> None:
    """Missing evidence appears as explicit blanks, never invented values."""
    conn = db.connect(tmp_path / "t.db")
    lead_id = _lead_row(conn, "a2", "Beta School District")
    _verified_contact(conn, lead_id, phone="", official_domain="")
    action = _prepare(conn, FakeGateway(), lead_id)
    assert "Phone: blank — not verified" in action.preview
    assert "Website: blank" in action.preview
    payload = __import__("json").loads(
        str(conn.execute("SELECT payload_json FROM crm_actions").fetchone()[0])
    )
    lead_payload = payload["lead"]
    for forbidden in ("Phone", "Website", "Street", "PostalCode", "Industry"):
        assert forbidden not in lead_payload


def test_preview_requires_usable_contact(tmp_path: Path) -> None:
    """No verified/LinkedIn contact (or only not_found) fails closed."""
    conn = db.connect(tmp_path / "t.db")
    lead_id = _lead_row(conn, "a3", "Gamma School District")
    with pytest.raises(ValueError, match="no verified or LinkedIn-sourced contact"):
        _prepare(conn, FakeGateway(), lead_id)
    db.mark_contact_not_found(conn, lead_id)
    with pytest.raises(ValueError, match="no verified or LinkedIn-sourced contact"):
        _prepare(conn, FakeGateway(), lead_id)
    assert conn.execute("SELECT COUNT(*) FROM crm_actions").fetchone()[0] == 0


def test_multiple_verified_contacts_require_contact_id(tmp_path: Path) -> None:
    """Several usable contacts force an explicit choice; wrong ids fail closed."""
    conn = db.connect(tmp_path / "t.db")
    lead_id = _lead_row(conn, "a4", "Delta School District")
    first = _verified_contact(conn, lead_id)
    _verified_contact(conn, lead_id, name="Sam Jones", email="sjones@delta.org")
    with pytest.raises(ValueError, match="specify contact_id"):
        _prepare(conn, FakeGateway(), lead_id)
    with pytest.raises(ValueError, match="not a usable contact"):
        _prepare(conn, FakeGateway(), lead_id, contact_id=99999)
    action = _prepare(conn, FakeGateway(), lead_id, contact_id=first)
    assert "Jane Smith" in action.preview


def test_confirm_creates_lead_then_task_with_whoid(tmp_path: Path) -> None:
    """Happy path: person Lead first, then the activity Task on it."""
    conn = db.connect(tmp_path / "t.db")
    lead_id = _lead_row(conn, "a5", "Alpha School District")
    _verified_contact(conn, lead_id)
    gateway = FakeGateway()
    action = _prepare(conn, gateway, lead_id)
    result = _confirm(conn, gateway, action)
    assert result.state == campaigns.CampaignActionState.COMPLETE
    assert gateway.calls == ["create_lead", "get_record", "create_task"]
    lead_payload = gateway.created_leads[0]
    assert lead_payload["FirstName"] == "Jane"
    assert lead_payload["LastName"] == "Smith"
    assert lead_payload["Email"] == "jsmith@alpha.k12.ca.us"
    assert lead_payload["Title"] == "Technology Director"
    assert lead_payload["Company"] == "Alpha School District"
    assert lead_payload["State"] == "CA"
    assert lead_payload["Website"] == "alphausd.org"
    assert lead_payload["OwnerId"] == USER_ID
    task = gateway.created_tasks[0]
    assert task["WhoId"] == LEAD_SF_ID
    assert task["Subject"] == "Grant AI: record created from grant lead"
    assert "Jane Smith" in str(task["Description"])
    stored = conn.execute("SELECT campaign_id,state FROM crm_actions").fetchone()
    assert tuple(stored) == (LEAD_SF_ID, "complete")


def test_existing_single_high_match_attaches_task_only(tmp_path: Path) -> None:
    """A single confident CRM match gets the Task; no duplicate Lead is created."""
    conn = db.connect(tmp_path / "t.db")
    lead_id = _lead_row(conn, "a6", "Alpha School District")
    _verified_contact(conn, lead_id)
    gateway = FakeGateway()
    action = _prepare(conn, gateway, lead_id, lookup=_found([_account_match()]))
    assert "already in Salesforce" in action.preview
    result = _confirm(conn, gateway, action)
    assert result.state == campaigns.CampaignActionState.COMPLETE
    assert gateway.calls == ["create_task"]
    assert gateway.created_tasks[0]["WhatId"] == ACCOUNT_ID
    assert "WhoId" not in gateway.created_tasks[0]


def test_existing_lead_match_uses_whoid(tmp_path: Path) -> None:
    """Lead/Contact matches attach via WhoId instead of WhatId."""
    conn = db.connect(tmp_path / "t.db")
    lead_id = _lead_row(conn, "a7", "Alpha School District")
    _verified_contact(conn, lead_id)
    match = salesforce.SFMatch(
        sobject="Lead",
        record_id=LEAD_SF_ID,
        name="Alpha School District",
        company="Alpha School District",
        owner="Chase Gonzales",
        link=_link("Lead", LEAD_SF_ID),
        confidence="high",
        state="CA",
    )
    gateway = FakeGateway()
    action = _prepare(conn, gateway, lead_id, lookup=_found([match]))
    _confirm(conn, gateway, action)
    assert gateway.created_tasks[0]["WhoId"] == LEAD_SF_ID


@pytest.mark.parametrize(
    "status",
    [
        salesforce.SFResultStatus.AMBIGUOUS,
        salesforce.SFResultStatus.PARTIAL,
        salesforce.SFResultStatus.UNAVAILABLE,
    ],
)
def test_unprovable_lookup_fails_closed(
    tmp_path: Path, status: salesforce.SFResultStatus
) -> None:
    """Ambiguous/partial/unavailable duplicate checks refuse to create anything."""
    conn = db.connect(tmp_path / "t.db")
    lead_id = _lead_row(conn, "a8", "Alpha School District")
    _verified_contact(conn, lead_id)

    def lookup(*_a: object, **_k: object) -> salesforce.SFResult:
        """Return a canned unprovable lookup result."""
        return salesforce.SFResult(status=status)

    with pytest.raises(ValueError, match="refusing"):
        _prepare(conn, FakeGateway(), lead_id, lookup=lookup)
    assert conn.execute("SELECT COUNT(*) FROM crm_actions").fetchone()[0] == 0


def test_multiple_or_weak_matches_fail_closed(tmp_path: Path) -> None:
    """Two high matches, or only possible ones, are never auto-picked."""
    conn = db.connect(tmp_path / "t.db")
    lead_id = _lead_row(conn, "a9", "Alpha School District")
    _verified_contact(conn, lead_id)
    two = [_account_match(), _account_match()]
    with pytest.raises(ValueError, match="more than one plausible"):
        _prepare(conn, FakeGateway(), lead_id, lookup=_found(two))
    weak = [
        salesforce.SFMatch(
            sobject="Account",
            record_id=ACCOUNT_ID,
            name="Alpha School District",
            company="Alpha School District",
            owner="",
            link="",
            confidence="possible",
        )
    ]
    with pytest.raises(ValueError, match="more than one plausible"):
        _prepare(conn, FakeGateway(), lead_id, lookup=_found(weak))


def test_mismatched_existing_record_fails_closed(tmp_path: Path) -> None:
    """A confident match for a different org never receives the Task."""
    conn = db.connect(tmp_path / "t.db")
    lead_id = _lead_row(conn, "a10", "Alpha School District")
    _verified_contact(conn, lead_id)
    other = _account_match(company="Totally Different Hospital")
    with pytest.raises(ValueError, match="does not provably belong"):
        _prepare(conn, FakeGateway(), lead_id, lookup=_found([other]))


def test_task_failure_after_lead_create_is_partial(tmp_path: Path) -> None:
    """A failed Task after a real Lead reports PARTIAL with the Lead id kept."""
    conn = db.connect(tmp_path / "t.db")
    lead_id = _lead_row(conn, "a11", "Alpha School District")
    _verified_contact(conn, lead_id)
    gateway = FakeGateway(
        task_result=gateway_mod.CreateResult(False, error="FIELD_CUSTOM_VALIDATION")
    )
    action = _prepare(conn, gateway, lead_id)
    result = _confirm(conn, gateway, action)
    assert result.state == campaigns.CampaignActionState.PARTIAL
    assert "Lead is real" in result.message
    stored = conn.execute("SELECT campaign_id,state FROM crm_actions").fetchone()
    assert tuple(stored) == (LEAD_SF_ID, "partial")


def test_timeout_during_lead_create_is_unknown(tmp_path: Path) -> None:
    """A network timeout after submission resolves UNKNOWN and never retries."""
    conn = db.connect(tmp_path / "t.db")
    lead_id = _lead_row(conn, "a12", "Alpha School District")
    _verified_contact(conn, lead_id)
    gateway = TimeoutGateway()
    action = _prepare(conn, gateway, lead_id)
    result = _confirm(conn, gateway, action)
    assert result.state == campaigns.CampaignActionState.UNKNOWN
    with pytest.raises(ValueError, match="already"):
        _confirm(conn, gateway, action)


def test_rerun_same_lead_is_refused(tmp_path: Path) -> None:
    """A completed or pending contact record blocks a second one for the lead."""
    conn = db.connect(tmp_path / "t.db")
    lead_id = _lead_row(conn, "a13", "Alpha School District")
    _verified_contact(conn, lead_id)
    gateway = FakeGateway()
    action = _prepare(conn, gateway, lead_id)
    with pytest.raises(ValueError, match="already exists or is pending"):
        _prepare(conn, gateway, lead_id)
    _confirm(conn, gateway, action)
    with pytest.raises(ValueError, match="already exists or is pending"):
        _prepare(conn, gateway, lead_id)


def test_expiry_requester_and_flag_gates(tmp_path: Path) -> None:
    """Expired previews, foreign requesters, and disabled writes fail closed."""
    conn = db.connect(tmp_path / "t.db")
    lead_id = _lead_row(conn, "a14", "Alpha School District")
    _verified_contact(conn, lead_id)
    gateway = FakeGateway()
    action = _prepare(conn, gateway, lead_id)
    with pytest.raises(PermissionError, match="initiating user"):
        _confirm(conn, gateway, action, requester="UOTHER")
    with conn:
        conn.execute(
            "UPDATE crm_actions SET expires_at='2020-01-01T00:00:00+00:00' WHERE id=?",
            (action.action_id,),
        )
    with pytest.raises(TimeoutError):
        _confirm(conn, gateway, action)
    assert gateway.calls == []


def test_disabled_writes_flag_blocks_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The feature flag stops execution after approval with zero gateway calls."""
    conn = db.connect(tmp_path / "t.db")
    lead_id = _lead_row(conn, "a15", "Alpha School District")
    _verified_contact(conn, lead_id)
    gateway = FakeGateway()
    action = _prepare(conn, gateway, lead_id)
    monkeypatch.setenv("SALESFORCE_CAMPAIGN_WRITES_ENABLED", "0")
    result = _confirm(conn, gateway, action)
    assert result.state == campaigns.CampaignActionState.FAILED
    assert "disabled" in result.message
    assert gateway.calls == []


def test_gateway_task_allowlist_and_prefix() -> None:
    """Task is creatable and validated; other objects stay forbidden."""
    gateway_mod.validate_record_id(TASK_ID, "Task")
    gateway = gateway_mod.SalesforceCampaignGateway()
    with pytest.raises(ValueError, match="create forbidden"):
        gateway._create_one("Account", {"Name": "nope"})


def test_linkedin_only_contact_builds_emailless_lead(tmp_path: Path) -> None:
    """A LinkedIn person becomes a Lead with no email and honest evidence text."""
    conn = db.connect(tmp_path / "t.db")
    lead_id = _lead_row(conn, "a16", "Alpha School District")
    db.save_linkedin_contact(
        conn, lead_id, "Joshua Ihrig", "Information Systems",
        "https://www.linkedin.com/in/joshuaihrig",
    )
    gateway = FakeGateway()
    action = _prepare(conn, gateway, lead_id)
    assert "LinkedIn profile (ownership not verified)" in action.preview
    assert "Email: blank" in action.preview
    result = _confirm(conn, gateway, action)
    assert result.state == campaigns.CampaignActionState.COMPLETE
    payload = gateway.created_leads[0]
    assert "Email" not in payload and "Phone" not in payload
    assert payload["FirstName"] == "Joshua"
    assert payload["LastName"] == "Ihrig"
    assert "linkedin.com/in/joshuaihrig" in str(payload["Description"])


def test_verified_contact_preferred_over_linkedin(tmp_path: Path) -> None:
    """When both evidence classes exist, the website-verified contact wins."""
    conn = db.connect(tmp_path / "t.db")
    lead_id = _lead_row(conn, "a17", "Alpha School District")
    db.save_linkedin_contact(
        conn, lead_id, "Joshua Ihrig", "IT", "https://www.linkedin.com/in/x"
    )
    _verified_contact(conn, lead_id)
    action = _prepare(conn, FakeGateway(), lead_id)
    assert "Jane Smith" in action.preview
    assert "jsmith@alpha.k12.ca.us" in action.preview


def test_contact_record_tool_schema_exposed() -> None:
    """The Slack tool schema exists with lead_id as its only required input."""
    from grant_watch.slack import tools

    schema = next(
        s for s in tools.TOOL_SCHEMAS
        if s["name"] == "salesforce_contact_record_preview"
    )
    assert schema["input_schema"]["required"] == ["lead_id"]


def test_find_person_linkedin_persists_with_lead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The LinkedIn tool saves a linkedin_only contact when bound to a real lead."""
    from grant_watch.enrich import finder
    from grant_watch.slack import tools

    conn = db.connect(tmp_path / "t.db")
    lead_id = _lead_row(conn, "a18", "Provo City")
    monkeypatch.setattr(db, "connect", lambda *a, **k: conn)
    monkeypatch.setattr(
        finder,
        "linkedin_person",
        lambda *a, **k: {
            "name": "Joshua Ihrig",
            "title": "Information Systems",
            "url": "https://www.linkedin.com/in/joshuaihrig",
        },
    )
    text = tools.find_person_linkedin("Provo City", "UT", lead_id=lead_id)
    assert f"Saved as contact" in text
    row = conn.execute(
        "SELECT name,contact_status,email FROM contacts WHERE lead_id=?", (lead_id,)
    ).fetchone()
    assert tuple(row) == ("Joshua Ihrig", "linkedin_only", None)
