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
    note_result: gateway_mod.CreateResult = field(
        default_factory=lambda: gateway_mod.CreateResult(True, "002000000000001")
    )
    created_leads: list[dict[str, object]] = field(default_factory=list)
    created_notes: list[dict[str, object]] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)

    def lead_record_type_id(self, developer_name: str) -> str:
        """Resolve a deterministic fake Lead RecordType id."""
        return "0122M000000viFyQAI" if developer_name == "Verkada" else ""

    def create_content_note(
        self, parent_id: str, title: str, body_html: str
    ) -> gateway_mod.CreateResult:
        """Record one Lightning ContentNote create and its link target."""
        self.calls.append("create_content_note")
        self.created_notes.append(
            {"ParentId": parent_id, "Title": title, "Content": body_html}
        )
        return self.note_result

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
    blanks_line = action.preview.split("Blank (no verified source):")[1]
    assert "Street" in blanks_line  # blank fields are named on one compact line
    assert "Record Type (Verkada): set" in action.preview
    assert "Plus a Note with the grant context" in action.preview
    row = conn.execute("SELECT state,action_type FROM crm_actions").fetchone()
    assert tuple(row) == ("ready", records.ACTION_TYPE)


def test_preview_discloses_blanks_and_never_guesses(tmp_path: Path) -> None:
    """Missing evidence appears as explicit blanks, never invented values."""
    conn = db.connect(tmp_path / "t.db")
    lead_id = _lead_row(conn, "a2", "Beta School District")
    _verified_contact(conn, lead_id, phone="", official_domain="")
    action = _prepare(conn, FakeGateway(), lead_id)
    blanks_line = action.preview.split("Blank (no verified source):")[1]
    assert "Phone" in blanks_line and "Website" in blanks_line
    payload = __import__("json").loads(
        str(conn.execute("SELECT payload_json FROM crm_actions").fetchone()[0])
    )
    lead_payload = payload["lead"]
    # No verified evidence → these keys are omitted entirely, never guessed.
    for forbidden in ("Phone", "Website", "Street", "PostalCode", "MobilePhone"):
        assert forbidden not in lead_payload
    # Industry is a classification from the school name, not invented data.
    assert lead_payload["Industry"] == "K-12 Schools"


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


def test_confirm_creates_lead_then_note(tmp_path: Path) -> None:
    """Happy path: person Lead first, then the grant Note on it — no Task."""
    conn = db.connect(tmp_path / "t.db")
    lead_id = _lead_row(conn, "a5", "Alpha School District")
    _verified_contact(conn, lead_id)
    gateway = FakeGateway()
    action = _prepare(conn, gateway, lead_id)
    result = _confirm(conn, gateway, action)
    assert result.state == campaigns.CampaignActionState.COMPLETE
    assert gateway.calls == [
        "create_lead",
        "get_record",
        "create_content_note",
    ]
    assert "create_task" not in gateway.calls
    lead_payload = gateway.created_leads[0]
    assert lead_payload["FirstName"] == "Jane"
    assert lead_payload["LastName"] == "Smith"
    assert lead_payload["Email"] == "jsmith@alpha.k12.ca.us"
    assert lead_payload["Title"] == "Technology Director"
    assert lead_payload["Company"] == "Alpha School District"
    assert lead_payload["State"] == "CA"
    assert lead_payload["Website"] == "https://alphausd.org"
    assert lead_payload["OwnerId"] == USER_ID
    note = gateway.created_notes[0]
    assert note["ParentId"] == LEAD_SF_ID
    assert "Jane Smith" in str(note["Content"])
    stored = conn.execute("SELECT campaign_id,state FROM crm_actions").fetchone()
    assert tuple(stored) == (LEAD_SF_ID, "complete")
    # The success message carries a clickable link to the record, not a raw id
    # (Chase 2026-07-18), and never mentions a Task.
    assert f"<{_link('Lead', LEAD_SF_ID)}|" in result.message
    assert f"(id {LEAD_SF_ID})" not in result.message
    assert "Task" not in result.message
    assert "Note" in result.message


