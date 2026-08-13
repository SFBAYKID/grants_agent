"""Preview or execute the one-time embedded-to-standalone ZoomInfo ledger cutover.

This command is intentionally separate from the everyday CLI. It never contacts
ZoomInfo and never changes application data. Execution takes a temporary source
write lock and writes only the explicit destination after validating and reconciling
every legacy spend row. Every legacy writer must remain stopped through cutover.

Usage:
    python -m grant_watch.zoominfo_ledger_migration \
      --source production=/absolute/exported-production.db \
      --source laptop=/absolute/laptop.db \
      --destination /absolute/private/zoominfo-credit-ledger.db
    python -m grant_watch.zoominfo_ledger_migration \
      --source production=/absolute/exported-production.db \
      --source laptop=/absolute/laptop.db \
      --destination /absolute/private/zoominfo-credit-ledger.db --execute
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from . import db
from .enrich.zoominfo_credits import (
    LegacyLedgerSource,
    LedgerMigrationError,
    LedgerMigrationSummary,
    inspect_legacy_ledgers,
    migrate_legacy_ledgers,
)


def _summary_text(summary: LedgerMigrationSummary) -> str:
    """Render only aggregate, non-sensitive verification totals."""
    state = "already migrated" if summary.already_migrated else "validated"
    return (
        f"{state}: sources={summary.sources}, periods={summary.periods}, "
        f"spends={summary.spends}, "
        f"consumed={summary.consumed_credits}, "
        f"reserved={summary.reserved_credits}, billed={summary.billed_credits}"
    )


def _connect_cutover_source(path: Path) -> sqlite3.Connection:
    """Open an existing source read/write so execution can fence legacy writers.

    This deliberately does not call ``db.connect``: applying an application migration
    would mutate the source during a one-time credit-ledger copy. ``mode=rw`` refuses
    a missing path, while ``BEGIN IMMEDIATE`` inside the migration supplies the lock.
    """
    resolved = path.expanduser().resolve()
    conn = sqlite3.connect(f"{resolved.as_uri()}?mode=rw", uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _normalized_sources(
    value: Path | tuple[tuple[str, Path], ...],
) -> tuple[tuple[str, Path], ...]:
    """Preserve the historical one-path API while supporting explicit scopes."""
    if isinstance(value, Path):
        return (("legacy-default", value),)
    return value


def run(
    source_paths: Path | tuple[tuple[str, Path], ...],
    destination: Path,
    *,
    execute: bool,
) -> int:
    """Validate all histories and optionally create their reconciled exact copy."""
    specifications = _normalized_sources(source_paths)
    sources: list[sqlite3.Connection] = []
    try:
        sources = [
            _connect_cutover_source(path) if execute else db.connect_readonly(path)
            for _scope, path in specifications
        ]
        scoped = tuple(
            LegacyLedgerSource(scope, source)
            for (scope, _path), source in zip(specifications, sources, strict=True)
        )
        summary = (
            migrate_legacy_ledgers(scoped, destination)
            if execute
            else inspect_legacy_ledgers(scoped)
        )
    except (LedgerMigrationError, sqlite3.DatabaseError, OSError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    finally:
        for source in sources:
            source.close()
    prefix = "executed" if execute else "preview"
    print(f"{prefix}: {_summary_text(summary)}")
    if not execute:
        print("no files changed; rerun with --execute during the reviewed cutover")
    return 0


def _source(value: str) -> tuple[str, Path]:
    """Parse one explicit ``scope=/absolute/source.db`` argument."""
    scope, separator, raw_path = value.partition("=")
    if not separator or not scope.strip() or not raw_path.strip():
        raise argparse.ArgumentTypeError("source must be scope=/absolute/source.db")
    return scope.strip(), Path(raw_path.strip())


def main(argv: list[str] | None = None) -> int:
    """Parse the explicit source/destination and keep preview as the default."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        action="append",
        type=_source,
        help=(
            "repeatable scope=/absolute/app.db; every same-account history must be "
            "enumerated (defaults to production=<standard app DB>)"
        ),
    )
    parser.add_argument(
        "--destination",
        type=Path,
        required=True,
        help="absolute path for the private standalone ledger",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="write the destination (preview is the default)",
    )
    args = parser.parse_args(argv)
    sources = tuple(args.source or (("production", db.DEFAULT_DB_PATH),))
    return run(sources, args.destination, execute=args.execute)


if __name__ == "__main__":
    raise SystemExit(main())
