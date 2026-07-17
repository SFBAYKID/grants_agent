"""Versioned SQLite migrations for Grant's durable truth and workflow state.

Why: schema changes must be explicit, ordered, and testable from the deployed legacy
database. Each migration is idempotent at the version boundary; conditional column
adds handle databases created by older releases without rewriting user data.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class Migration:
    """One ordered schema transition."""

    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]


def _now() -> str:
    """Return an ISO UTC timestamp for migration audit rows."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """Return whether ``table`` exists in the current SQLite database."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return existing column names for a table, or an empty set when absent."""
    if not _table_exists(conn, table):
        return set()
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn: sqlite3.Connection, table: str, definition: str) -> None:
    """Add a column only when its leading name is not already present."""
    name = definition.split()[0]
    if name not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def _execute_script(conn: sqlite3.Connection, script: str) -> None:
    """Execute a multi-statement script without sqlite3's implicit commit behavior."""
    statement = ""
    for line in script.splitlines():
        statement += f"{line}\n"
        if sqlite3.complete_statement(statement):
            sql = statement.strip()
            if sql:
                conn.execute(sql)
            statement = ""
    if statement.strip():
        raise ValueError("migration SQL ended with an incomplete statement")


def _migration_1_base(conn: sqlite3.Connection) -> None:
    """Create the legacy-compatible core tables for a brand-new database."""
    _execute_script(
        conn,
        """
        CREATE TABLE IF NOT EXISTS leads (
          id INTEGER PRIMARY KEY,
          source TEXT NOT NULL,
          source_item_id TEXT NOT NULL,
          lead_grade TEXT CHECK(lead_grade IN ('gold','silver','watch')),
          entity_name TEXT NOT NULL,
          title TEXT,
          entity_type TEXT,
          state TEXT, county TEXT,
          program TEXT,
          amount REAL,
          funds_start DATE, funds_end DATE,
          detail_url TEXT,
          raw_json TEXT,
          first_seen TIMESTAMP, last_seen TIMESTAMP,
          status TEXT DEFAULT 'new',
          status_note TEXT,
          assigned_to TEXT,
          assigned_at TIMESTAMP,
          UNIQUE(source, source_item_id)
        );
        CREATE TABLE IF NOT EXISTS contacts (
          id INTEGER PRIMARY KEY,
          lead_id INTEGER REFERENCES leads(id),
          name TEXT, title TEXT, email TEXT, phone TEXT,
          source_url TEXT,
          confidence TEXT CHECK(confidence IN ('high','medium','low')),
          contact_status TEXT DEFAULT 'unverified'
        );
        CREATE TABLE IF NOT EXISTS outreach (
          id INTEGER PRIMARY KEY,
          lead_id INTEGER REFERENCES leads(id),
          contact_id INTEGER REFERENCES contacts(id),
          channel TEXT, draft TEXT, approved_by TEXT,
          sent_at TIMESTAMP, response TEXT
        );
        CREATE TABLE IF NOT EXISTS runs (
          id INTEGER PRIMARY KEY,
          started TIMESTAMP, finished TIMESTAMP,
          source TEXT, items_seen INT, items_new INT, errors TEXT
        );
        CREATE TABLE IF NOT EXISTS posts (
          id INTEGER PRIMARY KEY,
          kind TEXT NOT NULL CHECK(kind IN ('nugget','bulletin')),
          lead_id INTEGER REFERENCES leads(id),
          channel TEXT NOT NULL,
          ts TEXT NOT NULL,
          style TEXT,
          posted_at TIMESTAMP,
          UNIQUE(channel, ts)
        );
        CREATE TABLE IF NOT EXISTS engagement (
          id INTEGER PRIMARY KEY,
          post_id INTEGER REFERENCES posts(id),
          slack_user TEXT NOT NULL,
          kind TEXT NOT NULL CHECK(kind IN ('reply','reaction','claim','question')),
          at TIMESTAMP,
          UNIQUE(post_id, slack_user, kind)
        );
        """,
    )
    for definition in (
        "lead_grade TEXT",
        "entity_name TEXT",
        "title TEXT",
        "entity_type TEXT",
        "state TEXT",
        "county TEXT",
        "program TEXT",
        "amount REAL",
        "funds_start DATE",
        "funds_end DATE",
        "detail_url TEXT",
        "raw_json TEXT",
        "first_seen TIMESTAMP",
        "last_seen TIMESTAMP",
        "status TEXT DEFAULT 'new'",
        "status_note TEXT",
        "assigned_to TEXT",
        "assigned_at TIMESTAMP",
    ):
        _add_column(conn, "leads", definition)


