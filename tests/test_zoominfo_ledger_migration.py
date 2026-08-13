"""The shared ZoomInfo ledger cutover must preserve every already-spent credit."""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

import pytest

from grant_watch import db, zoominfo_ledger_migration
from grant_watch import zoominfo_ledger_state
from grant_watch.enrich import zoominfo_credits as credits
from tests.paid_provider_support import configure_authority


@pytest.fixture(autouse=True)
def _authority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind every migration target to one test-local ZoomInfo account authority."""
    configure_authority(tmp_path, monkeypatch, "zoominfo")
    monkeypatch.setenv("ZOOMINFO_CLIENT_ID", "test-client")
    monkeypatch.setenv("ZOOMINFO_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("ZOOMINFO_MONTHLY_CREDITS", "1000")


def _settled_legacy_ledger(path: Path) -> sqlite3.Connection:
    """Build the exact aggregate shape observed in production: seven spends / 14."""
    conn = db.connect(path)
    conn.execute(
        """INSERT INTO zoominfo_credit_periods
             (period,credit_limit,consumed,updated_at)
           VALUES ('2026-08',1000,14,'2026-08-12T00:00:00+00:00')"""
    )
    conn.executemany(
        """INSERT INTO zoominfo_credit_spends
             (id,period,request_key,requested_by,lead_id,reserved_credits,
              billed_credits,state,started_at,finished_at,error)
           VALUES (?,'2026-08',?,?,?,2,2,'settled',?,?,NULL)""",
        [
            (
                f"spend-{index}",
                f"request-{index}",
                f"U{index}",
                index,
                f"2026-08-{index:02d}T00:00:00+00:00",
                f"2026-08-{index:02d}T00:01:00+00:00",
            )
            for index in range(1, 8)
        ],
    )
    conn.commit()
    return conn


def _legacy_spends(
    path: Path,
    rows: tuple[tuple[str, str, int], ...],
    *,
    limit: int = 1000,
) -> sqlite3.Connection:
    """Build one reconciled legacy ledger from id/key/billed tuples."""
    conn = db.connect(path)
    consumed = sum(row[2] for row in rows)
    conn.execute(
        """INSERT INTO zoominfo_credit_periods
             (period,credit_limit,consumed,updated_at)
           VALUES ('2026-08',?,?,?)""",
        (limit, consumed, "2026-08-12T00:00:00+00:00"),
    )
    conn.executemany(
        """INSERT INTO zoominfo_credit_spends
             (id,period,request_key,requested_by,lead_id,reserved_credits,
              billed_credits,state,started_at,finished_at,error)
           VALUES (?,'2026-08',?,'U',1,?,?,'settled',?,?,NULL)""",
        [
            (
                spend_id,
                request_key,
                billed,
                billed,
                "2026-08-12T00:00:00+00:00",
                "2026-08-12T00:01:00+00:00",
            )
            for spend_id, request_key, billed in rows
        ],
    )
    conn.commit()
    return conn


def test_migration_preserves_the_period_and_all_spend_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cutover retains IDs, request keys, states, and the observed 14/1000 balance."""
    source = _settled_legacy_ledger(tmp_path / "app.db")
    destination = tmp_path / "private" / "zoominfo-credit-ledger.db"

    summary = credits.migrate_legacy_ledger(source, destination)

    assert summary == credits.LedgerMigrationSummary(
        periods=1,
        spends=7,
        consumed_credits=14,
        reserved_credits=14,
        billed_credits=14,
    )
    assert destination.stat().st_mode & 0o077 == 0
    monkeypatch.setenv("ZOOMINFO_CREDIT_LEDGER_PATH", str(destination))
    monkeypatch.setenv("ZOOMINFO_MONTHLY_CREDITS", "1000")
    assert credits.usage(source, "2026-08") == (14, 1000)
    ledger = credits.connect_ledger()
    try:
        assert [
            tuple(row)
            for row in ledger.execute(
                "SELECT id,request_key,state,reserved_credits,billed_credits "
                "FROM zoominfo_credit_spends ORDER BY id"
            )
        ] == [
            (f"spend-{index}", f"request-{index}", "settled", 2, 2)
            for index in range(1, 8)
        ]
        assert ledger.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        ledger.close()
        source.close()