def test_existing_single_high_match_attaches_note_only(tmp_path: Path) -> None:
    """A single confident CRM match gets a Note; no duplicate Lead is created."""
    conn = db.connect(tmp_path / "t.db")
    lead_id = _lead_row(conn, "a6", "Alpha School District")
    _verified_contact(conn, lead_id)
    gateway = FakeGateway()
    action = _prepare(conn, gateway, lead_id, lookup=_found([_account_match()]))
    assert "already in Salesforce" in action.preview
    result = _confirm(conn, gateway, action)
    assert result.state == campaigns.CampaignActionState.COMPLETE
    assert gateway.calls == ["create_content_note"]
    assert gateway.created_notes[0]["ParentId"] == ACCOUNT_ID
    assert "no duplicate Lead" in result.message
    assert "Task" not in result.message


def test_existing_lead_match_attaches_note_to_lead(tmp_path: Path) -> None:
    """A Lead/Contact match gets the Note attached to that record's id."""
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
    assert gateway.created_notes[0]["ParentId"] == LEAD_SF_ID


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
    # A single low-confidence ("possible") match is zero HIGH matches — the message
    # must say so, not falsely claim "more than one" (still fail-closed either way).
    with pytest.raises(ValueError, match="none a confident single record"):
        _prepare(conn, FakeGateway(), lead_id, lookup=_found(weak))


def test_mismatched_existing_record_fails_closed(tmp_path: Path) -> None:
    """A confident match for a different org never receives the Note."""
    conn = db.connect(tmp_path / "t.db")
    lead_id = _lead_row(conn, "a10", "Alpha School District")
    _verified_contact(conn, lead_id)
    other = _account_match(company="Totally Different Hospital")
    with pytest.raises(ValueError, match="does not provably belong"):
        _prepare(conn, FakeGateway(), lead_id, lookup=_found([other]))


def test_note_failure_after_lead_create_is_partial(tmp_path: Path) -> None:
    """A failed Note after a real Lead reports PARTIAL with the Lead id kept."""
    conn = db.connect(tmp_path / "t.db")
    lead_id = _lead_row(conn, "a11", "Alpha School District")
    _verified_contact(conn, lead_id)
    gateway = FakeGateway(
        note_result=gateway_mod.CreateResult(False, error="FIELD_CUSTOM_VALIDATION")
    )
    action = _prepare(conn, gateway, lead_id)
    result = _confirm(conn, gateway, action)
    assert result.state == campaigns.CampaignActionState.PARTIAL
    assert "Lead is real" in result.message
    # The rep still gets a clickable link even when the Note fails.
    assert f"<{_link('Lead', LEAD_SF_ID)}|" in result.message
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


@pytest.mark.parametrize(
    "name, first, last",
    [
        ("Mr. Joel Padgett", "Joel", "Padgett"),
        ("Dr. Harry D. Smith", "Harry D.", "Smith"),
        ("Mrs Jane Doe", "Jane", "Doe"),
        ("Joel Padgett", "Joel", "Padgett"),  # no honorific, unchanged
        ("Dr. Smith", "", "Smith"),  # honorific + single name → blank first
        ("Padgett", "", "Padgett"),  # single token stays the last name
    ],
)
def test_split_person_name_drops_honorifics(name: str, first: str, last: str) -> None:
    """A leading honorific never leaks into FirstName (live: 'Mr. Joel' bug)."""
    assert records.split_person_name(name) == (first, last)


def test_gateway_forbids_task_and_allows_note() -> None:
    """Grant cannot create a Task (Chase: no tasks); ContentNote stays allowed."""
    assert "Task" not in gateway_mod._ALLOWED_CREATE_OBJECTS
    assert "ContentNote" in gateway_mod._ALLOWED_CREATE_OBJECTS
    assert not hasattr(gateway_mod.SalesforceCampaignGateway, "create_task")
    gateway = gateway_mod.SalesforceCampaignGateway()
    with pytest.raises(ValueError, match="create forbidden"):
        gateway._create_one("Task", {"Subject": "nope"})
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
    assert "Email" in action.preview.split("Blank (no verified source):")[1]
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
    """The tool schema accepts a lead_id OR an entity name (no hard-required id)."""
    from grant_watch.slack import tools

    schema = next(
        s for s in tools.TOOL_SCHEMAS
        if s["name"] == "salesforce_contact_record_preview"
    )
    props = schema["input_schema"]["properties"]
    assert "lead_id" in props and "entity" in props and "state" in props
    # lead_id is no longer hard-required — a natural "add <person> to Salesforce"
    # resolves the lead from the org name instead of demanding a number.
    assert "required" not in schema["input_schema"]


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


