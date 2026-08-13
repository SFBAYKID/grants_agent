"""Preview or merge legacy app-DB Firecrawl state into one authority ledger.

All execution sources are write-locked in deterministic path order and remain locked
through exact destination verification and atomic publication.  Preview is the
default and never creates a file.  Unknown legacy request hashes are preserved and
open an account-wide reconciliation circuit in the destination.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Sequence

from .enrich import firecrawl_runtime_ledger as ledger_runtime
from .paid_provider_authority import (
    PaidProviderAuthorityError,
    ProviderBinding,
    load_binding,
)

_PERIOD_COLUMNS = (
    "billing_period",
    "call_limit",
    "reserved_calls",
    "created_at",
    "updated_at",
)
_ATTEMPT_COLUMNS = (
    "id",
    "request_key",
    "workflow",
    "operation",
    "billing_period",
    "state",
    "started_at",
    "finished_at",
    "http_status",
    "retry_after_seconds",
    "error_code",
    "request_hash",
    "attempt_number",
)
_PROVIDER_COLUMNS = ("provider", "blocked_until", "reason", "updated_at")
_RATE_COLUMNS = ("singleton", "next_call_at", "updated_at")
_ATTEMPT_STATES = frozenset(
    {"in_flight", "completed", "failed", "indeterminate", "rate_limited"}
)
_HASH_RE = re.compile(r"[0-9a-f]{64}")


class FirecrawlLedgerMigrationError(RuntimeError):
    """Legacy Firecrawl history cannot be merged without weakening its guard."""


@dataclass(frozen=True)
class LedgerSnapshot:
    """Canonical Firecrawl state independent of an application's schema version."""

    periods: tuple[tuple[object, ...], ...]
    attempts: tuple[tuple[object, ...], ...]
    provider_state: tuple[tuple[object, ...], ...]
    rate_state: tuple[tuple[object, ...], ...] = ()
    reconciliation_required: bool = False


@dataclass(frozen=True)
class MigrationSummary:
    """Non-sensitive totals proving what the merge inspected and retained."""

    sources: int
    periods: int
    attempts: int
    reserved_calls: int
    reconciliation_required: bool
    already_migrated: bool = False


def _tables(conn: sqlite3.Connection) -> set[str]:
    """Return the source's concrete table names."""
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return one table's column names."""
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _snapshot(conn: sqlite3.Connection) -> LedgerSnapshot:
    """Read either schema-42 app tables or the current standalone table shape."""
    required = {
        "firecrawl_runtime_periods",
        "firecrawl_runtime_attempts",
        "firecrawl_runtime_provider_state",
    }
    present = required & _tables(conn)
    if not present:
        return LedgerSnapshot((), (), ())
    if present != required:
        raise FirecrawlLedgerMigrationError(
            "legacy Firecrawl tables are partial; refusing to infer missing state"
        )
    attempt_columns = _columns(conn, "firecrawl_runtime_attempts")
    request_hash = "request_hash" if "request_hash" in attempt_columns else "NULL"
    attempt_number = "attempt_number" if "attempt_number" in attempt_columns else "1"
    try:
        periods = tuple(
            tuple(row)
            for row in conn.execute(
                f"SELECT {','.join(_PERIOD_COLUMNS)} "
                "FROM firecrawl_runtime_periods ORDER BY billing_period"
            )
        )
        attempts = tuple(
            tuple(row)
            for row in conn.execute(
                "SELECT id,request_key,workflow,operation,billing_period,state,"
                "started_at,finished_at,http_status,retry_after_seconds,error_code,"
                f"{request_hash},{attempt_number} "
                "FROM firecrawl_runtime_attempts ORDER BY id"
            )
        )
        provider_state = tuple(
            tuple(row)
            for row in conn.execute(
                f"SELECT {','.join(_PROVIDER_COLUMNS)} "
                "FROM firecrawl_runtime_provider_state ORDER BY provider"
            )
        )
    except sqlite3.DatabaseError as exc:
        raise FirecrawlLedgerMigrationError(
            "legacy Firecrawl tables are unreadable or unknown"
        ) from exc
    rate_state = (
        tuple(
            tuple(row)
            for row in conn.execute(
                f"SELECT {','.join(_RATE_COLUMNS)} "
                "FROM firecrawl_runtime_rate_state ORDER BY singleton"
            )
        )
        if "firecrawl_runtime_rate_state" in _tables(conn)
        else ()
    )
    reconciliation = any(
        row[11] is None and str(row[5]) in {"in_flight", "indeterminate"}
        for row in attempts
    )
    return LedgerSnapshot(
        periods,
        attempts,
        provider_state,
        rate_state,
        reconciliation_required=reconciliation,
    )


