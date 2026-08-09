"""Forward-only schema for campaign batch ATTEMPTS, including the ones that fail.

WHY THIS EXISTS. An SDR asked Grant to put 347 California leads on a Campaign, it
refused, she asked three more times, and she stopped using the product. Afterwards
there was nothing in the database to find: every validation failure in
``prepare_campaign_batch`` raises BEFORE ``_insert_manifest``, so a batch id is minted
and thrown away and the only trace is the Slack transcript. Seven separate raise sites
behave this way — bad context, empty request, bad state/grade, an unparseable Campaign
link, an unreadable Campaign, an incomplete selection, and no matching leads.

The consequence is worse than a missing log line. A follow-up worker cannot honestly
nudge about work it has no record of, so the single most important failure — "I asked,
something went wrong, and nothing happened" — is exactly the one Grant can never
notice. This table is what makes that failure investigable and, later, chase-able.

Deliberately its own table rather than an early row in ``crm_campaign_batches``: at
attempt time none of that table's counts, hashes or writer identity are known yet, and
most are NOT NULL. Nothing references this table, so it is rollback-inert.
"""

from __future__ import annotations

import sqlite3

# What became of one attempt. Validated in Python rather than by a CHECK so adding a
# state is a code change with a failing test, not a runtime IntegrityError.
ATTEMPT_STATES = ("started", "prepared", "failed")


def migration_31_campaign_attempts(conn: sqlite3.Connection) -> None:
    """Create the durable record of every campaign batch attempt."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS crm_campaign_attempts (
               id TEXT PRIMARY KEY,
               batch_id TEXT,
               workspace TEXT NOT NULL DEFAULT '',
               channel TEXT NOT NULL DEFAULT '',
               thread_ts TEXT NOT NULL DEFAULT '',
               requested_by TEXT NOT NULL DEFAULT '',
               request_json TEXT NOT NULL DEFAULT '[]',
               state TEXT NOT NULL,
               failure_kind TEXT,
               failure_detail TEXT,
               started_at TIMESTAMP NOT NULL,
               finished_at TIMESTAMP
           )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS ix_campaign_attempts_requester
           ON crm_campaign_attempts(requested_by, state, started_at)"""
    )
