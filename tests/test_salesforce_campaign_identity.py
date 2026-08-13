"""Campaign snapshot completeness and organization-identity boundary tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from grant_watch import db
from grant_watch.enrich import salesforce_campaigns as campaigns
from grant_watch.enrich.salesforce_campaign_gateway import (
    SalesforceCampaignGateway,
    SalesforceRecordRef,
)
from grant_watch.enrich.salesforce_campaign_policy import record_matches_organization
from grant_watch.models import LeadGrade
from grant_watch.slack import tools as slack_tools
from campaign_batch_support import (
    CAMPAIGNS,
    BatchGateway,
    campaign_link as _link,
    insert_leads as _insert_leads,
)


@pytest.fixture(autouse=True)
def _writer_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Allow only the offline test channel and fake writer hostname."""
    monkeypatch.setenv("GRANT_SALESFORCE_WRITE_CHANNEL_IDS", "CGRANTS")
    monkeypatch.setenv(
        "SALESFORCE_WRITE_MY_DOMAIN_URL", "https://writer.salesforce.test"
    )
    monkeypatch.setenv("SALESFORCE_CAMPAIGN_WRITES_ENABLED", "1")


def test_legacy_incomplete_search_snapshot_cannot_prepare_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The former frozen-ID path rejects snapshots without completeness proof."""
    conn = db.connect(tmp_path / "batch.db")
    _insert_leads(conn, "IL", LeadGrade.GOLD, 1, 0)
    snapshot_id = db.save_search_request(
        conn,
        "TWORK:CGRANTS:123.4:UREP",
        "UREP",
        {"state": "IL", "grade": "gold"},
        "all",
        None,
        "slack",
        [1],
        1,
        False,
    )
    monkeypatch.setattr(slack_tools.db, "connect", lambda: conn)
    monkeypatch.setattr(campaigns, "SalesforceCampaignGateway", lambda: BatchGateway())
    result = slack_tools.salesforce_campaign_members_preview(
        {
            "campaign_link": _link("Campaign", CAMPAIGNS["IL"][0]),
            "search_request_id": snapshot_id,
        },
        "UREP",
        "TWORK",
        "CGRANTS",
        "123.4",
    )
    assert "not the complete state/tier set" in result
    assert "salesforce_campaign_batch_preview" in result
    assert conn.execute("SELECT COUNT(*) FROM crm_actions").fetchone()[0] == 0


def test_identity_matching_requires_exact_nonblank_organization_state() -> None:
    """Blank or cross-state records can never become automatic Campaign members."""
    exact = SalesforceRecordRef(
        "Lead",
        "00Q000000000001",
        "Person",
        _link("Lead", "00Q000000000001"),
        company="Springfield School District",
        state="IL",
    )
    blank_state = SalesforceRecordRef(
        "Lead",
        "00Q000000000002",
        "Person",
        _link("Lead", "00Q000000000002"),
        company="Springfield School District",
        state="",
    )
    assert record_matches_organization(exact, "Springfield School District", "IL")
    assert not record_matches_organization(exact, "Springfield School District", "TX")
    assert not record_matches_organization(exact, "Springfield School District", "")
    assert not record_matches_organization(
        blank_state, "Springfield School District", "IL"
    )


def test_contact_identity_uses_account_billing_not_person_mailing_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Contact's personal mailing address cannot bind the organization state."""
    gateway = SalesforceCampaignGateway()
    captured: dict[str, str] = {}

    def fake_get(_path: str, params: dict[str, str]) -> dict[str, object]:
        """Return a Contact whose person and organization states differ."""
        captured.update(params)
        return {
            "Id": "003000000000001",
            "Name": "Person",
            "MailingState": "TX",
            "Account": {
                "Id": "001000000000001",
                "Name": "Springfield School District",
                "BillingState": "IL",
            },
        }

    monkeypatch.setattr(gateway, "_get", fake_get)
    monkeypatch.setattr(gateway, "lightning_link", _link)
    record = gateway.get_record("Contact", "003000000000001")
    assert record.state == "IL" and record.account_id == "001000000000001"
    assert "Account.BillingState" in captured["fields"]
    assert "MailingState" not in captured["fields"]