def _timestamp(value: object, label: str) -> datetime:
    """Parse a required ISO timestamp for deterministic comparisons."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise FirecrawlLedgerMigrationError(f"invalid {label} timestamp") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _validate(snapshot: LedgerSnapshot) -> None:
    """Prove counters, attempts, and provider state reconcile within one source."""
    period_map = {str(row[0]): row for row in snapshot.periods}
    if len(period_map) != len(snapshot.periods):
        raise FirecrawlLedgerMigrationError("duplicate Firecrawl billing period")
    counts = {period: 0 for period in period_map}
    ids: set[str] = set()
    request_keys: set[str] = set()
    for row in snapshot.attempts:
        attempt_id, request_key = str(row[0]), str(row[1])
        period, state = str(row[4]), str(row[5])
        if (
            not attempt_id
            or attempt_id in ids
            or not request_key
            or request_key in request_keys
        ):
            raise FirecrawlLedgerMigrationError(
                "duplicate/empty Firecrawl attempt identity"
            )
        ids.add(attempt_id)
        request_keys.add(request_key)
        if str(row[3]) not in {"search", "scrape"} or state not in _ATTEMPT_STATES:
            raise FirecrawlLedgerMigrationError("unknown Firecrawl operation or state")
        if period not in period_map or int(row[12]) <= 0:
            raise FirecrawlLedgerMigrationError(
                "Firecrawl attempt references an absent period or invalid attempt number"
            )
        if row[11] is not None and _HASH_RE.fullmatch(str(row[11])) is None:
            raise FirecrawlLedgerMigrationError("Firecrawl request hash is malformed")
        _timestamp(row[6], "attempt")
        counts[period] += 1
    for period, row in period_map.items():
        limit, reserved = int(row[1]), int(row[2])
        if limit <= 0 or reserved < 0 or reserved > limit or reserved != counts[period]:
            raise FirecrawlLedgerMigrationError(
                f"Firecrawl period {period} does not reconcile to its attempts"
            )
        _timestamp(row[3], "period creation")
        _timestamp(row[4], "period update")
    if len(snapshot.provider_state) > 1:
        raise FirecrawlLedgerMigrationError("multiple Firecrawl provider-state rows")
    for row in snapshot.provider_state:
        if str(row[0]) != "firecrawl":
            raise FirecrawlLedgerMigrationError("unknown Firecrawl provider-state row")
        if row[1]:
            _timestamp(row[1], "provider backoff")
        _timestamp(row[3], "provider update")
    if len(snapshot.rate_state) > 1:
        raise FirecrawlLedgerMigrationError("multiple Firecrawl rate-state rows")
    for row in snapshot.rate_state:
        if int(row[0]) != 1:
            raise FirecrawlLedgerMigrationError("unknown Firecrawl rate-state row")
        _timestamp(row[1], "next-call")
        _timestamp(row[2], "rate-state update")


def _merge(
    snapshots: Sequence[LedgerSnapshot], approved_limit: int | None
) -> LedgerSnapshot:
    """Merge actual calls while never adding independent configured ceilings."""
    for snapshot in snapshots:
        _validate(snapshot)
    attempts_by_id: dict[str, tuple[object, ...]] = {}
    request_to_id: dict[str, str] = {}
    for snapshot in snapshots:
        for row in snapshot.attempts:
            attempt_id, request_key = str(row[0]), str(row[1])
            prior = attempts_by_id.get(attempt_id)
            if prior is not None and prior != row:
                raise FirecrawlLedgerMigrationError(
                    "same Firecrawl attempt id has conflicting history"
                )
            prior_id = request_to_id.get(request_key)
            if prior_id is not None and prior_id != attempt_id:
                raise FirecrawlLedgerMigrationError(
                    "same Firecrawl request key has conflicting attempts"
                )
            attempts_by_id[attempt_id] = row
            request_to_id[request_key] = attempt_id
    attempts = tuple(sorted(attempts_by_id.values(), key=lambda row: str(row[0])))

    period_rows: dict[str, list[tuple[object, ...]]] = {}
    for snapshot in snapshots:
        for row in snapshot.periods:
            period_rows.setdefault(str(row[0]), []).append(row)
    attempts_per_period: dict[str, int] = {}
    for row in attempts:
        period = str(row[4])
        attempts_per_period[period] = attempts_per_period.get(period, 0) + 1
    periods: list[tuple[object, ...]] = []
    for period, rows in sorted(period_rows.items()):
        limits = {int(row[1]) for row in rows}
        if len(limits) > 1 and approved_limit is None:
            raise FirecrawlLedgerMigrationError(
                f"Firecrawl period {period} has conflicting ceilings; "
                "an explicit reviewed limit is required"
            )
        selected_limit = (
            approved_limit if approved_limit is not None else next(iter(limits))
        )
        if selected_limit is None or selected_limit <= 0:
            raise FirecrawlLedgerMigrationError(
                "approved Firecrawl limit must be positive"
            )
        reserved = attempts_per_period.get(period, 0)
        if reserved > selected_limit:
            raise FirecrawlLedgerMigrationError(
                f"merged Firecrawl period {period} consumes {reserved} calls, "
                f"above the reviewed ceiling {selected_limit}"
            )
        periods.append(
            (
                period,
                selected_limit,
                reserved,
                min(rows, key=lambda row: _timestamp(row[3], "period creation"))[3],
                max(rows, key=lambda row: _timestamp(row[4], "period update"))[4],
            )
        )

    provider_rows = [row for snapshot in snapshots for row in snapshot.provider_state]
    provider_state: tuple[tuple[object, ...], ...] = ()
    if provider_rows:
        latest = max(
            provider_rows,
            key=lambda row: (
                _timestamp(row[1], "provider backoff")
                if row[1]
                else datetime.min.replace(tzinfo=timezone.utc),
                _timestamp(row[3], "provider update"),
            ),
        )
        provider_state = (latest,)
    rate_rows = [row for snapshot in snapshots for row in snapshot.rate_state]
    rate_state: tuple[tuple[object, ...], ...] = ()
    if rate_rows:
        latest_rate = max(
            rate_rows,
            key=lambda row: (
                _timestamp(row[1], "next-call"),
                _timestamp(row[2], "rate-state update"),
            ),
        )
        rate_state = (latest_rate,)
    return LedgerSnapshot(
        tuple(periods),
        attempts,
        provider_state,
        rate_state,
        any(snapshot.reconciliation_required for snapshot in snapshots),
    )


def _summary(snapshot: LedgerSnapshot, sources: int) -> MigrationSummary:
    """Return safe aggregate totals for one validated merged snapshot."""
    return MigrationSummary(
        sources=sources,
        periods=len(snapshot.periods),
        attempts=len(snapshot.attempts),
        reserved_calls=sum(int(row[2]) for row in snapshot.periods),
        reconciliation_required=snapshot.reconciliation_required,
    )


def inspect_legacy_ledgers(
    sources: Sequence[sqlite3.Connection], *, approved_limit: int | None = None
) -> MigrationSummary:
    """Read-only merge validation for every explicitly supplied legacy source."""
    if not sources:
        raise FirecrawlLedgerMigrationError("at least one legacy source is required")
    merged = _merge(tuple(_snapshot(source) for source in sources), approved_limit)
    return _summary(merged, len(sources))


def _copy(conn: sqlite3.Connection, snapshot: LedgerSnapshot) -> None:
    """Insert one complete merged snapshot into an initialized empty destination."""
    conn.executemany(
        f"INSERT INTO firecrawl_runtime_periods ({','.join(_PERIOD_COLUMNS)}) "
        f"VALUES ({','.join('?' for _ in _PERIOD_COLUMNS)})",
        snapshot.periods,
    )
    conn.executemany(
        f"INSERT INTO firecrawl_runtime_attempts ({','.join(_ATTEMPT_COLUMNS)}) "
        f"VALUES ({','.join('?' for _ in _ATTEMPT_COLUMNS)})",
        snapshot.attempts,
    )
    conn.executemany(
        f"INSERT INTO firecrawl_runtime_provider_state ({','.join(_PROVIDER_COLUMNS)}) "
        f"VALUES ({','.join('?' for _ in _PROVIDER_COLUMNS)})",
        snapshot.provider_state,
    )
    conn.executemany(
        f"INSERT INTO firecrawl_runtime_rate_state ({','.join(_RATE_COLUMNS)}) "
        f"VALUES ({','.join('?' for _ in _RATE_COLUMNS)})",
        snapshot.rate_state,
    )
    conn.execute(
        """UPDATE firecrawl_runtime_ledger_metadata
              SET reconciliation_required=? WHERE singleton=1""",
        (int(snapshot.reconciliation_required),),
    )


def _verify(conn: sqlite3.Connection, snapshot: LedgerSnapshot) -> None:
    """Require exact tuples, circuit state, and healthy SQLite integrity."""
    if _snapshot(conn) != snapshot:
        raise FirecrawlLedgerMigrationError(
            "Firecrawl destination verification did not match merged sources"
        )
    metadata = conn.execute(
        """SELECT reconciliation_required
             FROM firecrawl_runtime_ledger_metadata WHERE singleton=1"""
    ).fetchone()
    if metadata is None or bool(metadata[0]) != snapshot.reconciliation_required:
        raise FirecrawlLedgerMigrationError(
            "Firecrawl destination reconciliation circuit did not match sources"
        )
    integrity = conn.execute("PRAGMA integrity_check").fetchone()
    if integrity is None or integrity[0] != "ok":
        raise FirecrawlLedgerMigrationError("Firecrawl destination integrity failed")


def _write_new(
    destination: Path, snapshot: LedgerSnapshot, binding: ProviderBinding
) -> None:
    """Build a sibling ledger, verify it, and publish without replacement."""
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp_path = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        conn = sqlite3.connect(temp_path, timeout=10.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            ledger_runtime._initialize(conn, binding)
            conn.execute("BEGIN IMMEDIATE")
            try:
                _copy(conn, snapshot)
                _verify(conn, snapshot)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            ledger_runtime._enable_wal_for_publication(conn)
        finally:
            conn.close()
        temp_path.chmod(0o600)
        ledger_runtime.publish_without_replacement(temp_path, destination)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _source_path(conn: sqlite3.Connection) -> Path:
    """Return one connection's resolved on-disk main database path."""
    row = conn.execute("PRAGMA database_list").fetchone()
    if row is None or not row[2]:
        raise FirecrawlLedgerMigrationError("legacy source must be an on-disk database")
    return Path(str(row[2])).resolve()


