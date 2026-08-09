"""Forward-only schema for rep-requested reminders and the right to switch them off.

WHY THIS EXISTS. In July a rep asked Grant to email her a list of Texas leads and the
thread simply died — Grant had no way to send mail and no way to remember an ask past
the end of a conversation. Both halves of that failure are addressed here: `reminders`
gives an ask a life of its own, and `followup_optouts` gives the rep an unconditional
way to end it.

THE OPT-OUT IS DELIBERATELY BROADER THAN THE REMINDERS TABLE. When someone says "stop
reminding me", they do not mean "cancel reminder #4 but keep the proactive nudges
coming" — they mean stop. So the opt-out is checked by BOTH `reminders` and
`slack/nudges.py`, and it is keyed by Slack user rather than by thread, because a
person who is done being chased is done everywhere. `scope` allows a narrower "just
this subject" opt-out when that is genuinely what was asked for, but the default that
natural language maps to is `all`.

An opt-out is never deleted, only superseded by a later row, so "did we keep emailing
someone after they asked us not to?" stays an answerable question.
"""

from __future__ import annotations

import sqlite3

# One ask, one schedule. `once` fires a single time and completes; the recurring
# cadences re-arm themselves after each delivery.
REMINDER_CADENCES = ("once", "daily", "weekly")
REMINDER_STATES = ("active", "cancelled", "completed", "failed")
# Where a due reminder is delivered. Email is only ever sent to a reviewed roster
# mailbox — see notify/resend_client.py, which fails closed on anything else.
REMINDER_CHANNELS = ("slack", "email", "both")
OPTOUT_SCOPES = ("all", "reminders", "nudges")


def migration_33_reminders_and_optouts(conn: sqlite3.Connection) -> None:
    """Create the reminder schedule, its delivery ledger, and the opt-out register."""
    cadences = ",".join(f"'{value}'" for value in REMINDER_CADENCES)
    states = ",".join(f"'{value}'" for value in REMINDER_STATES)
    channels = ",".join(f"'{value}'" for value in REMINDER_CHANNELS)
    scopes = ",".join(f"'{value}'" for value in OPTOUT_SCOPES)

    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS reminders (
              id INTEGER PRIMARY KEY,
              requested_by_slack TEXT NOT NULL,
              audience TEXT NOT NULL,
              thread_ts TEXT NOT NULL,
              subject TEXT NOT NULL,
              search_spec TEXT,
              cadence TEXT NOT NULL CHECK(cadence IN ({cadences})),
              deliver_via TEXT NOT NULL CHECK(deliver_via IN ({channels})),
              next_due_at TIMESTAMP NOT NULL,
              state TEXT NOT NULL CHECK(state IN ({states})),
              created_at TIMESTAMP NOT NULL,
              updated_at TIMESTAMP NOT NULL,
              cancelled_by_slack TEXT,
              cancelled_at TIMESTAMP,
              last_error TEXT
            )"""
    )
    # The worker's hot query is "what is due now", and it runs every 30 minutes.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_reminders_due ON reminders(state, next_due_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_reminders_owner "
        "ON reminders(requested_by_slack, state)"
    )
    # One delivery row per (reminder, occurrence). The UNIQUE constraint IS the
    # idempotency guarantee: a worker that crashes after sending but before recording
    # cannot send the same occurrence twice, because the insert is what reserves it.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS reminder_deliveries (
              id INTEGER PRIMARY KEY,
              reminder_id INTEGER NOT NULL REFERENCES reminders(id),
              occurrence_key TEXT NOT NULL,
              channel TEXT NOT NULL,
              state TEXT NOT NULL,
              slack_ts TEXT,
              email_id TEXT,
              recipient TEXT,
              detail TEXT,
              reserved_at TIMESTAMP NOT NULL,
              completed_at TIMESTAMP,
              UNIQUE(reminder_id, occurrence_key, channel)
            )"""
    )
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS followup_optouts (
              id INTEGER PRIMARY KEY,
              slack_user TEXT NOT NULL,
              scope TEXT NOT NULL CHECK(scope IN ({scopes})),
              active INTEGER NOT NULL DEFAULT 1,
              requested_in_channel TEXT,
              requested_in_thread TEXT,
              note TEXT,
              created_at TIMESTAMP NOT NULL
            )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_optouts_user "
        "ON followup_optouts(slack_user, active)"
    )
