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


def migration_24_atomic_proactive_daily_slots(conn: sqlite3.Connection) -> None:
    """Serialize rich-card and future follow-up claims under one Pacific-day cap."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS proactive_daily_slots (
             audience TEXT NOT NULL,
             local_date TEXT NOT NULL,
             delivery_kind TEXT NOT NULL
               CHECK(delivery_kind IN ('rich_award','salesforce_followup')),
             delivery_key TEXT NOT NULL UNIQUE,
             reserved_at TIMESTAMP NOT NULL,
             PRIMARY KEY(audience,local_date)
           )"""
    )


def migration_25_typed_provenance_and_card_mode(conn: sqlite3.Connection) -> None:
    """Freeze WHY the website + contact email were trusted, and the card's mode.

    ``official_website_provenance`` and ``contact_domain_binding`` record the typed,
    non-heuristic reason each was accepted (NCES / approved directory / verified org
    page; org-site / authoritative-directory) so an audit can prove a card never
    trusted a name guess. ``card_mode`` distinguishes a draft-ready card from a
    Salesforce research-needed card, which must not offer an active draft action.
    All three are nullable so the (empty in production) rich tables upgrade cleanly.
    """
    if "official_website_provenance" not in _column_names(
        conn, "rich_card_snapshot_truth"
    ):
        conn.execute(
            "ALTER TABLE rich_card_snapshot_truth "
            "ADD COLUMN official_website_provenance TEXT"
        )
    if "contact_domain_binding" not in _column_names(
        conn, "rich_card_snapshot_truth"
    ):
        conn.execute(
            "ALTER TABLE rich_card_snapshot_truth ADD COLUMN contact_domain_binding TEXT"
        )
    if "card_mode" not in _column_names(conn, "rich_card_snapshots"):
        conn.execute("ALTER TABLE rich_card_snapshots ADD COLUMN card_mode TEXT")


def migration_26_exact_nces_website(conn: sqlite3.Connection) -> None:
    """Home for the EXACT NCES-published official website — the only org->website evidence
    that may back a DRAFT-READY card (Chase, 2026-07-23; a heuristic ``_looks_official``
    website caps a card at research-needed).

    Nullable and forward-only (ADD COLUMN, O(1) metadata change, rollback-inert). NO
    runtime source populates it yet, so every current lead stays NULL and therefore
    research-only; a future AUTHORIZED authoritative fetch may set it, lighting up
    draft-ready cards. Old code that never reads the column is unaffected."""
    if "nces_website" not in _column_names(conn, "leads"):
        conn.execute("ALTER TABLE leads ADD COLUMN nces_website TEXT")
