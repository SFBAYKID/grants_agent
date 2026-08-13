"""Legacy Firecrawl histories merge exactly into the sole authority ledger."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from grant_watch import db, firecrawl_ledger_migration as migration
from grant_watch.enrich import firecrawl_gateway
from grant_watch.paid_provider_authority import ProviderBinding
from tests.paid_provider_support import configure_authority

NOW = datetime(2026, 8, 12, tzinfo=timezone.utc)


def _legacy(
    path: Path,
    *,
    attempt_id: str,
    request_key: str,
    request_hash: str | None,
    state: str = "completed",
    limit: int = 10,
    blocked_for: int = 0,
) -> sqlite3.Connection:
    """Create one internally reconciled app-DB runtime history."""
    conn = db.connect(path)
    at = NOW.isoformat()
    conn.execute(
        """INSERT INTO firecrawl_runtime_periods
             (billing_period,call_limit,reserved_calls,created_at,updated_at)
           VALUES ('2026-08',?,1,?,?)""",
        (limit, at, at),
    )
    conn.execute(
        """INSERT INTO firecrawl_runtime_attempts
             (id,request_key,workflow,operation,billing_period,state,started_at,
              finished_at,http_status,request_hash,attempt_number)
           VALUES (?,?,'test','search','2026-08',?,?,?,200,?,1)""",
        (
            attempt_id,
            request_key,
            state,
            at,
            at if state not in {"in_flight", "indeterminate"} else None,
            request_hash,
        ),
    )
    if blocked_for:
        conn.execute(
            """INSERT INTO firecrawl_runtime_provider_state
                 (provider,blocked_until,reason,updated_at)
               VALUES ('firecrawl',?,'rate_limited',?)""",
            ((NOW + timedelta(seconds=blocked_for)).isoformat(), at),
        )
    conn.commit()
    return conn


def _rows(path: Path, table: str) -> list[tuple[object, ...]]:
    """Read deterministic tuples from one migrated ledger."""
    conn = sqlite3.connect(path)
    try:
        return [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
    finally:
        conn.close()


def test_disjoint_app_histories_merge_and_same_hash_calls_both_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two app DBs cannot collapse distinct potentially billed calls by request hash."""
    configure_authority(tmp_path, monkeypatch, "firecrawl")
    digest = "a" * 64
    first = _legacy(
        tmp_path / "first.db",
        attempt_id="one",
        request_key="search:one",
        request_hash=digest,
        blocked_for=60,
    )
    second = _legacy(
        tmp_path / "second.db",
        attempt_id="two",
        request_key="search:two",
        request_hash=digest,
        blocked_for=120,
    )
    destination = tmp_path / "firecrawl-authority.db"

    summary = migration.migrate_legacy_ledgers((second, first), destination)

    assert summary.attempts == 2
    assert summary.reserved_calls == 2
    periods = _rows(destination, "firecrawl_runtime_periods")
    assert periods[0][2] == 2
    assert len(_rows(destination, "firecrawl_runtime_attempts")) == 2
    state = _rows(destination, "firecrawl_runtime_provider_state")[0]
    assert state[1] == (NOW + timedelta(seconds=120)).isoformat()
    first.close()
    second.close()


