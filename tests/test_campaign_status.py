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


def _redirect_db(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    """Point every bare db.connect() at a throwaway file.

    Patching db.DEFAULT_DB_PATH does NOT work: connect() binds its default at import
    time, so a bare call keeps opening the real database. See tests/conftest.py.
    """
    from grant_watch import db as db_module

    real = db_module.connect

    def connect(db_path: object = None, *args: object, **kwargs: object) -> object:
        """Open the throwaway file whenever no explicit path is given."""
        return real(path if db_path is None else db_path, *args, **kwargs)

    monkeypatch.setattr(db_module, "connect", connect)


@pytest.fixture(autouse=True)
def _local_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the tool at a throwaway database."""
    db.connect(tmp_path / "s.db").close()
    _redirect_db(monkeypatch, tmp_path / "s.db")


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
    # The REAL shape of an aggregate response: one AggregateResult row carrying the
    # number. The old stub returned 13 empty dicts, which made a row-counting bug
    # pass — live, that bug reported "0 members" for a Campaign holding 13.
    monkeypatch.setattr(
        salesforce,
        "readonly_soql",
        lambda q: ([{"attributes": {"type": "AggregateResult"}, "c": 13}], "https://x"),
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
    monkeypatch.setattr(salesforce, "readonly_soql", lambda q: ([{"c": 5}], "h"))
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


def test_the_member_count_query_uses_an_aggregate_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare SELECT COUNT() returns ZERO records — its total lives in totalSize.

    Counting rows on that query reported "0 members" for a Campaign that really held
    13, and Grant then reasoned confidently on top of the false zero ("someone must
    have removed them"). Found only by running it against production; the original
    stub returned 13 empty dicts and made the bug pass.
    """
    _stub_campaign(monkeypatch)
    seen: dict[str, str] = {}

    def capture(query: str) -> Any:
        """Record the SOQL the tool actually sends."""
        seen["q"] = query
        return [{"c": 7}], "h"

    monkeypatch.setattr(salesforce, "readonly_soql", capture)
    out = tools.salesforce_campaign_status("California Grant 2026")
    assert "COUNT(Id)" in seen["q"]
    assert "COUNT()" not in seen["q"]
    assert "7 member(s) on it right now" in out
