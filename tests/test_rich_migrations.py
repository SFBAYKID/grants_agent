"""Migrations 14-24 for the rich award-card campaign: fresh apply, historical upgrade,
data preservation through the posts rebuild, and rollback inertness.

The rich card MUST write a posts row (thread attribution runs through the posts table),
so the posts.kind CHECK widening (v15) repeats the migration-13 rebuild recipe. These
tests guard that the rebuild preserves ids (engagement.post_id references) and that a
rolled-back reader is inert to the new tables/columns.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from grant_watch import db, migrations
from grant_watch.migrations import MIGRATIONS

# The head schema version, maintained BY HAND. Deriving it from MIGRATIONS[-1]
# makes every assertion below tautological — MAX(version) from schema_migrations
# cannot differ from the migration list while migrations apply at all. The literal
# is the point: adding a migration must fail this file until someone bumps it
# deliberately, which is the schema-change review gate.
HEAD_VERSION = 37


def test_fresh_database_reaches_v28_with_all_rich_tables(tmp_path: Path) -> None:
    """A brand-new database applies every migration through 28."""
    conn = db.connect(tmp_path / "fresh.db")
    assert (
        conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        == HEAD_VERSION
    )
    for table in (
        "rich_card_snapshots",
        "rich_card_actions",
        "contact_evidence",
        "salesforce_activity_snapshots",
        "organization_kind_evidence",
        "paid_enrichment_attempts",
        "rich_card_snapshot_truth",
        "proactive_daily_slots",
    ):
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name=?", (table,)
        ).fetchone(), table
    posts_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='posts'"
    ).fetchone()[0]
    assert "rich_award" in posts_sql
    assert "snapshot_id" in {r[1] for r in conn.execute("PRAGMA table_info(posts)")}
    assert "snapshot_id" in {
        r[1] for r in conn.execute("PRAGMA table_info(notification_outbox)")
    }
    assert "state" in {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
    assert {"last_confirmed_run_id", "last_confirmed_at", "nces_website"} <= {
        r[1] for r in conn.execute("PRAGMA table_info(leads)")
    }
    assert {"owner_id", "owner_email"} <= {
        r[1] for r in conn.execute("PRAGMA table_info(salesforce_matches)")
    }
    assert {
        "contact_title",
        "contact_evidence_hash",
        "sf_activity_owner_email",
        "source_item_id",
        "card_mode",
    } <= {r[1] for r in conn.execute("PRAGMA table_info(rich_card_snapshots)")}
    assert {
        "official_website_provenance",
        "contact_domain_binding",
    } <= {r[1] for r in conn.execute("PRAGMA table_info(rich_card_snapshot_truth)")}
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def _at_version(path: Path, target: int) -> sqlite3.Connection:
    """Build one real historical schema from the ordered migration functions."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        """CREATE TABLE schema_migrations (
             version INTEGER PRIMARY KEY,name TEXT NOT NULL,applied_at TIMESTAMP NOT NULL
           )"""
    )
    for migration in migrations.MIGRATIONS:
        if migration.version > target:
            break
        migration.apply(conn)
        conn.execute(
            "INSERT INTO schema_migrations VALUES (?,?,?)",
            (migration.version, migration.name, "2026-07-01T00:00:00+00:00"),
        )
    conn.commit()
    return conn


def _at_v13(path: Path) -> sqlite3.Connection:
    """Build the real deployed v13 schema for data-preservation assertions."""
    return _at_version(path, 13)


@pytest.mark.parametrize("version", [1, 2, 3, 4, 5, 6, 7, 8, 9, 13])
def test_upgrade_from_each_supported_historical_schema(
    tmp_path: Path, version: int
) -> None:
    """Every ledger version that existed upgrades to head with integrity intact."""
    path = tmp_path / f"v{version}.db"
    historical = _at_version(path, version)
    historical.close()
    upgraded = db.connect(path)
    assert (
        upgraded.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        == HEAD_VERSION
    )
    assert upgraded.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_v13_upgrade_preserves_posts_ids_and_engagement(tmp_path: Path) -> None:
    """The v15 posts rebuild must preserve every post id (engagement.post_id references)
    and every row — the exact guarantee migration 13 established."""
    path = tmp_path / "hist.db"
    seed = _at_v13(path)
    # a lead + a post + an engagement row referencing it (the FK the rebuild must keep)
    seed.execute(
        "INSERT INTO leads (id, source, source_item_id, entity_name, status) "
        "VALUES (7, 'usaspending:16.071', 'A', 'Test District', 'new')"
    )
    seed.execute(
        "INSERT INTO posts (id, kind, lead_id, channel, ts, posted_at) "
        "VALUES (42, 'nugget', 7, 'C1', '1.0', '2026-07-01T00:00:00+00:00')"
    )
    seed.execute(
        "INSERT INTO engagement (post_id, slack_user, kind, at) "
        "VALUES (42, 'U1', 'reply', '2026-07-01T00:01:00+00:00')"
    )
    seed.commit()
    seed.close()

    upgraded = db.connect(path)
    assert (
        upgraded.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        == HEAD_VERSION
    )
    assert (
        upgraded.execute("SELECT kind FROM posts WHERE id=42").fetchone()[0] == "nugget"
    )
    assert upgraded.execute("SELECT post_id FROM engagement").fetchone()[0] == 42, (
        "engagement.post_id reference broken by rebuild"
    )
    # the widened CHECK now admits the rich kind
    upgraded.execute(
        "INSERT INTO posts (kind, channel, ts) VALUES ('rich_award','C1','2.0')"
    )
    upgraded.commit()
    assert upgraded.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_new_columns_are_nullable_so_legacy_inserts_still_work(tmp_path: Path) -> None:
    """Rollback safety: old code inserts posts/outbox rows WITHOUT snapshot_id. The new
    columns must be nullable-with-default so those inserts still succeed."""
    conn = db.connect(tmp_path / "rb.db")
    # a legacy-shaped posts insert (no snapshot_id, no rich kind)
    conn.execute(
        "INSERT INTO posts (kind, channel, ts, posted_at) "
        "VALUES ('nugget','C1','9.9','2026-07-01T00:00:00+00:00')"
    )
    conn.commit()
    row = conn.execute("SELECT snapshot_id FROM posts WHERE ts='9.9'").fetchone()
    assert row["snapshot_id"] is None


def test_the_migration_list_head_matches_this_module_s_literal() -> None:
    """The one place the hand-maintained literal is checked against reality.

    This is deliberately the ONLY link between the literal and the code. Adding a
    migration fails here and nowhere else, forcing a human to look at the new
    migration rather than letting three data-preservation tests silently re-point
    themselves at whatever version happens to be current.
    """
    assert MIGRATIONS[-1].version == HEAD_VERSION
