"""The 2026-08-11 campaign-load dead end: what a rep can actually reach, and told.

Every test here is a real message a rep received on 2026-08-11 while trying to load
200 California silver leads onto a Campaign. The batch ended with zero members and
the approval card reported "Unresolved/ambiguous and skipped: 0" while nine of ten
organizations were being dropped.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from grant_watch import db, persequor_client
from grant_watch.enrich import salesforce_campaigns as campaigns
from grant_watch.enrich.salesforce_campaign_batch import prepare_campaign_batch
from grant_watch.enrich.salesforce_campaign_batch_models import CampaignTargetRequest
from grant_watch.enrich.salesforce_campaign_models import CampaignActionState
from grant_watch.enrich.salesforce_campaign_gateway import (
    CreateResult,
    SalesforceRecordRef,
)
from grant_watch.models import LeadGrade

from campaign_batch_support import CAMPAIGNS, BatchGateway, campaign_link, insert_leads


@pytest.fixture(autouse=True)
def _writer_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Allow the offline test channel, fake writer host, and one mapped rep."""
    monkeypatch.setenv("GRANT_SALESFORCE_WRITE_CHANNEL_IDS", "CGRANTS")
    monkeypatch.setenv(
        "SALESFORCE_WRITE_MY_DOMAIN_URL", "https://writer.salesforce.test"
    )
    monkeypatch.setenv("SALESFORCE_CAMPAIGN_WRITES_ENABLED", "1")
    # Organization-only Leads must be OWNED by the requesting rep, so these tests
    # need one mapped rep. No existing batch test reaches a successful org-lead
    # creation — every one of them stops at this error — which is part of why the
    # discard below survived.
    monkeypatch.setattr(
        persequor_client, "rep_email_for", lambda _slack: "rep@monarchconnected.test"
    )


class NellyGateway(BatchGateway):
    """Nelly's exact shape: most organizations missing, one genuinely ambiguous."""

    ambiguous_names: set[str] = set()

    def find_people(self, entity_name: str, state: str) -> list[SalesforceRecordRef]:
        """Return TWO exact Leads for the ambiguous organization, like Dinuba."""
        if entity_name in self.ambiguous_names:
            return [
                SalesforceRecordRef(
                    "Lead",
                    f"00Qamb{suffix}00000000",
                    f"{entity_name} Contact {suffix}",
                    campaign_link("Lead", f"00Qamb{suffix}00000000"),
                    company=entity_name,
                    state=state,
                )
                for suffix in ("a", "b")
            ]
        return super().find_people(entity_name, state)

    def create_leads(self, payloads: list[dict[str, object]]) -> list[CreateResult]:
        """Create Leads and register them so the readback in execution succeeds."""
        results: list[CreateResult] = []
        for index, payload in enumerate(payloads, start=1):
            record_id = f"00Qorg{index:012d}"
            self.records[record_id] = SalesforceRecordRef(
                "Lead",
                record_id,
                str(payload.get("LastName") or ""),
                campaign_link("Lead", record_id),
                company=str(payload["Company"]),
                state=str(payload.get("State") or ""),
            )
            results.append(CreateResult(True, record_id))
        return results

    def find_active_user_by_email(self, email: str) -> list[SalesforceRecordRef]:
        """Resolve the one mapped rep who will own any organization-only Lead."""
        return [
            SalesforceRecordRef(
                "User",
                "005000000000001",
                "Test Rep",
                campaign_link("User", "005000000000001"),
            )
        ]


def _nelly_batch(tmp_path: Path, name: str) -> tuple[object, NellyGateway, tuple]:
    """10 IL silver organizations: 1 exact, 8 missing, 1 ambiguous."""
    conn = db.connect(tmp_path / name)
    insert_leads(conn, "IL", LeadGrade.SILVER, 10, 0)
    gateway = NellyGateway()
    gateway.missing_names = {f"IL Organization {n:03d}" for n in range(1, 9)}
    gateway.ambiguous_names = {"IL Organization 009"}
    requests = (
        CampaignTargetRequest(
            campaign_link("Campaign", CAMPAIGNS["IL"][0]), "IL", ("silver",)
        ),
    )
    return conn, gateway, requests


