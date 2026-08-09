"""Rich pacing and delivery tests with fixture DBs and fake Slack only."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier
from typing import Any

from slack_sdk.errors import SlackApiError

from grant_watch import db
from grant_watch.campaign import delivery, pacing
from tests.test_rich_preparation import _eligible_conn

READY = datetime(2026, 7, 22, 17, 59, tzinfo=timezone.utc)


class FakeSlack:
    """Record the outbox state observed at the moment Slack is called."""

    def __init__(
        self, conn: sqlite3.Connection, error: Exception | None = None
    ) -> None:
        """Initialize the fake with optional canned Slack failure."""
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


class FakeSlackResponse:
    """Minimal HTTP-200 Slack rejection payload."""

    status_code = 200

    def __init__(self, code: str) -> None:
        """Store one definitive Slack error code."""
        self.code = code

    def get(self, key: str, default: object = None) -> object:
        """Expose Slack's error field through mapping-like access."""
        return self.code if key == "error" else default


def test_pacing_hard_cutoff_and_weekday_slot() -> None:
    """The deterministic slot never permits a late-afternoon catch-up."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE posts(id INTEGER,channel TEXT,posted_at TEXT);"
        # `state` mirrors the real schema: the daily-cap count excludes reservations
        # that provably never reached Slack, so a stub without it cannot exercise it.
        "CREATE TABLE notification_outbox(id INTEGER,lead_id INTEGER,"
        "audience TEXT,created_at TEXT,state TEXT);"
        "CREATE TABLE proactive_daily_slots(audience TEXT,local_date TEXT,delivery_kind TEXT,delivery_key TEXT,reserved_at TEXT);"
    )
    before = datetime(2026, 7, 22, 16, 0, tzinfo=timezone.utc)  # 09:00 PT
    # 11:00 PT is now ADMITTED: it is the first :00/:30 cron tick after any slot in
    # 10:31–10:45, which the old 11:00 cutoff refused — making those slots unreachable.
    late_morning_tick = datetime(2026, 7, 22, 18, 0, tzinfo=timezone.utc)  # 11:00 PT
    at_cutoff = datetime(2026, 7, 22, 18, 30, tzinfo=timezone.utc)  # 11:30 PT
    assert pacing.should_post(conn, "C", before)[0] is False
    assert pacing.should_post(conn, "C", late_morning_tick)[0] is True
    assert pacing.should_post(conn, "C", at_cutoff) == (
        False,
        "missed the 11:30 Pacific hard cutoff",
    )
    assert pacing.should_post(conn, "C", READY)[0] is True


def test_force_never_bypasses_one_message_daily_cap() -> None:
    """Force bypasses timing for operators, never the engagement flood limit."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE posts(id INTEGER,channel TEXT,posted_at TEXT);"
        # `state` mirrors the real schema: the daily-cap count excludes reservations
        # that provably never reached Slack, so a stub without it cannot exercise it.
        "CREATE TABLE notification_outbox(id INTEGER,lead_id INTEGER,"
        "audience TEXT,created_at TEXT,state TEXT);"
        "CREATE TABLE proactive_daily_slots(audience TEXT,local_date TEXT,delivery_kind TEXT,delivery_key TEXT,reserved_at TEXT);"
    )
    conn.execute("INSERT INTO posts VALUES (1,'C','2026-07-22T17:00:00+00:00')")
    assert pacing.should_post(conn, "C", READY, force=True) == (
        False,
        "daily cap reached (1)",
    )


def test_dst_conversion_keeps_local_pacific_band() -> None:
    """Winter and summer slots are both 10:xx Pacific / 13:xx Eastern."""
    summer = pacing.daily_slot("C", datetime(2026, 7, 22, 17, tzinfo=timezone.utc))
    winter = pacing.daily_slot("C", datetime(2026, 1, 22, 18, tzinfo=timezone.utc))
    for slot in (summer, winter):
        assert slot.hour == 10 and 0 <= slot.minute <= 45
        assert slot.astimezone(pacing.ET).hour == 13


