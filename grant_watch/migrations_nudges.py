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
    "card_escalated",  # a card went unanswered; the manager is told once, in channel
    "offer_unanswered",  # Grant offered to do something and nobody ever answered
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
    couldn't find an email". A row is written by the code that knows exactly which
    capability was missing.

    TWO WRITERS, and the difference matters. `slack/reminder_tools._remember_unmet`
    files a row AT THE MOMENT OF REFUSAL, which is how this stays true going forward.
    `cli capability-seed` files rows compiled by hand from Slack history, which is
    how the asks that predate this table got in. A seeded row is not evidence Grant
    noticed anything — its `recorded_by` says which it was.

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


def migration_36_nudge_message_variants(conn: sqlite3.Connection) -> None:
    """Record WHICH WORDING was sent, and whether the person answered it.

    Chase's ask: "if a user does not respond to a message, the system should say to
    itself, maybe I should write it differently next time." That is an optimisation
    problem, and the honest first step is not an optimiser — it is a measurement.
    Without these two columns there is no way to tell whether one wording does
    better than another, so any rewriting would be superstition.

    `variant` is the wording identifier chosen at send time. `engaged_at` is set when
    a HUMAN posts in that thread after the nudge landed, which is the only engagement
    signal Grant can honestly observe: a reply in the thread it spoke into. It is
    deliberately NOT "the rep did the work" — they may have phoned the district — so
    everything computed from this is a reply rate, never an outcome rate.
    """
    existing = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(followup_nudges)")
    }
    if "variant" not in existing:
        conn.execute("ALTER TABLE followup_nudges ADD COLUMN variant TEXT")
    if "engaged_at" not in existing:
        conn.execute("ALTER TABLE followup_nudges ADD COLUMN engaged_at TIMESTAMP")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_nudges_variant "
        "ON followup_nudges(subject_kind, variant, state)"
    )


def migration_38_announcements(conn: sqlite3.Connection) -> None:
    """A short, plain-language "here's what changed" post, and proof it went out once.

    WHY A TABLE AND NOT A GENERATED SUMMARY. The obvious implementation is to
    summarise recent commits, and it would be wrong twice: commit messages are
    written for engineers, and a model summarising its own changelog will confidently
    describe capabilities that do not work yet. This has happened here already —
    Grant once told a rep it would "keep watching those states" when nothing of the
    kind existed, and she never came back.

    So an announcement is AUTHORED, reviewed, and stored as the exact words that will
    be posted. Grant delivers it; it does not compose it. `posted_at` makes the
    one-shot rule a fact about the row rather than a hope about the scheduler.
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS announcements (
              id INTEGER PRIMARY KEY,
              slug TEXT NOT NULL UNIQUE,
              audience TEXT NOT NULL,
              body TEXT NOT NULL,
              capabilities TEXT NOT NULL DEFAULT '',
              created_at TIMESTAMP NOT NULL,
              posted_at TIMESTAMP,
              slack_ts TEXT
            )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_announcements_pending "
        "ON announcements(posted_at, audience)"
    )


def migration_39_user_memory(conn: sqlite3.Connection) -> None:
    """What Grant remembers about a colleague, with the words that justify it.

    Chase: "You are trying to almost befriend the user and build a relationship with
    them." A colleague who tells you their kid plays lacrosse, or that they only work
    Texas, or that they hate spreadsheets, expects you to still know that next month.
    Grant currently forgets everything the moment a thread ends.

    THREE THINGS MAKE THIS SAFE TO KEEP.

    `evidence` holds the person's OWN words, verbatim, and a memory may not be stored
    without them. A remembered "fact" about a person that nobody can trace to
    something they said is gossip Grant invented about a colleague — the same failure
    as a fabricated contact, aimed at someone on the team.

    `expires_at` is written at insert, not enforced by a cleanup someone remembers to
    run. Chase set the horizon: "Maybe six months of memory per user... After that it
    can delete or override itself because you do not need that much data." A read
    filters on it, so an expired memory is invisible even if the purge never runs.

    `superseded_by` lets a later statement replace an earlier one instead of both
    being true at once. People change territory, job title, and preference; a memory
    store that only accumulates will eventually tell Grant two contradictory things
    about the same person and it will pick one at random.
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS user_memory (
              id INTEGER PRIMARY KEY,
              slack_user TEXT NOT NULL,
              kind TEXT NOT NULL,
              fact TEXT NOT NULL,
              evidence TEXT NOT NULL,
              audience TEXT NOT NULL DEFAULT '',
              thread_ts TEXT NOT NULL DEFAULT '',
              message_ts TEXT NOT NULL DEFAULT '',
              recorded_at TIMESTAMP NOT NULL,
              expires_at TIMESTAMP NOT NULL,
              superseded_by INTEGER REFERENCES user_memory(id),
              UNIQUE(slack_user, kind, fact)
            )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_user_memory_live "
        "ON user_memory(slack_user, expires_at, superseded_by)"
    )
