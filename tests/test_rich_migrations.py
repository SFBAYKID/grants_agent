"""Migrations 14-19 for the rich award-card campaign: fresh apply, historical upgrade,
data preservation through the posts rebuild, and rollback inertness.

The rich card MUST write a posts row (thread attribution runs through the posts table),
so the posts.kind CHECK widening (v15) repeats the migration-13 rebuild recipe. These
tests guard that the rebuild preserves ids (engagement.post_id references) and that a
rolled-back reader is inert to the new tables/columns.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from grant_watch import db


def test_fresh_database_reaches_v19_with_all_rich_tables(tmp_path: Path) -> None:
    """A brand-new database applies every migration through 19."""
    conn = db.connect(tmp_path / "fresh.db")
    assert (
        conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 19
    )
    for table in (
        "rich_card_snapshots",
        "rich_card_actions",
        "contact_evidence",
        "salesforce_activity_snapshots",
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
    assert {"last_confirmed_run_id", "last_confirmed_at"} <= {
        r[1] for r in conn.execute("PRAGMA table_info(leads)")
    }
    assert {"owner_id", "owner_email"} <= {
        r[1] for r in conn.execute("PRAGMA table_info(salesforce_matches)")
    }
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def _at_v13(path: Path) -> sqlite3.Connection:
    """Build a database, then rewind its ledger to v13 so 14-19 are 'pending'."""
    conn = db.connect(path)
    conn.execute("DELETE FROM schema_migrations WHERE version > 13")
    conn.commit()
    conn.close()
    return db.connect(path)  # re-open: applies 14-19 as an upgrade


def test_v13_upgrade_preserves_posts_ids_and_engagement(tmp_path: Path) -> None:
    """The v15 posts rebuild must preserve every post id (engagement.post_id references)
    and every row — the exact guarantee migration 13 established."""
    path = tmp_path / "hist.db"
    seed = db.connect(path)
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

    upgraded = _at_v13(path)
    assert (
        upgraded.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        == 19
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