def test_dst_transition_dates_keep_pacific_and_eastern_slot_alignment() -> None:
    """Spring-forward and fall-back dates preserve the 10 PT / 13 ET band."""
    transition_instants = (
        datetime(2026, 3, 8, 17, tzinfo=timezone.utc),
        datetime(2026, 11, 1, 18, tzinfo=timezone.utc),
    )
    for instant in transition_instants:
        slot = pacing.daily_slot("C", instant)
        assert slot.hour == 10 and slot.astimezone(pacing.ET).hour == 13


def test_rich_and_followup_claim_one_daily_slot_atomically(tmp_path: Path) -> None:
    """Concurrent delivery types cannot both reserve the same channel/PT day."""
    path = tmp_path / "atomic-cap.db"
    conn = db.connect(path)
    conn.close()
    barrier = Barrier(2)

    def claim(kind: str) -> bool:
        """Open an independent worker connection and race one slot insertion."""
        worker = sqlite3.connect(path, timeout=5)
        try:
            barrier.wait()
            return pacing.reserve_daily_slot(
                worker, "CGRANTS", READY, kind, f"{kind}-delivery"
            )
        finally:
            worker.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ("rich_award", "salesforce_followup")))
    check = sqlite3.connect(path)
    try:
        assert sorted(results) == [False, True]
        assert (
            check.execute("SELECT COUNT(*) FROM proactive_daily_slots").fetchone()[0]
            == 1
        )
    finally:
        check.close()


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
    assert conn.execute("SELECT COUNT(*) FROM proactive_daily_slots").fetchone()[0] == 1
    second_client = FakeSlack(conn)
    second = delivery.run(second_client, "CGRANTS", conn, force=True, now=READY)
    assert second.startswith("skip:")
    assert second_client.calls == []


def test_stable_award_key_blocks_delivery_across_snapshot_versions(
    tmp_path: Path,
) -> None:
    """Evidence supersession cannot deliver the same source-qualified award twice."""
    conn = _eligible_conn(tmp_path / "stable-key.db")
    first = db.reserve_notification(
        conn, 1, 2, "CGRANTS", "rich_award", {}, "snap-one", "stable-award"
    )
    second = db.reserve_notification(
        conn, 1, 3, "CGRANTS", "rich_award", {}, "snap-two", "stable-award"
    )
    assert first is not None and second is None


def test_systemic_slack_rejection_blocks_channel_without_consuming_lead(
    tmp_path: Path,
) -> None:
    """Credential/channel failures release the award and prevent cron-loop retries."""
    conn = _eligible_conn(tmp_path / "blocked.db")
    error = SlackApiError("rejected", FakeSlackResponse("invalid_auth"))
    first = delivery.run(FakeSlack(conn, error), "CGRANTS", conn, force=True, now=READY)
    assert first.startswith("blocked:")
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM notification_outbox WHERE lead_id IS NOT NULL"
        ).fetchone()[0]
        == 0
    )
    assert conn.execute("SELECT COUNT(*) FROM proactive_daily_slots").fetchone()[0] == 0
    second = delivery.run(FakeSlack(conn), "CGRANTS", conn, force=True, now=READY)
    assert second.startswith("blocked:")


def test_renderer_failure_quarantines_before_any_slack_call(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A deterministic render defect cannot wedge the top candidate forever."""
    conn = _eligible_conn(tmp_path / "render.db")
    client = FakeSlack(conn)
    monkeypatch.setattr(
        delivery.card,
        "render",
        lambda _snapshot: (_ for _ in ()).throw(ValueError("bad render")),
    )
    outcome = delivery.run(client, "CGRANTS", conn, force=True, now=READY)
    assert outcome.startswith("quarantined:") and client.calls == []
    assert (
        conn.execute("SELECT state FROM notification_outbox").fetchone()[0]
        == "unrenderable"
    )


def test_content_rejection_is_quarantined_not_retried(tmp_path: Path) -> None:
    """Slack-proven invalid Block Kit sets aside the unusable evidence version."""
    conn = _eligible_conn(tmp_path / "content.db")
    error = SlackApiError("rejected", FakeSlackResponse("invalid_blocks"))
    outcome = delivery.run(
        FakeSlack(conn, error), "CGRANTS", conn, force=True, now=READY
    )
    assert outcome.startswith("quarantined:")
    assert (
        conn.execute("SELECT state FROM notification_outbox").fetchone()[0]
        == "rejected"
    )
