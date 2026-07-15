"""One-at-a-time Salesforce Opportunity approval safety tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from grant_watch import db
from grant_watch.enrich import salesforce_campaigns as campaigns
from grant_watch.enrich import salesforce_campaign_gateway as gateway_mod

ACCOUNT_ID = "001000000000001"
OWNER_ID = "005000000000001"
OPP_ID = "006000000000001"


class FakeGateway:
    """Exact Account/Opportunity boundary with call recording."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.open_items: list[gateway_mod.OpportunityRecord] = []

    def get_record(self, sobject: str, record_id: str) -> gateway_mod.SalesforceRecordRef:
        """Return the exact Account reference."""
        assert (sobject, record_id) == ("Account", ACCOUNT_ID)
        return gateway_mod.SalesforceRecordRef(
            "Account", ACCOUNT_ID, "Dinuba Unified School District",
            f"https://writer.test/lightning/r/Account/{ACCOUNT_ID}/view", state="CA")

    def opportunity_stages(self) -> set[str]:
        """Return one active sandbox stage."""
        return {"Prospecting"}

    def open_opportunities(self, _account_id: str) -> list[gateway_mod.OpportunityRecord]:
        """Return configured exact open records."""
        return list(self.open_items)

    def create_opportunity(self, _payload: dict[str, object]) -> gateway_mod.CreateResult:
        """Create one record and expose it to readback."""
        self.calls.append("create_opportunity")
        self.open_items.append(gateway_mod.OpportunityRecord(
            OPP_ID, "Dinuba Security Project", ACCOUNT_ID, "Prospecting",
            "2026-12-31", OWNER_ID, 3_000_000, False,
            f"https://writer.test/lightning/r/Opportunity/{OPP_ID}/view"))
        return gateway_mod.CreateResult(True, OPP_ID)


@pytest.fixture(autouse=True)
def config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable only the exact Opportunity action in the test channel."""
    monkeypatch.setenv("GRANT_SALESFORCE_WRITE_CHANNEL_IDS", "CGRANTS")
    monkeypatch.setenv("SALESFORCE_WRITE_MY_DOMAIN_URL", "https://writer.test")
    monkeypatch.setenv("SALESFORCE_CAMPAIGN_WRITES_ENABLED", "0")
    monkeypatch.setenv("SALESFORCE_OPPORTUNITY_WRITES_ENABLED", "1")


def _prepare(tmp_path: Path, gateway: FakeGateway) -> tuple[object, campaigns.PreparedAction]:
    """Create one immutable Opportunity preview."""
    conn = db.connect(tmp_path / "opp.db")
    action = campaigns.prepare_opportunity_creation(
        conn, gateway, "TWORK", "CGRANTS", "1.1", "UCHASE",
        f"https://writer.test/lightning/r/Account/{ACCOUNT_ID}/view",
        "Dinuba Security Project", "Prospecting", "2026-12-31",
        OWNER_ID, "Chase", 3_000_000)
    return conn, action


def test_preview_is_duplicate_checked_and_write_free(tmp_path: Path) -> None:
    """Preview freezes one Account-bound deal without creating anything."""
    gateway = FakeGateway()
    conn, action = _prepare(tmp_path, gateway)
    assert "Dinuba Security Project" in action.preview and gateway.calls == []
    assert conn.execute("SELECT action_type FROM crm_actions").fetchone()[0] == "create_opportunity"


def test_duplicate_at_confirmation_prevents_create(tmp_path: Path) -> None:
    """A matching open deal appearing after preview closes the race."""
    gateway = FakeGateway()
    conn, action = _prepare(tmp_path, gateway)
    gateway.open_items.append(gateway_mod.OpportunityRecord(
        OPP_ID, "Dinuba Security Project", ACCOUNT_ID, "Prospecting", "2026-12-31",
        OWNER_ID, None, False, "https://writer.test/existing"))
    result = campaigns.confirm_action(
        conn, gateway, action.action_id, action.nonce,
        "TWORK", "CGRANTS", "1.1", "UCHASE")
    assert result.already_present == 1 and gateway.calls == []


def test_confirmation_creates_exactly_one_opportunity(tmp_path: Path) -> None:
    """One click creates one Opportunity and validates exact readback."""
    gateway = FakeGateway()
    conn, action = _prepare(tmp_path, gateway)
    result = campaigns.confirm_action(
        conn, gateway, action.action_id, action.nonce,
        "TWORK", "CGRANTS", "1.1", "UCHASE")
    assert result.added == 1 and gateway.calls == ["create_opportunity"]
    row = conn.execute("SELECT state,salesforce_id FROM crm_action_items").fetchone()
    assert tuple(row) == ("opportunity_created", OPP_ID)
