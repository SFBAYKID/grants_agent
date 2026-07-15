"""Storage tests: schema creation, dedup upserts, idempotent CSV seeding, run logging.
Each test builds its own throwaway DB (tmp_path) — no shared state (CLAUDE.md rule 3)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from grant_watch import db
from grant_watch import migrations
from grant_watch.models import (
    DatePrecision,
    FundingEventType,
    Lead,
    LeadGrade,
    RawItem,
    RunStats,
    VerificationStatus,
)

SEED_CSV = Path(__file__).resolve().parent.parent / "data" / "svpp_active_awards_CA_MI_PA_WA.csv"


def _lead(item_id: str = "A1", source: str = "usaspending:16.071") -> Lead:
    return Lead(
        item=RawItem(source=source, item_id=item_id, title="SVPP award",
                     entity="Castle Rock SD 401", state="WA", program="SVPP",
                     amount=500_000.0, start="2025-10-01", end="2028-09-30",
                     url="https://example.gov/a1", raw={"k": "v"}),
        grade=LeadGrade.GOLD,
    )


def test_upsert_dedups_on_source_and_item_id(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "t.db")
    assert db.upsert_lead(conn, _lead()) is True      # first sight -> new
    assert db.upsert_lead(conn, _lead()) is False     # same item -> dedup
    # Same item id under a DIFFERENT source must NOT collide (the CFDA-split rule).
    assert db.upsert_lead(conn, _lead(source="usaspending:16.710")) is True
    assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 2


def test_upsert_refreshes_last_seen(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "t.db")
    db.upsert_lead(conn, _lead())
    first = conn.execute("SELECT first_seen, last_seen FROM leads").fetchone()
    db.upsert_lead(conn, _lead())
    second = conn.execute("SELECT first_seen, last_seen FROM leads").fetchone()
    assert second[0] == first[0]          # first_seen never moves
    assert second[1] >= first[1]          # last_seen refreshed


def test_seed_is_idempotent(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "t.db")
    rows, new = db.seed_from_csv(conn, SEED_CSV)
    assert rows == 75 and new == 75       # the verified CSV: 75 GOLD awards
    rows2, new2 = db.seed_from_csv(conn, SEED_CSV)
    assert rows2 == 75 and new2 == 0      # re-seeding inserts nothing
    grades = {g for (g,) in conn.execute("SELECT DISTINCT lead_grade FROM leads")}
    assert grades == {"gold"}


def test_run_logging(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "t.db")
    db.log_run(conn, "2026-07-13T00:00:00+00:00",
               RunStats(source="TestSource", items_seen=5, items_new=2, errors=""))
    row = conn.execute("SELECT source, items_seen, items_new FROM runs").fetchone()
    assert tuple(row) == ("TestSource", 5, 2)  # connect() sets row_factory=Row


def test_readonly_connection_cannot_mutate_or_create_database(tmp_path: Path) -> None:
    """Dry-run storage opens existing state query-only and refuses absent files."""
    path = tmp_path / "readonly.db"
    writable = db.connect(path)
    db.upsert_lead(writable, _lead())
    writable.close()
    readonly = db.connect_readonly(path)
    assert readonly.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 1
    with pytest.raises(sqlite3.OperationalError):
        readonly.execute("UPDATE leads SET status='dead'")
    readonly.close()
    with pytest.raises(sqlite3.OperationalError):
        db.connect_readonly(tmp_path / "missing.db")


def test_versioned_migrations_and_backfill_suppression(tmp_path: Path) -> None:
    """A deployed-style legacy row becomes history, never a fresh award alert."""
    path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(path)
    legacy.execute(
        """CREATE TABLE leads (
             id INTEGER PRIMARY KEY, source TEXT, source_item_id TEXT,
             lead_grade TEXT, entity_name TEXT, amount REAL, first_seen TEXT,
             UNIQUE(source, source_item_id))"""
    )
    legacy.execute(
        "INSERT INTO leads(source,source_item_id,lead_grade,entity_name,amount,first_seen) "
        "VALUES ('legacy','1','gold','Old District',100,'2025-01-01')"
    )
    legacy.commit()
    legacy.close()

    conn = db.connect(path)
    versions = [row[0] for row in conn.execute(
        "SELECT version FROM schema_migrations ORDER BY version")]
    assert versions == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    crm_tables = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name LIKE 'salesforce_%'"
        )
    }
    assert {"salesforce_lookup_state", "salesforce_matches"} <= crm_tables
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='conversation_sessions'"
    ).fetchone() is None
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='slack_conversation_threads'"
    ).fetchone() is not None
    event = conn.execute(
        "SELECT event_type, backfill, suppressed FROM funding_events"
    ).fetchone()
    assert tuple(event) == ("record_observed", 1, 1)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_failed_migration_rolls_back_schema_and_version(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A mid-migration exception leaves neither partial DDL nor a version marker."""
    path = tmp_path / "rollback.db"
    conn = db.connect(path)
    conn.close()

    def fail_after_ddl(connection: sqlite3.Connection) -> None:
        """Create one table and fail so the explicit transaction must undo it."""
        connection.execute("CREATE TABLE should_rollback(id INTEGER)")
        raise RuntimeError("injected migration failure")

    failing = migrations.Migration(999, "injected failure", fail_after_ddl)
    monkeypatch.setattr(migrations, "MIGRATIONS", (*migrations.MIGRATIONS, failing))
    raw = sqlite3.connect(path)
    with pytest.raises(RuntimeError, match="injected"):
        migrations.apply_migrations(raw)
    assert raw.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='should_rollback'"
    ).fetchone() is None
    assert raw.execute(
        "SELECT 1 FROM schema_migrations WHERE version=999").fetchone() is None


