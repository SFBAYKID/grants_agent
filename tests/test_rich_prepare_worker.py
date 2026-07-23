"""Durable paid-call and bounded preparation-worker tests with fakes only."""

from __future__ import annotations

from pathlib import Path

import pytest

from grant_watch.campaign import contact_evidence, paid_calls, prepare_worker
from grant_watch.enrich import finder, salesforce_activity
from tests.test_rich_preparation import NOW, _eligible_conn


def test_paid_marker_is_committed_before_callback(tmp_path: Path) -> None:
    """The callback observes its durable in-flight row before any simulated HTTP."""
    conn = _eligible_conn(tmp_path / "paid.db")

    def work() -> str:
        """Assert the durable marker exists before simulated paid work."""
        row = conn.execute(
            "SELECT state FROM paid_enrichment_attempts WHERE request_key='req-1'"
        ).fetchone()
        assert row["state"] == "in_flight"
        return "done"

    assert paid_calls.execute(conn, 1, "contact_refresh", "req-1", work) == "done"
    assert (
        conn.execute("SELECT state FROM paid_enrichment_attempts").fetchone()[0]
        == "completed"
    )


def test_restart_never_silently_retries_indeterminate_paid_call(tmp_path: Path) -> None:
    """An abandoned in-flight row blocks until explicit retry records indeterminate."""
    conn = _eligible_conn(tmp_path / "indeterminate.db")
    conn.execute(
        """INSERT INTO paid_enrichment_attempts
             (id,lead_id,operation,request_key,attempt_no,state,started_at)
           VALUES ('old',1,'contact_refresh','req-2',1,'in_flight',
                   '2026-07-22T00:00:00+00:00')"""
    )
    conn.commit()
    with pytest.raises(paid_calls.IndeterminatePaidCall):
        paid_calls.execute(conn, 1, "contact_refresh", "req-2", lambda: "bad")
    assert (
        paid_calls.execute(
            conn,
            1,
            "contact_refresh",
            "req-2",
            lambda: "retried",
            retry_indeterminate=True,
        )
        == "retried"
    )
    assert [
        tuple(row)
        for row in conn.execute(
            "SELECT attempt_no,state FROM paid_enrichment_attempts ORDER BY attempt_no"
        )
    ] == [(1, "indeterminate"), (2, "completed")]


def test_contact_refresh_supersedes_old_evidence_with_new_hash(tmp_path: Path) -> None:
    """A definite verified result advances the append-only freshness lifecycle."""
    conn = _eligible_conn(tmp_path / "contact.db")
    conn.execute("UPDATE contact_evidence SET expires_at='2026-07-01T00:00:00+00:00'")
    conn.commit()
    fact = contact_evidence.ContactFact(
        "named_direct",
        "Jane Doe",
        "Technology Director",
        "jane@montebello.k12.ca.us",
        "https://montebello.k12.ca.us/directory",
        "montebello.k12.ca.us",
    )
    status = contact_evidence.refresh(conn, 1, finder_fn=lambda _lead: fact, now=NOW)
    assert status == "verified"
    rows = conn.execute("SELECT * FROM contact_evidence ORDER BY rowid").fetchall()
    assert [row["status"] for row in rows] == ["superseded", "verified"]
    assert rows[-1]["evidence_hash"] and rows[-1]["email"] == fact.email


def test_source_outage_preserves_prior_contact_evidence(tmp_path: Path) -> None:
    """Unavailable is not removed/not-found and cannot erase the last known fact."""
    conn = _eligible_conn(tmp_path / "outage.db")
    conn.execute("UPDATE contact_evidence SET expires_at='2026-07-01T00:00:00+00:00'")
    conn.commit()

    def unavailable(_lead: object) -> contact_evidence.ContactFact | None:
        """Simulate an unavailable official contact page."""
        raise finder.SourceUnreachable("offline")

    with pytest.raises(finder.SourceUnreachable):
        contact_evidence.refresh(conn, 1, finder_fn=unavailable, now=NOW)
    assert (
        conn.execute("SELECT status FROM contact_evidence").fetchone()[0] == "verified"
    )
    assert (
        conn.execute("SELECT state FROM paid_enrichment_attempts").fetchone()[0]
        == "indeterminate"
    )


