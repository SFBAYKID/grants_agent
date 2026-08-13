"""Atomic capability activation through authored one-shot announcements."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest
from slack_sdk.errors import SlackApiError

from grant_watch import announce, capability_asks, db

CHANNEL = "C0ANNOUNCE"
USER = "U0ANNOUNCE"


def _connect(path: Path) -> sqlite3.Connection:
    """Open one fully migrated test database."""
    return db.connect(path)


def _document(path: Path, announcements: list[dict[str, object]]) -> Path:
    """Write a reviewed announcement fixture to a test-owned path."""
    path.write_text(json.dumps({"announcements": announcements}))
    return path


def _announcement(
    path: Path, capabilities: list[str], *, slug: str = "release"
) -> Path:
    """Write one ordinary authored announcement."""
    return _document(
        path,
        [
            {
                "slug": slug,
                "audience": CHANNEL,
                "body": "Morning all — these reviewed capabilities are live.",
                "capabilities": capabilities,
            }
        ],
    )


def _ask(conn: sqlite3.Connection, capability: str, suffix: str) -> None:
    """Record one waiting user ask for a capability."""
    inserted = capability_asks.record(
        conn,
        slack_user=USER,
        audience=CHANNEL,
        thread_ts=f"1700000000.{suffix}",
        message_ts=f"1700000001.{suffix}",
        ask_text=f"Can you do {capability}?",
        capability=capability,
        asked_at="2026-08-01T12:00:00+00:00",
        recorded_by="test",
    )
    assert inserted is not None


class RecordingClient:
    """Thread-safe Slack double that can fail after inspecting committed state."""

    def __init__(
        self,
        *,
        failure: str = "",
        before_send: object | None = None,
    ) -> None:
        """Configure optional failure behavior and an empty synchronized call log."""
        self.failure = failure
        self.before_send = before_send
        self.calls: list[dict[str, object]] = []
        self._lock = threading.Lock()

    def chat_postMessage(self, **kwargs: object) -> dict[str, object]:
        """Record one attempted Slack post, optionally raising a configured failure."""
        with self._lock:
            self.calls.append(dict(kwargs))
        if callable(self.before_send):
            self.before_send()
        if self.failure == "slack":
            raise SlackApiError("rejected", {"error": "channel_not_found"})
        if self.failure == "generic":
            raise RuntimeError("socket outcome unknown")
        return {"ok": True, "ts": "9.1"}


def test_execution_atomically_activates_every_declared_capability(
    tmp_path: Path,
) -> None:
    """The announcement reservation and every waiting ask share one timestamp."""
    conn = _connect(tmp_path / "leads.db")
    _ask(conn, "email_results", "1")
    _ask(conn, "reminders", "2")
    announce.load(
        conn,
        _announcement(
            tmp_path / "announcement.json",
            ["email_results", "email_results", "reminders"],
        ),
    )
    pending = announce.pending(conn)
    assert pending is not None
    assert pending.capabilities == ("email_results", "reminders")

    def assert_committed_before_slack() -> None:
        """Observe the durable declaration from inside the outbound Slack call."""
        row = conn.execute(
            "SELECT posted_at FROM announcements WHERE slug='release'"
        ).fetchone()
        asks = conn.execute(
            "SELECT available_since FROM capability_asks ORDER BY capability"
        ).fetchall()
        assert row["posted_at"]
        assert {ask["available_since"] for ask in asks} == {row["posted_at"]}

    client = RecordingClient(before_send=assert_committed_before_slack)
    assert announce.run(client, conn, dry_run=False).startswith("announced ")
    first = conn.execute(
        "SELECT posted_at,slack_ts FROM announcements WHERE slug='release'"
    ).fetchone()
    assert first["slack_ts"] == "9.1"

    assert announce.run(client, conn, dry_run=False) == "skip: nothing to announce"
    repeated = conn.execute(
        "SELECT posted_at,slack_ts FROM announcements WHERE slug='release'"
    ).fetchone()
    assert tuple(repeated) == tuple(first)
    assert len(client.calls) == 1
    conn.close()


def test_dry_run_changes_neither_announcement_nor_asks(tmp_path: Path) -> None:
    """A preview may validate content but cannot consume or activate it."""
    conn = _connect(tmp_path / "leads.db")
    _ask(conn, "email_results", "1")
    announce.load(
        conn,
        _announcement(tmp_path / "announcement.json", ["email_results"]),
    )
    client = RecordingClient()

    assert announce.run(client, conn, dry_run=True).startswith("[dry-run]")
    row = conn.execute(
        """SELECT a.posted_at,c.available_since
             FROM announcements a CROSS JOIN capability_asks c"""
    ).fetchone()
    assert tuple(row) == (None, None)
    assert client.calls == []
    conn.close()


@pytest.mark.parametrize("bad_capability", ["Not Valid", "future_capability"])
def test_load_rejects_one_bad_capability_without_partial_mutation(
    tmp_path: Path, bad_capability: str
) -> None:
    """A later malformed or unworded item rolls back the whole authored document."""
    conn = _connect(tmp_path / "leads.db")
    _ask(conn, "email_results", "1")
    path = _document(
        tmp_path / "announcements.json",
        [
            {
                "slug": "valid-first",
                "audience": CHANNEL,
                "body": "This entry is valid.",
                "capabilities": ["email_results"],
            },
            {
                "slug": "bad-second",
                "audience": CHANNEL,
                "body": "This entry must invalidate the document.",
                "capabilities": [bad_capability],
            },
        ],
    )

    with pytest.raises(ValueError):
        announce.load(conn, path)

    assert conn.execute("SELECT COUNT(*) FROM announcements").fetchone()[0] == 0
    assert (
        conn.execute("SELECT available_since FROM capability_asks").fetchone()[0]
        is None
    )
    conn.close()


def test_run_revalidates_stored_capabilities_before_mutation(tmp_path: Path) -> None:
    """A legacy or manually corrupted row cannot bypass authored-file validation."""
    conn = _connect(tmp_path / "leads.db")
    _ask(conn, "email_results", "1")
    conn.execute(
        """INSERT INTO announcements
             (slug,audience,body,capabilities,created_at)
           VALUES ('tampered',?,?,?,?)""",
        (
            CHANNEL,
            "This row bypassed the loader.",
            "email_results,future_capability",
            "2026-08-13T12:00:00+00:00",
        ),
    )
    conn.commit()
    client = RecordingClient()

    with pytest.raises(ValueError, match="hand-written"):
        announce.run(client, conn, dry_run=False)

    row = conn.execute(
        """SELECT a.posted_at,c.available_since
             FROM announcements a CROSS JOIN capability_asks c"""
    ).fetchone()
    assert tuple(row) == (None, None)
    assert client.calls == []
    conn.close()


@pytest.mark.parametrize(
    ("failure", "expected"),
    [("slack", "announce failed"), ("generic", "announce ambiguous")],
)
def test_slack_failure_preserves_reservation_and_capability_eligibility(
    tmp_path: Path, failure: str, expected: str
) -> None:
    """A failed or ambiguous post is never retried after the declaration commits."""
    conn = _connect(tmp_path / "leads.db")
    _ask(conn, "email_results", "1")
    announce.load(
        conn,
        _announcement(tmp_path / "announcement.json", ["email_results"]),
    )
    client = RecordingClient(failure=failure)

    assert announce.run(client, conn, dry_run=False).startswith(expected)
    row = conn.execute(
        """SELECT a.posted_at,a.slack_ts,c.available_since
             FROM announcements a CROSS JOIN capability_asks c"""
    ).fetchone()
    assert row["posted_at"]
    assert row["available_since"] == row["posted_at"]
    assert not row["slack_ts"]
    assert announce.run(client, conn, dry_run=False) == "skip: nothing to announce"
    assert len(client.calls) == 1
    conn.close()


def test_second_capability_database_failure_rolls_back_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial eligibility update cannot escape if a later update fails."""
    conn = _connect(tmp_path / "leads.db")
    _ask(conn, "email_results", "1")
    _ask(conn, "reminders", "2")
    announce.load(
        conn,
        _announcement(tmp_path / "announcement.json", ["email_results", "reminders"]),
    )
    original = capability_asks.mark_available_in_transaction
    calls = 0

    def fail_second(
        database: sqlite3.Connection, capability: str, *, shipped_at: str
    ) -> int:
        """Apply the first update, then simulate a definite database failure."""
        nonlocal calls
        calls += 1
        if calls == 2:
            raise sqlite3.OperationalError("injected second update failure")
        return original(database, capability, shipped_at=shipped_at)

    monkeypatch.setattr(capability_asks, "mark_available_in_transaction", fail_second)
    client = RecordingClient()

    with pytest.raises(sqlite3.OperationalError, match="injected"):
        announce.run(client, conn, dry_run=False)

    assert conn.execute("SELECT posted_at FROM announcements").fetchone()[0] is None
    assert {
        row["available_since"]
        for row in conn.execute("SELECT available_since FROM capability_asks")
    } == {None}
    assert client.calls == []
    conn.close()


