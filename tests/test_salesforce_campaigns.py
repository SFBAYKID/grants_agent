"""Salesforce Campaign write-gate tests; every test is offline and side-effect free."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import requests

from grant_watch import db
from grant_watch.enrich import salesforce_campaigns as campaigns
from grant_watch.enrich import salesforce_campaign_gateway as gateway_mod
from grant_watch.models import Lead, LeadGrade, RawItem

CAMPAIGN_ID = "701000000000001"
LEAD_ID = "00Q000000000001"
CONTACT_ID = "003000000000001"


@dataclass
class FakeGateway:
    """Deterministic in-memory Salesforce boundary for approval/execution tests."""

    people: dict[str, list[campaigns.SalesforceRecordRef]] = field(default_factory=dict)
    status_exists: bool = True
    existing: set[str] = field(default_factory=set)
    member_results: list[gateway_mod.CreateResult] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)

    def campaign_picklists(self) -> tuple[set[str], set[str]]:
        """Return the sandbox-verified defaults used by Grant-created Campaigns."""
        return {"Other", "Event"}, {"Planned", "In Progress"}

    def search_campaigns(self, _name: str) -> list[campaigns.SalesforceRecordRef]:
        """Return no campaigns unless a test supplies a direct ref."""
        return []

    def get_record(self, sobject: str, record_id: str) -> campaigns.SalesforceRecordRef:
        """Read back a known fake record."""
        if sobject == "Campaign":
            return campaigns.SalesforceRecordRef(
                "Campaign", record_id, "Grant QA", campaigns_link("Campaign", record_id))
        return campaigns.SalesforceRecordRef(
            sobject, record_id, "Known Person", campaigns_link(sobject, record_id),
            company="Alpha School District", state="CA",
        )

    def find_people(self, entity_name: str,
                    _state: str) -> list[campaigns.SalesforceRecordRef]:
        """Return exact configured people for an organization."""
        return self.people.get(entity_name, [])

    def member_status_exists(self, _campaign_id: str) -> bool:
        """Return whether the honest status already exists."""
        return self.status_exists

    def create_member_status(self, _campaign_id: str) -> gateway_mod.CreateResult:
        """Record creation of the disclosed non-response status."""
        self.calls.append("create_status")
        return gateway_mod.CreateResult(True, "01Y000000000001")

    def existing_members(self, _campaign_id: str, _ids: list[str]) -> set[str]:
        """Return preconfigured duplicate member IDs."""
        return set(self.existing)

    def create_campaign(self, _payload: dict[str, object]) -> gateway_mod.CreateResult:
        """Record one Campaign create."""
        self.calls.append("create_campaign")
        return gateway_mod.CreateResult(True, CAMPAIGN_ID)

    def create_leads(self, payloads: list[dict[str, object]]) -> list[gateway_mod.CreateResult]:
        """Return one unique Salesforce Lead ID per approved org payload."""
        self.calls.append("create_leads")
        return [gateway_mod.CreateResult(True, f"00Q0000000000{index:02d}")
                for index, _payload in enumerate(payloads, start=10)]

    def create_members(self, payloads: list[dict[str, object]]) -> list[gateway_mod.CreateResult]:
        """Return configured partial results or all successful member creates."""
        self.calls.append("create_members")
        if self.member_results:
            return self.member_results
        return [gateway_mod.CreateResult(True, f"00v0000000000{index:02d}")
                for index, _payload in enumerate(payloads, start=10)]


def campaigns_link(sobject: str, record_id: str) -> str:
    """Build a fake Lightning link using the configured test hostname."""
    return f"https://writer.salesforce.test/lightning/r/{sobject}/{record_id}/view"


def _lead(conn: sqlite3.Connection, item_id: str, entity: str,
          state: str = "CA") -> int:
    """Insert one Grant organization and return its local lead ID."""
    db.upsert_lead(conn, Lead(
        item=RawItem(
            source="test", item_id=item_id, title="security award", entity=entity,
            state=state, program="SVPP", amount=100_000, start="2026-01-01",
            end="2027-12-31", url=f"https://source.test/{item_id}", raw={},
        ),
        grade=LeadGrade.GOLD,
    ))
    return int(conn.execute(
        "SELECT id FROM leads WHERE source='test' AND source_item_id=?", (item_id,)
    ).fetchone()[0])


@pytest.fixture(autouse=True)
def writer_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure one allowed Slack channel and fake Salesforce hostname."""
    monkeypatch.setenv("GRANT_SALESFORCE_WRITE_CHANNEL_IDS", "CGRANTS")
    monkeypatch.setenv("SALESFORCE_WRITE_MY_DOMAIN_URL", "https://writer.salesforce.test")
    monkeypatch.setenv("SALESFORCE_CAMPAIGN_WRITES_ENABLED", "1")
    monkeypatch.setenv("SALESFORCE_PERSON_LEAD_WRITES_ENABLED", "1")