def _migration_2_truth_events(conn: sqlite3.Connection) -> None:
    """Add immutable observations/events and enrich the current lead projection."""
    for definition in (
        "current_event_id INTEGER",
        "canonical_entity_key TEXT",
        "nces_id TEXT",
        "enrollment INTEGER",
        "location_city TEXT",
        "location_confidence TEXT",
    ):
        _add_column(conn, "leads", definition)
    _execute_script(
        conn,
        """
        CREATE TABLE IF NOT EXISTS source_observations (
          id INTEGER PRIMARY KEY,
          lead_id INTEGER NOT NULL REFERENCES leads(id),
          source TEXT NOT NULL,
          source_item_id TEXT NOT NULL,
          observed_at TIMESTAMP NOT NULL,
          payload_hash TEXT NOT NULL,
          raw_json TEXT NOT NULL,
          source_url TEXT,
          source_locator TEXT,
          verification_status TEXT NOT NULL,
          UNIQUE(source, source_item_id, payload_hash)
        );
        CREATE TABLE IF NOT EXISTS funding_events (
          id INTEGER PRIMARY KEY,
          lead_id INTEGER NOT NULL REFERENCES leads(id),
          observation_id INTEGER REFERENCES source_observations(id),
          event_type TEXT NOT NULL,
          occurred_on DATE,
          date_precision TEXT NOT NULL,
          amount REAL,
          funded_scope TEXT,
          eligible_scope TEXT,
          application_portal TEXT,
          evidence_excerpt TEXT,
          evidence_hash TEXT,
          source_url TEXT,
          source_locator TEXT,
          verification_status TEXT NOT NULL,
          backfill INTEGER NOT NULL DEFAULT 0,
          suppressed INTEGER NOT NULL DEFAULT 0,
          created_at TIMESTAMP NOT NULL,
          UNIQUE(observation_id)
        );
        CREATE INDEX IF NOT EXISTS idx_events_lead ON funding_events(lead_id, id);
        CREATE INDEX IF NOT EXISTS idx_events_type_date
          ON funding_events(event_type, occurred_on);
        """,
    )
    # Existing rows are history, not newly announced awards. Backfill them exactly once
    # as suppressed record observations so deployment cannot trigger a notification wave.
    conn.execute(
        """
        INSERT OR IGNORE INTO source_observations
          (lead_id, source, source_item_id, observed_at, payload_hash, raw_json,
           source_url, source_locator, verification_status)
        SELECT id, source, source_item_id, COALESCE(first_seen, ?),
               'backfill:' || id, COALESCE(raw_json, '{}'), detail_url, '', 'assumed'
        FROM leads
        """,
        (_now(),),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO funding_events
          (lead_id, observation_id, event_type, occurred_on, date_precision, amount,
           funded_scope, eligible_scope, application_portal, evidence_excerpt,
           evidence_hash, source_url, source_locator, verification_status,
           backfill, suppressed, created_at)
        SELECT l.id, o.id, 'record_observed', NULL, 'unknown', l.amount,
               '', '', '', COALESCE(l.title, ''), o.payload_hash, l.detail_url, '',
               'assumed', 1, 1, COALESCE(l.first_seen, ?)
        FROM leads l
        JOIN source_observations o
          ON o.source=l.source AND o.source_item_id=l.source_item_id
         AND o.payload_hash='backfill:' || l.id
        """,
        (_now(),),
    )
    conn.execute(
        """
        UPDATE leads SET current_event_id=(
          SELECT MAX(id) FROM funding_events WHERE funding_events.lead_id=leads.id
        ) WHERE current_event_id IS NULL
        """
    )


def _migration_3_durable_workflows(conn: sqlite3.Connection) -> None:
    """Add durable Slack, export, outcome, outreach, and Salesforce action state."""
    for definition in (
        "event_id INTEGER",
        "delivery_key TEXT",
        "delivery_status TEXT",
        "urgent INTEGER DEFAULT 0",
    ):
        _add_column(conn, "posts", definition)
    for definition in (
        "request_id TEXT",
        "status TEXT DEFAULT 'draft'",
        "attempts INTEGER DEFAULT 0",
        "last_error TEXT",
        "next_attempt_at TIMESTAMP",
        "created_at TIMESTAMP",
    ):
        _add_column(conn, "outreach", definition)
    for definition in ("official_domain TEXT", "field_evidence_json TEXT"):
        _add_column(conn, "contacts", definition)
    for definition in ("complete INTEGER DEFAULT 1", "error_code TEXT"):
        _add_column(conn, "runs", definition)
    _execute_script(
        conn,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_outreach_request_id
          ON outreach(request_id) WHERE request_id IS NOT NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_posts_delivery_key
          ON posts(delivery_key) WHERE delivery_key IS NOT NULL;
        CREATE TABLE IF NOT EXISTS slack_event_receipts (
          event_id TEXT PRIMARY KEY,
          workspace TEXT NOT NULL,
          channel TEXT NOT NULL,
          thread_ts TEXT,
          slack_user TEXT,
          state TEXT NOT NULL,
          received_at TIMESTAMP NOT NULL,
          finished_at TIMESTAMP,
          error TEXT
        );
        CREATE TABLE IF NOT EXISTS poll_locks (
          name TEXT PRIMARY KEY,
          owner TEXT NOT NULL,
          acquired_at TIMESTAMP NOT NULL
        );
        CREATE TABLE IF NOT EXISTS conversation_sessions (
          session_key TEXT PRIMARY KEY,
          workspace TEXT NOT NULL,
          channel TEXT NOT NULL,
          thread_ts TEXT,
          slack_user TEXT NOT NULL,
          state TEXT NOT NULL,
          data_json TEXT NOT NULL,
          version INTEGER NOT NULL DEFAULT 1,
          updated_at TIMESTAMP NOT NULL,
          expires_at TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS search_requests (
          id TEXT PRIMARY KEY,
          session_key TEXT NOT NULL,
          requested_by TEXT NOT NULL,
          filters_json TEXT NOT NULL,
          scope TEXT NOT NULL,
          top_n INTEGER,
          format TEXT,
          state TEXT NOT NULL,
          result_lead_ids_json TEXT,
          created_at TIMESTAMP NOT NULL,
          updated_at TIMESTAMP NOT NULL
        );
        CREATE TABLE IF NOT EXISTS notification_outbox (
          id INTEGER PRIMARY KEY,
          delivery_key TEXT NOT NULL UNIQUE,
          event_id INTEGER REFERENCES funding_events(id),
          lead_id INTEGER REFERENCES leads(id),
          audience TEXT NOT NULL,
          delivery_class TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          state TEXT NOT NULL,
          slack_ts TEXT,
          attempts INTEGER NOT NULL DEFAULT 0,
          available_at TIMESTAMP NOT NULL,
          created_at TIMESTAMP NOT NULL,
          updated_at TIMESTAMP NOT NULL,
          last_error TEXT
        );
        CREATE TABLE IF NOT EXISTS export_jobs (
          id TEXT PRIMARY KEY,
          search_request_id TEXT REFERENCES search_requests(id),
          requested_by TEXT NOT NULL,
          format TEXT NOT NULL,
          idempotency_key TEXT NOT NULL UNIQUE,
          state TEXT NOT NULL,
          external_id TEXT,
          url TEXT,
          error TEXT,
          created_at TIMESTAMP NOT NULL,
          updated_at TIMESTAMP NOT NULL
        );
        CREATE TABLE IF NOT EXISTS outcome_events (
          id TEXT PRIMARY KEY,
          lead_id INTEGER REFERENCES leads(id),
          post_id INTEGER REFERENCES posts(id),
          slack_user TEXT NOT NULL,
          kind TEXT NOT NULL,
          points INTEGER NOT NULL,
          source_key TEXT NOT NULL,
          occurred_at TIMESTAMP NOT NULL,
          UNIQUE(source_key, kind)
        );
        CREATE TABLE IF NOT EXISTS crm_actions (
          id TEXT PRIMARY KEY,
          action_type TEXT NOT NULL,
          workspace TEXT NOT NULL,
          channel TEXT NOT NULL,
          thread_ts TEXT NOT NULL,
          requested_by TEXT NOT NULL,
          state TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          payload_hash TEXT NOT NULL,
          nonce_hash TEXT NOT NULL,
          expires_at TIMESTAMP NOT NULL,
          approved_at TIMESTAMP,
          committed_at TIMESTAMP,
          campaign_id TEXT,
          attempts INTEGER NOT NULL DEFAULT 0,
          last_error TEXT,
          created_at TIMESTAMP NOT NULL,
          updated_at TIMESTAMP NOT NULL
        );
        CREATE TABLE IF NOT EXISTS crm_action_items (
          id INTEGER PRIMARY KEY,
          action_id TEXT NOT NULL REFERENCES crm_actions(id),
          lead_id INTEGER REFERENCES leads(id),
          canonical_entity_key TEXT NOT NULL,
          operation TEXT NOT NULL,
          proposed_json TEXT NOT NULL,
          state TEXT NOT NULL,
          salesforce_id TEXT,
          campaign_member_id TEXT,
          error TEXT,
          UNIQUE(action_id, canonical_entity_key, operation)
        );
        """,
    )