def test_paid_timeout_requires_explicit_retry(tmp_path: Path) -> None:
    """A callback timeout stays indeterminate and never retries on the next run."""
    conn = _eligible_conn(tmp_path / "timeout.db")
    calls = 0

    def timeout() -> str:
        """Model an exception after a provider might have accepted the request."""
        nonlocal calls
        calls += 1
        raise TimeoutError("provider result unknown")

    with pytest.raises(TimeoutError):
        paid_calls.execute(conn, 1, "contact_refresh", "req-timeout", timeout)
    with pytest.raises(paid_calls.IndeterminatePaidCall):
        paid_calls.execute(conn, 1, "contact_refresh", "req-timeout", timeout)
    assert calls == 1
    assert (
        paid_calls.execute(
            conn,
            1,
            "contact_refresh",
            "req-timeout",
            lambda: "operator-retried",
            retry_indeterminate=True,
        )
        == "operator-retried"
    )


def test_worker_preview_does_no_calls_or_writes(tmp_path: Path) -> None:
    """Default-safe preparation mode only reports the bounded batch."""
    conn = _eligible_conn(tmp_path / "preview.db")
    before = conn.total_changes

    def forbidden(_lead: object) -> contact_evidence.ContactFact | None:
        """Fail if preview mode invokes the paid finder."""
        raise AssertionError("preview called paid finder")

    summary = prepare_worker.run(
        conn, "CGRANTS", dry_run=True, contact_finder=forbidden, now=NOW
    )
    assert summary.candidates == 1 and summary.writes == 0
    assert conn.total_changes == before


def test_completed_not_found_attempt_is_not_counted_contact_fresh(
    tmp_path: Path,
) -> None:
    """Paid-call completion proves execution, not that usable contact evidence exists."""
    conn = _eligible_conn(tmp_path / "completed-miss.db")
    conn.execute("DELETE FROM contact_evidence")
    conn.execute(
        """INSERT INTO paid_enrichment_attempts
             (id,lead_id,operation,request_key,attempt_no,state,started_at,finished_at)
           VALUES ('done',1,'contact_refresh','rich-contact:1:2026-07-22',1,'completed',
                   '2026-07-22T00:00:00+00:00','2026-07-22T00:01:00+00:00')"""
    )
    conn.commit()
    summary = prepare_worker.run(conn, "CGRANTS", dry_run=False, now=NOW)
    assert summary.contact_fresh == 0


def test_worker_persists_fake_readonly_activity_for_exact_account(
    tmp_path: Path,
) -> None:
    """Exact Account/person binding feeds one typed local activity snapshot."""
    conn = _eligible_conn(tmp_path / "activity.db")
    conn.execute("UPDATE salesforce_lookup_state SET status='found'")
    conn.executemany(
        """INSERT INTO salesforce_matches
             (lead_id,sobject,record_id,name,owner,link,confidence,account_id,
              checked_at,owner_id,owner_email)
           VALUES (1,?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                "Account",
                "001000000000001AAA",
                "Montebello USD",
                "Anthony",
                "https://sf.test/account",
                "high",
                None,
                NOW.isoformat(),
                "005000000000001AAA",
                "anthony@monarchconnected.com",
            ),
            (
                "Contact",
                "003000000000001AAA",
                "Jon Smith",
                "Anthony",
                "https://sf.test/contact",
                "high",
                "001000000000001AAA",
                NOW.isoformat(),
                "005000000000001AAA",
                "anthony@monarchconnected.com",
            ),
        ],
    )
    conn.commit()

    def no_call(
        account: str, people: frozenset[str]
    ) -> salesforce_activity.ActivityEvidence:
        """Return a deterministic exact-account no-call result."""
        assert account == "001000000000001AAA"
        assert people == frozenset({"003000000000001AAA"})
        return salesforce_activity.ActivityEvidence(
            salesforce_activity.ActivityStatus.NO_RECENT_CALL, NOW
        )

    summary = prepare_worker.run(
        conn,
        "CGRANTS",
        dry_run=False,
        contact_finder=lambda _lead: None,
        activity_lookup=no_call,
        now=NOW,
    )
    assert summary.contact_fresh == 1
    assert summary.activity_checked == 1
    assert (
        conn.execute("SELECT status FROM salesforce_activity_snapshots").fetchone()[0]
        == "no_recent_call"
    )
