"""Every Lead Grant creates must carry the Verkada record type explicitly.

WHY THIS FILE EXISTS. On 2026-09-22 a rep approved 53 Oregon organizations for the
GRANTS Campaign and 52 of them failed with "CANNOT_INSERT_UPDATE_ACTIVATE_ENTITY:
record type missing for: Lead" (29 more had failed the same way on 2026-08-27). The
campaign path never sent RecordTypeId and relied on the creating user's default —
Verkada for the human user that wrote August's Leads, Master for the integration
user writing now, and Salesforce rejects Master. Every fake gateway in the suite
accepted any payload, so no test could see it. Offline and side-effect free.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import requests

from grant_watch import db
from grant_watch.enrich import salesforce_campaign_gateway as gateway_mod
from grant_watch.enrich import salesforce_campaigns as campaigns
from grant_watch.enrich.salesforce_campaign_ownership import (
    LAST_NAME_MAX,
    required_lead_record_type,
)
from test_salesforce_campaigns import (
    CAMPAIGN_ID,
    CHASE_SLACK_ID,
    FakeGateway,
    _lead,
)
from test_salesforce_campaigns import writer_config as writer_config  # noqa: F401

VERKADA = "0122M000000viFyQAI"


def _prepare(
    conn: sqlite3.Connection, gateway: FakeGateway, lead_id: int
) -> campaigns.PreparedAction:
    """Prepare one organization-only Campaign membership in the allowed channel."""
    return campaigns.prepare_membership(
        conn,
        gateway,
        "TWORK",
        "CGRANTS",
        "123.4",
        CHASE_SLACK_ID,
        gateway.get_record("Campaign", CAMPAIGN_ID),
        [lead_id],
        allow_org_leads=True,
    )


def test_an_approved_org_lead_is_created_with_the_verkada_record_type(
    tmp_path: Path,
) -> None:
    """The exact 2026-09-22 shape: org-only Lead, approved, sent, and accepted.

    The name matches FakeGateway's readback, so the create is verified end to end.
    """
    conn = db.connect(tmp_path / "t.db")
    gateway = FakeGateway()
    action = _prepare(conn, gateway, _lead(conn, "OR1", "Alpha School District"))
    result = campaigns.confirm_action(
        conn,
        gateway,
        action.action_id,
        action.nonce,
        "TWORK",
        "CGRANTS",
        "123.4",
        CHASE_SLACK_ID,
    )
    assert gateway.created_lead_payloads[0]["RecordTypeId"] == VERKADA
    assert result.state is campaigns.CampaignActionState.COMPLETE


def test_a_long_organization_name_fits_last_name_and_keeps_company_whole(
    tmp_path: Path,
) -> None:
    """LastName is capped at 80; 8 diocese Leads failed "data value too large"."""
    conn = db.connect(tmp_path / "t.db")
    entity = (
        "EVERGREEN AVIATION AND SPACE MUSEUM AND THE CAPTAIN MICHAEL KING SMITH "
        "EDUCATIONAL INSTITUTE"
    )
    assert len(entity) > LAST_NAME_MAX
    gateway = FakeGateway()
    _prepare(conn, gateway, _lead(conn, "OR2", entity))
    payload = campaigns.json.loads(
        conn.execute("SELECT proposed_json FROM crm_action_items").fetchone()[0]
    )["proposed_lead"]
    assert len(payload["LastName"]) <= LAST_NAME_MAX
    assert entity.startswith(payload["LastName"])
    assert payload["Company"] == entity


def test_an_unresolvable_record_type_refuses_before_approval(tmp_path: Path) -> None:
    """Refuse at preview; never show a rep a card Salesforce will reject."""
    conn = db.connect(tmp_path / "t.db")

    class NoRecordType(FakeGateway):
        """Salesforce unreachable or the type withdrawn: lookup returns ''."""

        def lead_record_type_id(self, developer_name: str) -> str:
            """Return the real gateway's failure value."""
            return ""

    gateway = NoRecordType()
    with pytest.raises(ValueError, match="Verkada Lead record type"):
        _prepare(conn, gateway, _lead(conn, "OR3", "MOLALLA RIVER SCHOOL DISTRICT"))
    assert gateway.created_lead_payloads == []
    assert conn.execute("SELECT COUNT(*) FROM crm_actions").fetchone()[0] == 0


def test_a_record_type_id_of_the_wrong_object_is_refused() -> None:
    """A non-RecordType id (e.g. a User id) must never be written as the type."""

    class WrongPrefix:
        """Returns a well-formed id with the wrong object prefix."""

        def lead_record_type_id(self, developer_name: str) -> str:
            """Return a User id where a RecordType id belongs."""
            return "005000000000001"

    with pytest.raises(ValueError, match="RecordType"):
        required_lead_record_type(WrongPrefix())  # type: ignore[arg-type]


def test_a_failed_lookup_is_not_cached_but_a_success_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One transient failure must not block every Lead create until restart."""
    monkeypatch.setattr(gateway_mod, "_RECORD_TYPE_CACHE", {})
    gateway = gateway_mod.SalesforceCampaignGateway()
    calls: list[str] = []
    replies: list[object] = [
        requests.ConnectionError("reset"),
        {"records": [{"Id": VERKADA}]},
    ]

    def fake_get(path: str, params: dict[str, str] | None = None) -> dict:
        """Fail once, then answer, counting every Salesforce query."""
        calls.append(path)
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply  # type: ignore[return-value]

    monkeypatch.setattr(gateway, "_get", fake_get)
    assert gateway.lead_record_type_id("Verkada") == ""
    assert gateway.lead_record_type_id("Verkada") == VERKADA
    assert gateway.lead_record_type_id("Verkada") == VERKADA
    assert len(calls) == 2