def test_org_name_masquerading_as_title_is_dropped(tmp_path: Path) -> None:
    """A LinkedIn 'title' equal to the org name never becomes a CRM Title."""
    conn = db.connect(tmp_path / "t.db")
    lead_id = _lead_row(conn, "a19", "Chicago Jewish Day School", "IL")
    db.save_linkedin_contact(
        conn, lead_id, "Richard Moline", "Chicago Jewish Day School",
        "https://www.linkedin.com/in/richard-moline",
    )
    gateway = FakeGateway()
    action = _prepare(conn, gateway, lead_id)
    assert "Title" in action.preview.split("Blank (no verified source):")[1]
    _confirm(conn, gateway, action)
    assert "Title" not in gateway.created_leads[0]
    # With no verified title, the Note describes them by org, not a bogus title.
    assert "contact at Chicago Jewish Day School" in str(
        gateway.created_notes[0]["Content"]
    )


class ScopeRefusedGateway(FakeGateway):
    """Gateway whose create_lead fails the write-scope check before any POST."""

    def create_lead(self, payload: dict[str, object]) -> gateway_mod.CreateResult:
        """Reject at scope verification exactly as verify_write_scope does."""
        self.calls.append("create_lead")
        raise PermissionError("SALESFORCE_WRITE_ORG_ID is not configured")


def test_write_scope_refusal_resolves_failed_not_stranded(tmp_path: Path) -> None:
    """A pre-POST scope refusal ends FAILED so the lead is not blocked forever."""
    conn = db.connect(tmp_path / "t.db")
    lead_id = _lead_row(conn, "a20", "Alpha School District")
    _verified_contact(conn, lead_id)
    gateway = ScopeRefusedGateway()
    action = _prepare(conn, gateway, lead_id)
    result = _confirm(conn, gateway, action)
    assert result.state == campaigns.CampaignActionState.FAILED
    assert "was not changed" in result.message
    state = conn.execute("SELECT state FROM crm_actions").fetchone()[0]
    assert state == "failed"
    # The re-run guard must now release so the rep can retry once config is fixed.
    action2 = _prepare(conn, FakeGateway(), lead_id)
    assert action2.action_id != action.action_id


def test_full_field_lead_payload_from_org_profile(tmp_path: Path) -> None:
    """A lead with an org profile + NCES enrollment maps every available field."""
    conn = db.connect(tmp_path / "t.db")
    lead_id = _lead_row(conn, "f1", "Alpha School District")
    _verified_contact(conn, lead_id, phone="", email="")
    from grant_watch.enrich.organization_profile import OrgProfile

    db.save_org_profile(
        conn,
        lead_id,
        OrgProfile(
            website="https://alphausd.org",
            general_email="info@alphausd.org",
            phone="555-999-1000",
            street="1 Alpha Way",
            city="Sacramento",
            state="CA",
            postal_code="95814",
            source_url="https://alphausd.org/contact",
            status="found",
        ),
    )
    conn.execute("UPDATE leads SET enrollment=4200 WHERE id=?", (lead_id,))
    conn.commit()
    gateway = FakeGateway()
    action = _prepare(conn, gateway, lead_id)
    payload = __import__("json").loads(
        str(conn.execute("SELECT payload_json FROM crm_actions").fetchone()[0])
    )["lead"]
    assert payload["Website"] == "https://alphausd.org"
    assert payload["Email"] == "info@alphausd.org"  # general email, no direct
    assert payload["Phone"] == "555-999-1000"
    assert payload["Street"] == "1 Alpha Way"
    assert payload["City"] == "Sacramento"
    assert payload["PostalCode"] == "95814"
    assert payload["Number_of_Students__c"] == 4200
    assert payload["Industry"] == "K-12 Schools"
    assert payload["RecordTypeId"] == "0122M000000viFyQAI"
    assert "Email (org general — not the individual's)" in action.preview
    # The Note carries the general-vs-direct distinction honestly.
    note = __import__("json").loads(
        str(conn.execute("SELECT payload_json FROM crm_actions").fetchone()[0])
    )["note"]
    body = str(note["Body"])
    assert "info@alphausd.org" in body
    assert "organization general address" in body.lower()
    assert "was not found" in body.lower()