def migrate_legacy_ledgers(
    sources: Sequence[sqlite3.Connection],
    destination_path: Path,
    *,
    approved_limit: int | None = None,
) -> MigrationSummary:
    """Lock, merge, verify, and publish every legacy Firecrawl history atomically."""
    if not sources:
        raise FirecrawlLedgerMigrationError("at least one legacy source is required")
    destination = destination_path.expanduser()
    if not destination.is_absolute():
        raise FirecrawlLedgerMigrationError("Firecrawl destination must be absolute")
    ordered = sorted(
        ((_source_path(source), source) for source in sources), key=lambda x: x[0]
    )
    if len({path for path, _source in ordered}) != len(ordered):
        raise FirecrawlLedgerMigrationError("duplicate Firecrawl source path")
    if destination.resolve() in {path for path, _source in ordered}:
        raise FirecrawlLedgerMigrationError(
            "destination cannot replace a source database"
        )
    binding = load_binding("firecrawl")
    locked: list[sqlite3.Connection] = []
    try:
        for _path, source in ordered:
            if source.in_transaction:
                raise FirecrawlLedgerMigrationError(
                    "source has an open transaction; isolated cutover is required"
                )
            source.execute("BEGIN IMMEDIATE")
            locked.append(source)
        merged = _merge(
            tuple(_snapshot(source) for _path, source in ordered), approved_limit
        )
        summary = _summary(merged, len(ordered))
        if not destination.exists():
            _write_new(destination, merged, binding)
            return summary
        target = ledger_runtime.connect_existing(
            destination, binding, allow_reconciliation=True
        )
        try:
            current = _snapshot(target)
            if (
                current.periods
                or current.attempts
                or current.provider_state
                or current.rate_state
            ):
                if current != merged:
                    raise FirecrawlLedgerMigrationError(
                        "destination contains different Firecrawl history"
                    )
                _verify(target, merged)
                return replace(summary, already_migrated=True)
            target.execute("BEGIN IMMEDIATE")
            try:
                _copy(target, merged)
                _verify(target, merged)
                target.commit()
            except Exception:
                target.rollback()
                raise
        finally:
            target.close()
        return summary
    except sqlite3.DatabaseError as exc:
        raise FirecrawlLedgerMigrationError(
            "could not lock/read every source; stop all legacy Firecrawl writers"
        ) from exc
    except ledger_runtime.FirecrawlLedgerError as exc:
        raise FirecrawlLedgerMigrationError(str(exc)) from exc
    finally:
        for source in reversed(locked):
            source.rollback()


