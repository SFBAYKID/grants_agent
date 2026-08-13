"""Multi-source reconciliation for the standalone ZoomInfo account ledger.

Production and a laptop historically spent from the same vendor account while each
held an independent SQLite counter.  This module locks every explicitly scoped source,
validates each internally, deduplicates only exact cloned rows, preserves genuinely
distinct same-key spends with source identity, and publishes one exact authority.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Sequence

from .enrich import zoominfo_credits as credits
from .paid_provider_authority import load_binding

_PERIOD_COLUMNS = ("period", "credit_limit", "consumed", "updated_at")
_LEGACY_SPEND_COLUMNS = (
    "id",
    "period",
    "request_key",
    "requested_by",
    "lead_id",
    "reserved_credits",
    "billed_credits",
    "state",
    "started_at",
    "finished_at",
    "error",
)
_DESTINATION_SPEND_COLUMNS = (
    *_LEGACY_SPEND_COLUMNS,
    "source_scope",
    "legacy_id",
    "legacy_request_key",
)
_SCOPE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{2,79}")


def _source_path(conn: sqlite3.Connection) -> Path:
    """Return the resolved on-disk path for one legacy source connection."""
    row = conn.execute("PRAGMA database_list").fetchone()
    if row is None or not row[2]:
        raise credits.LedgerMigrationError(
            "legacy ZoomInfo source must be an on-disk database"
        )
    return Path(str(row[2])).resolve()


def default_source_scope(conn: sqlite3.Connection) -> str:
    """Derive a stable opaque fallback scope for the one-source compatibility API."""
    digest = hashlib.sha256(str(_source_path(conn)).encode("utf-8")).hexdigest()[:20]
    return f"legacy-{digest}"


def _tables(conn: sqlite3.Connection) -> set[str]:
    """Return concrete source table names."""
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _legacy_rows(
    conn: sqlite3.Connection,
) -> tuple[tuple[tuple[object, ...], ...], tuple[tuple[object, ...], ...]]:
    """Read canonical period/spend tuples or prove a pre-ledger source is empty."""
    required = {"zoominfo_credit_periods", "zoominfo_credit_spends"}
    present = required & _tables(conn)
    if not present:
        return (), ()
    if present != required:
        raise credits.LedgerMigrationError(
            "legacy ZoomInfo tables are partial; refusing to infer missing state"
        )
    try:
        periods = tuple(
            tuple(row)
            for row in conn.execute(
                f"SELECT {','.join(_PERIOD_COLUMNS)} "
                "FROM zoominfo_credit_periods ORDER BY period"
            )
        )
        spends = tuple(
            tuple(row)
            for row in conn.execute(
                f"SELECT {','.join(_LEGACY_SPEND_COLUMNS)} "
                "FROM zoominfo_credit_spends ORDER BY id"
            )
        )
    except sqlite3.DatabaseError as exc:
        raise credits.LedgerMigrationError(
            "legacy ZoomInfo ledger tables are unreadable or unknown"
        ) from exc
    return periods, spends


def _destination_rows(
    conn: sqlite3.Connection,
) -> tuple[tuple[tuple[object, ...], ...], tuple[tuple[object, ...], ...]]:
    """Read exact schema-2 destination tuples including their legacy identity."""
    try:
        periods = tuple(
            tuple(row)
            for row in conn.execute(
                f"SELECT {','.join(_PERIOD_COLUMNS)} "
                "FROM zoominfo_credit_periods ORDER BY period"
            )
        )
        spends = tuple(
            tuple(row)
            for row in conn.execute(
                f"SELECT {','.join(_DESTINATION_SPEND_COLUMNS)} "
                "FROM zoominfo_credit_spends ORDER BY id"
            )
        )
    except sqlite3.DatabaseError as exc:
        raise credits.LedgerMigrationError(
            "ZoomInfo destination schema is unreadable or unknown"
        ) from exc
    return periods, spends


def _timestamp(value: object, label: str) -> datetime:
    """Parse one ISO audit timestamp and normalize naive legacy values to UTC."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise credits.LedgerMigrationError(
            f"invalid ZoomInfo {label} timestamp"
        ) from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _validate_source(
    periods: tuple[tuple[object, ...], ...],
    spends: tuple[tuple[object, ...], ...],
) -> None:
    """Prove one source balance agrees with every recorded settled/released spend."""
    balances = {str(row[0]): (int(row[1]), int(row[2])) for row in periods}
    if len(balances) != len(periods):
        raise credits.LedgerMigrationError("duplicate ZoomInfo period")
    calculated = {period: 0 for period in balances}
    ids: set[str] = set()
    request_keys: set[str] = set()
    for row in spends:
        spend_id, period, request_key = str(row[0]), str(row[1]), str(row[2])
        reserved = int(row[5])
        billed = None if row[6] is None else int(row[6])
        state = str(row[7])
        if (
            not spend_id
            or spend_id in ids
            or not request_key
            or request_key in request_keys
        ):
            raise credits.LedgerMigrationError(
                "duplicate/empty ZoomInfo spend identity"
            )
        ids.add(spend_id)
        request_keys.add(request_key)
        if period not in balances:
            raise credits.LedgerMigrationError(
                f"ZoomInfo spend references absent period {period}"
            )
        if reserved <= 0:
            raise credits.LedgerMigrationError("ZoomInfo reservation is not positive")
        if state in {"reserved", "indeterminate"}:
            raise credits.LedgerMigrationError(
                "legacy ledger has unsettled spend; reconcile it before cutover"
            )
        if state == "settled":
            if billed is None or billed < 0 or billed > reserved:
                raise credits.LedgerMigrationError(
                    "settled ZoomInfo spend has an invalid billed amount"
                )
            calculated[period] += billed
        elif state == "released":
            if billed not in {None, 0}:
                raise credits.LedgerMigrationError(
                    "released ZoomInfo spend has a non-zero billed amount"
                )
        else:
            raise credits.LedgerMigrationError(
                f"legacy ledger has unknown spend state {state}"
            )
        _timestamp(row[8], "spend start")
    for period, (limit, consumed) in balances.items():
        if limit < 0 or consumed > limit or consumed != calculated[period]:
            raise credits.LedgerMigrationError(
                f"legacy period {period} balance does not reconcile to its spend rows"
            )
    for row in periods:
        _timestamp(row[3], "period update")


