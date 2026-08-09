"""Forward-only schema for contact facts a HUMAN supplied.

WHY THIS EXISTS. Grant refused a phone number a rep typed into chat, on the grounds
that it had not come from a source Grant pulled. That is the honesty rule applied to
the wrong case, and Chase called it a fail. The rule exists to stop Grant INVENTING a
contact and presenting it as discovered; it was never meant to stop a human telling
Grant something true. A rep who types a number is not a source Grant has to verify —
they are the authority, and refusing them is friction with no safety benefit.

THE DISTINCTION THAT MATTERS is not "verified vs unverified", it is WHO IS CLAIMING IT:
  page_verified    Grant found it and checked it verbatim on the org's own page
  linkedin_claimed Grant found a profile; ownership unproven
  org_general      the organization's shared mailbox, verified on its page
  vendor_licensed  a data vendor asserts it; nobody checked
  human_asserted   a named rep told Grant, in a thread, on a date  <-- new

Only the first is Grant vouching for something. The last is a person vouching for it,
and the honest thing is to record WHO and WHEN and show that wherever the fact is
shown — attribution is the mechanism, not refusal. `human_asserted` is a value no
existing `== 'verified'` comparison can match, so a supplied fact still cannot slip
into a rich card or an outreach brief as though Grant had proved it.
"""

from __future__ import annotations

import sqlite3

from .migrations_zoominfo import CONTACT_PROVENANCE_VALUES

# The full set after this migration. Kept derived from the earlier tuple so the two
# cannot drift apart silently.
HUMAN_PROVENANCE = "human_asserted"
ALL_CONTACT_PROVENANCE = (*CONTACT_PROVENANCE_VALUES, HUMAN_PROVENANCE)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return the existing columns for one SQLite table."""
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn: sqlite3.Connection, table: str, definition: str) -> None:
    """Add one column without disturbing databases that already contain it."""
    if definition.split()[0] not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def migration_32_human_asserted_contacts(conn: sqlite3.Connection) -> None:
    """Allow a rep to supply a contact fact, recorded with who said it and when.

    The provenance CHECK added in migration 29 has to be widened, and SQLite cannot
    alter a constraint in place. Rather than rebuild `contacts` — a table with live
    foreign-key children, on a database whose rollback is restore-from-backup — the
    old constraint is left in force on the old column and a NEW column carries the
    wider vocabulary. Existing values are copied across, so readers have one column
    to consult and the rebuild is avoided entirely.
    """
    allowed = ",".join(f"'{value}'" for value in ALL_CONTACT_PROVENANCE)
    _add_column(
        conn,
        "contacts",
        f"provenance TEXT CHECK(provenance IS NULL OR provenance IN ({allowed}))",
    )
    _add_column(conn, "contacts", "asserted_by_slack_user TEXT")
    _add_column(conn, "contacts", "asserted_at TIMESTAMP")
    # Carry forward what migration 29 established, so `provenance` is complete from
    # the moment it exists and nothing has to consult two columns.
    conn.execute(
        "UPDATE contacts SET provenance=contact_provenance "
        "WHERE provenance IS NULL AND contact_provenance IS NOT NULL"
    )
