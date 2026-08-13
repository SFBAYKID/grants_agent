"""Durable paid-call and bounded preparation-worker tests with fakes only."""

from __future__ import annotations

from pathlib import Path

import pytest
import requests

from grant_watch.campaign import contact_evidence, paid_calls, prepare_worker
from grant_watch.enrich import finder, firecrawl_gateway, salesforce_activity
from tests.contact_support import verified_contact_evidence
from tests.paid_provider_support import configure_firecrawl_runtime
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
        verified_contact_evidence(
            "Jane Doe",
            "jane@montebello.k12.ca.us",
            "https://montebello.k12.ca.us/directory",
            title="Technology Director",
        ),
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


def test_contact_retry_authority_reaches_exact_firecrawl_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator flag reopens both durable paid ledgers, never just the outer one."""
    conn = _eligible_conn(tmp_path / "firecrawl-retry.db")
    conn.execute("UPDATE contact_evidence SET expires_at='2026-07-01T00:00:00+00:00'")
    conn.commit()
    configure_firecrawl_runtime(tmp_path, monkeypatch, limit=5)
    calls = 0

    class Response:
        """Minimal successful search response for the explicit second attempt."""

        status_code = 200
        headers: dict[str, str] = {}

        def json(self) -> dict[str, object]:
            """Return a definite empty provider result."""
            return {"data": []}

    def post(*_args: object, **_kwargs: object) -> Response:
        """Lose the first response and complete the explicitly retried request."""
        nonlocal calls
        calls += 1
        if calls == 1:
            raise requests.Timeout("lost response")
        return Response()

    def lookup(_lead: object) -> contact_evidence.ContactFact | None:
        """Exercise the real gateway from inside the paid contact callback."""
        firecrawl_gateway.search("exact contact query", conn=conn)
        return None

    monkeypatch.setattr(firecrawl_gateway.requests, "post", post)
    with pytest.raises(firecrawl_gateway.FirecrawlIndeterminate):
        contact_evidence.refresh(conn, 1, finder_fn=lookup, now=NOW)
    assert (
        contact_evidence.refresh(
            conn, 1, finder_fn=lookup, retry_indeterminate=True, now=NOW
        )
        == "removed"
    )
    assert calls == 2
    ledger = firecrawl_gateway.connect_ledger()
    assert [
        tuple(row)
        for row in ledger.execute(
            """SELECT attempt_number,state FROM firecrawl_runtime_attempts
                 ORDER BY rowid"""
        )
    ] == [(1, "indeterminate"), (2, "completed")]
    ledger.close()


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


def test_worker_skips_leads_preparation_could_never_help(tmp_path: Path) -> None:
    """A blocker preparation cannot close must not consume the paid batch.

    Production 2026-08-06: 176 of 184 Gold candidates were `entity_kind_unsupported`
    (no nces_id), yet `--limit 25` by raw lead_score paid to enrich 25 of them every
    weekday. No amount of contact or website discovery can change an entity-kind
    rejection -- only the NCES binder can -- so such a lead must never be picked.
    """
    conn = _eligible_conn(tmp_path / "unhelpable.db")
    conn.execute("UPDATE leads SET nces_id=''")
    conn.commit()
    called: list[int] = []

    summary = prepare_worker.run(
        conn,
        "CGRANTS",
        dry_run=False,
        contact_finder=lambda lead: called.append(int(lead["id"])),  # type: ignore[arg-type,return-value]
        now=NOW,
    )
    assert summary.candidates == 0
    assert called == [], "a kind-rejected lead must not trigger a paid contact call"


def test_worker_discovers_a_missing_website_exactly_once(tmp_path: Path) -> None:
    """Website discovery runs for a lead that never had one, and never repeats.

    `enrich_org_profile` short-circuits only on a prior ``found``, so without the
    `_needs_website` guard a ``not_found`` lead would be re-scraped (Firecrawl +
    Anthropic) on every weekday run forever.
    """
    conn = _eligible_conn(tmp_path / "website.db")
    conn.execute(
        """UPDATE leads SET org_website='',org_profile_status='',
                  nces_website='',nces_website_status=NULL"""
    )
    conn.commit()
    attempts: list[int] = []

    def spy(_conn: object, lead_id: int) -> object:
        """Record the attempt and simulate an honest not-found outcome."""
        attempts.append(lead_id)
        conn.execute(
            "UPDATE leads SET org_profile_status='not_found' WHERE id=?", (lead_id,)
        )
        conn.commit()
        return None

    first = prepare_worker.run(
        conn,
        "CGRANTS",
        dry_run=False,
        contact_finder=lambda _lead: None,
        website_finder=spy,
        now=NOW,
    )
    assert first.website_checked == 1
    assert attempts == [1]

    prepare_worker.run(
        conn,
        "CGRANTS",
        dry_run=False,
        contact_finder=lambda _lead: None,
        website_finder=spy,
        now=NOW,
    )
    assert attempts == [1], "a recorded not_found must not be re-scraped and re-billed"


def test_worker_skips_paid_website_discovery_when_nces_verified_it(
    tmp_path: Path,
) -> None:
    """An exact NCES site closes the website gap without Firecrawl or Anthropic."""
    conn = _eligible_conn(tmp_path / "nces-site.db")
    conn.execute("UPDATE leads SET org_website='',org_profile_status=''")
    conn.commit()

    def fail(_conn: object, _lead_id: int) -> object:
        """No paid organization lookup may run behind authoritative evidence."""
        raise AssertionError("NCES-verified site triggered paid website discovery")

    summary = prepare_worker.run(
        conn,
        "CGRANTS",
        dry_run=False,
        contact_finder=lambda _lead: None,
        website_finder=fail,
        now=NOW,
    )
    assert summary.website_checked == 0
    conn.close()


def test_worker_refreshes_missing_crm_state_for_its_own_targets(
    tmp_path: Path,
) -> None:
    """Preparation must create the CRM state its own candidates are judged on.

    Nothing scheduled wrote `salesforce_lookup_state`: it stood at 0 rows in production
    while the rich card was enabled, so every candidate failed CRM_UNSAFE and no card
    could post. `salesforce_sync.sync` cannot close this -- it ranks globally and caps
    at 100 rows, and the pipeline's leads ranked 51-165 among 10,627 stale leads, so no
    --limit reached them. The worker refreshes the leads it is actually preparing.
    """
    conn = _eligible_conn(tmp_path / "crm-missing.db")
    conn.execute("DELETE FROM salesforce_lookup_state")
    conn.commit()
    refreshed: list[int] = []

    def refresh(_conn: object, lead_id: int) -> str:
        """Record the refresh and persist a no-match snapshot without network I/O."""
        refreshed.append(lead_id)
        conn.execute(
            """INSERT INTO salesforce_lookup_state(lead_id,status,error,checked_at)
               VALUES (?,'no_match',NULL,?)""",
            (lead_id, NOW.isoformat()),
        )
        conn.commit()
        return "no_match"

    summary = prepare_worker.run(
        conn,
        "CGRANTS",
        dry_run=False,
        contact_finder=lambda _lead: None,
        crm_refresh=refresh,
        now=NOW,
    )
    assert refreshed == [1]
    assert summary.crm_checked == 1


def test_worker_leaves_a_fresh_crm_snapshot_alone(tmp_path: Path) -> None:
    """A snapshot inside CRM_FRESH_HOURS must not be re-queried every weekday."""
    conn = _eligible_conn(tmp_path / "crm-fresh.db")
    conn.execute("UPDATE salesforce_lookup_state SET checked_at=?", (NOW.isoformat(),))
    conn.commit()

    # Recorded, NOT raised: the worker catches Exception per candidate, so a raising
    # stub would be swallowed and the assertion would pass against broken code too.
    # (Caught by mutation testing -- replacing the staleness guard with `if True`
    # initially still passed.)
    calls: list[int] = []

    summary = prepare_worker.run(
        conn,
        "CGRANTS",
        dry_run=False,
        contact_finder=lambda _lead: None,
        crm_refresh=lambda _conn, lead_id: (calls.append(lead_id), "no_match")[1],
        now=NOW,
    )
    assert calls == [], "a fresh CRM snapshot was re-queried"
    assert summary.crm_checked == 0
