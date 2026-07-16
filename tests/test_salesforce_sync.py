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


def test_sync_dry_run_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dry-run may read Salesforce but creates no local snapshot rows."""
    conn = db.connect(tmp_path / "sf.db")
    _lead(conn)
    monkeypatch.setattr(salesforce, "lookup", lambda *_args, **_kwargs: _found())
    summary = salesforce_sync.sync(conn, dry_run=True)
    assert summary.writes == 0
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