def _connect_source(path: Path, *, execute: bool) -> sqlite3.Connection:
    """Open an existing source without applying application migrations."""
    resolved = path.expanduser().resolve()
    mode = "rw" if execute else "ro"
    conn = sqlite3.connect(f"{resolved.as_uri()}?mode={mode}", uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def run(
    source_paths: Sequence[Path],
    destination: Path,
    *,
    execute: bool,
    approved_limit: int | None = None,
) -> int:
    """Preview or execute a complete explicitly enumerated legacy merge."""
    sources: list[sqlite3.Connection] = []
    try:
        sources = [_connect_source(path, execute=execute) for path in source_paths]
        summary = (
            migrate_legacy_ledgers(sources, destination, approved_limit=approved_limit)
            if execute
            else inspect_legacy_ledgers(sources, approved_limit=approved_limit)
        )
    except (
        FirecrawlLedgerMigrationError,
        PaidProviderAuthorityError,
        sqlite3.DatabaseError,
        OSError,
    ) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    finally:
        for source in sources:
            source.close()
    prefix = "executed" if execute else "preview"
    print(
        f"{prefix}: sources={summary.sources}, periods={summary.periods}, "
        f"attempts={summary.attempts}, reserved={summary.reserved_calls}, "
        f"reconciliation_required={str(summary.reconciliation_required).lower()}"
    )
    if not execute:
        print("no files changed; rerun with --execute during the reviewed cutover")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse repeatable sources and preserve dry preview as the default."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", action="append", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--approved-monthly-limit", type=int)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    return run(
        tuple(args.source),
        args.destination,
        execute=args.execute,
        approved_limit=args.approved_monthly_limit,
    )


if __name__ == "__main__":
    raise SystemExit(main())