def _scoped_request_key(scope: str, row: tuple[object, ...]) -> str:
    """Return an opaque unique key for colliding local legacy request keys."""
    digest = hashlib.sha256(f"{scope}\0{row[0]}\0{row[2]}".encode("utf-8")).hexdigest()[
        :24
    ]
    return f"legacy:{scope}:{digest}"


def _merge(
    sources: Sequence[credits.LegacyLedgerSource],
) -> tuple[
    tuple[tuple[object, ...], ...],
    tuple[tuple[object, ...], ...],
    credits.LedgerMigrationSummary,
]:
    """Merge actual spend rows, deduplicating only byte-equivalent history clones."""
    if not sources:
        raise credits.LedgerMigrationError("at least one ZoomInfo source is required")
    scoped_rows: list[
        tuple[str, tuple[tuple[object, ...], ...], tuple[tuple[object, ...], ...]]
    ] = []
    seen_scopes: set[str] = set()
    for source in sources:
        scope = source.source_scope.strip()
        if _SCOPE_RE.fullmatch(scope) is None or scope in seen_scopes:
            raise credits.LedgerMigrationError(
                "ZoomInfo source scopes must be unique bounded opaque identifiers"
            )
        seen_scopes.add(scope)
        periods, spends = _legacy_rows(source.connection)
        _validate_source(periods, spends)
        scoped_rows.append((scope, periods, spends))

    id_rows: dict[str, tuple[object, ...]] = {}
    occurrences: dict[tuple[object, ...], set[str]] = {}
    for scope, _periods, spends in scoped_rows:
        for row in spends:
            spend_id = str(row[0])
            prior = id_rows.get(spend_id)
            if prior is not None and prior != row:
                raise credits.LedgerMigrationError(
                    "same ZoomInfo spend id has conflicting history"
                )
            id_rows[spend_id] = row
            occurrences.setdefault(row, set()).add(scope)
    unique_rows = sorted(occurrences, key=lambda row: str(row[0]))
    request_groups: dict[str, list[tuple[object, ...]]] = {}
    for row in unique_rows:
        request_groups.setdefault(str(row[2]), []).append(row)

    destination_spends: list[tuple[object, ...]] = []
    for row in unique_rows:
        scope = min(occurrences[row])
        original_key = str(row[2])
        request_key = (
            original_key
            if len(request_groups[original_key]) == 1
            else _scoped_request_key(scope, row)
        )
        destination_spends.append(
            (
                row[0],
                row[1],
                request_key,
                *row[3:],
                scope,
                row[0],
                original_key,
            )
        )
    if len({str(row[2]) for row in destination_spends}) != len(destination_spends):
        raise credits.LedgerMigrationError("merged ZoomInfo request-key collision")

    period_sources: dict[str, list[tuple[object, ...]]] = {}
    for _scope, periods, _spends in scoped_rows:
        for row in periods:
            period_sources.setdefault(str(row[0]), []).append(row)
    billed_by_period: dict[str, int] = {}
    for row in destination_spends:
        if str(row[7]) == "settled":
            billed_by_period[str(row[1])] = billed_by_period.get(str(row[1]), 0) + int(
                row[6]
            )
    destination_periods: list[tuple[object, ...]] = []
    for period, rows in sorted(period_sources.items()):
        limits = {int(row[1]) for row in rows}
        if len(limits) != 1:
            raise credits.LedgerMigrationError(
                f"ZoomInfo period {period} has conflicting account limits"
            )
        limit = next(iter(limits))
        consumed = billed_by_period.get(period, 0)
        if consumed > limit:
            raise credits.LedgerMigrationError(
                f"merged ZoomInfo period {period} exceeds its account limit"
            )
        destination_periods.append(
            (
                period,
                limit,
                consumed,
                max(rows, key=lambda row: _timestamp(row[3], "period update"))[3],
            )
        )
    reserved_total = sum(int(row[5]) for row in destination_spends)
    billed_total = sum(
        int(row[6]) for row in destination_spends if str(row[7]) == "settled"
    )
    summary = credits.LedgerMigrationSummary(
        periods=len(destination_periods),
        spends=len(destination_spends),
        consumed_credits=sum(int(row[2]) for row in destination_periods),
        reserved_credits=reserved_total,
        billed_credits=billed_total,
        sources=len(sources),
    )
    return tuple(destination_periods), tuple(destination_spends), summary