def _migration_4_salesforce_read_snapshots(conn: sqlite3.Connection) -> None:
    """Persist read-only CRM context so proactive ranking never writes Salesforce."""
    _execute_script(
        conn,
        """
        CREATE TABLE IF NOT EXISTS salesforce_lookup_state (
          lead_id INTEGER PRIMARY KEY REFERENCES leads(id),
          status TEXT NOT NULL,
          error TEXT,
          checked_at TIMESTAMP NOT NULL
        );
        CREATE TABLE IF NOT EXISTS salesforce_matches (
          lead_id INTEGER NOT NULL REFERENCES leads(id),
          sobject TEXT NOT NULL,
          record_id TEXT NOT NULL,
          name TEXT NOT NULL,
          company TEXT,
          owner TEXT,
          link TEXT NOT NULL,
          confidence TEXT NOT NULL,
          account_id TEXT,
          stage TEXT,
          is_closed INTEGER,
          checked_at TIMESTAMP NOT NULL,
          PRIMARY KEY (lead_id, sobject, record_id)
        );
        CREATE INDEX IF NOT EXISTS idx_sf_matches_lead_object
          ON salesforce_matches(lead_id, sobject, confidence);
        """,
    )


def _migration_5_remove_unused_sessions(conn: sqlite3.Connection) -> None:
    """Retire the unused session table; Slack receipts/snapshots own durable state."""
    conn.execute("DROP TABLE IF EXISTS conversation_sessions")


