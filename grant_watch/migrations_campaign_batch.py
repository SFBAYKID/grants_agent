"""Forward-only schema for exact, auditable Salesforce Campaign batches."""

from __future__ import annotations

import sqlite3


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return the existing columns for one SQLite table."""
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn: sqlite3.Connection, table: str, definition: str) -> None:
    """Add one column without disturbing databases that already contain it."""
    if definition.split()[0] not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def _execute_script(conn: sqlite3.Connection, script: str) -> None:
    """Execute DDL without sqlite3.executescript's implicit transaction commit."""
    statement = ""
    for line in script.splitlines():
        statement += f"{line}\n"
        if sqlite3.complete_statement(statement):
            if statement.strip():
                conn.execute(statement)
            statement = ""
    if statement.strip():
        raise ValueError("campaign batch migration SQL is incomplete")


def migration_27_exact_campaign_batches(conn: sqlite3.Connection) -> None:
    """Add complete search snapshots, batch manifests, and write verification state."""
    _add_column(conn, "search_requests", "total_count INTEGER")
    _add_column(conn, "search_requests", "result_complete INTEGER NOT NULL DEFAULT 0")
    _add_column(conn, "crm_actions", "batch_id TEXT")
    _add_column(conn, "crm_actions", "batch_target_id TEXT")
    _add_column(
        conn,
        "crm_action_items",
        "verification_state TEXT NOT NULL DEFAULT 'legacy_unverified'",
    )
    _add_column(conn, "crm_action_items", "verified_at TIMESTAMP")
    _execute_script(
        conn,
        """
        CREATE TABLE IF NOT EXISTS crm_campaign_batches (
          id TEXT PRIMARY KEY,
          workspace TEXT NOT NULL,
          channel TEXT NOT NULL,
          thread_ts TEXT NOT NULL,
          requested_by TEXT NOT NULL,
          query_version TEXT NOT NULL,
          state TEXT NOT NULL,
          completion_mode TEXT NOT NULL,
          expected_source_row_count INTEGER NOT NULL,
          stored_source_row_count INTEGER NOT NULL,
          source_row_count INTEGER NOT NULL,
          unique_org_count INTEGER NOT NULL,
          selection_hash TEXT NOT NULL,
          writer_org_id TEXT NOT NULL,
          writer_is_sandbox INTEGER NOT NULL,
          writer_host TEXT NOT NULL,
          created_at TIMESTAMP NOT NULL,
          updated_at TIMESTAMP NOT NULL
        );
        CREATE TABLE IF NOT EXISTS crm_campaign_batch_targets (
          id TEXT PRIMARY KEY,
          batch_id TEXT NOT NULL REFERENCES crm_campaign_batches(id),
          campaign_id TEXT NOT NULL,
          campaign_name TEXT NOT NULL,
          campaign_link TEXT NOT NULL,
          state_code TEXT NOT NULL,
          grades_json TEXT NOT NULL,
          expected_source_row_count INTEGER NOT NULL,
          stored_source_row_count INTEGER NOT NULL,
          source_row_count INTEGER NOT NULL,
          unique_org_count INTEGER NOT NULL,
          selection_hash TEXT NOT NULL,
          approved_org_count INTEGER NOT NULL,
          approved_selection_hash TEXT NOT NULL,
          completion_mode TEXT NOT NULL,
          state TEXT NOT NULL,
          action_id TEXT REFERENCES crm_actions(id),
          last_error TEXT,
          created_at TIMESTAMP NOT NULL,
          updated_at TIMESTAMP NOT NULL,
          UNIQUE(batch_id, campaign_id)
        );
        CREATE TABLE IF NOT EXISTS crm_campaign_batch_items (
          id TEXT PRIMARY KEY,
          target_id TEXT NOT NULL REFERENCES crm_campaign_batch_targets(id),
          canonical_entity_key TEXT NOT NULL,
          representative_lead_id INTEGER NOT NULL REFERENCES leads(id),
          source_lead_ids_json TEXT NOT NULL,
          grades_json TEXT NOT NULL,
          entity_name TEXT NOT NULL,
          state_code TEXT NOT NULL,
          item_hash TEXT NOT NULL,
          resolution_state TEXT NOT NULL,
          salesforce_sobject TEXT,
          salesforce_id TEXT,
          salesforce_account_id TEXT,
          crm_action_item_id INTEGER REFERENCES crm_action_items(id),
          note TEXT,
          UNIQUE(target_id, canonical_entity_key)
        );
        CREATE TABLE IF NOT EXISTS crm_campaign_write_attempts (
          id TEXT PRIMARY KEY,
          action_id TEXT NOT NULL REFERENCES crm_actions(id),
          operation TEXT NOT NULL,
          request_hash TEXT NOT NULL,
          state TEXT NOT NULL,
          submitted_ids_json TEXT NOT NULL,
          returned_ids_json TEXT,
          error TEXT,
          started_at TIMESTAMP NOT NULL,
          finished_at TIMESTAMP,
          UNIQUE(action_id, operation, request_hash)
        );
        CREATE TABLE IF NOT EXISTS crm_campaign_approval_attempts (
          id TEXT PRIMARY KEY,
          action_id TEXT,
          batch_id TEXT,
          actor_slack TEXT NOT NULL,
          workspace TEXT NOT NULL,
          channel TEXT NOT NULL,
          thread_ts TEXT NOT NULL,
          action_ts TEXT NOT NULL,
          outcome TEXT NOT NULL,
          reason TEXT,
          occurred_at TIMESTAMP NOT NULL,
          UNIQUE(action_id, actor_slack, action_ts, outcome)
        );
        CREATE INDEX IF NOT EXISTS idx_campaign_batch_target
          ON crm_campaign_batch_items(target_id, resolution_state);
        CREATE INDEX IF NOT EXISTS idx_campaign_attempt_action
          ON crm_campaign_write_attempts(action_id, state);
        """,
    )
    # Keep the under-development migration idempotent for local databases that
    # were initialized before these final completeness columns were added.
    _add_column(
        conn,
        "crm_campaign_batches",
        "completion_mode TEXT NOT NULL DEFAULT 'full'",
    )
    _add_column(
        conn,
        "crm_campaign_batch_targets",
        "approved_org_count INTEGER NOT NULL DEFAULT 0",
    )
    _add_column(
        conn,
        "crm_campaign_batch_targets",
        "approved_selection_hash TEXT NOT NULL DEFAULT ''",
    )
    _add_column(
        conn,
        "crm_campaign_batch_targets",
        "completion_mode TEXT NOT NULL DEFAULT 'full'",
    )
