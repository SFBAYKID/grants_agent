"""Forward-migration coverage for divergent Campaign/search database shapes."""

from __future__ import annotations

import sqlite3

from grant_watch import migrations
from grant_watch.migrations_campaign_batch import migration_27_exact_campaign_batches

# Hand-maintained head version — see tests/test_rich_migrations.py for why this is
# a literal and not MIGRATIONS[-1].version.
HEAD_VERSION = 47


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return SQLite column names for one test table."""
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def test_campaign_batch_migration_accepts_legacy_search_count_columns() -> None:
    """A production-shaped DB with divergent search columns upgrades idempotently."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE leads (id INTEGER PRIMARY KEY);
        CREATE TABLE search_requests (
          id TEXT PRIMARY KEY,
          total_count INTEGER,
          result_complete INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE crm_actions (id TEXT PRIMARY KEY);
        CREATE TABLE crm_action_items (id INTEGER PRIMARY KEY);
        """
    )
    migration_27_exact_campaign_batches(conn)
    migration_27_exact_campaign_batches(conn)
    assert {"total_count", "result_complete"} <= _columns(conn, "search_requests")
    assert {"batch_id", "batch_target_id"} <= _columns(conn, "crm_actions")
    assert {"verification_state", "verified_at"} <= _columns(conn, "crm_action_items")
    assert {
        "expected_source_row_count",
        "stored_source_row_count",
        "approved_org_count",
        "approved_selection_hash",
        "completion_mode",
    } <= _columns(conn, "crm_campaign_batch_targets")
    assert "completion_mode" in _columns(conn, "crm_campaign_batches")
    assert (
        conn.execute(
            """SELECT COUNT(*) FROM sqlite_master
           WHERE type='table' AND name LIKE 'crm_campaign_%'"""
        ).fetchone()[0]
        == 5
    )


def test_sanitized_production_v13_history_upgrades_without_losing_rows() -> None:
    """A v13-shaped divergent ledger reaches current head with valid foreign keys."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    legacy = tuple(item for item in migrations.MIGRATIONS if item.version <= 13)
    migrations._run_migrations(conn, legacy, lambda: "2026-07-24T00:00:00+00:00")
    # Production consumed 10-12 on its divergent lineage; unknown applied
    # versions must not prevent Grant's forward migrations.
    conn.executemany(
        """INSERT INTO schema_migrations(version,name,applied_at)
           VALUES (?,?,'2026-01-01T00:00:00+00:00')""",
        (
            (10, "production lineage"),
            (11, "production lineage"),
            (12, "production lineage"),
        ),
    )
    conn.execute(
        """INSERT INTO leads
             (id,source,source_item_id,entity_name,state,lead_grade,status)
           VALUES (41,'legacy','kept','Kept District','IL','gold','new')"""
    )
    conn.commit()
    migrations.apply_migrations(conn)
    preserved = conn.execute(
        "SELECT source_item_id,entity_name,state,status FROM leads WHERE id=41"
    ).fetchone()
    assert tuple(preserved) == ("kept", "Kept District", "IL", "new")
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert [
        row[0]
        for row in conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
        )
    ] == [HEAD_VERSION]
    assert {
        "approved_org_count",
        "approved_selection_hash",
        "completion_mode",
    } <= _columns(conn, "crm_campaign_batch_targets")