def inspect_legacy_ledger(conn: sqlite3.Connection) -> credits.LedgerMigrationSummary:
    """Validate one source using the backwards-compatible inspection API."""
    return inspect_legacy_ledgers(
        (credits.LegacyLedgerSource(default_source_scope(conn), conn),)
    )


def inspect_legacy_ledgers(
    sources: Sequence[credits.LegacyLedgerSource],
) -> credits.LedgerMigrationSummary:
    """Validate the full same-account reconciliation without writing or locking."""
    _periods, _spends, summary = _merge(sources)
    return summary


def _copy(
    destination: sqlite3.Connection,
    periods: tuple[tuple[object, ...], ...],
    spends: tuple[tuple[object, ...], ...],
) -> None:
    """Insert one reconciled snapshot inside the destination transaction."""
    destination.executemany(
        f"INSERT INTO zoominfo_credit_periods ({','.join(_PERIOD_COLUMNS)}) "
        f"VALUES ({','.join('?' for _ in _PERIOD_COLUMNS)})",
        periods,
    )
    destination.executemany(
        f"INSERT INTO zoominfo_credit_spends ({','.join(_DESTINATION_SPEND_COLUMNS)}) "
        f"VALUES ({','.join('?' for _ in _DESTINATION_SPEND_COLUMNS)})",
        spends,
    )