def _migration_6_release_safety_state(conn: sqlite3.Connection) -> None:
    """Track external-write starts and Slack action/delivery reconciliation state."""
    _add_column(
        conn, "crm_actions", "external_write_started INTEGER NOT NULL DEFAULT 0"
    )
    _add_column(conn, "crm_actions", "items_hash TEXT")
    for definition in (
        "action_state TEXT NOT NULL DEFAULT 'pending'",
        "delivery_state TEXT NOT NULL DEFAULT 'pending'",
        "reviewed_at TIMESTAMP",
    ):
        _add_column(conn, "slack_event_receipts", definition)


def _migration_7_conversation_and_outreach_truth(conn: sqlite3.Connection) -> None:
    """Track mention-led threads and distinguish draft intake from email sending."""
    _add_column(conn, "outreach", "submitted_at TIMESTAMP")
    conn.execute(
        """UPDATE outreach SET submitted_at=sent_at, sent_at=NULL, approved_by=NULL
           WHERE status='submitted' AND submitted_at IS NULL"""
    )
    _execute_script(
        conn,
        """
        CREATE TABLE IF NOT EXISTS slack_conversation_threads (
          workspace TEXT NOT NULL,
          channel TEXT NOT NULL,
          thread_ts TEXT NOT NULL,
          initiated_by TEXT NOT NULL,
          created_at TIMESTAMP NOT NULL,
          last_active_at TIMESTAMP NOT NULL,
          PRIMARY KEY (workspace, channel, thread_ts)
        );
        """,
    )