def test_substantive_update_creates_new_event_and_refreshes_projection(
        tmp_path: Path) -> None:
    """A later cycle changes facts and becomes a new evidence-backed event."""
    conn = db.connect(tmp_path / "events.db")
    first = _lead()
    first.item.event_type = FundingEventType.AWARD_ANNOUNCED
    first.item.event_date = "2026-06-01"
    first.item.date_precision = DatePrecision.DAY
    first.item.verification_status = VerificationStatus.VERIFIED
    assert db.upsert_lead(conn, first) is True
    assert db.upsert_lead(conn, first) is False

    changed = _lead()
    changed.item.amount = 600_000.0
    changed.item.event_type = FundingEventType.FUNDING_CYCLE_CHANGED
    changed.item.event_date = "2026-07-01"
    changed.item.date_precision = DatePrecision.DAY
    changed.item.verification_status = VerificationStatus.VERIFIED
    assert db.upsert_lead(conn, changed) is True
    row = conn.execute("SELECT amount,status FROM leads").fetchone()
    assert tuple(row) == (600_000.0, "new")
    assert conn.execute("SELECT COUNT(*) FROM funding_events").fetchone()[0] == 2


def test_event_only_update_creates_event_and_resets_status(tmp_path: Path) -> None:
    """A newly learned award date is an event even when projection fields match."""
    conn = db.connect(tmp_path / "event-only.db")
    first = _lead()
    first.item.event_type = FundingEventType.AWARD_ANNOUNCED
    first.item.verification_status = VerificationStatus.VERIFIED
    assert db.upsert_lead(conn, first) is True
    conn.execute("UPDATE leads SET status='surfaced'")
    conn.commit()

    dated = _lead()
    dated.item.event_type = FundingEventType.AWARD_ANNOUNCED
    dated.item.event_date = "2026-07-14"
    dated.item.date_precision = DatePrecision.DAY
    dated.item.verification_status = VerificationStatus.VERIFIED
    assert db.upsert_lead(conn, dated) is True
    assert conn.execute("SELECT status FROM leads").fetchone()[0] == "new"
    assert conn.execute("SELECT COUNT(*) FROM funding_events").fetchone()[0] == 2


def test_raw_only_update_refreshes_projection_without_new_event(tmp_path: Path) -> None:
    """Source request metadata can change without creating another notification."""
    conn = db.connect(tmp_path / "raw-only.db")
    first = _lead()
    first.item.event_type = FundingEventType.AWARD_ANNOUNCED
    first.item.verification_status = VerificationStatus.VERIFIED
    assert db.upsert_lead(conn, first) is True
    conn.execute("UPDATE leads SET status='surfaced'")
    conn.commit()

    refreshed = _lead()
    refreshed.item.raw = {"request_id": "changed-metadata"}
    refreshed.item.event_type = FundingEventType.AWARD_ANNOUNCED
    refreshed.item.verification_status = VerificationStatus.VERIFIED
    assert db.upsert_lead(conn, refreshed) is False
    row = conn.execute("SELECT status,raw_json FROM leads").fetchone()
    assert row["status"] == "surfaced"
    assert json.loads(row["raw_json"]) == {"request_id": "changed-metadata"}
    assert conn.execute("SELECT COUNT(*) FROM funding_events").fetchone()[0] == 1


def test_oversized_raw_payload_remains_valid_json(tmp_path: Path) -> None:
    """A bounded source snapshot is parseable and carries truncation provenance."""
    lead = _lead()
    lead.item.raw = {"large": "x" * 10_000}
    encoded = lead.item.raw_json()
    parsed = json.loads(encoded)
    assert len(encoded) <= 5_000
    assert parsed["_truncated"] is True
    assert parsed["original_length"] > 5_000
    assert len(parsed["sha256"]) == 64