def test_one_ambiguous_org_no_longer_discards_every_org_only_lead(
    tmp_path: Path,
) -> None:
    """The defect: approving org-only Leads produced exactly one member, silently.

    `allow_org_leads` was accepted and recorded, then `_materialize_target_action`
    filtered to `existing_record` only, so 8 of 9 approved organizations vanished
    between the answer and the action.
    """
    conn, gateway, requests = _nelly_batch(tmp_path, "compose.db")
    batch = prepare_campaign_batch(
        conn,
        gateway,
        "TWORK",
        "CGRANTS",
        "1.0",
        "UREP",
        requests,
        allow_org_leads=True,
        allow_resolved_only=True,
    )
    assert len(batch.actions) == 1
    operations = dict(
        conn.execute(
            "SELECT operation, COUNT(*) FROM crm_action_items GROUP BY operation"
        )
    )
    assert operations == {"existing_record": 1, "create_org_lead": 8}


def test_the_skipped_organization_is_named_on_the_card(tmp_path: Path) -> None:
    """ "Unresolved/ambiguous and skipped: 0" was printed while 9 were dropped."""
    conn, gateway, requests = _nelly_batch(tmp_path, "disclose.db")
    batch = prepare_campaign_batch(
        conn,
        gateway,
        "TWORK",
        "CGRANTS",
        "1.0",
        "UREP",
        requests,
        allow_org_leads=True,
        allow_resolved_only=True,
    )
    preview = batch.actions[0].preview
    assert "Left out of this batch: 1" in preview
    assert "IL Organization 009 (IL)" in preview
    assert "Will be added now: 9" in preview


def test_ambiguity_still_stops_before_approval_and_says_which_lever_helps(
    tmp_path: Path,
) -> None:
    """Blocking is correct; blaming Salesforce and hiding the remedy was not."""
    conn, gateway, requests = _nelly_batch(tmp_path, "blocked.db")
    batch = prepare_campaign_batch(
        conn,
        gateway,
        "TWORK",
        "CGRANTS",
        "1.0",
        "UREP",
        requests,
        allow_org_leads=True,
    )
    assert batch.actions == ()
    assert "not a Salesforce error" in batch.summary
    assert "1 match more than one Salesforce record" in batch.summary
    assert "allow_resolved_only" in batch.summary


def test_a_batch_of_one_hundred_is_prepared_whole(tmp_path: Path) -> None:
    """A rep must be able to load 100 organizations in a single approval."""
    conn = db.connect(tmp_path / "hundred.db")
    insert_leads(conn, "IL", LeadGrade.SILVER, 100, 0)
    gateway = NellyGateway()
    gateway.missing_names = {f"IL Organization {n:03d}" for n in range(50, 100)}
    batch = prepare_campaign_batch(
        conn,
        gateway,
        "TWORK",
        "CGRANTS",
        "1.0",
        "UREP",
        (
            CampaignTargetRequest(
                campaign_link("Campaign", CAMPAIGNS["IL"][0]), "IL", ("silver",)
            ),
        ),
        allow_org_leads=True,
    )
    assert len(batch.actions) == 1
    assert "batch 1 of" not in batch.summary
    assert conn.execute("SELECT COUNT(*) FROM crm_action_items").fetchone()[0] == 100
    assert "Will be added now: 100" in batch.actions[0].preview


def _one_lead_preview(
    tmp_path: Path, name: str, ref: SalesforceRecordRef, members: dict
) -> str:
    """Prepare a single-organization membership preview against a frozen record."""
    conn = db.connect(tmp_path / name)
    insert_leads(conn, "IL", LeadGrade.GOLD, 1, 0)
    lead_id = int(conn.execute("SELECT id FROM leads").fetchone()[0])
    gateway = BatchGateway(members=members)
    action = campaigns.prepare_membership(
        conn,
        gateway,
        "TWORK",
        "CGRANTS",
        "1.0",
        "UREP",
        gateway.get_record("Campaign", CAMPAIGNS["IL"][0]),
        [lead_id],
        resolved_records={lead_id: ref},
    )
    return action.preview


