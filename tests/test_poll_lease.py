"""Polling lease renewal, takeover, and write-fencing behavior."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from grant_watch import db, poll_lease

NOW = datetime.now(timezone.utc)


def test_live_lease_is_exclusive_and_heartbeat_extends_it(tmp_path: Path) -> None:
    """A healthy owner stays exclusive beyond its original expiry after renewal."""
    path = tmp_path / "lease.db"
    first = db.connect(path)
    second = db.connect(path)
    lease = poll_lease.acquire(first, "poll", "worker-a", now=NOW, duration_seconds=60)
    assert lease is not None
    assert (
        poll_lease.acquire(
            second,
            "poll",
            "worker-b",
            now=NOW + timedelta(seconds=30),
            duration_seconds=60,
        )
        is None
    )

    poll_lease.heartbeat(
        first,
        lease,
        now=NOW + timedelta(seconds=50),
        duration_seconds=60,
    )
    assert (
        poll_lease.acquire(
            second,
            "poll",
            "worker-b",
            now=NOW + timedelta(seconds=90),
            duration_seconds=60,
        )
        is None
    )
    first.close()
    second.close()


def test_takeover_advances_token_and_fences_the_old_writer(tmp_path: Path) -> None:
    """A resumed stale process cannot commit after a replacement takes ownership."""
    path = tmp_path / "takeover.db"
    stale_conn = db.connect(path)
    current_conn = db.connect(path)
    stale = poll_lease.acquire(
        stale_conn, "poll", "worker-a", now=NOW, duration_seconds=60
    )
    assert stale is not None
    current = poll_lease.acquire(
        current_conn,
        "poll",
        "worker-b",
        now=NOW + timedelta(seconds=61),
        duration_seconds=60,
    )
    assert current is not None and current.token == stale.token + 1

    with pytest.raises(poll_lease.LeaseLost):
        with poll_lease.fenced_transaction(stale_conn, stale):
            stale_conn.execute(
                "INSERT INTO runs(started,source,state) VALUES (?,?,?)",
                (NOW.isoformat(), "stale", "pending"),
            )
    assert (
        current_conn.execute(
            "SELECT COUNT(*) FROM runs WHERE source='stale'"
        ).fetchone()[0]
        == 0
    )

    with poll_lease.fenced_transaction(current_conn, current):
        current_conn.execute(
            "INSERT INTO runs(started,source,state) VALUES (?,?,?)",
            (NOW.isoformat(), "current", "pending"),
        )
    assert (
        current_conn.execute(
            "SELECT COUNT(*) FROM runs WHERE source='current'"
        ).fetchone()[0]
        == 1
    )
    stale_conn.close()
    current_conn.close()


def test_releasing_an_old_token_cannot_unlock_its_successor(tmp_path: Path) -> None:
    """Cleanup from a stale process is token-scoped and harmless."""
    path = tmp_path / "release.db"
    first = db.connect(path)
    second = db.connect(path)
    stale = poll_lease.acquire(first, "poll", "worker-a", now=NOW, duration_seconds=60)
    assert stale is not None
    current = poll_lease.acquire(
        second,
        "poll",
        "worker-b",
        now=NOW + timedelta(seconds=61),
        duration_seconds=60,
    )
    assert current is not None

    poll_lease.release(first, stale)
    row = second.execute(
        "SELECT owner,fence_token FROM poll_locks WHERE name='poll'"
    ).fetchone()
    assert (row["owner"], row["fence_token"]) == ("worker-b", current.token)
    first.close()
    second.close()


@pytest.mark.parametrize(
    ("configured", "expected"),
    [("bad", 300), ("1", 60), ("99999", 3600), ("120", 120)],
)
def test_lease_configuration_is_bounded(
    configured: str, expected: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Malformed or extreme environment values cannot disable lease safety."""
    monkeypatch.setenv("GRANT_POLL_LEASE_SECONDS", configured)
    assert poll_lease.lease_seconds() == expected


def test_keeper_stops_authorizing_writes_after_the_maximum_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A living heartbeat cannot legitimize an endlessly hung poller."""
    conn = db.connect(tmp_path / "max-runtime.db")
    lease = poll_lease.acquire(conn, "poll", "worker", duration_seconds=60)
    assert lease is not None
    clock = [100.0]
    monkeypatch.setattr(poll_lease.time, "monotonic", lambda: clock[0])
    keeper = poll_lease.LeaseKeeper(
        conn,
        lease,
        duration_seconds=60,
        maximum_runtime_seconds=10,
    )
    clock[0] = 111.0

    with pytest.raises(poll_lease.LeaseLost, match="maximum runtime"):
        keeper.checkpoint(conn)


@pytest.mark.parametrize(
    ("configured", "expected"),
    [("bad", 7200), ("1", 300), ("99999", 21600), ("900", 900)],
)
def test_maximum_runtime_configuration_is_bounded(
    configured: str, expected: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed runtime ceiling cannot make lease renewal indefinite."""
    monkeypatch.setenv("GRANT_POLL_MAX_RUNTIME_SECONDS", configured)
    assert poll_lease.max_runtime_seconds() == expected
