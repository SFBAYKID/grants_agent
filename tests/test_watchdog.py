"""A conversation killed mid-turn must never leave a spinner on screen.

Chase, on a thread that had been sitting on "Thinking…" since a restart four hours
earlier: "The bot is still thinking. Its been hours." Nothing was running — the
process had died 42 seconds into the turn and the placeholder was simply never
edited. Every test here is about that message getting resolved.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from grant_watch import db
from grant_watch.slack import watchdog

NOW = datetime(2026, 8, 10, 6, 0, tzinfo=timezone.utc)
BOT = "UBOT"


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    """A migrated database, never the developer's own."""
    return db.connect(tmp_path / "w.db")


def _receipt(
    conn: sqlite3.Connection,
    *,
    event_id: str = "Ev1",
    state: str = "processing",
    age: timedelta = timedelta(hours=4),
    channel: str = "C0PLAY",
) -> None:
    """One receipt for a turn that started and never finished."""
    conn.execute(
        """INSERT INTO slack_event_receipts
             (event_id,workspace,channel,thread_ts,slack_user,state,received_at)
           VALUES (?,?,?,?,?,?,?)""",
        (event_id, "T1", channel, "100.1", "UCHASE", state, (NOW - age).isoformat()),
    )
    conn.commit()


class _Slack:
    """A Slack stand-in holding one thread."""

    def __init__(self, messages: list[dict[str, object]]) -> None:
        """Seed the thread this fake will return."""
        self.messages = messages
        self.updated: list[tuple[str, str]] = []

    def conversations_replies(self, **_kw: object) -> dict[str, object]:
        """The thread under test."""
        return {"messages": self.messages}

    def chat_update(self, channel: str, ts: str, text: str) -> dict[str, object]:
        """Record the repair."""
        self.updated.append((ts, text))
        return {"ok": True}


def test_a_stranded_spinner_is_replaced_in_place(conn: sqlite3.Connection) -> None:
    """The whole point: the message a person is staring at gets resolved."""
    _receipt(conn)
    client = _Slack(
        [
            {
                "user": "UCHASE",
                "ts": "100.1",
                "text": "find me every school in chicago",
            },
            {"user": BOT, "ts": "100.2", "text": "| Thinking…"},
        ]
    )
    out = watchdog.run(client, conn, bot_id=BOT, dry_run=False, now=NOW)
    assert "1 stranded spinner(s) replaced" in out, out
    assert client.updated == [("100.2", watchdog.RECOVERY_TEXT)]
    row = conn.execute("SELECT reviewed_at FROM slack_event_receipts").fetchone()
    assert row["reviewed_at"], "the dead receipt was never closed"
    conn.close()


def test_the_spinner_is_matched_on_user_not_bot_id(conn: sqlite3.Connection) -> None:
    """Grant's messages carry BOTH a user id and a bot id, and they differ.

    `auth_test()` returns the USER id. An earlier version of this module compared it
    against the message's `bot_id`, which never matches — the repair would have found
    nothing and reported success, which is the same silence it exists to end.
    """
    _receipt(conn)
    client = _Slack(
        [{"user": BOT, "bot_id": "B0BH4C9098R", "ts": "100.2", "text": "| Thinking…"}]
    )
    assert watchdog.run(client, conn, bot_id=BOT, dry_run=False, now=NOW).endswith(
        "0 already answered"
    )
    assert client.updated, "matching on bot_id instead of user found nothing"
    conn.close()


def test_a_completed_answer_is_never_overwritten(conn: sqlite3.Connection) -> None:
    """A stale receipt on a thread Grant DID answer must not become an apology.

    The July case: the receipt was left at `processing` but Grant had recovered
    conversationally and answered in full. Replacing that with "I lost my train of
    thought" would destroy a good answer and tell the person something false.
    """
    _receipt(conn)
    client = _Slack(
        [
            {"user": "UCHASE", "ts": "100.1", "text": "who runs IT at Bellaire?"},
            {"user": BOT, "ts": "100.2", "text": "Scott Trimble, Superintendent."},
        ]
    )
    out = watchdog.run(client, conn, bot_id=BOT, dry_run=False, now=NOW)
    assert client.updated == []
    assert "1 already answered" in out
    row = conn.execute("SELECT reviewed_at FROM slack_event_receipts").fetchone()
    assert row["reviewed_at"], "a resolved thread stayed queued as dead forever"
    conn.close()


