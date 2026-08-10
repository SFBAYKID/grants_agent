"""Reminders a rep asked for, and the unconditional right to stop them.

WHY THIS EXISTS. In July a rep asked Grant to send her a list of Texas leads and the
thread died: Grant could not send mail, and it could not hold an ask past the end of a
conversation. A request that outlives the conversation needs somewhere to live, a
worker to deliver it, and — the part that makes it acceptable to build at all — a way
for the person being reminded to make it stop.

THE FROZEN SPEC IS RE-RUN, NOT REPLAYED. A reminder stores the SEARCH, not its
results, so "remind me weekly about Texas leads" delivers the leads that exist on the
day it fires. Storing the rendered text instead would produce confidently stale
answers, which is the failure mode rule 1 exists to prevent.

THE STORED SPEC IS UNTRUSTED INPUT. It is JSON written by the model, splatted into a
function call at delivery time — so `_search_kwargs` filters it against an explicit
allowlist. Without that, a stored spec could set `db_path` or `requester_slack` and
the reminder worker would honour it. Freezing a call's arguments is only safe if the
thaw is narrower than the freeze.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Final

from .migrations_reminders import OPTOUT_SCOPES, REMINDER_CADENCES, REMINDER_CHANNELS

# Exactly the `search_leads` parameters a reminder may carry. Everything outside this
# set — db_path, requester_slack, export, on_progress — is either identity, plumbing,
# or a side effect, and none of those belong in text the model composed.
SPEC_KEYS: Final[frozenset[str]] = frozenset(
    {
        "state",
        "org_type",
        "program",
        "grade",
        "record_kind",
        "amount_min",
        "amount_max",
        "enrollment_min",
        "enrollment_max",
        "city",
        "name_contains",
        "date_field",
        "date_from",
        "date_to",
        "open_only",
        "limit",
    }
)
_NUMERIC = {"amount_min", "amount_max"}
_INTEGER = {"enrollment_min", "enrollment_max", "limit"}
_BOOLEAN = {"open_only"}
MAX_REMINDER_ROWS: Final = 25
# A recurring reminder that nobody ever acts on is nagging with extra steps. Both
# cadences stop on their own; only an explicit ask restarts them.
MAX_OCCURRENCES: Final = 12


class OptedOut(PermissionError):
    """Raised when the person asked not to be chased any more."""


@dataclass(frozen=True)
class Reminder:
    """One scheduled ask, exactly as stored."""

    reminder_id: int
    requested_by_slack: str
    audience: str
    thread_ts: str
    subject: str
    search_spec: dict[str, Any]
    cadence: str
    deliver_via: str
    next_due_at: datetime
    state: str


def _now() -> str:
    """UTC timestamp in the repo's standard stored form."""
    return datetime.now(timezone.utc).isoformat()


def _parse(value: object) -> datetime:
    """Read a stored timestamp back as an aware UTC datetime."""
    text = str(value or "")
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _row_to_reminder(row: sqlite3.Row) -> Reminder:
    """Rebuild the typed record from one database row."""
    raw = str(row["search_spec"] or "") or "{}"
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError:
        spec = {}
    return Reminder(
        reminder_id=int(row["id"]),
        requested_by_slack=str(row["requested_by_slack"]),
        audience=str(row["audience"]),
        thread_ts=str(row["thread_ts"]),
        subject=str(row["subject"]),
        search_spec=spec if isinstance(spec, dict) else {},
        cadence=str(row["cadence"]),
        deliver_via=str(row["deliver_via"]),
        next_due_at=_parse(row["next_due_at"]),
        state=str(row["state"]),
    )


def search_kwargs(spec: dict[str, Any]) -> dict[str, Any]:
    """Narrow a stored spec to safe, correctly typed `search_leads` arguments.

    This is the thaw side of the freeze, and it is deliberately stricter: unknown keys
    are dropped rather than passed through, and every value is coerced to the type the
    search expects so a string "50" cannot reach an integer comparison.
    """
    out: dict[str, Any] = {}
    for key, value in spec.items():
        if key not in SPEC_KEYS or value is None or value == "":
            continue
        if key in _NUMERIC:
            out[key] = float(value)
        elif key in _INTEGER:
            out[key] = int(value)
        elif key in _BOOLEAN:
            out[key] = bool(value)
        else:
            out[key] = str(value)
    out["limit"] = min(int(out.get("limit", MAX_REMINDER_ROWS)), MAX_REMINDER_ROWS)
    return out


def is_opted_out(
    conn: sqlite3.Connection, slack_user: str, *, scope: str = "reminders"
) -> bool:
    """Whether this person has switched off follow-ups of this kind.

    An `all` opt-out covers every scope, which is what "stop reminding me" means in
    plain English — a person who is done being chased is done everywhere, not just in
    the subsystem that happened to send the last message.
    """
    row = conn.execute(
        "SELECT 1 FROM followup_optouts WHERE slack_user=? AND active=1 "
        "AND scope IN ('all', ?) LIMIT 1",
        (slack_user, scope),
    ).fetchone()
    return row is not None


