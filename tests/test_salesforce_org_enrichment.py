"""Contact-independent, blank-only Salesforce organization enrichment tests."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from grant_watch import db
from grant_watch.enrich import salesforce_campaign_gateway as gateway_mod
from grant_watch.enrich import salesforce_campaigns as campaigns
from grant_watch.enrich import salesforce_org_enrichment as enrichment
from grant_watch.enrich.organization_profile import OrganizationProfile
from grant_watch.models import (
    FundingEventType,
    Lead,
    LeadGrade,
    RawItem,
    VerificationStatus,
)

LEAD_ID = "00Q000000000001"


class FakeGateway:
    """Exact blank organization Lead boundary with one audited update call."""

    def __init__(
        self, company: str = "Birmingham Community Charter High School"
    ) -> None:
        self.company = company
        self.values: dict[str, str | float | None] = {
            key: None for key in gateway_mod._LEAD_ENRICHMENT_FIELDS
        }
        self.values["State"] = "CA"
        self.stamp = "2026-07-15T22:00:00.000+0000"
        self.calls: list[dict[str, object]] = []

    def lead_enrichment_snapshot(
        self, lead_id: str
    ) -> gateway_mod.LeadEnrichmentSnapshot:
        """Return the current exact organization-only Lead state."""
        assert lead_id == LEAD_ID
        return gateway_mod.LeadEnrichmentSnapshot(
            LEAD_ID,
            self.company,
            "",
            self.stamp,
            dict(self.values),
            "https://writer.test/lead",
        )

    def enrich_lead_with_audit_bundle(
        self,
        lead_id: str,
        delta: dict[str, object],
        expected_system_modstamp: str,
        action_id: str,
        _note_body: str,
        _task_description: str,
        _activity_date: str,
    ) -> gateway_mod.LeadAuditResult:
        """Apply the one immutable update and its audit records."""
        assert lead_id == LEAD_ID and expected_system_modstamp == self.stamp
        self.calls.append(delta)
        self.values.update(delta)  # type: ignore[arg-type]  # fake mirrors Salesforce JSON
        self.stamp = "2026-07-15T22:01:00.000+0000"
        return gateway_mod.LeadAuditResult(
            True,
            "069000000000001",
            "06A000000000001",
            "00T000000000001",
            lead_id=lead_id,
        )

    def verify_lead_audit_bundle(
        self,
        _lead_id: str,
        _action_id: str,
        _note_body: str,
        _task_description: str,
        _result: gateway_mod.LeadAuditResult,
    ) -> bool:
        """Accept the deterministic audit readback."""
        return True


@pytest.fixture(autouse=True)
def config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable only exact Lead enrichment and its audit bundle."""
    monkeypatch.setenv("GRANT_SALESFORCE_WRITE_CHANNEL_IDS", "CGRANTS")
    monkeypatch.setenv("SALESFORCE_WRITE_MY_DOMAIN_URL", "https://writer.test")
    monkeypatch.setenv("SALESFORCE_LEAD_ENRICHMENT_UPDATES_ENABLED", "1")
    monkeypatch.setenv("SALESFORCE_GRANT_AUDIT_RECORDS_ENABLED", "1")


def _lead(tmp_path: Path) -> tuple[sqlite3.Connection, int]:
    """Persist one verified Birmingham award with no contact rows."""
    conn = db.connect(tmp_path / "org-enrichment.db")
    item = RawItem(
        source="usaspending:16.071",
        item_id="birmingham",
        title="School Violence Prevention Program award",
        entity="Birmingham Community Charter High School",
        state="CA",
        program="SVPP",
        amount=500_000,
        start="2025-10-01",
        end="2028-09-30",
        url="https://www.usaspending.gov/award/ASST_NON_15JCOPS25GG01291SSIX_015",
        raw={},
        event_type=FundingEventType.AWARD_OBLIGATED,
        verification_status=VerificationStatus.VERIFIED,
        evidence_excerpt="Published award record",
    )
    db.upsert_lead(conn, Lead(item, LeadGrade.GOLD, entity_type="school"))
    lead_id = int(conn.execute("SELECT id FROM leads").fetchone()[0])
    return conn, lead_id


def _profile() -> OrganizationProfile:
    """Return complete official-site organization data for offline tests."""
    return OrganizationProfile(
        website="https://www.bcchs.net/",
        street="17000 Haynes St.",
        city="Lake Balboa",
        state="CA",
        postal_code="91406",
        country="United States",
        main_phone="(818) 758-5200",
        source_url="https://www.bcchs.net/contact",
    )


def test_preview_needs_no_contact_and_fills_complete_blank_address(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No contact row or email is needed for verified organization-only fields."""
    conn, lead_id = _lead(tmp_path)
    monkeypatch.setattr(
        enrichment.finder,
        "find_official_site",
        lambda *_args: enrichment.finder.OfficialSite(
            "bcchs.net", "https://www.bcchs.net/contact", "official site"
        ),
    )
    monkeypatch.setattr(enrichment, "fetch_profile", lambda *_args: _profile())
    action = campaigns.prepare_organization_lead_enrichment(
        conn,
        FakeGateway(),
        "T",
        "CGRANTS",
        "1.1",
        "U",
        lead_id,
        f"https://writer.test/lightning/r/Lead/{LEAD_ID}/view",
    )
    payload = json.loads(
        conn.execute("SELECT payload_json FROM crm_actions").fetchone()[0]
    )
    delta = payload["delta"]
    assert {"Street", "City", "PostalCode", "Country", "Website", "Phone"} <= set(delta)
    assert not ({"FirstName", "LastName", "Email", "Title"} & set(delta))
    assert "Grant research summary for Birmingham" in delta["Description"]
    assert "salesforce_lead_enrichment_preview" not in delta["Description"]
    assert "No person, email, Campaign" in action.preview


def test_wrong_salesforce_company_fails_before_preview(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An exact Lead link cannot cross organization identity boundaries."""
    conn, lead_id = _lead(tmp_path)
    monkeypatch.setattr(enrichment, "_profile", lambda *_args: _profile())
    with pytest.raises(ValueError, match="does not match"):
        campaigns.prepare_organization_lead_enrichment(
            conn,
            FakeGateway("Other School"),
            "T",
            "CGRANTS",
            "1.1",
            "U",
            lead_id,
            f"https://writer.test/lightning/r/Lead/{LEAD_ID}/view",
        )


def test_confirmation_updates_one_exact_lead_and_creates_no_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Confirmation reuses the existing single-record audited update boundary."""
    conn, lead_id = _lead(tmp_path)
    monkeypatch.setattr(enrichment, "_profile", lambda *_args: _profile())
    gateway = FakeGateway()
    action = campaigns.prepare_organization_lead_enrichment(
        conn,
        gateway,
        "T",
        "CGRANTS",
        "1.1",
        "U",
        lead_id,
        f"https://writer.test/lightning/r/Lead/{LEAD_ID}/view",
    )
    result = campaigns.confirm_action(
        conn, gateway, action.action_id, action.nonce, "T", "CGRANTS", "1.1", "U"
    )
    assert result.added == 1 and len(gateway.calls) == 1
    assert (
        conn.execute("SELECT state FROM crm_action_items").fetchone()[0]
        == "lead_enriched"
    )
