"""Forward migration enforcing one active Campaign-creation preview per thread."""

from __future__ import annotations

import sqlite3


def migration_28_single_ready_campaign_creation(conn: sqlite3.Connection) -> None:
    """Cancel superseded duplicates and add the durable one-ready-preview invariant."""
    conn.execute(
        """UPDATE crm_actions AS older
           SET state='cancelled',
               last_error=COALESCE(
                   last_error,
                   'Superseded by migration 28: duplicate ready Campaign preview'
               ),
               updated_at=CURRENT_TIMESTAMP
           WHERE older.action_type='create_campaign'
             AND older.state='ready'
             AND EXISTS (
                 SELECT 1
                 FROM crm_actions AS newer
                 WHERE newer.action_type='create_campaign'
                   AND newer.state='ready'
                   AND newer.workspace=older.workspace
                   AND newer.channel=older.channel
                   AND newer.thread_ts=older.thread_ts
                   AND newer.requested_by=older.requested_by
                   AND (
                       newer.created_at>older.created_at
                       OR (
                           newer.created_at=older.created_at
                           AND newer.rowid>older.rowid
                       )
                   )
             )"""
    )
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS
               ux_crm_one_ready_campaign_creation
           ON crm_actions(workspace,channel,thread_ts,requested_by)
           WHERE action_type='create_campaign' AND state='ready'"""
    )