def test_note_lands_on_the_new_lead(tmp_path: Path) -> None:
    """The grant Note is linked to the freshly-created Lead record."""
    conn = db.connect(tmp_path / "t.db")
    lead_id = _lead_row(conn, "f2", "Alpha School District")
    _verified_contact(conn, lead_id)
    gateway = FakeGateway()
    action = _prepare(conn, gateway, lead_id)
    _confirm(conn, gateway, action)
    assert gateway.created_notes[0]["ParentId"] == LEAD_SF_ID
    assert "Alpha School District" in str(gateway.created_notes[0]["Title"])


def test_note_body_reads_like_a_lead_briefing(tmp_path: Path) -> None:
    """The Lightning Note leads with why-this-lead-is-here, then the contact facts."""
    from grant_watch.enrich.organization_profile import OrgProfile

    conn = db.connect(tmp_path / "t.db")
    lead_id = _lead_row(conn, "f3", "Alpha School District")
    _verified_contact(conn, lead_id)
    db.save_org_profile(
        conn,
        lead_id,
        OrgProfile(
            website="https://alphausd.org",
            phone="555-999-1000",
            street="1 Alpha Way",
            city="Sacramento",
            state="CA",
            postal_code="95814",
            source_url="https://alphausd.org/contact",
            status="found",
        ),
    )
    gateway = FakeGateway()
    action = _prepare(conn, gateway, lead_id)
    _confirm(conn, gateway, action)
    stored = __import__("json").loads(
        str(conn.execute("SELECT payload_json FROM crm_actions").fetchone()[0])
    )["note"]
    body = str(stored["Body"])
    # Why this lead is here — program, amount, human spend window, and the Lead #.
    assert (
        f"Alpha School District — Lead #{lead_id} — SVPP · $500,000 · "
        "spend window Jan 2026 – Dec 2027" in body
    )
    assert "• Lead: Jane Smith, Technology Director at Alpha School District" in body
    assert "• Email: jsmith@alpha.k12.ca.us (direct, verified)" in body
    assert "• Phone: 555-123-4567" in body
    assert "• Address: 1 Alpha Way, Sacramento, CA 95814" in body
    # The confirmed ContentNote stores escaped HTML, not raw text.
    content = str(gateway.created_notes[0]["Content"])
    assert content.startswith("<p>") and "<br/>" in content
    assert "Lead #" in content


def test_writer_credentials_fall_back_to_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    """One Connected App for read+write is valid (Chase): the writer client id/secret and
    My Domain default to the READER's when no separate SALESFORCE_WRITE_* app is set — no
    env duplication — while a distinct writer app still takes precedence when configured."""
    for key in (
        "SALESFORCE_WRITE_CLIENT_ID",
        "SALESFORCE_WRITE_CLIENT_SECRET",
        "SALESFORCE_WRITE_MY_DOMAIN_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SALESFORCE_CLIENT_ID", "reader-id")
    monkeypatch.setenv("SALESFORCE_CLIENT_SECRET", "reader-secret")
    monkeypatch.setenv("SALESFORCE_MY_DOMAIN_URL", "https://acme.my.salesforce.com")

    assert gateway_mod._write_client_id() == "reader-id"
    assert gateway_mod._write_client_secret() == "reader-secret"
    assert gateway_mod._write_my_domain() == "https://acme.my.salesforce.com"

    # A dedicated writer app, when set, overrides the reader fallback per-field.
    monkeypatch.setenv("SALESFORCE_WRITE_CLIENT_ID", "writer-id")
    monkeypatch.setenv("SALESFORCE_WRITE_MY_DOMAIN_URL", "https://writer.my.salesforce.com")
    assert gateway_mod._write_client_id() == "writer-id"
    assert gateway_mod._write_my_domain() == "https://writer.my.salesforce.com"
    assert gateway_mod._write_client_secret() == "reader-secret"  # unset -> still reader
