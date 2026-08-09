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
    "capability_now_available",  # someone asked for something Grant could not do yet
    "card_escalated",  # a tagged rep never answered; the manager is told once
    "thread_abandoned",  # a human stopped mid-conversation and never came back
)

# What a rep asked for that Grant had to refuse. The value is the CAPABILITY, not the
# wording, so one shipped feature can close every ask that was waiting on it.
CAPABILITY_KINDS = (
    "email_results",  # "just email me these" — no transport existed
    "campaign_load",  # "put these on a campaign" — the batch path was unreachable
    "reminders",  # "remind me about this" — nothing outlived the conversation
    "contact_supplied",  # a rep gave a fact and Grant refused to record it
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


def migration_34_capability_asks(conn: sqlite3.Connection) -> None:
    """Record asks Grant could not satisfy, so a shipped feature can close them.

    WHY A TABLE AND NOT A LOG SCAN. The alternative was to re-read Slack history and
    pattern-match refusals, which would make Grant's follow-ups depend on matching its
    own past prose — brittle, and unable to distinguish "I can't send email" from "I
    couldn't find an email". A row is written at the moment of refusal, by the code
    that knows exactly which capability was missing.

    `evidence_url` is the Slack permalink to the human's actual message. Every
    follow-up this feeds is a claim about something a named person said on a date, so
    the claim ships with its receipt and a human can check it in one click.

    The UNIQUE key is (audience, message_ts, capability): one ask, one capability, one
    follow-up — a rep who asks twice in one thread is not chased twice.
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS capability_asks (
              id INTEGER PRIMARY KEY,
              slack_user TEXT NOT NULL,
              audience TEXT NOT NULL,
              thread_ts TEXT NOT NULL,
              message_ts TEXT NOT NULL,
              asked_at TIMESTAMP NOT NULL,
              ask_text TEXT NOT NULL,
              capability TEXT NOT NULL,
              available_since TIMESTAMP,
              state TEXT NOT NULL DEFAULT 'open',
              evidence_url TEXT,
              recorded_by TEXT NOT NULL,
              created_at TIMESTAMP NOT NULL,
              UNIQUE(audience, message_ts, capability)
            )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_capability_asks_open "
        "ON capability_asks(state, capability)"
    )


def migration_35_capability_ask_correction(conn: sqlite3.Connection) -> None:
    """Carry a correction alongside an ask Grant answered with a false promise.

    Reopening an ask says "I couldn't do that then, I can now". For most asks that is
    the whole truth. For some it is not: Grant told a rep "I'll keep watching these
    states and flag new awards here as they land", had no such capability, and never
    contacted her again. Saying only "I couldn't do it then" would be technically
    true and quietly misleading — it omits that she was told it was handled.

    A correction is stored verbatim rather than generated, so the sentence that
    admits the error is written by a person and reviewed before it is sent. This is
    forward-only as a separate migration rather than an edit to 34, because 34 is
    already committed and the discipline is worth more than the saved column.
    """
    existing = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(capability_asks)")
    }
    if "correction" not in existing:
        conn.execute("ALTER TABLE capability_asks ADD COLUMN correction TEXT")