def test_exact_migration_is_idempotent_but_a_different_target_is_refused(
    tmp_path: Path,
) -> None:
    """A retry may verify the same copy; it may never merge divergent histories."""
    source = _settled_legacy_ledger(tmp_path / "app.db")
    destination = tmp_path / "ledger.db"
    credits.migrate_legacy_ledger(source, destination)

    repeated = credits.migrate_legacy_ledger(source, destination)
    assert repeated.already_migrated is True

    target = sqlite3.connect(destination)
    target.execute(
        "UPDATE zoominfo_credit_spends SET request_key='different' WHERE id='spend-1'"
    )
    target.commit()
    target.close()
    with pytest.raises(credits.LedgerMigrationError, match="different ZoomInfo"):
        credits.migrate_legacy_ledger(source, destination)
    source.close()


def test_production_and_laptop_histories_reconcile_to_known_total(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The observed 14 production + 3 local credits become one 17-credit truth."""
    production = _settled_legacy_ledger(tmp_path / "production.db")
    laptop = _legacy_spends(
        tmp_path / "laptop.db",
        (("local-1", "request-1", 1), ("local-2", "local-request-2", 2)),
    )
    destination = tmp_path / "combined.db"
    sources = (
        credits.LegacyLedgerSource("production", production),
        credits.LegacyLedgerSource("laptop", laptop),
    )

    summary = credits.migrate_legacy_ledgers(sources, destination)

    assert summary.sources == 2
    assert (summary.spends, summary.consumed_credits) == (9, 17)
    monkeypatch.setenv("ZOOMINFO_CREDIT_LEDGER_PATH", str(destination))
    assert credits.usage(production, "2026-08") == (17, 1000)
    ledger = credits.connect_ledger()
    same_local_key = ledger.execute(
        """SELECT id,request_key,legacy_request_key,source_scope
             FROM zoominfo_credit_spends
            WHERE legacy_request_key='request-1' ORDER BY id"""
    ).fetchall()
    assert len(same_local_key) == 2
    assert len({row["request_key"] for row in same_local_key}) == 2
    assert {row["source_scope"] for row in same_local_key} == {"production", "laptop"}
    ledger.close()
    production.close()
    laptop.close()


def test_multi_source_merge_is_order_independent_and_idempotent(
    tmp_path: Path,
) -> None:
    """Source enumeration order cannot change tuples or make a rerun diverge."""
    first = _legacy_spends(tmp_path / "a.db", (("a", "same-key", 2),))
    second = _legacy_spends(tmp_path / "b.db", (("b", "same-key", 3),))
    forward = (
        credits.LegacyLedgerSource("scope-a", first),
        credits.LegacyLedgerSource("scope-b", second),
    )
    reverse = tuple(reversed(forward))
    left = tmp_path / "left.db"
    right = tmp_path / "right.db"

    credits.migrate_legacy_ledgers(forward, left)
    credits.migrate_legacy_ledgers(reverse, right)
    left_db = sqlite3.connect(left)
    right_db = sqlite3.connect(right)
    for table in ("zoominfo_credit_periods", "zoominfo_credit_spends"):
        assert left_db.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall() == (
            right_db.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
        )
    left_db.close()
    right_db.close()
    assert credits.migrate_legacy_ledgers(forward, left).already_migrated is True
    first.close()
    second.close()


def test_exact_clone_deduplicates_but_conflicting_id_refuses(tmp_path: Path) -> None:
    """A copied database counts once; reused spend identity with new facts is fatal."""
    original = _settled_legacy_ledger(tmp_path / "original.db")
    clone = sqlite3.connect(tmp_path / "clone.db")
    original.backup(clone)
    clone.row_factory = sqlite3.Row
    sources = (
        credits.LegacyLedgerSource("original", original),
        credits.LegacyLedgerSource("clone", clone),
    )
    summary = credits.migrate_legacy_ledgers(sources, tmp_path / "dedup.db")
    assert (summary.spends, summary.consumed_credits) == (7, 14)

    clone.execute(
        "UPDATE zoominfo_credit_spends SET request_key='conflict' WHERE id='spend-1'"
    )
    clone.commit()
    with pytest.raises(credits.LedgerMigrationError, match="spend id.*conflicting"):
        credits.migrate_legacy_ledgers(sources, tmp_path / "conflict.db")
    original.close()
    clone.close()


def test_multi_source_limit_mismatch_and_combined_overdraw_are_refused(
    tmp_path: Path,
) -> None:
    """Reconciliation never chooses a cap or publishes a history beyond that cap."""
    first = _legacy_spends(tmp_path / "a.db", (("a", "a", 600),), limit=1000)
    mismatch = _legacy_spends(tmp_path / "m.db", (("m", "m", 1),), limit=999)
    sources = (
        credits.LegacyLedgerSource("first", first),
        credits.LegacyLedgerSource("mismatch", mismatch),
    )
    with pytest.raises(
        credits.LedgerMigrationError, match="conflicting account limits"
    ):
        credits.migrate_legacy_ledgers(sources, tmp_path / "mismatch-ledger.db")

    second = _legacy_spends(tmp_path / "b.db", (("b", "b", 600),), limit=1000)
    overdraw = (
        credits.LegacyLedgerSource("first", first),
        credits.LegacyLedgerSource("second", second),
    )
    with pytest.raises(credits.LedgerMigrationError, match="exceeds its account limit"):
        credits.migrate_legacy_ledgers(overdraw, tmp_path / "overdraw-ledger.db")
    first.close()
    mismatch.close()
    second.close()


@pytest.mark.parametrize("state", ["reserved", "indeterminate"])
def test_unsettled_legacy_spend_blocks_cutover(tmp_path: Path, state: str) -> None:
    """An in-flight or ambiguous call requires reconciliation before migration."""
    source = db.connect(tmp_path / "app.db")
    source.execute(
        """INSERT INTO zoominfo_credit_periods
             (period,credit_limit,consumed,updated_at)
           VALUES ('2026-08',10,1,'now')"""
    )
    source.execute(
        """INSERT INTO zoominfo_credit_spends
             (id,period,request_key,reserved_credits,state,started_at)
           VALUES ('s','2026-08','r',1,?,'now')""",
        (state,),
    )
    source.commit()
    destination = tmp_path / "ledger.db"

    with pytest.raises(credits.LedgerMigrationError, match="unsettled"):
        credits.migrate_legacy_ledger(source, destination)

    assert not destination.exists()
    source.close()


def test_runtime_refuses_to_initialize_a_missing_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo or skipped migration cannot reset visible account usage to zero."""
    path = tmp_path / "missing.db"
    monkeypatch.setenv("ZOOMINFO_CREDIT_LEDGER_PATH", str(path))
    with pytest.raises(credits.BudgetNotConfigured, match="does not exist"):
        credits.connect_ledger()
    assert not path.exists()


def test_migration_never_overwrites_a_destination_that_appears_mid_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent authority wins; migration refuses instead of replacing it."""
    source = _settled_legacy_ledger(tmp_path / "app.db")
    destination = tmp_path / "ledger.db"
    real_link = os.link

    def _raced_link(
        source_path: str | Path,
        destination_path: str | Path,
        *,
        follow_symlinks: bool = True,
    ) -> None:
        """Create a sentinel target immediately before the atomic publish."""
        Path(destination_path).write_bytes(b"concurrent-ledger")
        real_link(
            source_path,
            destination_path,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(credits.os, "link", _raced_link)
    with pytest.raises(credits.LedgerMigrationError, match="refusing to replace"):
        credits.migrate_legacy_ledger(source, destination)

    assert destination.read_bytes() == b"concurrent-ledger"
    source.close()


def test_migration_holds_source_write_lock_through_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A legacy spender cannot change the source snapshot while it is published."""
    source_path = tmp_path / "app.db"
    source = _settled_legacy_ledger(source_path)
    destination = tmp_path / "ledger.db"
    real_write = zoominfo_ledger_state._write_new
    writer_result: list[str] = []

    def write_while_locked(
        target: Path,
        periods: tuple[tuple[object, ...], ...],
        spends: tuple[tuple[object, ...], ...],
    ) -> None:
        """Try one competing legacy spend after migration acquired its lock."""

        def legacy_writer() -> None:
            """Use a short timeout so the test proves refusal, not thread timing."""
            competing = sqlite3.connect(source_path, timeout=0.05)
            try:
                competing.execute(
                    "UPDATE zoominfo_credit_periods SET consumed=15 "
                    "WHERE period='2026-08'"
                )
                competing.commit()
            except sqlite3.OperationalError as exc:
                writer_result.append(str(exc))
            finally:
                competing.close()

        thread = threading.Thread(target=legacy_writer)
        thread.start()
        thread.join(timeout=2.0)
        assert not thread.is_alive()
        real_write(target, periods, spends)

    monkeypatch.setattr(zoominfo_ledger_state, "_write_new", write_while_locked)
    summary = credits.migrate_legacy_ledger(source, destination)

    assert summary.consumed_credits == 14
    assert writer_result and "locked" in writer_result[0].lower()
    assert (
        source.execute(
            "SELECT consumed FROM zoominfo_credit_periods WHERE period='2026-08'"
        ).fetchone()[0]
        == 14
    )
    copied = sqlite3.connect(destination)
    assert (
        copied.execute(
            "SELECT consumed FROM zoominfo_credit_periods WHERE period='2026-08'"
        ).fetchone()[0]
        == 14
    )
    copied.close()
    source.close()


def test_execution_refuses_a_readonly_source_that_cannot_fence_writers(
    tmp_path: Path,
) -> None:
    """A copy cannot claim atomicity when its source connection cannot lock writes."""
    source_path = tmp_path / "app.db"
    _settled_legacy_ledger(source_path).close()
    source = db.connect_readonly(source_path)
    destination = tmp_path / "ledger.db"

    with pytest.raises(credits.LedgerMigrationError, match="lock every source"):
        credits.migrate_legacy_ledger(source, destination)

    assert not destination.exists()
    source.close()


def test_failure_to_lock_later_source_releases_all_and_publishes_nothing(
    tmp_path: Path,
) -> None:
    """Every accessible history must be fenced or the entire merge is abandoned."""
    first = _legacy_spends(tmp_path / "a.db", (("a", "a", 1),))
    second = _legacy_spends(tmp_path / "b.db", (("b", "b", 1),))
    blocker = sqlite3.connect(tmp_path / "b.db", timeout=0.05)
    blocker.execute("BEGIN IMMEDIATE")
    second.execute("PRAGMA busy_timeout=50")
    destination = tmp_path / "ledger.db"
    sources = (
        credits.LegacyLedgerSource("first", first),
        credits.LegacyLedgerSource("second", second),
    )

    with pytest.raises(credits.LedgerMigrationError, match="lock every source"):
        credits.migrate_legacy_ledgers(sources, destination)

    assert not destination.exists()
    assert first.in_transaction is False
    blocker.rollback()
    blocker.close()
    first.close()
    second.close()


def test_command_previews_without_writing_then_executes_the_validated_copy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The operational entrypoint is dry by default and reports safe aggregates."""
    source = _settled_legacy_ledger(tmp_path / "app.db")
    source.close()
    destination = tmp_path / "ledger.db"

    assert (
        zoominfo_ledger_migration.run(tmp_path / "app.db", destination, execute=False)
        == 0
    )
    assert (
        "preview: validated: sources=1, periods=1, spends=7, consumed=14"
        in capsys.readouterr().out
    )
    assert not destination.exists()

    assert (
        zoominfo_ledger_migration.run(tmp_path / "app.db", destination, execute=True)
        == 0
    )
    assert destination.exists()
