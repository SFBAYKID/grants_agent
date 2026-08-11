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
explicit act rather than a side effect of a code deploy — the code shipping and the
team being told about it are different decisions, and only the second one messages
real people.

`close` is called when the follow-up is actually DELIVERED (see slack/nudges.run), so
"we came back to them about this" is recorded rather than inferred from the one-shot
key.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

# Keeps a discovered capability name storable and safe to interpolate, without
# constraining WHICH capabilities may exist.
_SLUG = re.compile(r"[a-z][a-z0-9_]{2,39}")

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
    correction: str = "",
) -> int | None:
    """Record one unmet ask. Returns None when this ask is already on file.

    The duplicate case is a normal outcome, not an error: the same person asking the
    same thing twice in one thread should still produce exactly one follow-up.
    """
    # A SLUG, NOT A CLOSED ENUM. This used to reject anything outside a hard-coded
    # list, which meant the next kind of ask a rep invents could not be recorded
    # until someone edited this file — the exact "then we go hard-code that too"
    # trap Chase named. `CAPABILITY_KINDS` still lists the ones with hand-written
    # wording; anything else records fine and falls back to the generic offer.
    if not _SLUG.fullmatch(capability):
        raise ValueError("capability must be a short slug like 'email_results'")
    if not ask_text.strip():
        raise ValueError("an unmet ask needs the human's own words")
    if not slack_user or not audience or not thread_ts or not message_ts:
        raise ValueError("an unmet ask needs a person, a channel, and a message")
    try:
        cur = conn.execute(
            """INSERT INTO capability_asks
                 (slack_user,audience,thread_ts,message_ts,asked_at,ask_text,
                  capability,available_since,state,evidence_url,recorded_by,
                  correction,created_at)
               VALUES (?,?,?,?,?,?,?,?,'open',?,?,?,?)""",
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
                correction.strip(),
                _now(),
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        # The ask is already on file. Refresh only the CORRECTION — Grant's own
        # apology, which is authored and gets edited — and never `ask_text`, which is
        # what a colleague actually said and must stay verbatim forever.
        if correction.strip():
            conn.execute(
                """UPDATE capability_asks SET correction=?
                    WHERE audience=? AND message_ts=? AND capability=?""",
                (correction.strip(), audience, message_ts, capability),
            )
            conn.commit()
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
    # Same slug rule as `record`: a capability discovered by a thread scan must be
    # armable without a code change, or discovery just relocates the bottleneck.
    if not _SLUG.fullmatch(capability):
        raise ValueError("capability must be a short slug like 'email_results'")
    # DECLARING IS A BROADCAST, so it refuses a capability with no wording. This call
    # reopens EVERY ask waiting on the slug at once — production held nine for one of
    # them — and a slug missing from the wording tables does not degrade quietly: it
    # sends "Good news — I can do that one now" to all of them. Found because
    # `add_leads_to_campaign` had been declared live with three asks and no sentence.
    # Writing the sentence first is one line; unsending the generic one is impossible.
    from .slack.nudge_messages import wording_exists

    if not wording_exists(capability):
        raise ValueError(
            f"{capability!r} has no hand-written follow-up wording; add it to "
            "slack/nudge_messages.py before declaring the capability live"
        )
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
