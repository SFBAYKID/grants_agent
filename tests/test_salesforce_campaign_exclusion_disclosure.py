"""Exact Slack evidence for human-excluded Salesforce Campaign organizations."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from grant_watch import db
from grant_watch.enrich import salesforce_campaigns as campaigns
from grant_watch.models import LeadGrade
from campaign_batch_support import (
    CAMPAIGNS,
    BatchGateway,
    insert_leads,
)


@pytest.fixture(autouse=True)
def _writer_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Allow only the offline test channel and fake Salesforce hostname."""
    monkeypatch.setenv("GRANT_SALESFORCE_WRITE_CHANNEL_IDS", "CGRANTS")
    monkeypatch.setenv(
        "SALESFORCE_WRITE_MY_DOMAIN_URL", "https://writer.salesforce.test"
    )
    monkeypatch.setenv("SALESFORCE_CAMPAIGN_WRITES_ENABLED", "1")


def test_resolved_only_preview_and_result_name_excluded_organization(
    tmp_path: Path,
) -> None:
    """A human-approved exclusion stays explicit in the preview and final result."""
    conn = db.connect(tmp_path / "exclusion.db")
    insert_leads(conn, "IL", LeadGrade.GOLD, 2, 0)
    lead_ids = [int(row[0]) for row in conn.execute("SELECT id FROM leads ORDER BY id")]
    gateway = BatchGateway(missing_names={"IL Organization 001"})
    action = campaigns.prepare_membership(
        conn,
        gateway,
        "TWORK",
        "CGRANTS",
        "123.4",
        "UCHASE",
        gateway.get_record("Campaign", CAMPAIGNS["IL"][0]),
        lead_ids,
        allow_resolved_only=True,
    )
    assert "Explicitly excluded unresolved/ambiguous organizations: 1" in action.preview
    assert "IL Organization 001 (IL)" in action.preview
    payload = json.loads(
        conn.execute(
            "SELECT payload_json FROM crm_actions WHERE id=?", (action.action_id,)
        ).fetchone()[0]
    )
    assert payload["excluded_organizations"] == [
        {
            "entity_name": "IL Organization 001",
            "reason": (
                "Provide a Salesforce Lead/Contact link or approve an "
                "organization-only Lead."
            ),
            "state": "IL",
        }
    ]
    result = campaigns.confirm_action(
        conn,
        gateway,
        action.action_id,
        action.nonce,
        "TWORK",
        "CGRANTS",
        "123.4",
        "UCHASE",
    )
    assert result.state is campaigns.CampaignActionState.COMPLETE
    assert "0 unresolved" in result.message
    assert "Explicitly excluded/skipped before approval: 1" in result.message
    assert "IL Organization 001 (IL)" in result.message
