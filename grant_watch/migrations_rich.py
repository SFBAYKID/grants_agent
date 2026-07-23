"""Late forward-only migrations for the isolated rich award-card campaign.

Kept separate so the ordered migration ledger remains below the repository's hard
1,000-line cap. Functions remain idempotent at their version boundary.
"""

from __future__ import annotations

import sqlite3


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return the existing columns for one known migration table."""
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def migration_23_rich_snapshot_truth_and_retry_link(
    conn: sqlite3.Connection,
) -> None:
    """Freeze exact event/site truth and link queued outreach to its card action.

    The companion table deliberately has no foreign key: snapshot audit evidence must
    survive delete-based source reconciliation. ``award_dedup_key`` is source-qualified
    and policy-independent; immutable evidence versions may supersede one another while
    the delivery outbox continues to enforce one award/audience delivery.
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS rich_card_snapshot_truth (
             snapshot_id TEXT PRIMARY KEY,
             award_dedup_key TEXT NOT NULL,
             source_name TEXT NOT NULL,
             event_type TEXT NOT NULL
               CHECK(event_type IN ('award_announced','award_obligated')),
             event_amount REAL NOT NULL,
             event_verification_status TEXT NOT NULL,
             event_evidence_excerpt TEXT,
             event_evidence_hash TEXT NOT NULL,
             event_source_locator TEXT NOT NULL,
             official_website_evidence_url TEXT NOT NULL
           )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_rich_truth_award
             ON rich_card_snapshot_truth(award_dedup_key)"""
    )
    if "outreach_request_id" not in _column_names(conn, "rich_card_actions"):
        conn.execute(
            "ALTER TABLE rich_card_actions ADD COLUMN outreach_request_id TEXT"
        )
