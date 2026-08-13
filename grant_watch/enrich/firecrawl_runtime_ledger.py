"""Private standalone ledger used by every operational Firecrawl caller.

The application database is lead/workflow state, not vendor-account authority.  This
module opens only an explicitly initialized, owner-only SQLite file whose metadata is
bound to the host capability and Firecrawl account scope.
"""

from __future__ import annotations

import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..paid_provider_authority import ProviderBinding, load_binding

LEDGER_PATH_ENV = "FIRECRAWL_RUNTIME_LEDGER_PATH"
LEDGER_SCHEMA_VERSION = 2


class FirecrawlLedgerError(RuntimeError):
    """The standalone Firecrawl authority ledger is absent or invalid."""


def _now() -> str:
    """Return one aware UTC audit timestamp."""
    return datetime.now(timezone.utc).isoformat()


def configured_path() -> Path:
    """Return the explicit absolute runtime ledger path without creating it."""
    raw = os.environ.get(LEDGER_PATH_ENV, "").strip()
    if not raw:
        raise FirecrawlLedgerError(f"{LEDGER_PATH_ENV} is not configured")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise FirecrawlLedgerError(f"{LEDGER_PATH_ENV} must be absolute")
    return path


def _initialize(conn: sqlite3.Connection, binding: ProviderBinding) -> None:
    """Create the complete versioned ledger and authority binding."""
    if binding.provider != "firecrawl":
        raise FirecrawlLedgerError(
            "Firecrawl ledger received the wrong provider binding"
        )
    conn.executescript(
        """
        CREATE TABLE firecrawl_runtime_ledger_schema (
          version INTEGER PRIMARY KEY,
          applied_at TIMESTAMP NOT NULL
        );
        CREATE TABLE firecrawl_runtime_ledger_metadata (
          singleton INTEGER PRIMARY KEY CHECK(singleton=1),
          provider TEXT NOT NULL CHECK(provider='firecrawl'),
          authority_id TEXT NOT NULL,
          account_scope_id TEXT NOT NULL,
          reconciliation_required INTEGER NOT NULL DEFAULT 0
            CHECK(reconciliation_required IN (0,1)),
          created_at TIMESTAMP NOT NULL
        );
        CREATE TABLE firecrawl_runtime_periods (
          billing_period TEXT PRIMARY KEY,
          call_limit INTEGER NOT NULL CHECK(call_limit > 0),
          reserved_calls INTEGER NOT NULL DEFAULT 0 CHECK(reserved_calls >= 0),
          created_at TIMESTAMP NOT NULL,
          updated_at TIMESTAMP NOT NULL,
          CHECK(reserved_calls <= call_limit)
        );
        CREATE TABLE firecrawl_runtime_attempts (
          id TEXT PRIMARY KEY,
          request_key TEXT NOT NULL UNIQUE,
          workflow TEXT NOT NULL,
          operation TEXT NOT NULL CHECK(operation IN ('search','scrape')),
          billing_period TEXT NOT NULL
            REFERENCES firecrawl_runtime_periods(billing_period),
          state TEXT NOT NULL CHECK(state IN
            ('in_flight','completed','failed','indeterminate','rate_limited')),
          started_at TIMESTAMP NOT NULL,
          finished_at TIMESTAMP,
          http_status INTEGER,
          retry_after_seconds REAL,
          error_code TEXT,
          request_hash TEXT,
          attempt_number INTEGER NOT NULL DEFAULT 1 CHECK(attempt_number > 0)
        );
        CREATE INDEX ix_firecrawl_runtime_period_state
          ON firecrawl_runtime_attempts(billing_period,state,started_at);
        CREATE INDEX ix_firecrawl_runtime_request_attempt
          ON firecrawl_runtime_attempts(
            billing_period,request_hash,started_at DESC
          ) WHERE request_hash IS NOT NULL;
        CREATE TABLE firecrawl_runtime_provider_state (
          provider TEXT PRIMARY KEY CHECK(provider='firecrawl'),
          blocked_until TIMESTAMP,
          reason TEXT,
          updated_at TIMESTAMP NOT NULL
        );
        CREATE TABLE firecrawl_runtime_rate_state (
          singleton INTEGER PRIMARY KEY CHECK(singleton=1),
          next_call_at TIMESTAMP NOT NULL,
          updated_at TIMESTAMP NOT NULL
        );
        """
    )
    at = _now()
    conn.execute(
        "INSERT INTO firecrawl_runtime_ledger_schema VALUES (?,?)",
        (LEDGER_SCHEMA_VERSION, at),
    )
    conn.execute(
        """INSERT INTO firecrawl_runtime_ledger_metadata
             (singleton,provider,authority_id,account_scope_id,created_at)
           VALUES (1,'firecrawl',?,?,?)""",
        (binding.authority_id, binding.account_scope_id, at),
    )
    conn.commit()


