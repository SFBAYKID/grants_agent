"""Renewable, fenced ownership for the source-polling writer.

The old lock was a timestamp deleted after two hours. A healthy long poll could
therefore be declared stale while it was still writing, and the resumed old process
had no token that distinguished it from its replacement. This lease uses a monotonic
database token, renews while network reads are in progress, and checks ownership
inside every poll write transaction.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final, Iterator

DEFAULT_LEASE_SECONDS: Final = 300
MIN_LEASE_SECONDS: Final = 60
MAX_LEASE_SECONDS: Final = 3600
DEFAULT_MAX_RUNTIME_SECONDS: Final = 7200
MIN_MAX_RUNTIME_SECONDS: Final = 300
MAX_MAX_RUNTIME_SECONDS: Final = 21600


class LeaseLost(RuntimeError):
    """The caller no longer owns the current, unexpired polling lease."""


@dataclass(frozen=True)
class PollLease:
    """A fencing identity whose token changes on every successful takeover."""

    name: str
    owner: str
    token: int


def lease_seconds() -> int:
    """Return a bounded lease duration from configuration."""
    raw = os.environ.get("GRANT_POLL_LEASE_SECONDS", "").strip()
    try:
        value = int(raw) if raw else DEFAULT_LEASE_SECONDS
    except ValueError:
        value = DEFAULT_LEASE_SECONDS
    return max(MIN_LEASE_SECONDS, min(value, MAX_LEASE_SECONDS))


def max_runtime_seconds() -> int:
    """Return the bounded lifetime after which a poller loses write authority."""
    raw = os.environ.get("GRANT_POLL_MAX_RUNTIME_SECONDS", "").strip()
    try:
        value = int(raw) if raw else DEFAULT_MAX_RUNTIME_SECONDS
    except ValueError:
        value = DEFAULT_MAX_RUNTIME_SECONDS
    return max(MIN_MAX_RUNTIME_SECONDS, min(value, MAX_MAX_RUNTIME_SECONDS))


def _utc(moment: datetime | None = None) -> datetime:
    """Normalize a supplied clock value to aware UTC."""
    value = moment or datetime.now(timezone.utc)
    return value.astimezone(timezone.utc)


def _stamp(moment: datetime) -> str:
    """Serialize one lease clock value without discarding sub-second precision."""
    return moment.isoformat()


def _parse(value: object) -> datetime:
    """Parse a stored lease timestamp as aware UTC."""
    parsed = datetime.fromisoformat(str(value or ""))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def acquire(
    conn: sqlite3.Connection,
    name: str,
    owner: str,
    *,
    now: datetime | None = None,
    duration_seconds: float | None = None,
) -> PollLease | None:
    """Atomically acquire an absent/expired lease and advance its fencing token."""
    if not name.strip() or not owner.strip():
        raise ValueError("poll lease name and owner are required")
    current = _utc(now)
    duration = duration_seconds if duration_seconds is not None else lease_seconds()
    expires = current + timedelta(seconds=duration)
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM poll_locks WHERE name=?", (name,)).fetchone()
        if row is None:
            token = 1
            conn.execute(
                """INSERT INTO poll_locks
                     (name,owner,acquired_at,heartbeat_at,expires_at,fence_token)
                   VALUES (?,?,?,?,?,?)""",
                (name, owner, _stamp(current), _stamp(current), _stamp(expires), token),
            )
        else:
            stored_expiry = str(row["expires_at"] or row["acquired_at"] or "")
            if stored_expiry and _parse(stored_expiry) > current:
                conn.rollback()
                return None
            token = int(row["fence_token"] or 0) + 1
            conn.execute(
                """UPDATE poll_locks
                      SET owner=?,acquired_at=?,heartbeat_at=?,expires_at=?,fence_token=?
                    WHERE name=?""",
                (
                    owner,
                    _stamp(current),
                    _stamp(current),
                    _stamp(expires),
                    token,
                    name,
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return PollLease(name=name, owner=owner, token=token)


def _assert_owned_locked(
    conn: sqlite3.Connection, lease: PollLease, current: datetime
) -> None:
    """Assert ownership while the caller already holds SQLite's write lock."""
    row = conn.execute(
        """SELECT owner,fence_token,expires_at FROM poll_locks WHERE name=?""",
        (lease.name,),
    ).fetchone()
    if (
        row is None
        or str(row["owner"]) != lease.owner
        or int(row["fence_token"] or 0) != lease.token
        or not row["expires_at"]
        or _parse(row["expires_at"]) <= current
    ):
        raise LeaseLost(
            f"poll lease {lease.name!r} token {lease.token} is no longer current"
        )


