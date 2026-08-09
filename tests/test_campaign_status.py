"""Answering "who's on that campaign?" without conflating two different numbers.

Grant's own ledger and Salesforce's live membership answer different questions and
can legitimately disagree — someone may have added or removed members by hand. The
danger is not that they differ; it is reporting one as if it were the other, or
answering from memory of what Grant did earlier in the thread, which is a fabricated
CRM read dressed up as a fact.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from grant_watch import db
from grant_watch.enrich import salesforce
from grant_watch.enrich import salesforce_campaigns as campaigns
from grant_watch.enrich.salesforce_campaign_gateway import SalesforceRecordRef
from grant_watch.slack import tools

CAMPAIGN_ID = "701000000000001"


@pytest.fixture(autouse=True)
def _local_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the tool at a throwaway database."""
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "s.db")
    db.connect(tmp_path / "s.db").close()


def _stub_campaign(monkeypatch: pytest.MonkeyPatch, matches: int = 1) -> None:
    """Stub Campaign resolution so no Salesforce call is made for the lookup."""
    record = SalesforceRecordRef(
        sobject="Campaign",
        record_id=CAMPAIGN_ID,
        name="California Grant 2026",
        link=f"https://x.my.salesforce.com/lightning/r/Campaign/{CAMPAIGN_ID}/view",
    )

    class _Gateway:
        """Minimal gateway returning a fixed Campaign."""

        def search_campaigns(self, query: str) -> list[SalesforceRecordRef]:
            """Return the configured number of matches."""
            return [record] * matches

        def get_record(self, sobject: str, record_id: str) -> SalesforceRecordRef:
            """Return the fixed Campaign."""
            return record

    monkeypatch.setattr(campaigns, "SalesforceCampaignGateway", _Gateway)


def test_the_live_count_and_grants_own_count_are_reported_separately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both numbers appear, and the reply says they can legitimately differ."""
    _stub_campaign(monkeypatch)
    monkeypatch.setattr(
        salesforce, "readonly_soql", lambda q: ([{}] * 13, "https://x.test")
    )
    out = tools.salesforce_campaign_status("California Grant 2026")
    assert "13 member(s) on it right now" in out
    assert "live from Salesforce" in out
    assert "can differ legitimately" in out


def test_a_failed_live_read_says_so_instead_of_using_the_local_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Falling back to Grant's own count would answer the wrong question silently."""
    _stub_campaign(monkeypatch)

    def boom(query: str) -> Any:
        """Fail the way an unreachable Salesforce does."""
        raise RuntimeError("unreachable")

    monkeypatch.setattr(salesforce, "readonly_soql", boom)
    out = tools.salesforce_campaign_status("California Grant 2026")
    assert "couldn't read the live member count" in out
    assert "member(s) on it right now" not in out


def test_grant_does_not_claim_to_have_added_anyone_without_a_confirmed_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty ledger must read as "I haven't confirmed adding anyone"."""
    _stub_campaign(monkeypatch)
    monkeypatch.setattr(salesforce, "readonly_soql", lambda q: ([{}] * 5, "h"))
    out = tools.salesforce_campaign_status("California Grant 2026")
    assert "has not confirmed adding anyone" in out


def test_several_matching_campaigns_are_never_silently_picked_between(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reporting on the wrong Campaign is worse than asking which one."""
    _stub_campaign(monkeypatch, matches=2)
    out = tools.salesforce_campaign_status("California Grant")
    assert "2 Campaigns match" in out
    assert "which one" in out


def test_an_unknown_campaign_is_an_honest_miss(monkeypatch: pytest.MonkeyPatch) -> None:
    """No match must not become an empty status report."""
    _stub_campaign(monkeypatch, matches=0)
    out = tools.salesforce_campaign_status("Nonexistent Campaign")
    assert "No Salesforce Campaign matches" in out