def set_optout(
    conn: sqlite3.Connection,
    slack_user: str,
    *,
    scope: str = "all",
    channel: str = "",
    thread_ts: str = "",
    note: str = "",
) -> int:
    """Record that this person asked not to be followed up with.

    Also cancels their outstanding reminders in the same transaction — leaving them
    active while claiming to have stopped would be a lie the next worker run exposes.
    """
    if scope not in OPTOUT_SCOPES:
        raise ValueError(f"scope must be one of {OPTOUT_SCOPES}")
    if not slack_user:
        raise ValueError("an opt-out has to belong to someone")
    now = _now()
    conn.execute(
        """INSERT INTO followup_optouts
             (slack_user,scope,active,requested_in_channel,requested_in_thread,
              note,created_at)
           VALUES (?,?,1,?,?,?,?)""",
        (slack_user, scope, channel, thread_ts, note, now),
    )
    cancelled = 0
    if scope in {"all", "reminders"}:
        cancelled = int(
            conn.execute(
                "UPDATE reminders SET state='cancelled', cancelled_by_slack=?, "
                "cancelled_at=?, updated_at=? WHERE requested_by_slack=? "
                "AND state='active'",
                (slack_user, now, now, slack_user),
            ).rowcount
        )
    conn.commit()
    # Returns WHAT HAPPENED, not the row id, because the caller's next act is to tell
    # a human what happened. Grant used to say "I've cancelled the reminders you had
    # running" unconditionally — so a nudges-only opt-out produced a confirmation
    # that was simply false, and the reminders kept arriving afterwards.
    return cancelled


def clear_optout(conn: sqlite3.Connection, slack_user: str) -> int:
    """Let someone switch follow-ups back on after opting out.

    The rows are deactivated rather than deleted, so "were we still messaging them
    after they asked us to stop?" stays an answerable question.
    """
    cur = conn.execute(
        "UPDATE followup_optouts SET active=0 WHERE slack_user=? AND active=1",
        (slack_user,),
    )
    conn.commit()
    return int(cur.rowcount)


