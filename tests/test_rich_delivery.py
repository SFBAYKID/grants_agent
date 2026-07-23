"""Rich pacing and delivery tests with fixture DBs and fake Slack only."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from grant_watch.campaign import delivery, pacing
from tests.test_rich_preparation import _eligible_conn

NOW = datetime(2026, 7, 22, 18, 0, tzinfo=timezone.utc)  # 11:00 PT cutoff
READY = datetime(2026, 7, 22, 17, 59, tzinfo=timezone.utc)


class FakeSlack:
    """Record the outbox state observed at the moment Slack is called."""

    def __init__(
        self, conn: sqlite3.Connection, error: Exception | None = None
    ) -> None:
        self.conn = conn
        self.error = error
        self.calls: list[dict[str, object]] = []
        self.reserved_during_call = False

    def chat_postMessage(self, **kwargs: object) -> dict[str, Any]:
        """Assert reservation ordering, then return or raise a canned outcome."""
        self.calls.append(kwargs)
        self.reserved_during_call = bool(
            self.conn.execute(
                "SELECT 1 FROM notification_outbox WHERE state='sending'"
            ).fetchone()
        )
        if self.error is not None:
            raise self.error
        return {"ts": "171.001"}


def test_pacing_hard_cutoff_and_weekday_slot() -> None:
    """The deterministic slot never permits a late-afternoon catch-up."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE posts(id INTEGER,channel TEXT,posted_at TEXT);"
        "CREATE TABLE notification_outbox(id INTEGER,lead_id INTEGER,audience TEXT,created_at TEXT);"
    )
    before = datetime(2026, 7, 22, 16, 0, tzinfo=timezone.utc)  # 09:00 PT
    at_cutoff = datetime(2026, 7, 22, 18, 0, tzinfo=timezone.utc)  # 11:00 PT
    assert pacing.should_post(conn, "C", before)[0] is False
    assert pacing.should_post(conn, "C", at_cutoff) == (
        False,
        "missed the 11:00 Pacific hard cutoff",
    )
    assert pacing.should_post(conn, "C", READY)[0] is True


def test_dst_conversion_keeps_local_pacific_band() -> None:
    """Winter and summer slots are both 10:xx Pacific / 13:xx Eastern."""
    summer = pacing.daily_slot("C", datetime(2026, 7, 22, 17, tzinfo=timezone.utc))
    winter = pacing.daily_slot("C", datetime(2026, 1, 22, 18, tzinfo=timezone.utc))
    for slot in (summer, winter):
        assert slot.hour == 10 and 0 <= slot.minute <= 45
        assert slot.astimezone(pacing.ET).hour == 13


def test_dry_run_is_write_free_and_calls_no_slack(tmp_path: Path) -> None:
    """Dry-run reviews/renders but creates no snapshot, reservation, post, or status."""
    conn = _eligible_conn(tmp_path / "dry.db")
    client = FakeSlack(conn)
    before = conn.total_changes
    outcome = delivery.run(
        client,
        "CGRANTS",
        conn,
        channel_members=frozenset({"U01DFJWQQJ3"}),
        force=True,
        dry_run=True,
        now=READY,
    )
    assert outcome.startswith("[dry-run]")
    assert conn.total_changes == before
    assert client.calls == []
    assert conn.execute("SELECT COUNT(*) FROM rich_card_snapshots").fetchone()[0] == 0


def test_reservation_precedes_one_slack_post_and_finalizes_all_state(
    tmp_path: Path,
) -> None:
    """One confirmed send has a snapshot, pre-HTTP reservation, post, and surfaced lead."""
    conn = _eligible_conn(tmp_path / "sent.db")
    client = FakeSlack(conn)
    outcome = delivery.run(
        client,
        "CGRANTS",
        conn,
        channel_members=frozenset({"U01DFJWQQJ3"}),
        force=True,
        now=READY,
    )
    assert outcome.startswith("posted rich_award")
    assert client.reserved_during_call is True
    assert len(client.calls) == 1
    assert client.calls[0]["unfurl_links"] is False
    outbox = conn.execute("SELECT * FROM notification_outbox").fetchone()
    post = conn.execute("SELECT * FROM posts").fetchone()
    assert outbox["state"] == "delivered"
    assert post["snapshot_id"] == outbox["snapshot_id"]
    assert post["kind"] == "rich_award"
    assert conn.execute("SELECT status FROM leads").fetchone()[0] == "surfaced"


def test_ambiguous_send_is_never_blind_retried(tmp_path: Path) -> None:
    """A timeout retains an unknown reservation and excludes the snapshot forever."""
    conn = _eligible_conn(tmp_path / "unknown.db")
    first = delivery.run(
        FakeSlack(conn, TimeoutError("unknown")),
        "CGRANTS",
        conn,
        force=True,
        now=READY,
    )
    assert first.startswith("unknown:")
    assert (
        conn.execute("SELECT state FROM notification_outbox").fetchone()[0] == "unknown"
    )
    second_client = FakeSlack(conn)
    second = delivery.run(second_client, "CGRANTS", conn, force=True, now=READY)
    assert second.startswith("skip:")
    assert second_client.calls == []
