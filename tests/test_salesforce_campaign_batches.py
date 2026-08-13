"""Exact Campaign batch and post-write reconciliation tests; no network calls."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from grant_watch import db
from grant_watch.enrich import salesforce_campaigns as campaigns
from grant_watch.enrich.salesforce_campaign_batch import prepare_campaign_batch
from grant_watch.enrich.salesforce_campaign_batch_models import CampaignTargetRequest
from grant_watch.enrich.salesforce_campaign_gateway import (
    CreateResult,
    SalesforceOrganizationIdentity,
    SalesforceRecordRef,
)
from grant_watch.models import LeadGrade
from campaign_batch_support import (
    CAMPAIGNS,
    BatchGateway,
    campaign_link as _link,
    incident_matrix as _incident_matrix,
    incident_requests as _requests,
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


def test_exact_six_set_batch_freezes_and_verifies_all_67_rows(
    tmp_path: Path,
) -> None:
    """IL/FL/TX Gold+Silver creates three isolated, complete verified actions."""
    conn = db.connect(tmp_path / "batch.db")
    _incident_matrix(conn)
    gateway = BatchGateway()
    batch = prepare_campaign_batch(
        conn, gateway, "TWORK", "CGRANTS", "123.4", "UREP", _requests()
    )
    assert batch.state == "approval_ready"
    assert len(batch.actions) == 3
    parent = conn.execute(
        """SELECT source_row_count,unique_org_count,state
           FROM crm_campaign_batches WHERE id=?""",
        (batch.batch_id,),
    ).fetchone()
    assert tuple(parent) == (67, 67, "approval_ready")
    target_counts = [
        tuple(row)
        for row in conn.execute(
            """SELECT state_code,source_row_count,unique_org_count
               FROM crm_campaign_batch_targets WHERE batch_id=? ORDER BY state_code""",
            (batch.batch_id,),
        )
    ]
    assert target_counts == [("FL", 5, 5), ("IL", 33, 33), ("TX", 29, 29)]
    for action in batch.actions:
        result = campaigns.confirm_action(
            conn,
            gateway,
            action.action_id,
            action.nonce,
            "TWORK",
            "CGRANTS",
            "123.4",
            "UREP",
        )
        assert result.state is campaigns.CampaignActionState.COMPLETE
    assert (
        conn.execute(
            """SELECT COUNT(*) FROM crm_action_items
           WHERE verification_state='verified' AND state='added'"""
        ).fetchone()[0]
        == 67
    )
    assert (
        conn.execute(
            "SELECT state FROM crm_campaign_batches WHERE id=?", (batch.batch_id,)
        ).fetchone()[0]
        == "complete"
    )


def test_unresolved_or_account_only_batch_has_no_confirmation(
    tmp_path: Path,
) -> None:
    """Any unsafe identity blocks the whole batch and preserves its diagnosis."""
    conn = db.connect(tmp_path / "batch.db")
    _insert_leads(conn, "IL", LeadGrade.GOLD, 2, 0)
    gateway = BatchGateway(
        missing_names={"IL Organization 000"},
        account_names={"IL Organization 001"},
    )
    batch = prepare_campaign_batch(
        conn, gateway, "TWORK", "CGRANTS", "123.4", "UREP", (_requests()[0],)
    )
    assert batch.actions == ()
    assert "has not created any confirmation button" in batch.summary.lower()
    assert "IL Organization 000 (IL): missing" in batch.summary
    assert "IL Organization 001 (IL): account_only" in batch.summary
    states = {
        row[0]
        for row in conn.execute("SELECT resolution_state FROM crm_campaign_batch_items")
    }
    assert states == {"missing", "account_only"}
    assert conn.execute("SELECT COUNT(*) FROM crm_actions").fetchone()[0] == 0


def test_duplicate_events_retain_all_ids_and_grades_under_one_organization(
    tmp_path: Path,
) -> None:
    """Organization dedup never discards the Gold/Silver rows that contributed."""
    conn = db.connect(tmp_path / "batch.db")
    _insert_leads(conn, "IL", LeadGrade.GOLD, 1, 0)
    _insert_leads(conn, "IL", LeadGrade.SILVER, 1, 0)
    batch = prepare_campaign_batch(
        conn,
        BatchGateway(),
        "TWORK",
        "CGRANTS",
        "123.4",
        "UREP",
        (
            CampaignTargetRequest(
                _link("Campaign", CAMPAIGNS["IL"][0]),
                "IL",
                ("gold", "silver"),
            ),
        ),
        allow_org_leads=True,
    )
    target = conn.execute(
        """SELECT source_row_count,unique_org_count
           FROM crm_campaign_batch_targets WHERE batch_id=?""",
        (batch.batch_id,),
    ).fetchone()
    item = conn.execute(
        "SELECT source_lead_ids_json,grades_json FROM crm_campaign_batch_items"
    ).fetchone()
    assert tuple(target) == (2, 1)
    assert item["source_lead_ids_json"] == "[1,2]"
    assert item["grades_json"] == '["gold","silver"]'
    assert batch.actions == ()
    assert (
        conn.execute(
            "SELECT resolution_state FROM crm_campaign_batch_items"
        ).fetchone()[0]
        == "ambiguous"
    )


def test_later_target_failure_cancels_earlier_undelivered_action(
    tmp_path: Path,
) -> None:
    """A multi-Campaign preview error cannot leave an orphan executable nonce."""
    conn = db.connect(tmp_path / "batch.db")
    _insert_leads(conn, "IL", LeadGrade.GOLD, 1, 0)
    _insert_leads(conn, "FL", LeadGrade.GOLD, 1, 10)
    gateway = BatchGateway(missing_names={"FL Organization 010"})
    with pytest.raises(ValueError, match="not mapped to an approved rep email"):
        prepare_campaign_batch(
            conn,
            gateway,
            "TWORK",
            "CGRANTS",
            "123.4",
            "UREP",
            (
                CampaignTargetRequest(
                    _link("Campaign", CAMPAIGNS["IL"][0]), "IL", ("gold",)
                ),
                CampaignTargetRequest(
                    _link("Campaign", CAMPAIGNS["FL"][0]), "FL", ("gold",)
                ),
            ),
            allow_org_leads=True,
        )
    assert conn.execute("SELECT state FROM crm_actions").fetchone()[0] == "cancelled"
    assert (
        conn.execute("SELECT state FROM crm_campaign_batches").fetchone()[0] == "failed"
    )


def test_success_without_readback_is_unknown_not_added(tmp_path: Path) -> None:
    """A successful POST cannot produce a positive result without Salesforce evidence."""
    conn = db.connect(tmp_path / "batch.db")
    _insert_leads(conn, "IL", LeadGrade.GOLD, 1, 0)
    gateway = BatchGateway(write_mode="no_readback")
    batch = prepare_campaign_batch(
        conn,
        gateway,
        "TWORK",
        "CGRANTS",
        "123.4",
        "UREP",
        (
            CampaignTargetRequest(
                _link("Campaign", CAMPAIGNS["IL"][0]), "IL", ("gold",)
            ),
        ),
    )
    action = batch.actions[0]
    result = campaigns.confirm_action(
        conn,
        gateway,
        action.action_id,
        action.nonce,
        "TWORK",
        "CGRANTS",
        "123.4",
        "UREP",
    )
    assert result.state is campaigns.CampaignActionState.UNKNOWN
    assert result.added == 0 and result.unknown == 1
    replay = campaigns.confirm_action(
        conn,
        gateway,
        action.action_id,
        action.nonce,
        "TWORK",
        "CGRANTS",
        "123.4",
        "UREP",
    )
    assert replay.state is campaigns.CampaignActionState.UNKNOWN
    assert replay.added == 0 and gateway.create_member_calls == 1
    item = conn.execute(
        "SELECT state,verification_state FROM crm_action_items"
    ).fetchone()
    assert tuple(item) == ("unknown", "unknown")


def test_tampered_batch_completeness_fails_before_salesforce_write(
    tmp_path: Path,
) -> None:
    """Count/hash evidence is rechecked at click time before any POST."""
    conn = db.connect(tmp_path / "batch.db")
    _insert_leads(conn, "IL", LeadGrade.GOLD, 1, 0)
    gateway = BatchGateway()
    batch = prepare_campaign_batch(
        conn,
        gateway,
        "TWORK",
        "CGRANTS",
        "123.4",
        "UREP",
        (
            CampaignTargetRequest(
                _link("Campaign", CAMPAIGNS["IL"][0]), "IL", ("gold",)
            ),
        ),
    )
    conn.execute(
        """UPDATE crm_campaign_batch_targets
           SET stored_source_row_count=stored_source_row_count-1 WHERE batch_id=?""",
        (batch.batch_id,),
    )
    conn.commit()
    action = batch.actions[0]
    result = campaigns.confirm_action(
        conn,
        gateway,
        action.action_id,
        action.nonce,
        "TWORK",
        "CGRANTS",
        "123.4",
        "UREP",
    )
    assert result.state is campaigns.CampaignActionState.FAILED
    assert gateway.create_member_calls == 0


def test_full_batch_rejects_an_action_missing_one_manifest_organization(
    tmp_path: Path,
) -> None:
    """A recomputed child hash cannot authorize fewer rows than the full manifest."""
    conn = db.connect(tmp_path / "batch.db")
    _insert_leads(conn, "IL", LeadGrade.GOLD, 2, 0)
    gateway = BatchGateway()
    batch = prepare_campaign_batch(
        conn,
        gateway,
        "TWORK",
        "CGRANTS",
        "123.4",
        "UREP",
        (
            CampaignTargetRequest(
                _link("Campaign", CAMPAIGNS["IL"][0]), "IL", ("gold",)
            ),
        ),
    )
    action = batch.actions[0]
    removed = conn.execute(
        """SELECT id FROM crm_action_items WHERE action_id=? ORDER BY id LIMIT 1""",
        (action.action_id,),
    ).fetchone()
    conn.execute(
        "UPDATE crm_campaign_batch_items SET crm_action_item_id=NULL "
        "WHERE crm_action_item_id=?",
        (removed["id"],),
    )
    conn.execute("DELETE FROM crm_action_items WHERE id=?", (removed["id"],))
    remaining = [
        {
            "lead_id": row["lead_id"],
            "canonical_entity_key": row["canonical_entity_key"],
            "operation": row["operation"],
            "proposed": json.loads(str(row["proposed_json"])),
        }
        for row in conn.execute(
            """SELECT lead_id,canonical_entity_key,operation,proposed_json
               FROM crm_action_items WHERE action_id=? ORDER BY id""",
            (action.action_id,),
        )
    ]
    conn.execute(
        "UPDATE crm_actions SET items_hash=? WHERE id=?",
        (
            campaigns._hash(campaigns._stable_json(remaining)),
            action.action_id,
        ),
    )
    conn.commit()
    result = campaigns.confirm_action(
        conn,
        gateway,
        action.action_id,
        action.nonce,
        "TWORK",
        "CGRANTS",
        "123.4",
        "UREP",
    )
    assert result.state is campaigns.CampaignActionState.FAILED
    assert gateway.create_member_calls == 0


def test_delayed_readback_eventually_verifies_member(tmp_path: Path) -> None:
    """A bounded absence-then-presence sequence resolves to verified success."""
    conn = db.connect(tmp_path / "batch.db")
    _insert_leads(conn, "IL", LeadGrade.GOLD, 1, 0)
    gateway = BatchGateway(visibility_lag=1)
    batch = prepare_campaign_batch(
        conn,
        gateway,
        "TWORK",
        "CGRANTS",
        "123.4",
        "UREP",
        (
            CampaignTargetRequest(
                _link("Campaign", CAMPAIGNS["IL"][0]), "IL", ("gold",)
            ),
        ),
    )
    action = batch.actions[0]
    result = campaigns.confirm_action(
        conn,
        gateway,
        action.action_id,
        action.nonce,
        "TWORK",
        "CGRANTS",
        "123.4",
        "UREP",
    )
    assert result.state is campaigns.CampaignActionState.COMPLETE
    assert result.added == 1 and result.unknown == 0


def test_timeout_replay_reconciles_read_only_without_duplicate_create(
    tmp_path: Path,
) -> None:
    """An indeterminate POST is reconciled by GET and never submitted twice."""
    conn = db.connect(tmp_path / "batch.db")
    _insert_leads(conn, "IL", LeadGrade.GOLD, 1, 0)
    gateway = BatchGateway(write_mode="timeout_after_write")
    batch = prepare_campaign_batch(
        conn,
        gateway,
        "TWORK",
        "CGRANTS",
        "123.4",
        "UREP",
        (
            CampaignTargetRequest(
                _link("Campaign", CAMPAIGNS["IL"][0]), "IL", ("gold",)
            ),
        ),
    )
    action = batch.actions[0]
    first = campaigns.confirm_action(
        conn,
        gateway,
        action.action_id,
        action.nonce,
        "TWORK",
        "CGRANTS",
        "123.4",
        "UREP",
    )
    assert first.state is campaigns.CampaignActionState.UNKNOWN
    second = campaigns.confirm_action(
        conn,
        gateway,
        action.action_id,
        action.nonce,
        "TWORK",
        "CGRANTS",
        "123.4",
        "UREP",
    )
    assert second.state is campaigns.CampaignActionState.COMPLETE
    assert second.already_present == 1 and second.added == 0
    assert gateway.create_member_calls == 1
    attempt = conn.execute("SELECT state FROM crm_campaign_write_attempts").fetchone()[
        0
    ]
    assert attempt == "reconciled_present"


def test_reconciliation_rechecks_the_frozen_salesforce_writer_org(
    tmp_path: Path,
) -> None:
    """An unknown action cannot be reconciled against a different Salesforce org."""

    class SwitchingGateway(BatchGateway):
        """Expose an explicit writer-org switch after the uncertain write."""

        writer_org_id = "00D000000000001"

        def verify_write_scope(self) -> SalesforceOrganizationIdentity:
            """Return the currently selected fake writer organization."""
            return SalesforceOrganizationIdentity(
                self.writer_org_id,
                "Grant Sandbox",
                True,
                "TEST",
                "https://writer.salesforce.test",
            )

    conn = db.connect(tmp_path / "batch.db")
    _insert_leads(conn, "IL", LeadGrade.GOLD, 1, 0)
    gateway = SwitchingGateway(write_mode="timeout_after_write")
    batch = prepare_campaign_batch(
        conn,
        gateway,
        "TWORK",
        "CGRANTS",
        "123.4",
        "UREP",
        (
            CampaignTargetRequest(
                _link("Campaign", CAMPAIGNS["IL"][0]), "IL", ("gold",)
            ),
        ),
    )
    action = batch.actions[0]
    first = campaigns.confirm_action(
        conn,
        gateway,
        action.action_id,
        action.nonce,
        "TWORK",
        "CGRANTS",
        "123.4",
        "UREP",
    )
    assert first.state is campaigns.CampaignActionState.UNKNOWN
    gateway.writer_org_id = "00D000000000999"
    with pytest.raises(PermissionError, match="writer org changed"):
        campaigns.confirm_action(
            conn,
            gateway,
            action.action_id,
            action.nonce,
            "TWORK",
            "CGRANTS",
            "123.4",
            "UREP",
        )
    assert gateway.create_member_calls == 1
    assert (
        conn.execute(
            "SELECT state FROM crm_actions WHERE id=?", (action.action_id,)
        ).fetchone()[0]
        == "unknown"
    )


def test_returned_campaign_member_id_must_match_readback_id(tmp_path: Path) -> None:
    """A same-target member with a different ID cannot be credited to Grant."""

    class MismatchedMemberGateway(BatchGateway):
        """Return one POST ID while exposing another ID during readback."""

        def create_members(
            self, payloads: list[dict[str, object]]
        ) -> list[CreateResult]:
            """Persist deliberately mismatched CampaignMember evidence."""
            self.create_member_calls += 1
            target_id = str(payloads[0].get("LeadId") or payloads[0].get("ContactId"))
            self.members[target_id] = ("00v999999999999", str(payloads[0]["Status"]))
            return [CreateResult(True, "00v000000000001")]

    conn = db.connect(tmp_path / "batch.db")
    _insert_leads(conn, "IL", LeadGrade.GOLD, 1, 0)
    gateway = MismatchedMemberGateway()
    batch = prepare_campaign_batch(
        conn,
        gateway,
        "TWORK",
        "CGRANTS",
        "123.4",
        "UREP",
        (
            CampaignTargetRequest(
                _link("Campaign", CAMPAIGNS["IL"][0]), "IL", ("gold",)
            ),
        ),
    )
    action = batch.actions[0]
    result = campaigns.confirm_action(
        conn,
        gateway,
        action.action_id,
        action.nonce,
        "TWORK",
        "CGRANTS",
        "123.4",
        "UREP",
    )
    assert result.state is campaigns.CampaignActionState.UNKNOWN
    assert result.added == 0 and result.unknown == 1
    replay = campaigns.confirm_action(
        conn,
        gateway,
        action.action_id,
        action.nonce,
        "TWORK",
        "CGRANTS",
        "123.4",
        "UREP",
    )
    assert replay.state is campaigns.CampaignActionState.UNKNOWN
    assert replay.added == 0 and gateway.create_member_calls == 1


def test_campaign_replay_uses_returned_id_without_second_create(tmp_path: Path) -> None:
    """A persisted Campaign response can be reconciled after a local-state crash."""

    class CampaignGateway(BatchGateway):
        """Add Campaign creation behavior to the batch fake."""

        campaign_create_calls = 0

        def campaign_picklists(self) -> tuple[set[str], set[str]]:
            """Return the approved default preview values."""
            return {"Other"}, {"Planned"}

        def create_campaign(self, _payload: dict[str, object]) -> CreateResult:
            """Return one Campaign ID while counting POST attempts."""
            self.campaign_create_calls += 1
            return CreateResult(True, CAMPAIGNS["IL"][0])

    conn = db.connect(tmp_path / "batch.db")
    gateway = CampaignGateway()
    action = campaigns.prepare_campaign_creation(
        conn,
        gateway,
        "TWORK",
        "CGRANTS",
        "123.4",
        "UREP",
        campaigns.CampaignDraft("IL Grant 2026"),
    )
    first = campaigns.confirm_action(
        conn,
        gateway,
        action.action_id,
        action.nonce,
        "TWORK",
        "CGRANTS",
        "123.4",
        "UREP",
    )
    assert first.state is campaigns.CampaignActionState.COMPLETE
    conn.execute(
        "UPDATE crm_actions SET state='unknown',campaign_id=NULL WHERE id=?",
        (action.action_id,),
    )
    conn.commit()
    second = campaigns.confirm_action(
        conn,
        gateway,
        action.action_id,
        action.nonce,
        "TWORK",
        "CGRANTS",
        "123.4",
        "UREP",
    )
    assert second.state is campaigns.CampaignActionState.COMPLETE
    assert gateway.campaign_create_calls == 1


def test_campaign_creation_requires_the_approved_name_on_first_readback(
    tmp_path: Path,
) -> None:
    """A returned Campaign ID cannot complete against a differently named record."""

    class WrongNameGateway(BatchGateway):
        """Create the approved ID but return a mismatched Campaign name."""

        def campaign_picklists(self) -> tuple[set[str], set[str]]:
            """Return the approved default preview values."""
            return {"Other"}, {"Planned"}

        def create_campaign(self, _payload: dict[str, object]) -> CreateResult:
            """Return one syntactically valid Campaign ID."""
            return CreateResult(True, CAMPAIGNS["IL"][0])

        def get_record(self, sobject: str, record_id: str) -> SalesforceRecordRef:
            """Return a wrong-name Campaign for immediate verification."""
            if sobject == "Campaign":
                return SalesforceRecordRef(
                    "Campaign",
                    record_id,
                    "Wrong Campaign",
                    _link("Campaign", record_id),
                )
            return super().get_record(sobject, record_id)

    conn = db.connect(tmp_path / "batch.db")
    gateway = WrongNameGateway()
    action = campaigns.prepare_campaign_creation(
        conn,
        gateway,
        "TWORK",
        "CGRANTS",
        "123.4",
        "UREP",
        campaigns.CampaignDraft("IL Grant 2026"),
    )
    result = campaigns.confirm_action(
        conn,
        gateway,
        action.action_id,
        action.nonce,
        "TWORK",
        "CGRANTS",
        "123.4",
        "UREP",
    )
    assert result.state is campaigns.CampaignActionState.UNKNOWN


def test_explicit_resolved_only_subset_stays_partial_by_user(tmp_path: Path) -> None:
    """A human-approved exclusion never lets the parent claim full completion."""
    conn = db.connect(tmp_path / "batch.db")
    _insert_leads(conn, "IL", LeadGrade.GOLD, 2, 0)
    gateway = BatchGateway(missing_names={"IL Organization 001"})
    batch = prepare_campaign_batch(
        conn,
        gateway,
        "TWORK",
        "CGRANTS",
        "123.4",
        "UREP",
        (
            CampaignTargetRequest(
                _link("Campaign", CAMPAIGNS["IL"][0]), "IL", ("gold",)
            ),
        ),
        allow_resolved_only=True,
    )
    assert batch.state == "partial_by_user"
    assert len(batch.actions) == 1
    action = batch.actions[0]
    result = campaigns.confirm_action(
        conn,
        gateway,
        action.action_id,
        action.nonce,
        "TWORK",
        "CGRANTS",
        "123.4",
        "UREP",
    )
    assert result.state is campaigns.CampaignActionState.COMPLETE
    assert "0 unresolved" in result.message
    assert "IL Organization 001 (IL)" in result.message
    assert "Explicitly excluded/skipped before approval: 1" in result.message
    assert (
        conn.execute(
            "SELECT state FROM crm_campaign_batches WHERE id=?", (batch.batch_id,)
        ).fetchone()[0]
        == "partial_by_user"
    )


def test_three_target_resolved_only_batch_never_loses_partial_mode(
    tmp_path: Path,
) -> None:
    """Completing multiple children cannot upgrade an approved subset to complete."""
    conn = db.connect(tmp_path / "batch.db")
    _insert_leads(conn, "IL", LeadGrade.GOLD, 2, 0)
    _insert_leads(conn, "FL", LeadGrade.GOLD, 1, 10)
    _insert_leads(conn, "TX", LeadGrade.GOLD, 2, 20)
    gateway = BatchGateway(
        missing_names={
            "IL Organization 001",
            "FL Organization 010",
            "TX Organization 021",
        }
    )
    batch = prepare_campaign_batch(
        conn,
        gateway,
        "TWORK",
        "CGRANTS",
        "123.4",
        "UREP",
        tuple(
            CampaignTargetRequest(
                _link("Campaign", CAMPAIGNS[state][0]), state, ("gold",)
            )
            for state in ("IL", "FL", "TX")
        ),
        allow_resolved_only=True,
    )
    assert len(batch.actions) == 2
    for action in batch.actions:
        result = campaigns.confirm_action(
            conn,
            gateway,
            action.action_id,
            action.nonce,
            "TWORK",
            "CGRANTS",
            "123.4",
            "UREP",
        )
        assert result.state is campaigns.CampaignActionState.COMPLETE
    parent = conn.execute(
        """SELECT state,completion_mode FROM crm_campaign_batches WHERE id=?""",
        (batch.batch_id,),
    ).fetchone()
    assert tuple(parent) == ("partial_by_user", "partial_by_user")
    target_states = {
        row[0]
        for row in conn.execute(
            "SELECT state FROM crm_campaign_batch_targets WHERE batch_id=?",
            (batch.batch_id,),
        )
    }
    assert target_states == {"partial_by_user"}


def test_batch_selection_honors_every_human_disposition(tmp_path: Path) -> None:
    """Snoozed, rejected, and dead leads never enter a mutating Campaign scope."""
    conn = db.connect(tmp_path / "batch.db")
    _insert_leads(conn, "IL", LeadGrade.GOLD, 6, 0)
    conn.executemany(
        "UPDATE leads SET status=? WHERE id=?",
        (
            ("surfaced", 2),
            ("contacted", 3),
            ("snoozed", 4),
            ("not_relevant", 5),
            ("dead", 6),
        ),
    )
    conn.commit()
    batch = prepare_campaign_batch(
        conn,
        BatchGateway(),
        "TWORK",
        "CGRANTS",
        "123.4",
        "UREP",
        (
            CampaignTargetRequest(
                _link("Campaign", CAMPAIGNS["IL"][0]), "IL", ("gold",)
            ),
        ),
    )
    target = conn.execute(
        """SELECT source_row_count,approved_org_count
           FROM crm_campaign_batch_targets WHERE batch_id=?""",
        (batch.batch_id,),
    ).fetchone()
    assert tuple(target) == (3, 3)


def test_campaign_click_rechecks_disposition_before_any_write(tmp_path: Path) -> None:
    """A lead snoozed after preview invalidates the button before Salesforce I/O."""
    conn = db.connect(tmp_path / "batch.db")
    _insert_leads(conn, "IL", LeadGrade.GOLD, 1, 0)
    gateway = BatchGateway()
    batch = prepare_campaign_batch(
        conn,
        gateway,
        "TWORK",
        "CGRANTS",
        "123.4",
        "UREP",
        (
            CampaignTargetRequest(
                _link("Campaign", CAMPAIGNS["IL"][0]), "IL", ("gold",)
            ),
        ),
    )
    action = batch.actions[0]
    conn.execute("UPDATE leads SET status='snoozed'")
    conn.commit()

    with pytest.raises(ValueError, match="became ineligible"):
        campaigns.confirm_action(
            conn,
            gateway,
            action.action_id,
            action.nonce,
            "TWORK",
            "CGRANTS",
            "123.4",
            "UREP",
        )

    assert gateway.create_member_calls == 0


def test_untagged_row_cannot_inherit_a_neighboring_nces_identity(
    tmp_path: Path,
) -> None:
    """A same-name row without an NCES ID remains separate and ambiguous."""
    conn = db.connect(tmp_path / "batch.db")
    _insert_leads(conn, "IL", LeadGrade.GOLD, 1, 0)
    _insert_leads(conn, "IL", LeadGrade.SILVER, 1, 0)
    conn.execute("UPDATE leads SET nces_id='1234567' WHERE id=1")
    conn.commit()
    batch = prepare_campaign_batch(
        conn,
        BatchGateway(),
        "TWORK",
        "CGRANTS",
        "123.4",
        "UREP",
        (
            CampaignTargetRequest(
                _link("Campaign", CAMPAIGNS["IL"][0]),
                "IL",
                ("gold", "silver"),
            ),
        ),
    )
    assert batch.actions == ()
    rows = list(
        conn.execute(
            """SELECT canonical_entity_key,resolution_state
               FROM crm_campaign_batch_items ORDER BY canonical_entity_key"""
        )
    )
    assert len(rows) == 2
    assert {row["resolution_state"] for row in rows} == {
        "existing_record",
        "ambiguous",
    }
    assert any(str(row["canonical_entity_key"]).startswith("unbound:") for row in rows)


def test_distinct_nces_entities_cannot_share_one_salesforce_member(
    tmp_path: Path,
) -> None:
    """Two authoritative organizations mapped to one CRM ID fail closed."""

    class SharedRecordGateway(BatchGateway):
        """Return the same exact-looking Lead for both authoritative entities."""

        def find_people(
            self, entity_name: str, state: str
        ) -> list[SalesforceRecordRef]:
            """Return one stable Salesforce ID for each lookup."""
            return [
                SalesforceRecordRef(
                    "Lead",
                    "00Q000000000777",
                    "Shared Contact",
                    _link("Lead", "00Q000000000777"),
                    company=entity_name,
                    state=state,
                )
            ]

    conn = db.connect(tmp_path / "batch.db")
    _insert_leads(conn, "IL", LeadGrade.GOLD, 1, 0)
    _insert_leads(conn, "IL", LeadGrade.SILVER, 1, 0)
    conn.execute("UPDATE leads SET nces_id='1234567' WHERE id=1")
    conn.execute("UPDATE leads SET nces_id='7654321' WHERE id=2")
    conn.commit()
    batch = prepare_campaign_batch(
        conn,
        SharedRecordGateway(),
        "TWORK",
        "CGRANTS",
        "123.4",
        "UREP",
        (
            CampaignTargetRequest(
                _link("Campaign", CAMPAIGNS["IL"][0]),
                "IL",
                ("gold", "silver"),
            ),
        ),
    )
    assert batch.actions == ()
    assert {
        row[0]
        for row in conn.execute("SELECT resolution_state FROM crm_campaign_batch_items")
    } == {"ambiguous"}