def _verified_contact(conn: sqlite3.Connection, lead_id: int,
                      evidence: dict[str, bool] | None = None) -> int:
    """Persist one official-page contact for standalone Lead tests."""
    return db.save_contact(
        conn, lead_id, "Andrew Popp", "Principal", "andrew@district.test", "5551212",
        "https://district.test/staff", "high", "district.test",
        evidence if evidence is not None else {
            "name": True, "email": True, "title": True, "phone": True})


def test_record_links_validate_host_object_and_prefix() -> None:
    """Pasted links cannot cross orgs or smuggle an Account as a Campaign Member."""
    assert campaigns.parse_record_link(
        campaigns_link("Campaign", CAMPAIGN_ID), {"Campaign"}
    ) == ("Campaign", CAMPAIGN_ID)
    with pytest.raises(ValueError, match="configured Salesforce org"):
        campaigns.parse_record_link(
            f"https://evil.test/lightning/r/Campaign/{CAMPAIGN_ID}/view", {"Campaign"})
    with pytest.raises(ValueError, match="cannot be used"):
        campaigns.parse_record_link(
            campaigns_link("Account", "001000000000001"), {"Lead", "Contact"})


def test_campaign_preview_is_persisted_without_create(tmp_path: Path) -> None:
    """Preparing a Campaign records an immutable preview but performs no write."""
    conn = db.connect(tmp_path / "t.db")
    gateway = FakeGateway()
    action = campaigns.prepare_campaign_creation(
        conn, gateway, "TWORK", "CGRANTS", "123.4", "UCHASE",
        campaigns.CampaignDraft("FY27 Grant Leads"),
    )
    assert "FY27 Grant Leads" in action.preview
    assert gateway.calls == []
    row = conn.execute("SELECT state,payload_hash FROM crm_actions").fetchone()
    assert tuple(row) == ("ready", row["payload_hash"])


def test_only_initiator_can_confirm(tmp_path: Path) -> None:
    """Another channel member cannot execute someone else's approved preview."""
    conn = db.connect(tmp_path / "t.db")
    action = campaigns.prepare_campaign_creation(
        conn, FakeGateway(), "TWORK", "CGRANTS", "123.4", "UCHASE",
        campaigns.CampaignDraft("FY27 Grant Leads"),
    )
    with pytest.raises(PermissionError, match="initiating user"):
        campaigns.confirm_action(
            conn, FakeGateway(), action.action_id, action.nonce,
            "TWORK", "CGRANTS", "123.4", "UOTHER", dry_run=True,
        )