def test_source_order_does_not_change_merged_tuples(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deterministic merge output is independent of CLI/source argument order."""
    configure_authority(tmp_path, monkeypatch, "firecrawl")
    first = _legacy(
        tmp_path / "a.db",
        attempt_id="one",
        request_key="search:one",
        request_hash="1" * 64,
    )
    second = _legacy(
        tmp_path / "b.db",
        attempt_id="two",
        request_key="search:two",
        request_hash="2" * 64,
    )
    left = tmp_path / "left.db"
    right = tmp_path / "right.db"

    migration.migrate_legacy_ledgers((first, second), left)
    migration.migrate_legacy_ledgers((second, first), right)

    for table in (
        "firecrawl_runtime_periods",
        "firecrawl_runtime_attempts",
        "firecrawl_runtime_provider_state",
    ):
        assert _rows(left, table) == _rows(right, table)
    first.close()
    second.close()


def test_latest_proactive_rate_slot_survives_multi_source_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cutover cannot reopen a rate window already claimed in another history."""
    configure_authority(tmp_path, monkeypatch, "firecrawl")
    first = _legacy(
        tmp_path / "a.db",
        attempt_id="one",
        request_key="search:one",
        request_hash="1" * 64,
    )
    second = _legacy(
        tmp_path / "b.db",
        attempt_id="two",
        request_key="search:two",
        request_hash="2" * 64,
    )
    for conn, seconds in ((first, 30), (second, 60)):
        conn.execute(
            """CREATE TABLE firecrawl_runtime_rate_state (
                 singleton INTEGER PRIMARY KEY,
                 next_call_at TIMESTAMP NOT NULL,
                 updated_at TIMESTAMP NOT NULL)"""
        )
        conn.execute(
            "INSERT INTO firecrawl_runtime_rate_state VALUES (1,?,?)",
            (
                (NOW + timedelta(seconds=seconds)).isoformat(),
                NOW.isoformat(),
            ),
        )
        conn.commit()
    destination = tmp_path / "ledger.db"

    migration.migrate_legacy_ledgers((first, second), destination)

    rate = _rows(destination, "firecrawl_runtime_rate_state")[0]
    assert rate[1] == (NOW + timedelta(seconds=60)).isoformat()
    first.close()
    second.close()


def test_conflicting_ceilings_require_an_explicit_reviewed_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Independent caps are never added or silently selected during reconciliation."""
    configure_authority(tmp_path, monkeypatch, "firecrawl")
    first = _legacy(
        tmp_path / "a.db",
        attempt_id="one",
        request_key="search:one",
        request_hash="1" * 64,
        limit=10,
    )
    second = _legacy(
        tmp_path / "b.db",
        attempt_id="two",
        request_key="search:two",
        request_hash="2" * 64,
        limit=20,
    )
    destination = tmp_path / "ledger.db"
    with pytest.raises(migration.FirecrawlLedgerMigrationError, match="conflicting"):
        migration.migrate_legacy_ledgers((first, second), destination)
    assert not destination.exists()

    migration.migrate_legacy_ledgers((first, second), destination, approved_limit=15)
    assert _rows(destination, "firecrawl_runtime_periods")[0][1:3] == (15, 2)
    first.close()
    second.close()


def test_equal_source_ceilings_never_expand_to_fit_merged_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two 8-of-10 histories are 16-of-10, not a newly invented 16-call cap."""
    configure_authority(tmp_path, monkeypatch, "firecrawl")
    connections: list[sqlite3.Connection] = []
    for source_number in (1, 2):
        path = tmp_path / f"source-{source_number}.db"
        conn = db.connect(path)
        at = NOW.isoformat()
        conn.execute(
            """INSERT INTO firecrawl_runtime_periods
                 (billing_period,call_limit,reserved_calls,created_at,updated_at)
               VALUES ('2026-08',10,8,?,?)""",
            (at, at),
        )
        for attempt_number in range(8):
            identity = f"{source_number}-{attempt_number}"
            conn.execute(
                """INSERT INTO firecrawl_runtime_attempts
                     (id,request_key,workflow,operation,billing_period,state,
                      started_at,finished_at,http_status,request_hash,attempt_number)
                   VALUES (?,?,'test','search','2026-08','completed',?,?,200,?,1)""",
                (
                    identity,
                    f"search:{identity}",
                    at,
                    at,
                    f"{source_number}{attempt_number}".ljust(64, "0"),
                ),
            )
        conn.commit()
        connections.append(conn)

    destination = tmp_path / "ledger.db"
    with pytest.raises(migration.FirecrawlLedgerMigrationError, match="above.*ceiling"):
        migration.migrate_legacy_ledgers(connections, destination)
    assert not destination.exists()
    with pytest.raises(migration.FirecrawlLedgerMigrationError, match="above.*ceiling"):
        migration.migrate_legacy_ledgers(connections, destination, approved_limit=15)
    migration.migrate_legacy_ledgers(connections, destination, approved_limit=16)
    assert _rows(destination, "firecrawl_runtime_periods")[0][1:3] == (16, 16)
    for conn in connections:
        conn.close()


def test_unknown_legacy_attempt_opens_an_account_wide_runtime_circuit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unidentifiable possible call blocks every request, not only a guessed hash."""
    configure_authority(tmp_path, monkeypatch, "firecrawl")
    source = _legacy(
        tmp_path / "legacy.db",
        attempt_id="unknown",
        request_key="search:legacy-prefix:uuid",
        request_hash=None,
        state="indeterminate",
    )
    destination = tmp_path / "ledger.db"
    summary = migration.migrate_legacy_ledgers((source,), destination)
    assert summary.reconciliation_required is True
    monkeypatch.setenv(firecrawl_gateway.LEDGER_PATH_ENV, str(destination))
    monkeypatch.setenv(firecrawl_gateway.MONTHLY_LIMIT_ENV, "10")
    monkeypatch.setenv(firecrawl_gateway.RATE_LIMIT_ENV, "10")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")
    monkeypatch.setattr(
        firecrawl_gateway.requests,
        "post",
        lambda *_a, **_k: pytest.fail("reconciliation circuit reached HTTP"),
    )

    with pytest.raises(
        firecrawl_gateway.FirecrawlBudgetNotConfigured, match="reconciliation"
    ):
        firecrawl_gateway.search("unrelated query")
    source.close()


def test_exact_clone_deduplicates_but_identity_conflict_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Copied files count once; a reused id with different content is corruption."""
    configure_authority(tmp_path, monkeypatch, "firecrawl")
    original = _legacy(
        tmp_path / "original.db",
        attempt_id="same",
        request_key="search:same",
        request_hash="a" * 64,
    )
    clone_path = tmp_path / "clone.db"
    clone = sqlite3.connect(clone_path)
    original.backup(clone)
    clone.row_factory = sqlite3.Row
    destination = tmp_path / "dedup.db"
    assert (
        migration.migrate_legacy_ledgers((original, clone), destination).attempts == 1
    )

    clone.execute(
        "UPDATE firecrawl_runtime_attempts SET request_hash=? WHERE id='same'",
        ("b" * 64,),
    )
    clone.commit()
    with pytest.raises(migration.FirecrawlLedgerMigrationError, match="conflicting"):
        migration.migrate_legacy_ledgers((original, clone), tmp_path / "conflict.db")
    original.close()
    clone.close()


def test_lock_contention_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failure to fence one source releases earlier locks and leaves no authority."""
    configure_authority(tmp_path, monkeypatch, "firecrawl")
    first = _legacy(
        tmp_path / "a.db",
        attempt_id="one",
        request_key="search:one",
        request_hash="1" * 64,
    )
    second = _legacy(
        tmp_path / "b.db",
        attempt_id="two",
        request_key="search:two",
        request_hash="2" * 64,
    )
    blocker = sqlite3.connect(tmp_path / "b.db", timeout=0.05)
    blocker.execute("BEGIN IMMEDIATE")
    second.execute("PRAGMA busy_timeout=50")
    destination = tmp_path / "ledger.db"

    with pytest.raises(migration.FirecrawlLedgerMigrationError, match="lock/read"):
        migration.migrate_legacy_ledgers((first, second), destination)

    assert not destination.exists()
    assert first.in_transaction is False
    blocker.rollback()
    blocker.close()
    first.close()
    second.close()


def test_source_lock_is_held_through_destination_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A legacy writer cannot mutate its source during snapshot/copy/publication."""
    configure_authority(tmp_path, monkeypatch, "firecrawl")
    source_path = tmp_path / "source.db"
    source = _legacy(
        source_path,
        attempt_id="one",
        request_key="search:one",
        request_hash="1" * 64,
    )
    original = migration._write_new
    writer_was_blocked = False

    def _write_while_probing(
        destination: Path,
        snapshot: migration.LedgerSnapshot,
        binding: ProviderBinding,
    ) -> None:
        """Try a competing source write while the migration publishes."""
        nonlocal writer_was_blocked
        writer = sqlite3.connect(source_path, timeout=0.05)
        writer.execute("PRAGMA busy_timeout=50")
        try:
            writer.execute("UPDATE firecrawl_runtime_periods SET updated_at='changed'")
        except sqlite3.OperationalError as exc:
            writer_was_blocked = "locked" in str(exc).lower()
        finally:
            writer.close()
        original(destination, snapshot, binding)

    monkeypatch.setattr(migration, "_write_new", _write_while_probing)
    migration.migrate_legacy_ledgers((source,), tmp_path / "ledger.db")

    assert writer_was_blocked is True
    source.close()


def test_publication_race_preserves_winner_and_cleans_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A destination appearing after validation is never replaced or half-published."""
    configure_authority(tmp_path, monkeypatch, "firecrawl")
    source = _legacy(
        tmp_path / "source.db",
        attempt_id="one",
        request_key="search:one",
        request_hash="1" * 64,
    )
    destination = tmp_path / "ledger.db"
    original = migration.ledger_runtime.publish_without_replacement

    def _race(temp_path: Path, target: Path) -> None:
        """Publish a competing target immediately before the no-replace primitive."""
        target.write_bytes(b"concurrent winner")
        target.chmod(0o600)
        original(temp_path, target)

    monkeypatch.setattr(migration.ledger_runtime, "publish_without_replacement", _race)
    with pytest.raises(migration.FirecrawlLedgerMigrationError, match="appeared"):
        migration.migrate_legacy_ledgers((source,), destination)

    assert destination.read_bytes() == b"concurrent winner"
    assert not list(tmp_path.glob(".ledger.db.*.tmp*"))
    source.close()


def test_explicit_source_without_runtime_tables_proves_an_empty_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-schema-42 database can create an empty ledger only via reviewed migration."""
    configure_authority(tmp_path, monkeypatch, "firecrawl")
    source = sqlite3.connect(tmp_path / "old.db")
    source.row_factory = sqlite3.Row
    source.execute("CREATE TABLE legacy_marker (id INTEGER PRIMARY KEY)")
    source.commit()
    destination = tmp_path / "ledger.db"

    summary = migration.migrate_legacy_ledgers((source,), destination)

    assert summary == migration.MigrationSummary(1, 0, 0, 0, False)
    assert destination.exists()
    source.close()


def test_exact_rerun_is_idempotent_and_command_is_preview_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The documented CLI previews, executes, and recognizes an exact rerun."""
    configure_authority(tmp_path, monkeypatch, "firecrawl")
    source_path = tmp_path / "source.db"
    source = _legacy(
        source_path,
        attempt_id="one",
        request_key="search:one",
        request_hash="1" * 64,
    )
    source.close()
    destination = tmp_path / "ledger.db"

    assert migration.run((source_path,), destination, execute=False) == 0
    assert "preview: sources=1" in capsys.readouterr().out
    assert not destination.exists()
    assert migration.run((source_path,), destination, execute=True) == 0
    assert destination.exists()
    assert migration.run((source_path,), destination, execute=True) == 0

    observer = sqlite3.connect(destination)
    try:
        assert (
            observer.execute(
                "SELECT COUNT(*) FROM firecrawl_runtime_attempts"
            ).fetchone()[0]
            == 1
        )
    finally:
        observer.close()
