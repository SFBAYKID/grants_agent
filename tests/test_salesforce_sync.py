"""Offline read-only Salesforce snapshot worker tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from grant_watch import db
from grant_watch.enrich import salesforce, salesforce_sync
from grant_watch.models import (
    DatePrecision,
    FundingEventType,
    Lead,
    LeadGrade,
    RawItem,
    VerificationStatus,
)


def _lead(conn: sqlite3.Connection) -> int:
    """Insert one eligible award event and return its lead ID."""
    db.upsert_lead(
        conn,
        Lead(
            RawItem(
                "usaspending:16.071",
                "A1",
                "SVPP award",
                "Castle Rock School District",
                "WA",
                "SVPP",
                500_000.0,
                "2026-06-01",
                "2028-09-30",
                "",
                {},
                event_type=FundingEventType.AWARD_OBLIGATED,
                event_date="2026-06-15",
                date_precision=DatePrecision.DAY,
                verification_status=VerificationStatus.VERIFIED,
            ),
            LeadGrade.GOLD,
        ),
    )
    return int(conn.execute("SELECT id FROM leads").fetchone()[0])


def _found() -> salesforce.SFResult:
    """Return a high-confidence Account plus Account-bound open Opportunity."""
    return salesforce.SFResult(
        status=salesforce.SFResultStatus.FOUND,
        matches=[
            salesforce.SFMatch(
                "Account",
                "001A",
                "Castle Rock School District",
                "",
                "Anthony",
                "https://sf.test/lightning/r/Account/001A/view",
                "high",
                state="WA",
                owner_id="005A",
                owner_email="anthony@monarchconnected.com",
            ),
            salesforce.SFMatch(
                "Opportunity",
                "006A",
                "Security Upgrade",
                "",
                "Anthony",
                "https://sf.test/lightning/r/Opportunity/006A/view",
                "high",
                account_id="001A",
                stage="Prospecting",
                is_closed=False,
                owner_id="005A",
                owner_email="anthony@monarchconnected.com",
            ),
        ],
    )


def test_sync_persists_read_only_account_and_opportunity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A completed lookup becomes local prioritization context only."""
    conn = db.connect(tmp_path / "sf.db")
    lead_id = _lead(conn)
    monkeypatch.setattr(salesforce, "lookup", lambda *_args, **_kwargs: _found())
    summary = salesforce_sync.sync(conn)
    state = conn.execute(
        "SELECT * FROM salesforce_lookup_state WHERE lead_id=?", (lead_id,)
    ).fetchone()
    matches = conn.execute(
        "SELECT * FROM salesforce_matches WHERE lead_id=?", (lead_id,)
    ).fetchall()
    assert summary == salesforce_sync.SyncSummary(1, 1, 0, 0, 0, 0, 1)
    assert state["status"] == "found" and len(matches) == 2
    assert {row["sobject"] for row in matches} == {"Account", "Opportunity"}
    assert {row["owner_id"] for row in matches} == {"005A"}
    assert {row["owner_email"] for row in matches} == {"anthony@monarchconnected.com"}


def test_sync_dry_run_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dry-run creates no local rows AND issues no Salesforce request at all.

    This previously called lookup() per candidate and skipped only the local write, so
    `salesforce-sync --dry-run` quietly spent 150-350 live production API calls against
    the real org -- the precise thing an operator reaching for that flag is avoiding.
    Every other dry run in this CLI is network-free.
    """
    conn = db.connect(tmp_path / "sf.db")
    _lead(conn)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        """Provide test-local behavior for a call a preview must never make."""
        raise AssertionError("dry-run reached the Salesforce API")

    monkeypatch.setattr(salesforce, "lookup", forbidden)
    summary = salesforce_sync.sync(conn, dry_run=True)
    assert summary.writes == 0
    assert summary.checked == 1, "a preview still reports the batch it would check"
    assert (
        conn.execute("SELECT COUNT(*) FROM salesforce_lookup_state").fetchone()[0] == 0
    )


def test_outage_preserves_last_known_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An outage updates availability but does not erase previously verified links."""
    conn = db.connect(tmp_path / "sf.db")
    _lead(conn)
    monkeypatch.setattr(salesforce, "lookup", lambda *_args, **_kwargs: _found())
    salesforce_sync.sync(conn)
    conn.execute(
        "UPDATE salesforce_lookup_state SET checked_at='2000-01-01T00:00:00+00:00'"
    )
    conn.commit()
    unavailable = salesforce.SFResult(
        status=salesforce.SFResultStatus.UNAVAILABLE, error="reader offline"
    )
    monkeypatch.setattr(salesforce, "lookup", lambda *_args, **_kwargs: unavailable)
    summary = salesforce_sync.sync(conn)
    assert summary.unavailable == 1
    assert conn.execute("SELECT COUNT(*) FROM salesforce_matches").fetchone()[0] == 2
    assert (
        conn.execute("SELECT status FROM salesforce_lookup_state").fetchone()["status"]
        == "unavailable"
    )


def test_ranking_sees_every_stale_lead_not_an_arbitrary_slice(tmp_path: Path) -> None:
    """A high-value lead must be selected even when thousands of older rows precede it.

    `_candidates` once carried a `LIMIT 500` INSIDE the query, before the sort and with
    no ORDER BY, so SQLite returned an arbitrary (in practice oldest-rowid) slice and
    the "highest-base-value" ranking only ordered that slice. With production holding
    thousands of leads, the recent high-value awards the rich card depends on could
    never enter the window — a scheduled sync would have refreshed CRM state for the
    wrong leads indefinitely.
    """
    conn = db.connect(tmp_path / "rank.db")
    conn.executemany(
        """INSERT INTO leads(id,source,source_item_id,lead_grade,entity_name,state,
                             program,amount,status,canonical_entity_key)
           VALUES (?,'seed',?,'silver',?,'WA','',1.0,'new',?)""",
        [(i, f"old-{i}", f"Low Value {i}", f"low {i}|WA") for i in range(1, 601)],
    )
    # Inserted LAST, so it lands at the highest rowid — beyond any leading-500 window.
    conn.execute(
        """INSERT INTO leads(id,source,source_item_id,lead_grade,entity_name,state,
                             program,amount,status,canonical_entity_key)
           VALUES (9999,'usaspending:16.071','hot','gold','Hot Award District','WA',
                   'SVPP',500000.0,'new','hot award district|WA')"""
    )
    conn.commit()

    picked = salesforce_sync._candidates(conn, 1)
    assert [int(row["id"]) for row in picked] == [9999]