def test_concurrent_workers_have_one_reservation_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two workers that read the same pending row still produce one Slack call."""
    database = tmp_path / "leads.db"
    seed = _connect(database)
    announce.load(
        seed,
        _announcement(tmp_path / "announcement.json", ["email_results"]),
    )
    seed.close()
    original_pending = announce.pending
    both_read = threading.Barrier(2)

    def synchronized_pending(conn: sqlite3.Connection) -> announce.Announcement | None:
        """Make both workers hold the same pre-reservation snapshot."""
        item = original_pending(conn)
        both_read.wait(timeout=5)
        return item

    monkeypatch.setattr(announce, "pending", synchronized_pending)
    client = RecordingClient()
    results: list[str] = []
    failures: list[BaseException] = []
    result_lock = threading.Lock()

    def worker() -> None:
        """Execute one independent worker connection."""
        conn = _connect(database)
        try:
            outcome = announce.run(client, conn, dry_run=False)
            with result_lock:
                results.append(outcome)
        except BaseException as exc:  # pragma: no cover - asserted below
            with result_lock:
                failures.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert failures == []
    assert len(client.calls) == 1
    assert sum(outcome.startswith("announced ") for outcome in results) == 1
    assert sum("already reserved" in outcome for outcome in results) == 1


def test_empty_capability_announcement_still_posts(tmp_path: Path) -> None:
    """An ordinary authored update need not activate any personal follow-up."""
    conn = _connect(tmp_path / "leads.db")
    announce.load(conn, _announcement(tmp_path / "announcement.json", []))
    client = RecordingClient()

    assert announce.run(client, conn, dry_run=False).startswith("announced ")
    assert len(client.calls) == 1
    conn.close()


def test_missing_slack_client_is_mutation_free(tmp_path: Path) -> None:
    """A known preflight failure cannot consume the update or activate an ask."""
    conn = _connect(tmp_path / "leads.db")
    _ask(conn, "email_results", "1")
    announce.load(
        conn,
        _announcement(tmp_path / "announcement.json", ["email_results"]),
    )

    assert announce.run(None, conn, dry_run=False) == "skip: no Slack client configured"
    row = conn.execute(
        """SELECT a.posted_at,c.available_since
             FROM announcements a CROSS JOIN capability_asks c"""
    ).fetchone()
    assert tuple(row) == (None, None)
    conn.close()
