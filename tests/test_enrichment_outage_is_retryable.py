"""A source outage must not retire a lead forever.

`finder.SourceUnreachable` means no page was ever read — nothing was bought. It was
being filed in the paid ledger as `indeterminate`, the state reserved for "money may
have left", so every later pass raised `IndeterminatePaidCall` and the lead reported
`error` for good. The architectural critic reproduced it: source recovered after
pass 1, provider still never called again on passes 2, 3 or 4.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from grant_watch import db
from grant_watch.campaign import paid_calls
from grant_watch.enrich import finder
from grant_watch.models import FundingEventType, Lead, LeadGrade, RawItem
from grant_watch.slack import contact_enrichment


def _lead(conn: sqlite3.Connection) -> int:
    """Insert one award lead and return its id."""
    db.upsert_lead(
        conn,
        Lead(
            item=RawItem(
                source="outage-test",
                item_id="outage-1",
                title="verified security grant",
                entity="OUTAGE UNIFIED SCHOOL DISTRICT",
                state="CA",
                program="SVPP",
                amount=100_000,
                start="2026-01-01",
                end="2027-01-01",
                url="https://source.test/1",
                raw={},
                event_type=FundingEventType.AWARD_OBLIGATED,
            ),
            grade=LeadGrade.GOLD,
        ),
    )
    return int(conn.execute("SELECT id FROM leads").fetchone()[0])


def test_a_recovered_source_is_read_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Outage, then recovery: the second pass must actually call the finder."""
    conn = db.connect(tmp_path / "outage.db")
    lead_id = _lead(conn)
    attempts: list[str] = []

    def flaky(entity: str, state: str, on_progress: object = None) -> object:
        """Fail the first call the way a 429 or a timeout does, then succeed."""
        attempts.append(entity)
        if len(attempts) == 1:
            raise finder.SourceUnreachable("simulated outage")
        return finder.ContactCandidate(
            name="Dana Reyes",
            title="Director of Technology",
            email="dreyes@outage.k12.ca.us",
            phone="",
            source_url="https://outage.k12.ca.us/staff",
            confidence="high",
            official_domain="outage.k12.ca.us",
            field_evidence={"email": True},
        )

    monkeypatch.setattr(finder, "find_contact", flaky)

    first = contact_enrichment.enrich_lead_contact(conn, lead_id)
    assert first.status == "unreachable"
    # The attempt is filed retryable, NOT indeterminate — nothing was bought.
    assert [
        str(row[0])
        for row in conn.execute("SELECT state FROM paid_enrichment_attempts")
    ] == ["failed"]

    second = contact_enrichment.enrich_lead_contact(conn, lead_id)
    assert len(attempts) == 2, "the recovered source was never re-read"
    assert second.status == "verified"
    assert second.name == "Dana Reyes"


def test_a_genuinely_indeterminate_attempt_is_still_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: an error that CANNOT prove nothing was bought still blocks.

    Only exceptions the caller declares `provably_unspent` are downgraded. Anything
    else keeps the double-spend protection this ledger exists for.
    """
    conn = db.connect(tmp_path / "indeterminate.db")
    lead_id = _lead(conn)
    calls: list[int] = []

    def boom(entity: str, state: str, on_progress: object = None) -> object:
        """Fail in a way that could have reached a paid provider."""
        calls.append(1)
        raise RuntimeError("provider connection dropped mid-request")

    monkeypatch.setattr(finder, "find_contact", boom)

    with pytest.raises(RuntimeError):
        contact_enrichment.enrich_lead_contact(conn, lead_id)
    assert [
        str(row[0])
        for row in conn.execute("SELECT state FROM paid_enrichment_attempts")
    ] == ["indeterminate"]

    # The second pass reports honestly instead of re-spending or claiming no contact.
    outcome = contact_enrichment.enrich_lead_contact(conn, lead_id)
    assert outcome.status == "needs_operator_retry"
    assert len(calls) == 1, "an indeterminate spend must never be repeated silently"


def test_provably_unspent_is_opt_in(tmp_path: Path) -> None:
    """`paid_calls.execute` keeps its default behaviour for callers that say nothing."""
    conn = db.connect(tmp_path / "optin.db")
    lead_id = _lead(conn)

    def fail() -> None:
        """Raise the outage error without declaring it unspent."""
        raise finder.SourceUnreachable("outage")

    with pytest.raises(finder.SourceUnreachable):
        paid_calls.execute(conn, lead_id, "op", "key-1", fail)
    assert (
        conn.execute(
            "SELECT state FROM paid_enrichment_attempts WHERE request_key='key-1'"
        ).fetchone()[0]
        == "indeterminate"
    )