def _verify(
    destination: sqlite3.Connection,
    periods: tuple[tuple[object, ...], ...],
    spends: tuple[tuple[object, ...], ...],
) -> None:
    """Require exact tuples plus SQLite referential and structural integrity."""
    if _destination_rows(destination) != (periods, spends):
        raise credits.LedgerMigrationError(
            "ZoomInfo destination verification did not match reconciled sources"
        )
    integrity = destination.execute("PRAGMA integrity_check").fetchone()
    foreign_keys = destination.execute("PRAGMA foreign_key_check").fetchone()
    if integrity is None or integrity[0] != "ok" or foreign_keys is not None:
        raise credits.LedgerMigrationError("ZoomInfo destination integrity failed")


def _write_new(
    destination: Path,
    periods: tuple[tuple[object, ...], ...],
    spends: tuple[tuple[object, ...], ...],
) -> None:
    """Build and verify a sibling file before atomic no-replace publication."""
    binding = load_binding("zoominfo")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp_path = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        ledger = sqlite3.connect(temp_path, timeout=10.0)
        try:
            ledger.row_factory = sqlite3.Row
            ledger.execute("PRAGMA foreign_keys=ON")
            credits._initialize_ledger(ledger, binding)
            ledger.execute("BEGIN IMMEDIATE")
            try:
                _copy(ledger, periods, spends)
                _verify(ledger, periods, spends)
                ledger.commit()
            except Exception:
                ledger.rollback()
                raise
        finally:
            ledger.close()
        temp_path.chmod(0o600)
        credits._publish_without_replacement(temp_path, destination)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def migrate_legacy_ledgers(
    sources: Sequence[credits.LegacyLedgerSource], destination_path: Path
) -> credits.LedgerMigrationSummary:
    """Fence every source, reconcile actual spends, and publish one authority."""
    if not sources:
        raise credits.LedgerMigrationError("at least one ZoomInfo source is required")
    destination = destination_path.expanduser()
    if not destination.is_absolute():
        raise credits.LedgerMigrationError("ledger destination must be absolute")
    source_entries = sorted(
        ((_source_path(source.connection), source) for source in sources),
        key=lambda entry: entry[0],
    )
    paths = [path for path, _source in source_entries]
    if len(set(paths)) != len(paths):
        raise credits.LedgerMigrationError("duplicate ZoomInfo source path")
    if destination.resolve() in set(paths):
        raise credits.LedgerMigrationError("standalone ledger cannot replace a source")
    locked: list[sqlite3.Connection] = []
    try:
        for _path, source in source_entries:
            conn = source.connection
            if conn.in_transaction:
                raise credits.LedgerMigrationError(
                    "source already has a transaction; isolated cutover is required"
                )
            conn.execute("BEGIN IMMEDIATE")
            locked.append(conn)
        periods, spends, summary = _merge(
            tuple(source for _path, source in source_entries)
        )
        if not destination.exists():
            _write_new(destination, periods, spends)
            return summary
        binding = load_binding("zoominfo")
        ledger = credits._connect_existing_ledger(destination, binding)
        try:
            existing = _destination_rows(ledger)
            if existing[0] or existing[1]:
                if existing != (periods, spends):
                    raise credits.LedgerMigrationError(
                        "destination contains different ZoomInfo credit history"
                    )
                _verify(ledger, periods, spends)
                return replace(summary, already_migrated=True)
            ledger.execute("BEGIN IMMEDIATE")
            try:
                _copy(ledger, periods, spends)
                _verify(ledger, periods, spends)
                ledger.commit()
            except Exception:
                ledger.rollback()
                raise
        finally:
            ledger.close()
        return summary
    except sqlite3.DatabaseError as exc:
        raise credits.LedgerMigrationError(
            "could not lock every source; stop all legacy ZoomInfo writers"
        ) from exc
    finally:
        for conn in reversed(locked):
            conn.rollback()
