"""The ZoomInfo spend gate: nothing may be billed that a human did not approve.

ZoomInfo bills the ACCOUNT, so the dangerous case is two reps in two Slack threads
spending the same remaining credits at the same moment. The authorization here is a
conditional UPDATE whose rowcount is the permission, and these tests drive that
directly rather than trusting the arithmetic around it.

The other invariant under test is which direction the ledger errs. An attempt that
may have billed stays counted; credits come back only when the vendor's own response
proves fewer records were billable, or when the call provably never happened.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from grant_watch import db
from grant_watch.enrich import zoominfo_credits as credits
from tests.paid_provider_support import configure_zoominfo_runtime


@pytest.fixture()
def conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    """A migrated database with a 10-credit monthly ceiling configured."""
    configure_zoominfo_runtime(tmp_path, monkeypatch, limit=10)
    return db.connect(tmp_path / "credits.db")


def test_no_spend_is_authorized_without_a_configured_limit(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unset budget refuses outright rather than defaulting to "unlimited"."""
    monkeypatch.delenv("ZOOMINFO_MONTHLY_CREDITS", raising=False)
    with pytest.raises(credits.BudgetNotConfigured):
        credits.reserve(conn, request_key="k", credits=1)


def test_no_spend_is_authorized_without_a_shared_ledger_path(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A local app DB must not silently become an account-wide credit ledger."""
    monkeypatch.delenv("ZOOMINFO_CREDIT_LEDGER_PATH", raising=False)
    with pytest.raises(credits.BudgetNotConfigured, match="LEDGER_PATH"):
        credits.reserve(conn, request_key="k", credits=1)


def test_a_reservation_consumes_the_whole_approved_quantity(
    conn: sqlite3.Connection,
) -> None:
    """The number a rep approves is claimed at once, not drawn down row by row."""
    credits.reserve(conn, request_key="pull-1", credits=4)
    assert credits.remaining(conn) == 6
    consumed, limit = credits.usage(conn)
    assert (consumed, limit) == (4, 10)


def test_the_pool_cannot_be_overdrawn(conn: sqlite3.Connection) -> None:
    """A pull larger than the remainder is refused entirely — never partially.

    A partial reservation would make "this will use 9" false the moment row 6
    exhausted the pool, leaving a rep who approved a set with an arbitrary prefix.
    """
    credits.reserve(conn, request_key="pull-1", credits=6)
    with pytest.raises(credits.BudgetExhausted) as excinfo:
        credits.reserve(conn, request_key="pull-2", credits=6)
    assert "only 4 remain" in str(excinfo.value)
    # The refused pull consumed nothing.
    assert credits.remaining(conn) == 4


def test_two_concurrent_connections_cannot_both_win_the_last_credits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real hazard: two Slack threads reading the same balance at once.

    Both connections are opened before either reserves, so both observe a full pool.
    Exactly one may succeed — the conditional UPDATE, not the read, is what grants
    permission.
    """
    configure_zoominfo_runtime(tmp_path, monkeypatch, limit=10)
    path = tmp_path / "race.db"
    first = db.connect(path)
    second = db.connect(path)
    second.execute("PRAGMA busy_timeout=5000")

    assert credits.remaining(first) == 10
    assert credits.remaining(second) == 10

    credits.reserve(first, request_key="thread-a", credits=8)
    with pytest.raises(credits.BudgetExhausted):
        credits.reserve(second, request_key="thread-b", credits=8)

    assert credits.remaining(first) == 2
    assert credits.remaining(second) == 2
    first.close()
    second.close()


def test_settling_refunds_only_what_the_vendor_proved_was_free(
    conn: sqlite3.Connection,
) -> None:
    """A NO_MATCH costs nothing, so an unmatched row gives its credit back."""
    spend_id = credits.reserve(conn, request_key="pull-1", credits=9)
    assert credits.remaining(conn) == 1
    credits.settle(conn, spend_id, billed=5)
    assert credits.remaining(conn) == 5
    ledger = credits.connect_ledger()
    row = ledger.execute(
        "SELECT state,billed_credits FROM zoominfo_credit_spends WHERE id=?",
        (spend_id,),
    ).fetchone()
    assert (row["state"], row["billed_credits"]) == ("settled", 5)
    ledger.close()


def test_repeated_settlement_is_idempotent_and_cannot_manufacture_credits(
    conn: sqlite3.Connection,
) -> None:
    """Replaying reconciliation never applies the same refund twice."""
    spend_id = credits.reserve(conn, request_key="pull-idempotent", credits=9)
    credits.settle(conn, spend_id, billed=5)
    credits.settle(conn, spend_id, billed=5)
    assert credits.usage(conn) == (5, 10)
    with pytest.raises(ValueError, match="cannot be reconciled twice"):
        credits.settle(conn, spend_id, billed=4)
    assert credits.usage(conn) == (5, 10)


def test_settlement_cannot_bill_beyond_the_approved_set(
    conn: sqlite3.Connection,
) -> None:
    """A malformed vendor count stays reserved instead of exceeding consent."""
    spend_id = credits.reserve(conn, request_key="pull-overbilled", credits=2)
    with pytest.raises(ValueError, match="approved reservation"):
        credits.settle(conn, spend_id, billed=3)
    assert credits.usage(conn) == (2, 10)
    ledger = credits.connect_ledger()
    try:
        assert (
            ledger.execute(
                "SELECT state FROM zoominfo_credit_spends WHERE id=?", (spend_id,)
            ).fetchone()["state"]
            == "reserved"
        )
    finally:
        ledger.close()


def test_an_indeterminate_attempt_stays_counted_against_the_budget(
    conn: sqlite3.Connection,
) -> None:
    """A timeout is not evidence the vendor did nothing.

    Refunding here would let the same pull be retried into a genuine double-spend.
    Overstating spend costs headroom; understating it overdraws a shared resource.
    """
    spend_id = credits.reserve(conn, request_key="pull-1", credits=6)
    credits.mark_indeterminate(conn, spend_id, "ReadTimeout")
    assert credits.remaining(conn) == 4
    ledger = credits.connect_ledger()
    assert (
        ledger.execute(
            "SELECT state FROM zoominfo_credit_spends WHERE id=?", (spend_id,)
        ).fetchone()["state"]
        == "indeterminate"
    )
    ledger.close()


def test_a_completed_pull_cannot_be_repeated(conn: sqlite3.Connection) -> None:
    """Idempotency by request key: the same bounded pull bills at most once."""
    spend_id = credits.reserve(conn, request_key="pull-1", credits=2)
    credits.settle(conn, spend_id, billed=2)
    with pytest.raises(credits.AlreadySpent):
        credits.reserve(conn, request_key="pull-1", credits=2)


def test_an_unreconciled_pull_blocks_a_blind_retry(conn: sqlite3.Connection) -> None:
    """An indeterminate key demands an operator decision, not an automatic retry."""
    spend_id = credits.reserve(conn, request_key="pull-1", credits=2)
    credits.mark_indeterminate(conn, spend_id, "ReadTimeout")
    with pytest.raises(credits.SpendIndeterminate):
        credits.reserve(conn, request_key="pull-1", credits=2)


def test_release_returns_credits_only_for_a_call_that_never_happened(
    conn: sqlite3.Connection,
) -> None:
    """A refusal raised before any HTTP call gives the whole reservation back."""
    spend_id = credits.reserve(conn, request_key="pull-1", credits=7)
    credits.release(conn, spend_id, "refused before request")
    assert credits.remaining(conn) == 10
    # Releasing twice must not manufacture credits.
    credits.release(conn, spend_id, "again")
    assert credits.remaining(conn) == 10


def test_two_application_databases_share_one_vendor_account_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The authorization boundary is the vendor account, not an app DB file."""
    configure_zoominfo_runtime(tmp_path, monkeypatch, limit=10)
    first = db.connect(tmp_path / "app-a.db")
    second = db.connect(tmp_path / "app-b.db")

    credits.reserve(first, request_key="app-a", credits=8)
    assert credits.remaining(second) == 2
    with pytest.raises(credits.BudgetExhausted):
        credits.reserve(second, request_key="app-b", credits=3)

    first.close()
    second.close()


def test_spend_wrapper_settles_on_success_and_holds_on_failure(
    conn: sqlite3.Connection,
) -> None:
    """The wrapper is the only path callers should use; both endings are covered."""
    result = credits.spend(
        conn,
        request_key="ok",
        credits=4,
        work=lambda: (["a", "b"], 2),
    )
    assert result == ["a", "b"]
    assert credits.remaining(conn) == 8  # 4 reserved, 2 billed, 2 refunded

    def boom() -> tuple[list[str], int]:
        """Fail after the reservation the way a vendor timeout would."""
        raise TimeoutError("vendor timeout")

    with pytest.raises(TimeoutError):
        credits.spend(conn, request_key="bad", credits=3, work=boom)
    assert credits.remaining(conn) == 5  # the ambiguous 3 stay counted


def test_migration_backfills_provenance_from_existing_status(
    tmp_path: Path,
) -> None:
    """Existing contacts gain explicit provenance without any new claim.

    A page-verified row and a LinkedIn row already implied their evidence class; the
    backfill only makes that machine-checkable. Rows carrying no contact fact stay
    NULL rather than being assigned a provenance they never had.
    """
    from grant_watch import migrations

    conn = sqlite3.connect(tmp_path / "p.db")
    conn.row_factory = sqlite3.Row
    migrations._run_migrations(
        conn,
        migrations.MIGRATIONS[:28],
        lambda: "2026-08-01T00:00:00+00:00",
    )
    conn.execute(
        "INSERT INTO leads (source,source_item_id,entity_name,detail_url) "
        "VALUES ('s','1','X','u')"
    )
    lead_id = int(conn.execute("SELECT id FROM leads").fetchone()["id"])
    for status in ("verified", "linkedin_only", "not_found"):
        conn.execute(
            "INSERT INTO contacts (lead_id,name,contact_status) VALUES (?,?,?)",
            (lead_id, f"p-{status}", status),
        )
    conn.commit()
    # Apply the historical migration at its real boundary, before v46 quarantine.
    from grant_watch.migrations_zoominfo import migration_29_vendor_contacts_and_credits

    migration_29_vendor_contacts_and_credits(conn)
    conn.commit()
    rows = {
        r["contact_status"]: r["contact_provenance"]
        for r in conn.execute("SELECT contact_status,contact_provenance FROM contacts")
    }
    assert rows["verified"] == "page_verified"
    assert rows["linkedin_only"] == "linkedin_claimed"
    assert rows["not_found"] is None
    conn.close()


def test_provenance_column_rejects_an_invented_evidence_class(
    tmp_path: Path,
) -> None:
    """The CHECK constraint stops a typo becoming a fourth kind of evidence."""
    conn = db.connect(tmp_path / "c.db")
    conn.execute(
        "INSERT INTO leads (source,source_item_id,entity_name,detail_url) "
        "VALUES ('s','1','X','u')"
    )
    lead_id = int(conn.execute("SELECT id FROM leads").fetchone()["id"])
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO contacts (lead_id,name,contact_provenance) VALUES (?,?,?)",
            (lead_id, "bogus", "page_verifed"),  # deliberate typo
        )
    conn.close()
