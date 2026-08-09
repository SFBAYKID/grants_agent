"""Asks Grant had to refuse, so that shipping the feature can close the loop.

WHY THIS IS A LEDGER AND NOT A MEMORY. When a rep asked Grant to email her a list and
Grant could not, the ask vanished with the conversation. Nobody was at fault and
nothing was broken — there was simply no place for "somebody wanted this and did not
get it" to live. Every unmet ask was a silent, permanent loss of a customer signal.

WHAT MAKES THE FOLLOW-UP HONEST. Each row keeps the human's words VERBATIM plus a
permalink to the message. When Grant later says "you asked for this and I can do it
now", that sentence is a claim about what a named colleague said on a date, and it
ships with the receipt. `ask_text` is never paraphrased or regenerated — a summary
that drifts is Grant putting words in a person's mouth.

`available_since` is the hinge. It stays NULL while the capability is missing, so the
row is inert; setting it is what makes the ask eligible to be reopened, and it is set
by `mark_available` when a feature actually ships. That is deliberately a separate,
explicit act rather than a side effect of a code deploy.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from .migrations_nudges import CAPABILITY_KINDS

ASK_STATES = ("open", "answered", "dismissed")


@dataclass(frozen=True)
class CapabilityAsk:
    """One recorded ask, exactly as the human made it."""

    ask_id: int
    slack_user: str
    audience: str
    thread_ts: str
    ask_text: str
    capability: str
    state: str


def _now() -> str:
    """UTC timestamp in the repo's standard stored form."""
    return datetime.now(timezone.utc).isoformat()


def record(
    conn: sqlite3.Connection,
    *,
    slack_user: str,
    audience: str,
    thread_ts: str,
    message_ts: str,
    ask_text: str,
    capability: str,
    asked_at: str,
    recorded_by: str,
    evidence_url: str = "",
    available_since: str | None = None,
) -> int | None:
    """Record one unmet ask. Returns None when this ask is already on file.

    The duplicate case is a normal outcome, not an error: the same person asking the
    same thing twice in one thread should still produce exactly one follow-up.
    """
    if capability not in CAPABILITY_KINDS:
        raise ValueError(f"capability must be one of {CAPABILITY_KINDS}")
    if not ask_text.strip():
        raise ValueError("an unmet ask needs the human's own words")
    if not slack_user or not audience or not thread_ts or not message_ts:
        raise ValueError("an unmet ask needs a person, a channel, and a message")
    try:
        cur = conn.execute(
            """INSERT INTO capability_asks
                 (slack_user,audience,thread_ts,message_ts,asked_at,ask_text,
                  capability,available_since,state,evidence_url,recorded_by,created_at)
               VALUES (?,?,?,?,?,?,?,?,'open',?,?,?)""",
            (
                slack_user,
                audience,
                thread_ts,
                message_ts,
                asked_at,
                ask_text.strip(),
                capability,
                available_since,
                evidence_url,
                recorded_by,
                _now(),
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        return None
    return int(cur.lastrowid)


def mark_available(
    conn: sqlite3.Connection, capability: str, *, shipped_at: str | None = None
) -> int:
    """Declare a capability live, making every ask waiting on it eligible.

    Only rows that are still `open` and still waiting are touched, so re-running this
    after a later deploy cannot resurrect an ask somebody already answered or reopen
    one whose clock has already started.
    """
    if capability not in CAPABILITY_KINDS:
        raise ValueError(f"capability must be one of {CAPABILITY_KINDS}")
    cur = conn.execute(
        "UPDATE capability_asks SET available_since=? "
        "WHERE capability=? AND state='open' AND available_since IS NULL",
        (shipped_at or _now(), capability),
    )
    conn.commit()
    return int(cur.rowcount)


def close(conn: sqlite3.Connection, ask_id: int, *, state: str = "answered") -> bool:
    """Mark one ask dealt with so it is never reopened."""
    if state not in ASK_STATES:
        raise ValueError(f"state must be one of {ASK_STATES}")
    cur = conn.execute(
        "UPDATE capability_asks SET state=? WHERE id=? AND state='open'",
        (state, ask_id),
    )
    conn.commit()
    return cur.rowcount == 1


def open_asks(conn: sqlite3.Connection) -> list[CapabilityAsk]:
    """Every ask still waiting, oldest first."""
    rows = conn.execute(
        "SELECT * FROM capability_asks WHERE state='open' ORDER BY asked_at"
    ).fetchall()
    return [
        CapabilityAsk(
            ask_id=int(row["id"]),
            slack_user=str(row["slack_user"]),
            audience=str(row["audience"]),
            thread_ts=str(row["thread_ts"]),
            ask_text=str(row["ask_text"]),
            capability=str(row["capability"]),
            state=str(row["state"]),
        )
        for row in rows
    ]
