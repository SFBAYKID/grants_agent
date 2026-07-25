"""Shared deterministic fixtures for exact Salesforce Campaign batch tests."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

import requests

from grant_watch import db
from grant_watch.enrich.salesforce_campaign_batch_models import CampaignTargetRequest
from grant_watch.enrich.salesforce_campaign_gateway import (
    CreateResult,
    SalesforceOrganizationIdentity,
    SalesforceRecordRef,
)
from grant_watch.models import FundingEventType, Lead, LeadGrade, RawItem

CAMPAIGNS = {
    "IL": ("701000000000001", "IL Grant 2026"),
    "FL": ("701000000000002", "FL Grant 2026"),
    "TX": ("701000000000003", "TX Grant 2026"),
}


def campaign_link(sobject: str, record_id: str) -> str:
    """Build one fake Lightning URL for the configured sandbox writer."""
    return f"https://writer.salesforce.test/lightning/r/{sobject}/{record_id}/view"


@dataclass
class BatchGateway:
    """Deterministic Salesforce boundary with controllable readback behavior."""

    missing_names: set[str] = field(default_factory=set)
    account_names: set[str] = field(default_factory=set)
    members: dict[str, tuple[str, str]] = field(default_factory=dict)
    records: dict[str, SalesforceRecordRef] = field(default_factory=dict)
    latent_members: dict[str, tuple[str, str]] = field(default_factory=dict)
    visibility_lag: int = 0
    write_mode: str = "normal"
    create_member_calls: int = 0
    reconciliation_delays: tuple[float, ...] = (0.0, 0.0, 0.0)

    def verify_write_scope(self) -> SalesforceOrganizationIdentity:
        """Return one frozen sandbox identity."""
        return SalesforceOrganizationIdentity(
            "00D000000000001",
            "Grant Sandbox",
            True,
            "TEST",
            "https://writer.salesforce.test",
        )

    def get_record(self, sobject: str, record_id: str) -> SalesforceRecordRef:
        """Read a fake Campaign or previously resolved person."""
        if sobject == "Campaign":
            state = next(
                state for state, value in CAMPAIGNS.items() if value[0] == record_id
            )
            return SalesforceRecordRef(
                "Campaign",
                record_id,
                CAMPAIGNS[state][1],
                campaign_link("Campaign", record_id),
            )
        return self.records[record_id]

    def find_people(self, entity_name: str, state: str) -> list[SalesforceRecordRef]:
        """Resolve every organization except test-declared missing names."""
        if entity_name in self.missing_names or entity_name in self.account_names:
            return []
        record_id = f"00Q{len(self.records) + 1:012d}"
        ref = SalesforceRecordRef(
            "Lead",
            record_id,
            f"{entity_name} Contact",
            campaign_link("Lead", record_id),
            company=entity_name,
            state=state,
        )
        self.records.setdefault(record_id, ref)
        return [ref]

    def find_accounts(self, entity_name: str, state: str) -> list[SalesforceRecordRef]:
        """Return exact Accounts only for test-declared organizations."""
        if entity_name not in self.account_names:
            return []
        record_id = f"001{len(self.account_names):012d}"
        return [
            SalesforceRecordRef(
                "Account",
                record_id,
                entity_name,
                campaign_link("Account", record_id),
                company=entity_name,
                state=state,
                account_id=record_id,
            )
        ]

    def member_status_exists(self, _campaign_id: str) -> bool:
        """Keep status setup outside membership test noise."""
        return True

    def create_member_status(self, _campaign_id: str) -> CreateResult:
        """Provide protocol completeness for failure diagnostics."""
        return CreateResult(True, "01Y000000000001")

    def member_records(
        self, _campaign_id: str, record_ids: list[str]
    ) -> dict[str, tuple[str, str]]:
        """Expose members after an optional bounded visibility delay."""
        if self.latent_members:
            if self.visibility_lag > 0:
                self.visibility_lag -= 1
            else:
                self.members.update(self.latent_members)
                self.latent_members.clear()
        return {
            record_id: self.members[record_id]
            for record_id in record_ids
            if record_id in self.members
        }

    def existing_members(self, campaign_id: str, record_ids: list[str]) -> set[str]:
        """Support the legacy read method used by older workflows."""
        return set(self.member_records(campaign_id, record_ids))

    def create_members(self, payloads: list[dict[str, object]]) -> list[CreateResult]:
        """Create, delay, hide, or time out member records deterministically."""
        self.create_member_calls += 1
        results: list[CreateResult] = []
        created: dict[str, tuple[str, str]] = {}
        for index, payload in enumerate(payloads, start=1):
            target_id = str(payload.get("LeadId") or payload.get("ContactId"))
            member_id = f"00v{self.create_member_calls:03d}{index:09d}"
            results.append(CreateResult(True, member_id))
            created[target_id] = (member_id, str(payload["Status"]))
        if self.write_mode == "normal":
            if self.visibility_lag:
                self.latent_members.update(created)
            else:
                self.members.update(created)
        elif self.write_mode == "timeout_after_write":
            self.members.update(created)
            raise requests.Timeout("response lost")
        return results

    def create_leads(self, _payloads: list[dict[str, object]]) -> list[CreateResult]:
        """Organization-only creation is not used in these exact-match tests."""
        raise AssertionError("unexpected organization Lead creation")


def insert_leads(
    conn: sqlite3.Connection,
    state: str,
    grade: LeadGrade,
    count: int,
    offset: int,
) -> None:
    """Insert a deterministic set of Grant rows for one state and tier."""
    for index in range(count):
        number = offset + index
        db.upsert_lead(
            conn,
            Lead(
                item=RawItem(
                    source="batch-test",
                    item_id=f"{state}-{grade.value}-{number}",
                    title="verified security grant",
                    entity=f"{state} Organization {number:03d}",
                    state=state,
                    program="SVPP",
                    amount=100_000,
                    start="2026-01-01",
                    end="2027-01-01",
                    url=f"https://source.test/{state}/{number}",
                    raw={},
                    event_type=FundingEventType.AWARD_OBLIGATED,
                ),
                grade=grade,
            ),
        )


def incident_matrix(conn: sqlite3.Connection) -> None:
    """Create the exact six state/tier sets from the reported Slack incident."""
    specs = (
        ("IL", LeadGrade.GOLD, 15, 0),
        ("IL", LeadGrade.SILVER, 18, 100),
        ("FL", LeadGrade.GOLD, 1, 200),
        ("FL", LeadGrade.SILVER, 4, 300),
        ("TX", LeadGrade.GOLD, 9, 400),
        ("TX", LeadGrade.SILVER, 20, 500),
    )
    for spec in specs:
        insert_leads(conn, *spec)


def incident_requests() -> tuple[CampaignTargetRequest, ...]:
    """Return one Gold+Silver target for each incident Campaign."""
    return tuple(
        CampaignTargetRequest(
            campaign_link("Campaign", CAMPAIGNS[state][0]),
            state,
            ("gold", "silver"),
        )
        for state in ("IL", "FL", "TX")
    )