def create(
    conn: sqlite3.Connection,
    *,
    requested_by_slack: str,
    audience: str,
    thread_ts: str,
    subject: str,
    due_at: datetime,
    cadence: str = "once",
    deliver_via: str = "slack",
    search_spec: dict[str, Any] | None = None,
) -> int:
    """Schedule one reminder, refusing if the requester has opted out."""
    if cadence not in REMINDER_CADENCES:
        raise ValueError(f"cadence must be one of {REMINDER_CADENCES}")
    if deliver_via not in REMINDER_CHANNELS:
        raise ValueError(f"deliver_via must be one of {REMINDER_CHANNELS}")
    if not requested_by_slack or not audience or not thread_ts:
        raise ValueError("a reminder needs a requester, a channel, and a thread")
    if not subject.strip():
        raise ValueError("a reminder needs a subject so the rep knows what it is about")
    if is_opted_out(conn, requested_by_slack):
        raise OptedOut(
            "they asked not to receive reminders; that has to be reversed explicitly "
            "before a new one can be scheduled"
        )
    now = _now()
    kept = {k: v for k, v in (search_spec or {}).items() if k in SPEC_KEYS}
    # THAW IT NOW, WHILE SOMEONE IS LISTENING. `search_kwargs` coerces types at
    # DELIVERY time, days later, inside a cron worker — so a spec the model wrote as
    # {"amount_min": "$500,000"} used to store fine and then raise every tick. The
    # worker delivers oldest-first, so that one row sat permanently at the head of
    # the queue and nothing behind it ever went out again: one bad reminder silently
    # killed the whole feature. Failing here turns that into a sentence the rep reads
    # while they can still restate the ask.
    try:
        search_kwargs(kept)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "I couldn't pin down the filters for that reminder — tell me the state or "
            f"programme in plain words and I'll set it up ({exc})"
        ) from exc
    spec = json.dumps(kept, separators=(",", ":"))
    cur = conn.execute(
        """INSERT INTO reminders
             (requested_by_slack,audience,thread_ts,subject,search_spec,cadence,
              deliver_via,next_due_at,state,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,'active',?,?)""",
        (
            requested_by_slack,
            audience,
            thread_ts,
            subject.strip(),
            spec,
            cadence,
            deliver_via,
            due_at.astimezone(timezone.utc).isoformat(),
            now,
            now,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def for_user(
    conn: sqlite3.Connection, slack_user: str, *, state: str = "active"
) -> list[Reminder]:
    """Every reminder belonging to one person, soonest first."""
    rows = conn.execute(
        "SELECT * FROM reminders WHERE requested_by_slack=? AND state=? "
        "ORDER BY next_due_at",
        (slack_user, state),
    ).fetchall()
    return [_row_to_reminder(row) for row in rows]


def cancel(conn: sqlite3.Connection, reminder_id: int, by_slack: str) -> bool:
    """Cancel one reminder. Only its owner may, and only while it is active."""
    now = _now()
    cur = conn.execute(
        "UPDATE reminders SET state='cancelled', cancelled_by_slack=?, "
        "cancelled_at=?, updated_at=? WHERE id=? AND requested_by_slack=? "
        "AND state='active'",
        (by_slack, now, now, reminder_id, by_slack),
    )
    conn.commit()
    return cur.rowcount == 1


def cancel_all(conn: sqlite3.Connection, slack_user: str) -> int:
    """Cancel every active reminder for one person, returning how many stopped."""
    now = _now()
    cur = conn.execute(
        "UPDATE reminders SET state='cancelled', cancelled_by_slack=?, "
        "cancelled_at=?, updated_at=? WHERE requested_by_slack=? AND state='active'",
        (slack_user, now, now, slack_user),
    )
    conn.commit()
    return int(cur.rowcount)


def due(conn: sqlite3.Connection, now: datetime) -> list[Reminder]:
    """Active reminders whose time has come, oldest first."""
    rows = conn.execute(
        "SELECT * FROM reminders WHERE state='active' AND next_due_at<=? "
        "ORDER BY next_due_at",
        (now.astimezone(timezone.utc).isoformat(),),
    ).fetchall()
    return [_row_to_reminder(row) for row in rows]


def occurrence_key(reminder: Reminder) -> str:
    """Identify this firing so a retry cannot deliver it twice."""
    return reminder.next_due_at.astimezone(timezone.utc).isoformat()


def reserve(conn: sqlite3.Connection, reminder: Reminder, channel: str) -> int | None:
    """Claim one occurrence before contacting Slack or Resend.

    The UNIQUE constraint does the arbitration: a second worker, or a retry after a
    crash mid-send, loses the insert and returns None rather than sending again. The
    reservation is written FIRST for the same reason the drip writes its own first —
    an unrecorded send is indistinguishable from no send at all.
    """
    try:
        cur = conn.execute(
            """INSERT INTO reminder_deliveries
                 (reminder_id,occurrence_key,channel,state,reserved_at)
               VALUES (?,?,?,'reserved',?)""",
            (reminder.reminder_id, occurrence_key(reminder), channel, _now()),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        return None
    return int(cur.lastrowid)


def complete(
    conn: sqlite3.Connection,
    delivery_id: int,
    *,
    state: str,
    slack_ts: str = "",
    email_id: str = "",
    recipient: str = "",
    detail: str = "",
) -> None:
    """Record how one reserved delivery actually turned out."""
    conn.execute(
        """UPDATE reminder_deliveries
             SET state=?, slack_ts=?, email_id=?, recipient=?, detail=?,
                 completed_at=?
           WHERE id=?""",
        (state, slack_ts, email_id, recipient, detail, _now(), delivery_id),
    )
    conn.commit()


def advance(conn: sqlite3.Connection, reminder: Reminder) -> None:
    """Re-arm a recurring reminder, or retire one that has run its course.

    A recurring reminder stops after MAX_OCCURRENCES so an ask nobody ever engages
    with expires instead of becoming permanent noise. The count is taken from the
    delivery ledger rather than a counter column, so it cannot drift from what was
    actually sent.
    """
    now = _now()
    if reminder.cadence == "once":
        conn.execute(
            "UPDATE reminders SET state='completed', updated_at=? WHERE id=?",
            (now, reminder.reminder_id),
        )
        conn.commit()
        return
    sent = int(
        conn.execute(
            "SELECT COUNT(*) FROM reminder_deliveries WHERE reminder_id=? "
            "AND state='delivered'",
            (reminder.reminder_id,),
        ).fetchone()[0]
    )
    if sent >= MAX_OCCURRENCES:
        conn.execute(
            "UPDATE reminders SET state='completed', updated_at=?, "
            "last_error='reached the recurring-reminder limit' WHERE id=?",
            (now, reminder.reminder_id),
        )
        conn.commit()
        return
    step = timedelta(days=1 if reminder.cadence == "daily" else 7)
    # Step forward from the DUE time, not from now, so a worker that runs late does
    # not permanently shift a weekly reminder to a new day of the week.
    following = reminder.next_due_at + step
    current = datetime.now(timezone.utc)
    while following <= current:
        following += step
    conn.execute(
        "UPDATE reminders SET next_due_at=?, updated_at=? WHERE id=?",
        (following.isoformat(), now, reminder.reminder_id),
    )
    conn.commit()
