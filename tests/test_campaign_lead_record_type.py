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


def _vendor(conn: sqlite3.Connection, lead_id: int, *, dnc: bool) -> None:
    """Store one ZoomInfo contact through the real vendor save path."""
    db.save_vendor_contact(
        conn,
        lead_id,
        "Pat Rivera",
        "Director of Technology",
        "privera@sheridan.k12.or.us",
        "503-555-0100",
        "12345",
        do_not_call=dnc,
        mobile_phone="503-555-0199",
    )


def _payload(conn: sqlite3.Connection, lead_id: int) -> tuple[dict, str, str]:
    """Build the campaign Lead exactly as the batch path does."""
    from grant_watch.enrich.salesforce_campaign_ownership import (
        campaign_lead_payload,
    )

    row = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    owner = FakeGateway().users_by_email["chase@monarchconnected.com"][0]
    return campaign_lead_payload(conn, row, "UREP", "act", owner, FakeGateway())


def test_a_zoominfo_contact_becomes_a_named_lead_labelled_as_vendor_data(
    tmp_path: Path,
) -> None:
    """Paid-for ZoomInfo people were dropped, so every Lead stayed nameless."""
    conn = db.connect(tmp_path / "t.db")
    lead_id = _lead(conn, "OR4", "SHERIDAN SCHOOL DISTRICT", "OR")
    _vendor(conn, lead_id, dnc=False)
    payload, note, person = _payload(conn, lead_id)
    assert person == "Pat Rivera"
    assert (payload["FirstName"], payload["LastName"]) == ("Pat", "Rivera")
    assert payload["Title"] == "Director of Technology"
    assert payload["Email"] == "privera@sheridan.k12.or.us"
    assert payload["Phone"] == "503-555-0100"
    assert payload["MobilePhone"] == "503-555-0199"
    assert payload["RecordTypeId"] == VERKADA
    assert "Supplied by ZoomInfo" in str(payload["Description"])
    assert note.startswith("ZoomInfo contact Pat Rivera")
    assert "Verified" not in note


def test_a_do_not_call_zoominfo_contact_carries_no_personal_number(
    tmp_path: Path,
) -> None:
    """The DNC flag travels: no person phone or mobile, and it is said FIRST."""
    conn = db.connect(tmp_path / "t.db")
    lead_id = _lead(conn, "OR5", "SHERIDAN SCHOOL DISTRICT", "OR")
    _vendor(conn, lead_id, dnc=True)
    payload, _note, _person = _payload(conn, lead_id)
    assert payload.get("Phone") != "503-555-0100"
    assert "MobilePhone" not in payload
    assert "DO NOT CALL" in str(payload["Description"]).split("Supplied by")[0]


def test_a_page_verified_contact_still_beats_a_newer_zoominfo_one(
    tmp_path: Path,
) -> None:
    """Vendor data fills a gap; it never displaces a contact read off their site."""
    from test_contact_provenance_and_person_leads import _contact

    conn = db.connect(tmp_path / "t.db")
    lead_id = _lead(conn, "OR6", "SHERIDAN SCHOOL DISTRICT", "OR")
    _contact(
        conn,
        lead_id,
        name="Dana Reyes",
        title="Director of Information Technology",
        email="dreyes@sheridan.k12.or.us",
        status="verified",
    )
    _vendor(conn, lead_id, dnc=False)
    _payload_, note, person = _payload(conn, lead_id)
    assert person == "Dana Reyes"
    assert note.startswith("Verified contact Dana Reyes")


def test_the_card_never_calls_a_zoominfo_person_verified(tmp_path: Path) -> None:
    """The live 2026-09-22 card said "26 for a verified named person" — all ZoomInfo."""
    conn = db.connect(tmp_path / "t.db")
    lead_id = _lead(conn, "OR7", "SHERIDAN SCHOOL DISTRICT", "OR")
    _vendor(conn, lead_id, dnc=False)
    preview = _prepare(conn, FakeGateway(), lead_id).preview
    assert "create Lead for Pat Rivera (ZoomInfo)" in preview
    assert "1 naming a person (1 from ZoomInfo" in preview
    assert "verified named person" not in preview