def test_a_turn_that_may_still_be_running_is_left_alone(
    conn: sqlite3.Connection,
) -> None:
    """The agent loop can legitimately take minutes; interrupting it is worse."""
    _receipt(conn, age=timedelta(minutes=5))
    client = _Slack([{"user": BOT, "ts": "100.2", "text": "| Thinking…"}])
    assert "nothing stuck" in watchdog.run(
        client, conn, bot_id=BOT, dry_run=False, now=NOW
    )
    assert client.updated == []
    conn.close()


def test_an_ancient_spinner_is_not_resurfaced(conn: sqlite3.Connection) -> None:
    """A spinner from a month ago was read as a failure long ago. Leave it."""
    _receipt(conn, age=timedelta(days=30))
    client = _Slack([{"user": BOT, "ts": "100.2", "text": "| Thinking…"}])
    assert "nothing stuck" in watchdog.run(
        client, conn, bot_id=BOT, dry_run=False, now=NOW
    )
    conn.close()


def test_the_repair_horizon_covers_everything_the_nudge_worker_apologises_for(
    conn: sqlite3.Connection,
) -> None:
    """Two horizons that disagree leave a band where Grant apologises and never repairs.

    `thread_abandoned` reopens exactly the receipts this module repairs. While the
    watchdog gave up at 3 days and the nudge worker stayed interested until 14, a
    thread that died on day four got an apology while its "Thinking…" spinner stayed
    on screen forever. An apology beside an unresolved spinner is worse than either
    alone.
    """
    from grant_watch.slack import nudges

    assert watchdog.TOO_OLD >= nudges.DROP_AFTER, (
        "a receipt can be too old to repair and still young enough to apologise for"
    )
    conn.close()


def test_a_failed_edit_leaves_the_turn_for_the_next_run(
    conn: sqlite3.Connection,
) -> None:
    """Never mark a repair done that Slack refused — the spinner is still up."""
    from slack_sdk.errors import SlackApiError

    _receipt(conn)

    class _Broken(_Slack):
        """Slack refusing the edit."""

        def chat_update(self, channel: str, ts: str, text: str) -> dict[str, object]:
            """Refuse it."""
            raise SlackApiError("ratelimited", {"error": "ratelimited"})

    client = _Broken([{"user": BOT, "ts": "100.2", "text": "| Thinking…"}])
    watchdog.run(client, conn, bot_id=BOT, dry_run=False, now=NOW)
    row = conn.execute("SELECT reviewed_at FROM slack_event_receipts").fetchone()
    assert row["reviewed_at"] is None, "a failed repair was recorded as done"
    conn.close()


def test_a_dry_run_edits_nothing(conn: sqlite3.Connection) -> None:
    """Default-safe, like every other path that writes to Slack."""
    _receipt(conn)
    client = _Slack([{"user": BOT, "ts": "100.2", "text": "| Thinking…"}])
    assert watchdog.run(client, conn, bot_id=BOT, now=NOW).startswith("[dry-run]")
    assert client.updated == []
    row = conn.execute("SELECT reviewed_at FROM slack_event_receipts").fetchone()
    assert row["reviewed_at"] is None
    conn.close()


def test_a_dm_is_repaired_too(conn: sqlite3.Connection) -> None:
    """The real incident was in a DM-like channel the old boot sweep never looked at.

    That sweep scanned only `primary_channel_id()`, so a turn that died anywhere else
    stayed broken forever. Starting from the receipt makes the channel a fact rather
    than an assumption.
    """
    _receipt(conn, channel="D0BGW7EP3K5")
    client = _Slack([{"user": BOT, "ts": "100.2", "text": "| Thinking…"}])
    watchdog.run(client, conn, bot_id=BOT, dry_run=False, now=NOW)
    assert client.updated, "a dead turn outside the primary channel was never repaired"
    conn.close()
