"""What Grant remembers about a colleague between conversations.

Chase: "It has memory, it remembers, and it follows up. It should be able to say to a
user, 'Hey, I am following up on something from a few days ago.'… personal things, if
a user shares personal information, that should absolutely be captured. You are
trying to almost befriend the user and build a relationship with them."

Grant currently forgets everything when a thread ends. A rep who says "I only cover
Texas" says it again next week, and one who mentions their kid's game gets a blank
stare. That is the difference between an assistant and a search box.

THE RULE THAT MAKES A MEMORY ABOUT A PERSON SAFE. Every row carries the colleague's
OWN words, verbatim, and `remember` refuses without them — the quote must appear
literally in something they typed. A "fact" about a named colleague that nobody can
trace to something they said is not a memory, it is Grant inventing a claim about a
person, which is rule 1 aimed at the team instead of at a lead. Same mechanical check
as `thread_scanner`, for the same reason: an LLM asked to summarise what someone said
will happily produce something tidier and slightly false.

SIX MONTHS, AND IT EXPIRES WITHOUT ANYONE REMEMBERING TO PURGE. Chase set the
horizon. `expires_at` is stamped at insert and every read filters on it, so a memory
is invisible the day it lapses whether or not the purge job ever runs. `purge` only
reclaims space; it is not what enforces the policy.

CORRECTION BEATS ACCUMULATION. People change territory and job and mind. `supersede`
retires the old row rather than letting Grant hold two contradictory beliefs and pick
one by row order — the same defect that once let a rep see a Teacher instead of an
Assistant Superintendent.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# Chase's number. Long enough to feel like a colleague who pays attention, short
# enough that Grant is not keeping a dossier.
RETENTION = timedelta(days=182)

# What a memory may be about. Deliberately small and human — these are the things a
# colleague would actually recall, not a CRM schema.
KINDS = ("personal", "preference", "territory", "context")

# Below this a "quote" is too short to prove anything; "ok" appears in every thread.
MIN_EVIDENCE = 8


@dataclass(frozen=True)
class Memory:
    """One thing Grant knows about a colleague, and the words that justify it."""

    memory_id: int
    slack_user: str
    kind: str
    fact: str
    evidence: str
    recorded_at: datetime


def _now() -> datetime:
    """Current UTC moment."""
    return datetime.now(timezone.utc)


def _norm(text: str) -> str:
    """Collapse whitespace so a quote survives Slack's wrapping."""
    return re.sub(r"\s+", " ", text or "").strip().lower()


def evidence_supports(evidence: str, said: str) -> bool:
    """Is this evidence literally something the person typed?

    The guard that separates remembering from inventing. A paraphrase that means the
    same thing is not acceptable: Grant will later say "you mentioned" and attach the
    words to a named colleague, possibly months later, possibly in front of others.
    """
    needle = _norm(evidence)
    if len(needle) < MIN_EVIDENCE:
        return False
    return needle in _norm(said)


def remember(
    conn: sqlite3.Connection,
    *,
    slack_user: str,
    kind: str,
    fact: str,
    evidence: str,
    said: str,
    audience: str = "",
    thread_ts: str = "",
    message_ts: str = "",
    now: datetime | None = None,
) -> int | None:
    """Record one thing about a colleague. Returns None if already known.

    `said` is the full message the person actually wrote; `evidence` must appear
    inside it verbatim. Passing them separately is what lets the check be mechanical
    rather than a promise in a prompt.
    """
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    if not slack_user:
        raise ValueError("a memory needs the person it is about")
    if not fact.strip():
        raise ValueError("a memory needs something to remember")
    if not evidence_supports(evidence, said):
        raise ValueError(
            "a memory must quote the person verbatim; "
            "this evidence does not appear in what they said"
        )
    moment = now or _now()
    try:
        cur = conn.execute(
            """INSERT INTO user_memory
                 (slack_user,kind,fact,evidence,audience,thread_ts,message_ts,
                  recorded_at,expires_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                slack_user,
                kind,
                fact.strip(),
                evidence.strip(),
                audience,
                thread_ts,
                message_ts,
                moment.isoformat(),
                (moment + RETENTION).isoformat(),
            ),
        )
    except sqlite3.IntegrityError:
        return None  # already known; remembering twice is not new information
    conn.commit()
    return int(cur.lastrowid or 0)


def recall(
    conn: sqlite3.Connection, slack_user: str, *, now: datetime | None = None
) -> list[Memory]:
    """Everything Grant currently knows about one colleague, freshest first.

    Expiry and supersession are applied HERE rather than by a cleanup job, so a
    lapsed or corrected memory cannot reach a conversation even if no purge has run.
    """
    moment = (now or _now()).isoformat()
    rows = conn.execute(
        """SELECT id,slack_user,kind,fact,evidence,recorded_at
             FROM user_memory
            WHERE slack_user=?
              AND expires_at>?
              AND superseded_by IS NULL
            ORDER BY recorded_at DESC
            LIMIT 40""",
        (slack_user, moment),
    ).fetchall()
    out: list[Memory] = []
    for row in rows:
        recorded = datetime.fromisoformat(str(row["recorded_at"]))
        out.append(
            Memory(
                memory_id=int(row["id"]),
                slack_user=str(row["slack_user"]),
                kind=str(row["kind"]),
                fact=str(row["fact"]),
                evidence=str(row["evidence"]),
                recorded_at=recorded,
            )
        )
    return out


def supersede(conn: sqlite3.Connection, old_id: int, new_id: int) -> bool:
    """Retire a memory that a later statement replaced. Returns whether it applied."""
    if old_id == new_id:
        raise ValueError("a memory cannot supersede itself")
    cur = conn.execute(
        "UPDATE user_memory SET superseded_by=? WHERE id=? AND superseded_by IS NULL",
        (new_id, old_id),
    )
    conn.commit()
    return cur.rowcount > 0


def forget(conn: sqlite3.Connection, slack_user: str) -> int:
    """Delete everything Grant knows about one colleague. Returns how many rows.

    Someone who asks Grant to forget them means it, and the answer has to be deletion
    rather than a flag — "I've stopped remembering" must be true, not a filter.
    """
    cur = conn.execute("DELETE FROM user_memory WHERE slack_user=?", (slack_user,))
    conn.commit()
    return int(cur.rowcount)


def purge(conn: sqlite3.Connection, *, now: datetime | None = None) -> int:
    """Delete memories past the retention horizon. Returns how many were removed.

    Housekeeping only. `recall` already hides expired rows, so a purge that never
    runs costs disk, not correctness.
    """
    cur = conn.execute(
        "DELETE FROM user_memory WHERE expires_at<=?", ((now or _now()).isoformat(),)
    )
    conn.commit()
    return int(cur.rowcount)


def as_prompt_context(memories: list[Memory]) -> str:
    """Render what Grant knows for the system prompt, or "" if it knows nothing.

    Says "they mentioned" rather than stating facts flatly, because every line here
    came from one thing a person said once and may since have changed. Grant should
    sound like a colleague who remembers, not a database reciting a record.
    """
    if not memories:
        return ""
    lines = [f"- {m.fact} (they mentioned: “{m.evidence}”)" for m in memories[:12]]
    return (
        "Things this person has told you before. Use them naturally, the way a "
        "colleague would; never recite them back as a list, and never treat one as "
        "still true if they say otherwise now:\n" + "\n".join(lines)
    )