def test_confirmation_rejects_wrong_channel_nonce_and_tampered_payload(
        tmp_path: Path) -> None:
    """Every immutable approval binding fails closed before gateway writes."""
    conn = db.connect(tmp_path / "t.db")
    action = campaigns.prepare_campaign_creation(
        conn, FakeGateway(), "TWORK", "CGRANTS", "123.4", "UCHASE",
        campaigns.CampaignDraft("FY27 Grant Leads"),
    )
    with pytest.raises(PermissionError, match="context"):
        campaigns.confirm_action(
            conn, FakeGateway(), action.action_id, action.nonce,
            "TWORK", "COTHER", "123.4", "UCHASE", dry_run=True)
    with pytest.raises(PermissionError, match="thread"):
        campaigns.confirm_action(
            conn, FakeGateway(), action.action_id, action.nonce,
            "TWORK", "CGRANTS", "999.9", "UCHASE", dry_run=True)
    with pytest.raises(PermissionError, match="token"):
        campaigns.confirm_action(
            conn, FakeGateway(), action.action_id, "wrong",
            "TWORK", "CGRANTS", "123.4", "UCHASE", dry_run=True)
    conn.execute(
        "UPDATE crm_actions SET payload_json='{}' WHERE id=?", (action.action_id,))
    conn.commit()
    with pytest.raises(ValueError, match="payload changed"):
        campaigns.confirm_action(
            conn, FakeGateway(), action.action_id, action.nonce,
            "TWORK", "CGRANTS", "123.4", "UCHASE", dry_run=True)


def test_confirmation_rejects_tampered_member_mapping(tmp_path: Path) -> None:
    """Changing one frozen organization operation invalidates the final approval."""
    conn = db.connect(tmp_path / "t.db")
    lead_id = _lead(conn, "A1", "Alpha School District")
    gateway = FakeGateway()
    action = campaigns.prepare_membership(
        conn, gateway, "TWORK", "CGRANTS", "123.4", "UCHASE",
        gateway.get_record("Campaign", CAMPAIGN_ID), [lead_id],
        allow_org_leads=True,
    )
    conn.execute(
        "UPDATE crm_action_items SET operation='existing_record' WHERE action_id=?",
        (action.action_id,))
    conn.commit()
    with pytest.raises(ValueError, match="item mapping changed"):
        campaigns.confirm_action(
            conn, gateway, action.action_id, action.nonce,
            "TWORK", "CGRANTS", "123.4", "UCHASE", dry_run=True)


def test_expired_preview_is_persisted_expired_without_write(tmp_path: Path) -> None:
    """A stale Slack button cannot execute and leaves an auditable terminal state."""
    conn = db.connect(tmp_path / "t.db")
    gateway = FakeGateway()
    action = campaigns.prepare_campaign_creation(
        conn, gateway, "TWORK", "CGRANTS", "123.4", "UCHASE",
        campaigns.CampaignDraft("FY27 Grant Leads"),
    )
    conn.execute(
        "UPDATE crm_actions SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
        (action.action_id,),
    )
    conn.commit()
    with pytest.raises(TimeoutError, match="expired"):
        campaigns.confirm_action(
            conn, gateway, action.action_id, action.nonce,
            "TWORK", "CGRANTS", "123.4", "UCHASE")
    assert gateway.calls == []
    assert conn.execute(
        "SELECT state FROM crm_actions WHERE id=?", (action.action_id,)
    ).fetchone()["state"] == "expired"


def test_dry_run_proves_zero_gateway_writes(tmp_path: Path) -> None:
    """Final approval can be exercised without calling a Salesforce create method."""
    conn = db.connect(tmp_path / "t.db")
    gateway = FakeGateway()
    action = campaigns.prepare_campaign_creation(
        conn, gateway, "TWORK", "CGRANTS", "123.4", "UCHASE",
        campaigns.CampaignDraft("FY27 Grant Leads"),
    )
    result = campaigns.confirm_action(
        conn, gateway, action.action_id, action.nonce,
        "TWORK", "CGRANTS", "123.4", "UCHASE", dry_run=True,
    )
    assert result.state is campaigns.CampaignActionState.DRY_RUN
    assert gateway.calls == []