def _private_file(path: Path) -> None:
    """Require a non-symlinked owner-only regular ledger file."""
    if path.is_symlink() or not path.is_file():
        raise FirecrawlLedgerError(
            "Firecrawl runtime ledger does not exist; run the reviewed migration"
        )
    stat = path.stat()
    if stat.st_uid != os.geteuid() or stat.st_mode & 0o077:
        raise FirecrawlLedgerError(
            "Firecrawl runtime ledger must be owned by this tenant and mode 0600"
        )


def _validate(
    conn: sqlite3.Connection,
    binding: ProviderBinding,
    *,
    allow_reconciliation: bool = False,
) -> None:
    """Require exact schema, provider, authority, and account-scope metadata."""
    try:
        version = conn.execute(
            "SELECT MAX(version) FROM firecrawl_runtime_ledger_schema"
        ).fetchone()
        metadata = conn.execute(
            """SELECT provider,authority_id,account_scope_id,reconciliation_required
                 FROM firecrawl_runtime_ledger_metadata WHERE singleton=1"""
        ).fetchone()
    except sqlite3.DatabaseError as exc:
        raise FirecrawlLedgerError(
            "Firecrawl runtime ledger is uninitialized or malformed"
        ) from exc
    if version is None or version[0] != LEDGER_SCHEMA_VERSION or metadata is None:
        raise FirecrawlLedgerError(
            "Firecrawl runtime ledger schema is missing or unsupported"
        )
    if tuple(metadata[:3]) != (
        "firecrawl",
        binding.authority_id,
        binding.account_scope_id,
    ):
        raise FirecrawlLedgerError(
            "Firecrawl runtime ledger does not match this host/account authority"
        )
    if not allow_reconciliation and int(metadata[3]) != 0:
        raise FirecrawlLedgerError(
            "legacy Firecrawl attempts require account-wide operator reconciliation"
        )


def _enable_wal_for_publication(conn: sqlite3.Connection) -> None:
    """Persist WAL mode after all build writes, with no unpublished sidecar state."""
    row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
    if row is None or str(row[0]).lower() != "wal":
        raise FirecrawlLedgerError("could not enable Firecrawl ledger WAL mode")


def connect_existing(
    path: Path, binding: ProviderBinding, *, allow_reconciliation: bool = False
) -> sqlite3.Connection:
    """Open one exact private ledger, optionally for migration inspection."""
    _private_file(path)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        _validate(conn, binding, allow_reconciliation=allow_reconciliation)
        deadline = time.monotonic() + 10.0
        while True:
            journal = conn.execute("PRAGMA journal_mode").fetchone()
            if journal is not None and str(journal[0]).lower() == "wal":
                break
            try:
                _enable_wal_for_publication(conn)
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
    except Exception:
        conn.close()
        raise
    return conn


def connect_runtime(binding: ProviderBinding) -> sqlite3.Connection:
    """Open the configured standalone ledger for one provider call."""
    return connect_existing(configured_path(), binding)


def _fsync_path(path: Path) -> None:
    """Flush one published file and its containing directory to durable storage."""
    file_descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(file_descriptor)
    finally:
        os.close(file_descriptor)
    directory_descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def publish_without_replacement(temp_path: Path, destination: Path) -> None:
    """Atomically publish a sibling ledger and refuse a concurrent target."""
    try:
        os.link(temp_path, destination, follow_symlinks=False)
    except FileExistsError as exc:
        raise FirecrawlLedgerError(
            "Firecrawl ledger destination appeared; refusing to replace it"
        ) from exc
    _fsync_path(destination)


def initialize_empty_ledger(path: Path, binding: ProviderBinding | None = None) -> None:
    """Explicitly create a private zero-history ledger for a proven new account."""
    destination = path.expanduser()
    if not destination.is_absolute():
        raise FirecrawlLedgerError("Firecrawl ledger destination must be absolute")
    if destination.exists() or destination.is_symlink():
        raise FirecrawlLedgerError("refusing to replace an existing Firecrawl ledger")
    selected = binding or load_binding("firecrawl")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp_path = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        conn = sqlite3.connect(temp_path, timeout=10.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            _initialize(conn, selected)
            _enable_wal_for_publication(conn)
        finally:
            conn.close()
        temp_path.chmod(0o600)
        publish_without_replacement(temp_path, destination)
    finally:
        if temp_path.exists():
            temp_path.unlink()
