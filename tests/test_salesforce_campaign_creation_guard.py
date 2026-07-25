"""Deterministic guards for complete, single Salesforce Campaign previews."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from grant_watch import db
from grant_watch.enrich import salesforce_campaigns as campaigns
from grant_watch.enrich.salesforce_campaign_gateway import SalesforceRecordRef
from grant_watch.migrations_campaign_preview import (
    migration_28_single_ready_campaign_creation,
)
from grant_watch.slack import conversation
from grant_watch.slack import salesforce_campaign_tools as campaign_tools


class GuardGateway:
    """Offline Salesforce boundary used only after local argument validation."""

    def __init__(self) -> None:
        """Track whether validation reached owner or picklist lookups."""
        self.owner_lookups = 0
        self.picklist_lookups = 0

    def find_active_user_by_email(self, _email: str) -> list[SalesforceRecordRef]:
        """Resolve one requester owner without reaching Salesforce."""
        self.owner_lookups += 1
        return [
            SalesforceRecordRef(
                "User",
                "005000000000001AAA",
                "Chase Test",
                "https://writer.salesforce.test/lightning/r/User/005000000000001AAA/view",
            )
        ]

    def campaign_picklists(self) -> tuple[set[str], set[str]]:
        """Expose one active Type and Status."""
        self.picklist_lookups += 1
        return {"Other"}, {"Planned"}


@pytest.fixture
def preview_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[sqlite3.Connection, GuardGateway, list[int]]:
    """Install one isolated DB and gateway factory for Slack preview tests."""
    conn = db.connect(tmp_path / "campaign-preview.db")
    gateway = GuardGateway()
    factory_calls: list[int] = []

    def gateway_factory() -> GuardGateway:
        """Record each construction after validation succeeds."""
        factory_calls.append(1)
        return gateway

    monkeypatch.setenv("GRANT_SALESFORCE_WRITE_CHANNEL_IDS", "CGRANTS")
    monkeypatch.setattr(campaign_tools.db, "connect", lambda: conn)
    monkeypatch.setattr(campaign_tools, "SalesforceCampaignGateway", gateway_factory)
    monkeypatch.setattr(
        campaign_tools.persequor_client,
        "rep_email_for",
        lambda _slack: "chase@example.test",
    )
    return conn, gateway, factory_calls


def _complete_args(**overrides: object) -> dict[str, object]:
    """Return one fully explicit no-dates Campaign request."""
    args: dict[str, object] = {
        "name": "Grant Campaign Guard Test",
        "campaign_type": "Other",
        "status": "Planned",
        "is_active": True,
        "date_mode": "none",
    }
    args.update(overrides)
    return args


def _preview(
    args: dict[str, object],
    *,
    thread_ts: str = "123.4",
) -> str:
    """Call the production Slack tool with one fixed requester context."""
    return campaign_tools.salesforce_campaign_create_preview(
        args,
        "UCHASE",
        "TWORK",
        "CGRANTS",
        thread_ts,
    )


def test_create_schema_requires_every_setting_and_hides_owner_fields() -> None:
    """The model cannot omit settings or author Salesforce ownership."""
    schema = campaign_tools.CAMPAIGN_CREATE_TOOL_SCHEMA["input_schema"]
    assert set(schema["required"]) == {
        "name",
        "campaign_type",
        "status",
        "is_active",
        "date_mode",
    }
    properties = schema["properties"]
    assert "owner_id" not in properties
    assert "owner_label" not in properties
    assert all("default" not in properties[key] for key in schema["required"])


@pytest.mark.parametrize(
    "args,error",
    [
        ({"name": "Incomplete"}, "campaign_type"),
        (_complete_args(is_active="false"), "explicit boolean"),
        (_complete_args(date_mode="none", start_date="2026-01-01"), "cannot include"),
        (_complete_args(date_mode="range"), "both dates"),
        (
            _complete_args(
                date_mode="range",
                start_date="01/01/2026",
                end_date="2026-12-31",
            ),
            "YYYY-MM-DD",
        ),
        (
            _complete_args(
                date_mode="range",
                start_date="2026-12-31",
                end_date="2026-01-01",
            ),
            "cannot be after",
        ),
    ],
)
def test_invalid_settings_fail_before_gateway_or_database(
    preview_context: tuple[sqlite3.Connection, GuardGateway, list[int]],
    args: dict[str, object],
    error: str,
) -> None:
    """Incomplete or malformed settings cannot reach Salesforce or persistence."""
    conn, gateway, factory_calls = preview_context
    result = _preview(args)
    assert "ERROR: Campaign preview failed" in result
    assert error in result
    assert factory_calls == []
    assert gateway.owner_lookups == 0
    assert conn.execute("SELECT COUNT(*) FROM crm_actions").fetchone()[0] == 0


def test_explicit_no_dates_persists_exactly_one_preview(
    preview_context: tuple[sqlite3.Connection, GuardGateway, list[int]],
) -> None:
    """A complete no-dates request freezes exact settings and one button marker."""
    conn, gateway, factory_calls = preview_context
    result = _preview(_complete_args())
    assert result.count("<grant-crm-action>") == 1
    row = conn.execute("SELECT state,payload_json FROM crm_actions").fetchone()
    payload = json.loads(str(row["payload_json"]))["campaign"]
    assert row["state"] == "ready"
    assert payload["Name"] == "Grant Campaign Guard Test"
    assert payload["Type"] == "Other"
    assert payload["Status"] == "Planned"
    assert payload["IsActive"] is True
    assert "StartDate" not in payload
    assert "EndDate" not in payload
    assert len(factory_calls) == 1
    assert gateway.owner_lookups == 1
    assert gateway.picklist_lookups == 1


def test_valid_date_range_is_frozen_exactly(
    preview_context: tuple[sqlite3.Connection, GuardGateway, list[int]],
) -> None:
    """Both validated dates survive unchanged in the immutable payload."""
    conn, _gateway, _factory_calls = preview_context
    result = _preview(
        _complete_args(
            name="Dated Campaign",
            date_mode="range",
            start_date="2026-08-01",
            end_date="2026-12-31",
        ),
        thread_ts="dated.1",
    )
    assert result.count("<grant-crm-action>") == 1
    payload = json.loads(
        str(
            conn.execute(
                "SELECT payload_json FROM crm_actions WHERE thread_ts='dated.1'"
            ).fetchone()[0]
        )
    )["campaign"]
    assert payload["StartDate"] == "2026-08-01"
    assert payload["EndDate"] == "2026-12-31"


def test_duplicate_ready_preview_is_blocked_until_cancelled(
    preview_context: tuple[sqlite3.Connection, GuardGateway, list[int]],
) -> None:
    """Model retries cannot produce two executable buttons in one thread."""
    conn, _gateway, _factory_calls = preview_context
    first = _preview(_complete_args())
    second = _preview(_complete_args(name="Corrected Campaign"))
    assert first.count("<grant-crm-action>") == 1
    assert "active Campaign creation preview already exists" in second
    assert conn.execute("SELECT COUNT(*) FROM crm_actions").fetchone()[0] == 1
    action_id = str(conn.execute("SELECT id FROM crm_actions").fetchone()[0])
    assert campaigns.cancel_action(conn, action_id, "UCHASE")
    third = _preview(_complete_args(name="Corrected Campaign"))
    assert third.count("<grant-crm-action>") == 1
    states = [row[0] for row in conn.execute("SELECT state FROM crm_actions")]
    assert sorted(states) == ["cancelled", "ready"]


def test_migration_cancels_older_duplicates_and_adds_unique_index(
    tmp_path: Path,
) -> None:
    """Legacy duplicate ready actions become one active row before indexing."""
    conn = db.connect(tmp_path / "legacy-duplicates.db")
    conn.execute("DROP INDEX ux_crm_one_ready_campaign_creation")
    conn.execute("DELETE FROM schema_migrations WHERE version=28")
    first, _nonce, _expiry = campaigns._store_action(
        conn,
        "create_campaign",
        "TWORK",
        "CGRANTS",
        "123.4",
        "UCHASE",
        {"campaign": {"Name": "First"}},
    )
    second, _nonce, _expiry = campaigns._store_action(
        conn,
        "create_campaign",
        "TWORK",
        "CGRANTS",
        "123.4",
        "UCHASE",
        {"campaign": {"Name": "Second"}},
    )
    migration_28_single_ready_campaign_creation(conn)
    states = {
        row["id"]: row["state"]
        for row in conn.execute("SELECT id,state FROM crm_actions")
    }
    assert states == {first: "cancelled", second: "ready"}
    with pytest.raises(sqlite3.IntegrityError):
        campaigns._store_action(
            conn,
            "create_campaign",
            "TWORK",
            "CGRANTS",
            "123.4",
            "UCHASE",
            {"campaign": {"Name": "Third"}},
        )


def test_system_prompt_forbids_name_only_campaign_preview() -> None:
    """Keep the conversational gate aligned with the deterministic tool guard."""
    assert "A name alone is never preview-ready" in conversation._SYSTEM
    assert "Call the preview tool exactly once" in conversation._SYSTEM