def test_disabled_writer_fails_without_gateway_write(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The default-off feature flag remains the final external-write circuit breaker."""
    conn = db.connect(tmp_path / "t.db")
    gateway = FakeGateway()
    action = campaigns.prepare_campaign_creation(
        conn, gateway, "TWORK", "CGRANTS", "123.4", "UCHASE",
        campaigns.CampaignDraft("FY27 Grant Leads"),
    )
    monkeypatch.setenv("SALESFORCE_CAMPAIGN_WRITES_ENABLED", "0")
    result = campaigns.confirm_action(
        conn, gateway, action.action_id, action.nonce,
        "TWORK", "CGRANTS", "123.4", "UCHASE")
    assert result.state is campaigns.CampaignActionState.FAILED
    assert gateway.calls == []


def test_campaign_create_requires_readback_and_records_success(tmp_path: Path) -> None:
    """A successful create is not reported until Salesforce returns the record."""
    conn = db.connect(tmp_path / "t.db")
    gateway = FakeGateway()
    action = campaigns.prepare_campaign_creation(
        conn, gateway, "TWORK", "CGRANTS", "123.4", "UCHASE",
        campaigns.CampaignDraft("FY27 Grant Leads"),
    )
    result = campaigns.confirm_action(
        conn, gateway, action.action_id, action.nonce,
        "TWORK", "CGRANTS", "123.4", "UCHASE")
    assert result.state is campaigns.CampaignActionState.COMPLETE
    assert result.campaign_id == CAMPAIGN_ID
    assert gateway.calls == ["create_campaign"]


def test_campaign_create_timeout_is_unknown_and_not_retried(tmp_path: Path) -> None:
    """An indeterminate create never becomes success or an automatic second create."""
    conn = db.connect(tmp_path / "t.db")

    class TimeoutGateway(FakeGateway):
        """Raise after the approval boundary as a real HTTP timeout would."""

        def create_campaign(self, _payload: dict[str, object]
                            ) -> gateway_mod.CreateResult:
            self.calls.append("create_campaign")
            raise requests.Timeout("ambiguous")

    gateway = TimeoutGateway()
    action = campaigns.prepare_campaign_creation(
        conn, gateway, "TWORK", "CGRANTS", "123.4", "UCHASE",
        campaigns.CampaignDraft("FY27 Grant Leads"),
    )
    result = campaigns.confirm_action(
        conn, gateway, action.action_id, action.nonce,
        "TWORK", "CGRANTS", "123.4", "UCHASE")
    assert result.state is campaigns.CampaignActionState.UNKNOWN
    assert gateway.calls == ["create_campaign"]


def test_campaign_readback_failure_after_create_is_unknown(tmp_path: Path) -> None:
    """A confirmed create followed by a GET failure is never reported as no write."""
    conn = db.connect(tmp_path / "t.db")

    class ReadbackFailureGateway(FakeGateway):
        """Create successfully, then fail only when reading the new Campaign back."""

        created = False

        def create_campaign(self, payload: dict[str, object]
                            ) -> gateway_mod.CreateResult:
            """Record the successful create boundary."""
            self.created = True
            return super().create_campaign(payload)

        def get_record(self, sobject: str,
                       record_id: str) -> campaigns.SalesforceRecordRef:
            """Fail the post-create readback while allowing preview preparation."""
            if self.created and sobject == "Campaign":
                raise requests.ConnectionError("readback unavailable")
            return super().get_record(sobject, record_id)

    gateway = ReadbackFailureGateway()
    action = campaigns.prepare_campaign_creation(
        conn, gateway, "TWORK", "CGRANTS", "123.4", "UCHASE",
        campaigns.CampaignDraft("FY27 Grant Leads"),
    )
    result = campaigns.confirm_action(
        conn, gateway, action.action_id, action.nonce,
        "TWORK", "CGRANTS", "123.4", "UCHASE")
    assert result.state is campaigns.CampaignActionState.UNKNOWN
    row = conn.execute(
        "SELECT campaign_id,external_write_started FROM crm_actions WHERE id=?",
        (action.action_id,),
    ).fetchone()
    assert tuple(row) == (CAMPAIGN_ID, 1)
    with pytest.raises(ValueError, match="already unknown"):
        campaigns.confirm_action(
            conn, gateway, action.action_id, action.nonce,
            "TWORK", "CGRANTS", "123.4", "UCHASE")
    assert gateway.calls == ["create_campaign"]


def test_org_only_lead_preview_contains_no_person_fields(tmp_path: Path) -> None:
    """The fallback maps the real organization without inventing a person."""
    conn = db.connect(tmp_path / "t.db")
    grant_id = _lead(conn, "A1", "Alpha School District")
    gateway = FakeGateway()
    campaign = gateway.get_record("Campaign", CAMPAIGN_ID)
    action = campaigns.prepare_membership(
        conn, gateway, "TWORK", "CGRANTS", "123.4", "UCHASE",
        campaign, [grant_id], allow_org_leads=True,
    )
    item = conn.execute("SELECT proposed_json FROM crm_action_items").fetchone()
    proposed = json.loads(item["proposed_json"])["proposed_lead"]
    assert proposed["Company"] == "Alpha School District"
    assert proposed["LastName"] == "Alpha School District"
    for forbidden in ("FirstName", "Email", "Title", "Phone"):
        assert forbidden not in proposed
    assert "No individual contact" in proposed["Description"]
    assert "Organization-only" in action.preview


def test_membership_skips_duplicates_and_reports_partial(tmp_path: Path) -> None:
    """Existing members are idempotent and failed creates remain explicit."""
    conn = db.connect(tmp_path / "t.db")
    alpha_id = _lead(conn, "A1", "Alpha School District")
    beta_id = _lead(conn, "B1", "Beta School District")
    alpha_ref = campaigns.SalesforceRecordRef(
        "Lead", LEAD_ID, "Alpha Contact", campaigns_link("Lead", LEAD_ID),
        company="Alpha School District", state="CA",
    )
    beta_ref = campaigns.SalesforceRecordRef(
        "Contact", CONTACT_ID, "Beta Contact", campaigns_link("Contact", CONTACT_ID),
        company="Beta School District", state="CA",
    )
    gateway = FakeGateway(
        people={
            "Alpha School District": [alpha_ref],
            "Beta School District": [beta_ref],
        },
        existing={LEAD_ID},
        member_results=[gateway_mod.CreateResult(False, error="duplicate rule")],
    )
    action = campaigns.prepare_membership(
        conn, gateway, "TWORK", "CGRANTS", "123.4", "UCHASE",
        gateway.get_record("Campaign", CAMPAIGN_ID), [alpha_id, beta_id],
    )
    result = campaigns.confirm_action(
        conn, gateway, action.action_id, action.nonce,
        "TWORK", "CGRANTS", "123.4", "UCHASE",
    )
    assert result.state is campaigns.CampaignActionState.PARTIAL
    assert result.already_present == 1
    assert result.failed == 1
    states = {row[0] for row in conn.execute("SELECT state FROM crm_action_items")}
    assert states == {"already_present", "failed"}


def test_unresolved_organization_forces_partial_and_preview_names_mapping(
        tmp_path: Path) -> None:
    """Skipped organizations stay visible in the frozen preview and final state."""
    conn = db.connect(tmp_path / "t.db")
    alpha_id = _lead(conn, "A1", "Alpha School District")
    beta_id = _lead(conn, "B1", "Beta School District")
    alpha_ref = campaigns.SalesforceRecordRef(
        "Lead", LEAD_ID, "Alpha Contact", campaigns_link("Lead", LEAD_ID),
        company="Alpha School District", state="CA",
    )
    gateway = FakeGateway(people={
        "Alpha School District": [alpha_ref],
        "Beta School District": [],
    })
    action = campaigns.prepare_membership(
        conn, gateway, "TWORK", "CGRANTS", "123.4", "UCHASE",
        gateway.get_record("Campaign", CAMPAIGN_ID), [alpha_id, beta_id],
    )
    assert "Alpha School District" in action.preview
    assert "Beta School District" in action.preview and "skipped" in action.preview
    result = campaigns.confirm_action(
        conn, gateway, action.action_id, action.nonce,
        "TWORK", "CGRANTS", "123.4", "UCHASE")
    assert result.state is campaigns.CampaignActionState.PARTIAL
    assert result.added == 1 and result.unresolved == 1


def test_action_hard_cap_is_200(tmp_path: Path) -> None:
    """A single approval cannot exceed Salesforce's collection boundary."""
    conn = db.connect(tmp_path / "t.db")
    with pytest.raises(ValueError, match="between 1 and 200"):
        campaigns.prepare_membership(
            conn, FakeGateway(), "TWORK", "CGRANTS", "123.4", "UCHASE",
            FakeGateway().get_record("Campaign", CAMPAIGN_ID), list(range(1, 202)),
        )


def test_supplied_person_link_must_match_grant_organization(tmp_path: Path) -> None:
    """A valid Salesforce link for another company cannot enter the Campaign plan."""
    conn = db.connect(tmp_path / "t.db")
    grant_id = _lead(conn, "A1", "Beta School District")
    gateway = FakeGateway()
    with pytest.raises(ValueError, match="No organizations can be added"):
        campaigns.prepare_membership(
            conn, gateway, "TWORK", "CGRANTS", "123.4", "UCHASE",
            gateway.get_record("Campaign", CAMPAIGN_ID), [grant_id],
            supplied_links={grant_id: campaigns_link("Lead", LEAD_ID)},
            allow_org_leads=True,
        )
    assert conn.execute("SELECT COUNT(*) FROM crm_actions").fetchone()[0] == 0


def test_writer_token_cache_is_scoped_to_configured_org(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Changing writer org/Connected App cannot reuse another org's token."""
    cache = gateway_mod._TOKEN_CACHE
    original = (cache.token, cache.instance_url, cache.expires_at,
                cache.credential_scope)
    calls: list[str] = []

    class Response:
        """OAuth response tied to the requested writer domain."""

        def __init__(self, domain: str) -> None:
            self.domain = domain

        def raise_for_status(self) -> None:
            """Model a successful OAuth response."""

        def json(self) -> dict[str, str]:
            """Return a distinct token/instance per domain."""
            return {"access_token": f"token-{self.domain}",
                    "instance_url": self.domain}

    def post(url: str, **_kwargs: object) -> Response:
        domain = url.split("/services/", 1)[0]
        calls.append(domain)
        return Response(domain)

    monkeypatch.setattr(gateway_mod.requests, "post", post)
    monkeypatch.setenv("SALESFORCE_WRITE_CLIENT_SECRET", "secret")
    try:
        gateway = gateway_mod.SalesforceCampaignGateway()
        for suffix in ("one", "two"):
            monkeypatch.setenv(
                "SALESFORCE_WRITE_MY_DOMAIN_URL", f"https://{suffix}.test")
            monkeypatch.setenv("SALESFORCE_WRITE_CLIENT_ID", f"client-{suffix}")
            gateway._auth()
        assert calls == ["https://one.test", "https://two.test"]
    finally:
        (cache.token, cache.instance_url, cache.expires_at,
         cache.credential_scope) = original


def test_gateway_has_no_forbidden_object_create_path() -> None:
    """Even internal calls cannot create/update an Account or Opportunity."""
    gateway = gateway_mod.SalesforceCampaignGateway()
    with pytest.raises(ValueError, match="forbidden"):
        gateway._create_one("Account", {"Name": "Do not create"})
    with pytest.raises(ValueError, match="forbidden"):
        gateway._create_many("Opportunity", [{"Name": "Do not create"}])