def test_preview_says_plainly_when_confirming_would_change_nothing(
    tmp_path: Path,
) -> None:
    """The 7:14 card: 13 "existing Leads" that had been members since 06 August."""
    ref = SalesforceRecordRef(
        "Lead",
        "00Q000000000777",
        "Dana Reyes",
        campaign_link("Lead", "00Q000000000777"),
        company="IL Organization 000",
        state="IL",
    )
    preview = _one_lead_preview(
        tmp_path,
        "already.db",
        ref,
        {"00Q000000000777": ("00v1", "Identified by Grant")},
    )
    assert "Nothing would change" in preview
    assert "Will be added now: 0" in preview
    assert "Already on this Campaign, unchanged: 1" in preview


def test_preview_answers_whether_the_matched_record_names_a_person(
    tmp_path: Path,
) -> None:
    """Nelly had to ask. An organization-only Lead has no contact name on it."""
    placeholder = SalesforceRecordRef(
        "Lead",
        "00Q000000000888",
        "IL Organization 000",
        campaign_link("Lead", "00Q000000000888"),
        company="IL Organization 000",
        state="IL",
    )
    preview = _one_lead_preview(tmp_path, "placeholder.db", placeholder, {})
    assert "1 are organization-only placeholders" in preview

    person = SalesforceRecordRef(
        "Lead",
        "00Q000000000999",
        "Dana Reyes",
        campaign_link("Lead", "00Q000000000999"),
        company="IL Organization 000",
        state="IL",
    )
    assert "placeholders" not in _one_lead_preview(tmp_path, "person.db", person, {})


def test_confirming_the_batch_actually_writes_every_promised_member(
    tmp_path: Path,
) -> None:
    """THE TEST THAT WAS MISSING. Every other case stops at `crm_action_items`.

    The first version of this fix converted two of the four places that answer
    "which organizations are in this batch" and left the manifest count and the
    click-time gate reading `existing_record` only. The card offered nine, the
    action held nine, the manifest recorded one, and Confirm refused with an
    internal PermissionError *after* `_begin_commit` had consumed the nonce — so the
    rep could not even click again. 1359 tests passed while that was true, because
    not one of them pressed the button.
    """
    conn, gateway, requests = _nelly_batch(tmp_path, "confirm.db")
    batch = prepare_campaign_batch(
        conn,
        gateway,
        "TWORK",
        "CGRANTS",
        "1.0",
        "UREP",
        requests,
        allow_org_leads=True,
        allow_resolved_only=True,
    )
    action = batch.actions[0]
    assert "Will be added now: 9" in action.preview

    result = campaigns.confirm_action(
        conn,
        gateway,
        action.action_id,
        action.nonce,
        "TWORK",
        "CGRANTS",
        "1.0",
        "UREP",
    )
    assert result.state is CampaignActionState.COMPLETE, result.message
    assert result.added == 9, result.message
    assert result.failed == 0
    assert result.unknown == 0
    # The one organization Grant refused to guess at is named in the outcome, not
    # silently absent, and it is the ONLY one left out.
    assert "IL Organization 009" in result.message
    assert gateway.create_member_calls == 1


def test_the_manifest_records_exactly_what_the_action_holds(tmp_path: Path) -> None:
    """The durable approval evidence must not disagree with what was written.

    `approved_org_count` said 1 while `crm_action_items` held 9, and
    `payload_json` recorded `allow_resolved_only: False` for a batch prepared with
    it True.
    """
    conn, gateway, requests = _nelly_batch(tmp_path, "manifest.db")
    batch = prepare_campaign_batch(
        conn,
        gateway,
        "TWORK",
        "CGRANTS",
        "1.0",
        "UREP",
        requests,
        allow_org_leads=True,
        allow_resolved_only=True,
    )
    target = conn.execute(
        "SELECT approved_org_count FROM crm_campaign_batch_targets"
    ).fetchone()
    action_items = conn.execute(
        "SELECT COUNT(*) FROM crm_action_items WHERE action_id=?",
        (batch.actions[0].action_id,),
    ).fetchone()[0]
    assert int(target["approved_org_count"]) == action_items == 9
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM crm_campaign_batch_items WHERE included=1"
        ).fetchone()[0]
        == 9
    )
    payload = json.loads(
        conn.execute(
            "SELECT payload_json FROM crm_actions WHERE id=?",
            (batch.actions[0].action_id,),
        ).fetchone()[0]
    )
    assert payload["allow_org_leads"] is True
    assert payload["allow_resolved_only"] is True
