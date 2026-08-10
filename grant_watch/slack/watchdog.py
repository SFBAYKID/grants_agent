"""Repair conversations that died mid-turn, before anyone has to ask twice.

WHY THIS EXISTS. Chase, watching a live thread: "The bot is still thinking… Its been
hours." Grant posts a "Thinking…" spinner the moment it starts work and edits that
same message into the answer when it finishes. If the process dies in between — a
deploy restart, the keepalive relaunching the listener, an OOM — nobody edits it. The
spinner stays on screen forever and the person reasonably concludes the product is
broken. That is the worst possible failure for adoption, because it is silent, it is
permanent, and it looks exactly like the bot ignoring them.

The receipt already records the death: `slack_event_receipts` is left at `processing`
with `reviewed_at` NULL. `nudges.thread_abandoned` reads exactly that and follows up —
but on a one-day grace, which is the right cadence for "I never got you an answer"
and completely wrong for a spinner someone is looking at right now.

So this runs every few minutes and does the small honest thing: replace the spinner
with a sentence admitting what happened and offering to try again. It is the same
recovery, arriving while the person is still in the thread.

WHY IT SEARCHES THE THREAD FOR THE SPINNER rather than storing its timestamp. The
receipt has no column for it, and adding one would not repair the messages already
stranded — the ones this was written for. Matching Grant's own most recent spinner in
the thread works on the backlog and on everything new, with no migration.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# How long a turn may plausibly still be running. The agent loop is bounded at
# MAX_TOOL_TURNS with a 60s client timeout and two retries, so ~18 minutes is the
# true worst case for a live turn; past that it is dead, not slow. Sitting a little
# above that ceiling means this can never interrupt work that is still happening.
STUCK_AFTER = timedelta(minutes=20)

# Nothing older than this is worth editing — but the number is not free to choose.
# It MUST be at least the nudge worker's DROP_AFTER, because `thread_abandoned`
# apologises for exactly these receipts. At 3 days against DROP_AFTER's 14 there was
# a band where a thread was too old for the watchdog to repair and not yet stale for
# the nudge worker, so Grant would apologise for a dead conversation while its
# "Thinking…" spinner stayed on screen forever. An apology beside an unresolved
# spinner is worse than either alone.
TOO_OLD = timedelta(days=14)

# The live spinner is a frame char, a space, then a short phrase ending in an
# ellipsis — "/ Thinking…", "| Thinking…". Kept identical to the pattern the retired
# boot sweep used, which was proven against the real stranded message.
_SPINNER = re.compile(r"^[/\\|—] \S.{0,80}…$")

# What replaces it. Short, honest about the cause, and ends in something answerable —
# the same rules as a nudge, because this is the same kind of message.
RECOVERY_TEXT = (
    "Sorry — I lost my train of thought on this one and never finished. "
    "Nothing was saved. Want me to have another go?"
)


@dataclass(frozen=True)
class StuckTurn:
    """One conversation that started and never finished."""

    event_id: str
    channel: str
    thread_ts: str
    slack_user: str
    received_at: datetime


def _parse(value: object) -> datetime | None:
    """Parse a stored timestamp, tolerating NULL and junk."""
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def stuck_turns(conn: sqlite3.Connection, now: datetime) -> list[StuckTurn]:
    """Conversations that began, were never finished, and are old enough to be dead.

    `processing` AND `needs_reconciliation`: the first is a turn killed outright, the
    second one whose final message failed to send. Both leave a spinner on screen.
    """
    rows = conn.execute(
        """SELECT event_id,channel,thread_ts,slack_user,received_at
             FROM slack_event_receipts
            WHERE state IN ('processing','needs_reconciliation')
              AND reviewed_at IS NULL
              AND channel IS NOT NULL
              AND thread_ts IS NOT NULL
            ORDER BY received_at DESC
            LIMIT 50"""
    ).fetchall()
    out: list[StuckTurn] = []
    for row in rows:
        received = _parse(row["received_at"])
        if received is None:
            continue
        age = now - received
        if age < STUCK_AFTER or age > TOO_OLD:
            continue
        out.append(
            StuckTurn(
                event_id=str(row["event_id"]),
                channel=str(row["channel"]),
                thread_ts=str(row["thread_ts"]),
                slack_user=str(row["slack_user"] or ""),
                received_at=received,
            )
        )
    return out


def find_spinner(client: WebClient, turn: StuckTurn, bot_id: str) -> str | None:
    """Grant's stranded spinner ts, "" if it answered, or None if we could not look.

    THREE OUTCOMES, NOT TWO. A `SlackApiError` on this READ used to return "", which
    the caller reads as "Grant did answer" and closes the receipt. So one transient
    429 — on a Tier-3 method, called once per stuck turn, up to 50 per run, with no
    retry handler — left the spinner on screen forever AND permanently suppressed the
    `thread_abandoned` apology, killing both recovery paths at once.

    "I could not look" is not "there was nothing there", and the difference is the
    only thing keeping a dead conversation visible to the next run.
    """
    try:
        replies = client.conversations_replies(
            channel=turn.channel, ts=turn.thread_ts, limit=100
        )
    except SlackApiError:
        return None
    for message in reversed(replies.get("messages", []) or []):
        # Match on `user`, NOT `bot_id`. Grant's messages carry both — user
        # `U0BH0ESRJ4W` and bot `B0BH4C9098R` — and `auth_test()` returns the USER
        # id, so comparing it against `bot_id` never matches and the repair silently
        # finds nothing. Verified against a real message before relying on it.
        if bot_id and str(message.get("user") or "") != bot_id:
            continue
        text = str(message.get("text") or "").strip()
        if not text:
            continue
        # Only the most recent Grant message matters. If it is a spinner the turn
        # died; if it is anything else, Grant did answer.
        return str(message.get("ts") or "") if _SPINNER.match(text) else ""
    return ""


def run(
    client: WebClient | None,
    conn: sqlite3.Connection,
    *,
    bot_id: str = "",
    dry_run: bool = True,
    now: datetime | None = None,
) -> str:
    """Replace stranded spinners with an honest line. Returns what it did.

    Marks the receipt reviewed only after Slack confirms the edit, so a failure here
    leaves the turn visible to the next run and to the `thread_abandoned` follow-up
    rather than silently retiring it.
    """
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    turns = stuck_turns(conn, current)
    if not turns:
        return "watchdog: nothing stuck"
    if client is None:
        return f"watchdog: {len(turns)} stuck, no Slack client configured"

    # AN EMPTY BOT ID MATCHES EVERY MESSAGE. `find_spinner` skips messages whose
    # author is not Grant only when `bot_id` is truthy, so with "" the first message
    # it sees is a human's, that is not a spinner, and the whole backlog is closed as
    # "already answered" in a single run. An `auth_test()` that returns no user_id is
    # a reason to do nothing, not a reason to sweep.
    if not bot_id:
        return "watchdog: refusing to run without Grant's user id"

    repaired = 0
    answered = 0
    unreachable = 0
    for turn in turns:
        spinner_ts = find_spinner(client, turn, bot_id)
        if spinner_ts is None:
            unreachable += 1
            continue
        if not spinner_ts:
            # Grant did answer; the receipt is just wrong. Close it quietly so it
            # stops being counted as a dead thread.
            answered += 1
            if not dry_run:
                _mark_reviewed(conn, turn.event_id)
            continue
        if dry_run:
            repaired += 1
            continue
        try:
            client.chat_update(channel=turn.channel, ts=spinner_ts, text=RECOVERY_TEXT)
        except SlackApiError:
            continue  # left unreviewed on purpose, so the next run retries
        _mark_reviewed(conn, turn.event_id)
        repaired += 1

    prefix = "[dry-run] " if dry_run else ""
    unread = f", {unreachable} could not be read" if unreachable else ""
    return (
        f"{prefix}watchdog: {repaired} stranded spinner(s) replaced, "
        f"{answered} already answered{unread}"
    )


def _mark_reviewed(conn: sqlite3.Connection, event_id: str) -> None:
    """Record that this dead turn has been dealt with."""
    conn.execute(
        "UPDATE slack_event_receipts SET reviewed_at=? WHERE event_id=?",
        (datetime.now(timezone.utc).isoformat(), event_id),
    )
    conn.commit()
