"""Transactional runner shared by Grant's versioned SQLite migrations."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable
from typing import Protocol


class MigrationLike(Protocol):
    """Structural type accepted by the migration runner."""

    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]


def apply_migrations(
    conn: sqlite3.Connection,
    migrations: Iterable[MigrationLike],
    clock: Callable[[], str],
) -> None:
    """Apply unapplied migrations transactionally with foreign keys restored."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
               version INTEGER PRIMARY KEY,
               name TEXT NOT NULL,
               applied_at TIMESTAMP NOT NULL
           )"""
    )
    conn.commit()
    applied = {
        int(row[0]) for row in conn.execute("SELECT version FROM schema_migrations")
    }
    pending = [
        migration for migration in migrations if migration.version not in applied
    ]
    if not pending:
        return
    # SQLite cannot rebuild a referenced table while foreign keys are enabled.
    # Each rebuilding migration preserves row ids, and the flag is restored even
    # when a migration fails.
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        for migration in pending:
            try:
                conn.execute("BEGIN IMMEDIATE")
                migration.apply(conn)
                conn.execute(
                    "INSERT INTO schema_migrations(version,name,applied_at) VALUES (?,?,?)",
                    (migration.version, migration.name, clock()),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
