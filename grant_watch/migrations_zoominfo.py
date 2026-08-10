"""Forward-only schema for licensed-vendor contact data and its credit budget.

TWO SEPARATE JOBS, in one migration because neither is safe without the other.

1. TYPED CONTACT PROVENANCE. Grant's contact truth model is `finder.verify_on_page`:
   an email is trusted only when it appears VERBATIM on the organization's own page.
   Licensed vendor data can never satisfy that gate, and the laundering path is a
   single string literal — `db.save_contact` takes `contact_status` as a PARAMETER, so
   writing 'verified' would make a purchased record indistinguishable from a
   page-verified one everywhere downstream, including the Persequor brief that sends a
   real email to a school administrator. `contact_provenance` records HOW a fact was
   established, independently of the workflow status, and 'vendor_licensed' is a value
   that no existing `== 'verified'` comparison can ever match.

2. THE CREDIT BUDGET. ZoomInfo bills per returned record, so a spend must be claimed
   ATOMICALLY before the HTTP call or two concurrent Slack threads both read the same
   remaining balance and both spend it. The reservation is a conditional UPDATE whose
   rowcount is the authorization.

ROLLBACK POSTURE: forward-only and rollback-INERT. Every column is added (never
altered), both tables are new, and the provenance backfill only writes a value derived
deterministically from the row's existing `contact_status` — it asserts nothing that
was not already true and changes no pre-existing column. Code that predates this
migration reads none of it, so a code rollback needs no schema rollback. That is
deliberately unlike migration 28, whose rollback is restore-from-backup.
"""

from __future__ import annotations

import sqlite3

# How a contact fact was established. Enforced by a CHECK constraint so a typo
# cannot quietly invent a fourth class of evidence.
#   page_verified    — seen verbatim on the organization's own official page
#   linkedin_claimed — a person parsed from a LinkedIn search result; ownership
#                      is NOT verified and no email is ever claimed from it
#   org_general      — the organization's shared mailbox, verified on its page;
#                      belongs to the ORG, never to a named individual
#   vendor_licensed  — licensed third-party data (ZoomInfo). Never page-verified.
CONTACT_PROVENANCE_VALUES = (
    "page_verified",
    "linkedin_claimed",
    "org_general",
    "vendor_licensed",
)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return the existing columns for one SQLite table."""
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn: sqlite3.Connection, table: str, definition: str) -> None:
    """Add one column without disturbing databases that already contain it."""
    if definition.split()[0] not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def migration_29_vendor_contacts_and_credits(conn: sqlite3.Connection) -> None:
    """Add typed contact provenance, do-not-call state, and the ZoomInfo ledger."""
    allowed = ",".join(f"'{value}'" for value in CONTACT_PROVENANCE_VALUES)
    # SQLite enforces a CHECK added via ADD COLUMN on new INSERTs and UPDATEs while
    # leaving existing rows NULL — exactly the forward-only shape wanted here, with
    # no table rebuild and therefore no risk to the foreign keys pointing at contacts.
    _add_column(
        conn,
        "contacts",
        f"contact_provenance TEXT CHECK(contact_provenance IS NULL "
        f"OR contact_provenance IN ({allowed}))",
    )
    # Do-not-call is stored as first-class state, never inferred at read time. A
    # flagged number must never reach a Salesforce Phone field an SDR will dial.
    _add_column(conn, "contacts", "do_not_call INTEGER NOT NULL DEFAULT 0")
    # The vendor's own record id, kept so a later pull can be reconciled against
    # what was actually billed rather than re-matched by name.
    _add_column(conn, "contacts", "vendor_person_id TEXT")

    # Backfill provenance from the status each row already carries. This states
    # nothing new — it makes an existing implication explicit and machine-checkable.
    conn.execute(
        "UPDATE contacts SET contact_provenance='page_verified' "
        "WHERE contact_provenance IS NULL AND contact_status='verified'"
    )
    conn.execute(
        "UPDATE contacts SET contact_provenance='linkedin_claimed' "
        "WHERE contact_provenance IS NULL AND contact_status='linkedin_only'"
    )
    # not_found / unverified rows carry no contact fact, so they stay NULL rather
    # than being assigned a provenance they do not have.

    conn.execute(
        """CREATE TABLE IF NOT EXISTS zoominfo_credit_periods (
               period TEXT PRIMARY KEY,
               credit_limit INTEGER NOT NULL CHECK (credit_limit >= 0),
               consumed INTEGER NOT NULL DEFAULT 0 CHECK (consumed >= 0),
               updated_at TIMESTAMP NOT NULL
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS zoominfo_credit_spends (
               id TEXT PRIMARY KEY,
               period TEXT NOT NULL,
               request_key TEXT NOT NULL UNIQUE,
               requested_by TEXT NOT NULL DEFAULT '',
               lead_id INTEGER,
               reserved_credits INTEGER NOT NULL CHECK (reserved_credits > 0),
               billed_credits INTEGER,
               state TEXT NOT NULL CHECK (
                   state IN ('reserved','settled','indeterminate','released')
               ),
               started_at TIMESTAMP NOT NULL,
               finished_at TIMESTAMP,
               error TEXT,
               FOREIGN KEY (period) REFERENCES zoominfo_credit_periods(period)
           )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS ix_zoominfo_spends_period_state
           ON zoominfo_credit_spends(period, state)"""
    )


def migration_37_mobile_phone(conn: sqlite3.Connection) -> None:
    """Store a mobile number as its OWN fact, not as "the phone".

    ZoomInfo returns `mobilePhone` and `directPhone` separately, and the enrichment
    collapsed them with `mobile_phone or direct_phone` into a single `phone` column.
    Salesforce then received a mobile in its `Phone` field, which every rep reads as
    a desk line — the same class of error as putting a switchboard number next to a
    person's name, which this repo already fixed once.

    They are different facts about how to reach someone and are kept apart end to
    end, so `Phone` and `MobilePhone` can each be populated with what they actually
    mean, and neither is inferred from the other.
    """
    existing = {str(row[1]) for row in conn.execute("PRAGMA table_info(contacts)")}
    if "mobile_phone" not in existing:
        conn.execute("ALTER TABLE contacts ADD COLUMN mobile_phone TEXT")
