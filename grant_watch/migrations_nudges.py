"""Forward-only schema for proactive follow-up nudges.

WHY A SEPARATE TABLE AND NOT A NEW `posts` KIND. A nudge is a THREADED REPLY into
work that already exists — the thread under a card, or the thread where a preview was
built and abandoned. It is not a fresh channel post, so it needs no `posts` row and no
`proactive_daily_slots` claim. Both of those carry CHECK constraints that exclude any
new kind (`posts.kind` and `proactive_daily_slots.delivery_kind`), and widening either
means rebuilding a table with live foreign-key children on a database whose rollback
is restore-from-backup. Threading is also the better product answer: a nudge belongs
where the work is, not as new wallpaper in the channel.

THE ONE-SHOT RULE lives in the schema, not in the worker: UNIQUE(subject_kind,
subject_id, policy_version). One nudge per piece of abandoned work, ever. Grant has no
evidence anyone read the first one, so a second is nagging rather than helpfulness.
`policy_version` is part of the key so a deliberate rule or wording change can re-open
the subject — the existing `salesforce_followup_state` gets this wrong, keying on the
member id alone, which makes its POLICY_VERSION column decorative.

`observed_json` freezes the facts the wording was derived from. A nudge asserts what
Grant saw in its OWN records; re-verification happens in the same transaction as the
reservation, and anything that changed in between suppresses the send rather than
posting a claim that is no longer true.
"""

from __future__ import annotations

import sqlite3

# Subjects a nudge can be about. Validated in Python rather than by a CHECK so a new
# kind is a code change with a failing unit test, not a runtime IntegrityError on the
# droplet — the lesson of the posts.kind incident.
NUDGE_SUBJECT_KINDS = (
    "crm_preview_expired",  # a preview was shown and the button never clicked
    "crm_batch_blocked",  # a batch stopped for records a human must resolve
    "crm_batch_partial",  # the rep chose to proceed with only the resolved subset
    "card_unengaged",  # a daily card drew no reply, reaction, or CRM action
)

NUDGE_STATES = ("reserved", "delivered", "unknown", "suppressed", "dropped")


def migration_30_followup_nudges(conn: sqlite3.Connection) -> None:
    """Create the one-shot nudge ledger."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS followup_nudges (
               id TEXT PRIMARY KEY,
               subject_kind TEXT NOT NULL,
               subject_id TEXT NOT NULL,
               audience TEXT NOT NULL,
               target_slack TEXT NOT NULL DEFAULT '',
               anchor_ts TEXT NOT NULL,
               policy_version TEXT NOT NULL,
               due_at TIMESTAMP NOT NULL,
               drop_after TIMESTAMP NOT NULL,
               state TEXT NOT NULL,
               suppress_reason TEXT,
               observed_json TEXT NOT NULL DEFAULT '{}',
               delivery_key TEXT NOT NULL UNIQUE,
               slack_ts TEXT,
               reserved_at TIMESTAMP NOT NULL,
               delivered_at TIMESTAMP,
               last_error TEXT,
               UNIQUE(subject_kind, subject_id, policy_version)
           )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS ix_followup_nudges_day
           ON followup_nudges(audience, state, reserved_at)"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS ix_followup_nudges_target
           ON followup_nudges(target_slack, state, reserved_at)"""
    )