def _migration_8_salesforce_followup_state(conn: sqlite3.Connection) -> None:
    """Persist one-shot, fail-closed Salesforce follow-up reminder delivery."""
    _execute_script(
        conn,
        """
        CREATE TABLE IF NOT EXISTS salesforce_followup_state (
          campaign_member_id TEXT PRIMARY KEY,
          crm_action_item_id INTEGER NOT NULL UNIQUE REFERENCES crm_action_items(id),
          campaign_id TEXT NOT NULL,
          target_sobject TEXT NOT NULL CHECK(target_sobject IN ('Lead','Contact')),
          target_record_id TEXT NOT NULL,
          joined_at TIMESTAMP NOT NULL,
          due_at TIMESTAMP NOT NULL,
          policy_version TEXT NOT NULL,
          state TEXT NOT NULL,
          evidence_kind TEXT,
          evidence_id TEXT,
          evidence_at TIMESTAMP,
          checked_at TIMESTAMP,
          delivery_key TEXT UNIQUE,
          slack_ts TEXT,
          delivered_at TIMESTAMP,
          last_error TEXT
        );
        """,
    )


def _migration_9_organization_profile(conn: sqlite3.Connection) -> None:
    """Store verbatim-verified organization contact details for CRM records.

    These are org-level facts (general email, main phone, mailing address, site,
    student count) distinct from the person contact in ``contacts`` and the NCES
    district-office city in ``leads.location_city``. Each is written only after
    verbatim on-page verification; an absent value stays NULL and is disclosed."""
    for definition in (
        "org_website TEXT",
        "org_general_email TEXT",
        "org_phone TEXT",
        "org_street TEXT",
        "org_city TEXT",
        "org_state TEXT",
        "org_postal_code TEXT",
        "org_student_count INTEGER",
        "org_profile_status TEXT",
        "org_profile_source_url TEXT",
    ):
        _add_column(conn, "leads", definition)


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "legacy-compatible base", _migration_1_base),
    Migration(2, "truth observations and events", _migration_2_truth_events),
    Migration(3, "durable workflows", _migration_3_durable_workflows),
    Migration(
        4, "read-only Salesforce snapshots", _migration_4_salesforce_read_snapshots
    ),
    Migration(
        5, "remove unused conversation sessions", _migration_5_remove_unused_sessions
    ),
    Migration(6, "release safety state", _migration_6_release_safety_state),
    Migration(
        7,
        "conversation threads and outreach truth",
        _migration_7_conversation_and_outreach_truth,
    ),
    Migration(
        8, "Salesforce follow-up reminder state", _migration_8_salesforce_followup_state
    ),
    Migration(9, "organization profile columns", _migration_9_organization_profile),
)


def apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply every unapplied migration transactionally and record its version."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
               version INTEGER PRIMARY KEY,
               name TEXT NOT NULL,
               applied_at TIMESTAMP NOT NULL
           )"""
    )
    conn.commit()
    applied = {
        int(row[0]) for row in conn.execute("SELECT version FROM schema_migrations")
    }
    for migration in MIGRATIONS:
        if migration.version in applied:
            continue
        try:
            conn.execute("BEGIN IMMEDIATE")
            migration.apply(conn)
            conn.execute(
                "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?,?,?)",
                (migration.version, migration.name, _now()),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