def heartbeat(
    conn: sqlite3.Connection,
    lease: PollLease,
    *,
    now: datetime | None = None,
    duration_seconds: float | None = None,
) -> None:
    """Renew only the current unexpired token; an expired owner cannot revive itself."""
    current = _utc(now)
    duration = duration_seconds if duration_seconds is not None else lease_seconds()
    expires = current + timedelta(seconds=duration)
    conn.execute("BEGIN IMMEDIATE")
    try:
        _assert_owned_locked(conn, lease, current)
        conn.execute(
            """UPDATE poll_locks SET heartbeat_at=?,expires_at=?
               WHERE name=? AND owner=? AND fence_token=?""",
            (
                _stamp(current),
                _stamp(expires),
                lease.name,
                lease.owner,
                lease.token,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


@contextmanager
def fenced_transaction(
    conn: sqlite3.Connection, lease: PollLease | None
) -> Iterator[None]:
    """Run a write atomically only while ``lease`` remains the current token.

    ``BEGIN IMMEDIATE`` is part of the fence: after ownership is checked, another
    process cannot take over until this short write either commits or rolls back.
    A transaction that outlives its own expiry is rejected at the second check.
    """
    if lease is None:
        try:
            yield
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        _assert_owned_locked(conn, lease, _utc())
        yield
        _assert_owned_locked(conn, lease, _utc())
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def release(conn: sqlite3.Connection, lease: PollLease) -> None:
    """Expire only this token; releasing an obsolete lease is a harmless no-op."""
    current = _utc()
    with conn:
        conn.execute(
            """UPDATE poll_locks SET heartbeat_at=?,expires_at=?
               WHERE name=? AND owner=? AND fence_token=?""",
            (
                _stamp(current),
                _stamp(current),
                lease.name,
                lease.owner,
                lease.token,
            ),
        )


def _database_path(conn: sqlite3.Connection) -> Path | None:
    """Return the main file path, or None for an in-memory test database."""
    row = conn.execute("PRAGMA database_list").fetchone()
    if row is None:
        return None
    raw = str(row[2] or "")
    return Path(raw) if raw else None


class LeaseKeeper:
    """Renew a lease during blocking source HTTP calls and surface thread failure."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        lease: PollLease,
        *,
        duration_seconds: float | None = None,
        maximum_runtime_seconds: float | None = None,
    ) -> None:
        """Capture the durable DB path and bounded heartbeat cadence."""
        self._path = _database_path(conn)
        self._lease = lease
        self._duration = (
            duration_seconds if duration_seconds is not None else lease_seconds()
        )
        self._interval = max(1.0, self._duration / 3.0)
        self._maximum_runtime = (
            maximum_runtime_seconds
            if maximum_runtime_seconds is not None
            else max_runtime_seconds()
        )
        self._started = time.monotonic()
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> LeaseKeeper:
        """Start the heartbeat only for a durable file-backed database."""
        if self._path is not None:
            self._thread = threading.Thread(
                target=self._run,
                name=f"poll-lease-{self._lease.name}",
                daemon=True,
            )
            self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        """Stop promptly; the caller performs the token-scoped release."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=min(10.0, self._interval + 1.0))

    def _run(self) -> None:
        """Renew using an independent SQLite connection while the poller blocks."""
        assert self._path is not None
        while not self._stop.wait(self._interval):
            if time.monotonic() - self._started >= self._maximum_runtime:
                self._lost.set()
                return
            heartbeat_conn = sqlite3.connect(self._path, timeout=10.0)
            heartbeat_conn.row_factory = sqlite3.Row
            heartbeat_conn.execute("PRAGMA busy_timeout=10000")
            try:
                heartbeat(
                    heartbeat_conn,
                    self._lease,
                    duration_seconds=self._duration,
                )
            except (LeaseLost, sqlite3.Error):
                self._lost.set()
                return
            finally:
                heartbeat_conn.close()

    def checkpoint(self, conn: sqlite3.Connection) -> None:
        """Synchronously renew and fail before work if the background lease was lost."""
        if time.monotonic() - self._started >= self._maximum_runtime:
            self._lost.set()
            raise LeaseLost("poll exceeded its configured maximum runtime")
        if self._lost.is_set():
            raise LeaseLost("poll lease heartbeat failed or ownership changed")
        heartbeat(conn, self._lease, duration_seconds=self._duration)
