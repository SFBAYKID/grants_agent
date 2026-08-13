"""Vendor data must never be laundered into looking page-verified.

Grant's contact truth model is finder's verbatim gate: an email is trusted only when
it was seen on the organization's own page. Licensed data cannot pass that gate, and
the whole risk is that it gets written with the wrong status and then flows onward —
into the Persequor brief that emails a real school administrator, into a Salesforce
person Lead, into a rich card.

The other invariant here is do-not-call. Every consumer of contacts.phone treats that
column as dialable, so a flagged number is dropped at the boundary rather than
"kept for reference".
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from grant_watch import db
from grant_watch.enrich import zoominfo, zoominfo_credits, zoominfo_enrichment
from tests.paid_provider_support import configure_zoominfo_runtime


@pytest.fixture(autouse=True)
def _shared_account_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Use one explicit account ledger per test, separate from every app DB."""
    configure_zoominfo_runtime(tmp_path, monkeypatch, limit=50)


def _lead(tmp_path: Path) -> tuple[sqlite3.Connection, int]:
    """One lead to enrich against."""
    conn = db.connect(tmp_path / "z.db")
    conn.execute(
        "INSERT INTO leads (source,source_item_id,entity_name,state,detail_url) "
        "VALUES ('usaspending:16.071','A','Imperial Unified School District','CA','u')"
    )
    conn.commit()
    return conn, int(conn.execute("SELECT id FROM leads").fetchone()["id"])


def _detail(
    *, person_id: str = "1", dnc: bool = False, status: str = "FULL_MATCH"
) -> zoominfo.ZoomInfoContactDetail:
    """Build one enrich result row."""
    return zoominfo.ZoomInfoContactDetail(
        person_id=person_id,
        first_name="Dana",
        last_name="Reyes",
        job_title="Director of Technology",
        company_name="Imperial USD",
        email="dreyes@imperial.test",
        direct_phone="760-555-0100",
        mobile_phone="760-555-0199",
        direct_phone_do_not_call=dnc,
        mobile_phone_do_not_call=False,
        match_status=status,
    )


def test_a_vendor_contact_is_never_stored_as_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The single line that would launder purchased data into trusted evidence.

    db.save_contact takes contact_status as a parameter, so writing 'verified' here
    would make this record indistinguishable from a page-verified one everywhere
    downstream. It must land under its own status instead.
    """
    monkeypatch.setenv("ZOOMINFO_MONTHLY_CREDITS", "50")
    conn, lead_id = _lead(tmp_path)
    monkeypatch.setattr(zoominfo, "enrich_contacts", lambda ids: [_detail()])

    zoominfo_enrichment.apply_for_lead(conn, lead_id, ["1"])

    row = conn.execute("SELECT * FROM contacts WHERE lead_id=?", (lead_id,)).fetchone()
    assert row["contact_status"] == "vendor_licensed"
    assert row["contact_status"] != "verified"
    assert row["contact_provenance"] == "vendor_licensed"
    # The exact query shape every downstream consumer uses must not see it.
    verified = conn.execute(
        "SELECT COUNT(*) FROM contacts WHERE lead_id=? AND contact_status='verified'",
        (lead_id,),
    ).fetchone()[0]
    assert verified == 0
    conn.close()


def test_the_slack_enrichment_path_does_not_treat_a_vendor_row_as_a_contact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """enrich_lead_contact returns a verified contact early — never a purchased one."""
    monkeypatch.setenv("ZOOMINFO_MONTHLY_CREDITS", "50")
    conn, lead_id = _lead(tmp_path)
    monkeypatch.setattr(zoominfo, "enrich_contacts", lambda ids: [_detail()])
    zoominfo_enrichment.apply_for_lead(conn, lead_id, ["1"])

    from grant_watch.slack.contact_enrichment import (
        ContactOutcome,
        _recall_prior_outcome,
    )

    lead = db.get_lead(conn, lead_id)
    recalled = _recall_prior_outcome(conn, lead, lead_id)
    # A vendor row is neither a verified contact nor a linkedin_only person, so the
    # legacy chain must not claim it as either.
    assert recalled is None or recalled.status != "verified"
    assert isinstance(recalled, (ContactOutcome, type(None)))
    conn.close()


def test_a_do_not_call_number_is_never_stored_as_a_phone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """contacts.phone is a dialable column; a flagged number may not enter it.

    salesforce_contact_records copies contacts.phone straight into a Lead's Phone
    field, so storing it "for reference" would put it in front of an SDR to dial.
    """
    monkeypatch.setenv("ZOOMINFO_MONTHLY_CREDITS", "50")
    conn, lead_id = _lead(tmp_path)
    monkeypatch.setattr(zoominfo, "enrich_contacts", lambda ids: [_detail(dnc=True)])

    applied = zoominfo_enrichment.apply_for_lead(conn, lead_id, ["1"])

    row = conn.execute("SELECT * FROM contacts WHERE lead_id=?", (lead_id,)).fetchone()
    assert row["do_not_call"] == 1
    assert (row["phone"] or "") == ""
    assert applied.suppressed_numbers == 1
    assert "do-not-call" in applied.summary()
    conn.close()


def test_an_unmatched_record_is_neither_stored_nor_billed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A NO_MATCH is free, so the reserved credit must come back."""
    monkeypatch.setenv("ZOOMINFO_MONTHLY_CREDITS", "50")
    conn, lead_id = _lead(tmp_path)
    monkeypatch.setattr(
        zoominfo, "enrich_contacts", lambda ids: [_detail(status="NO_MATCH")]
    )

    applied = zoominfo_enrichment.apply_for_lead(conn, lead_id, ["1"])

    assert applied.stored == 0
    assert applied.billed == 0
    assert conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 0
    assert zoominfo_credits.remaining(conn) == 50
    conn.close()


def test_an_empty_approval_spends_nothing_and_calls_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Approving zero people must not authenticate, reserve, or bill."""
    monkeypatch.setenv("ZOOMINFO_MONTHLY_CREDITS", "50")
    conn, lead_id = _lead(tmp_path)

    def explode(ids: list[str]) -> list[zoominfo.ZoomInfoContactDetail]:
        """Fail if the paid endpoint is reached at all."""
        raise AssertionError("no paid call may happen for an empty approval")

    monkeypatch.setattr(zoominfo, "enrich_contacts", explode)
    applied = zoominfo_enrichment.apply_for_lead(conn, lead_id, [])
    assert applied.stored == 0
    assert zoominfo_credits.remaining(conn) == 50
    conn.close()


def test_the_preview_is_free_and_quotes_a_real_price(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The number a rep approves comes from free search, before anything is spent."""
    monkeypatch.setenv("ZOOMINFO_MONTHLY_CREDITS", "50")
    conn, lead_id = _lead(tmp_path)

    def fake_search(
        company: str, **kwargs: object
    ) -> list[zoominfo.ZoomInfoContactMatch]:
        """Return two free search hits, one flagged do-not-call."""
        return [
            zoominfo.ZoomInfoContactMatch(
                person_id=str(n),
                first_name="A",
                last_name=str(n),
                job_title="IT Director",
                company_name=company,
                has_email=True,
                has_direct_phone=False,
                has_mobile_phone=n == 1,
                has_supplemental_email=False,
                direct_phone_do_not_call=False,
                mobile_phone_do_not_call=n == 2,
            )
            for n in (1, 2)
        ]

    monkeypatch.setattr(zoominfo, "search_contacts", fake_search)
    preview = zoominfo_enrichment.preview_for_lead(conn, lead_id)

    assert preview.billable == 2
    assert preview.remaining == 50
    assert preview.affordable is True
    summary = preview.summary()
    assert "2 of your 50 remaining credits" in summary
    assert "not verified" in summary or "have not verified" in summary
    assert "1 are flagged do-not-call" in summary
    # Nothing was reserved by looking.
    assert zoominfo_credits.remaining(conn) == 50
    conn.close()


def test_a_pull_larger_than_the_budget_is_refused_before_any_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exhaustion is refused up front, not discovered part-way through a batch."""
    monkeypatch.setenv("ZOOMINFO_MONTHLY_CREDITS", "2")
    conn, lead_id = _lead(tmp_path)

    def explode(ids: list[str]) -> list[zoominfo.ZoomInfoContactDetail]:
        """Fail if the paid endpoint is reached despite an unaffordable request."""
        raise AssertionError("the vendor must not be called when the budget is short")

    monkeypatch.setattr(zoominfo, "enrich_contacts", explode)
    with pytest.raises(zoominfo_credits.BudgetExhausted):
        zoominfo_enrichment.apply_for_lead(conn, lead_id, ["1", "2", "3"])
    assert zoominfo_credits.remaining(conn) == 2
    conn.close()


def test_a_paid_pull_records_who_asked_for_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Credit spend must be attributable to a person, not just to a lead.

    requested_by was plumbed all the way from the tool into the ledger column and
    then never populated, so the first real production spend recorded an empty
    string — the ledger could account for the money but not for who asked.
    """
    monkeypatch.setenv("ZOOMINFO_MONTHLY_CREDITS", "50")
    conn, lead_id = _lead(tmp_path)
    monkeypatch.setattr(zoominfo, "enrich_contacts", lambda ids: [_detail()])

    zoominfo_enrichment.apply_for_lead(conn, lead_id, ["1"], requested_by="U0REP")

    ledger = zoominfo_credits.connect_ledger()
    row = ledger.execute("SELECT requested_by FROM zoominfo_credit_spends").fetchone()
    assert row["requested_by"] == "U0REP"
    ledger.close()
    conn.close()


def test_the_slack_tool_passes_the_requester_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gap was at the call site, so the call site is what this pins."""
    from grant_watch import db as db_module
    from grant_watch.slack import tools

    monkeypatch.setenv("ZOOMINFO_MONTHLY_CREDITS", "50")
    monkeypatch.setenv("ZOOMINFO_CLIENT_ID", "cid")
    monkeypatch.setenv("ZOOMINFO_CLIENT_SECRET", "secret")
    conn, lead_id = _lead(tmp_path)
    conn.close()
    real_connect = db_module.connect

    def redirected(db_path: object = None, *a: object, **k: object) -> object:
        """Send a bare db.connect() to the throwaway file."""
        return real_connect(tmp_path / "z.db" if db_path is None else db_path, *a, **k)

    monkeypatch.setattr(db_module, "connect", redirected)
    monkeypatch.setattr(zoominfo, "enrich_contacts", lambda ids: [_detail()])

    out, _artifact = tools.run_tool(
        "zoominfo_enrich_contacts",
        {"lead_id": lead_id, "person_ids": ["1"]},
        requester_slack="U0REP",
    )
    assert not out.startswith("ERROR"), out

    check = real_connect(tmp_path / "z.db")
    ledger = zoominfo_credits.connect_ledger()
    row = ledger.execute("SELECT requested_by FROM zoominfo_credit_spends").fetchone()
    assert row is not None and row["requested_by"] == "U0REP"
    ledger.close()
    check.close()


def test_invalid_person_id_is_refused_before_reservation_or_vendor_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A local input error must not become an indeterminate paid attempt."""
    monkeypatch.setenv("ZOOMINFO_MONTHLY_CREDITS", "50")
    conn, lead_id = _lead(tmp_path)

    def explode(ids: list[str]) -> list[zoominfo.ZoomInfoContactDetail]:
        """Prove validation runs ahead of the paid transport."""
        raise AssertionError("invalid IDs must never reach ZoomInfo")

    monkeypatch.setattr(zoominfo, "enrich_contacts", explode)
    with pytest.raises(ValueError, match="invalid ZoomInfo person ID"):
        zoominfo_enrichment.apply_for_lead(conn, lead_id, ["not-an-id"])

    assert zoominfo_credits.remaining(conn) == 50
    ledger = zoominfo_credits.connect_ledger()
    assert (
        ledger.execute("SELECT COUNT(*) FROM zoominfo_credit_spends").fetchone()[0] == 0
    )
    ledger.close()
    conn.close()


def test_duplicate_person_ids_are_billed_and_requested_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated copies in a tool payload cannot consume repeated credits."""
    monkeypatch.setenv("ZOOMINFO_MONTHLY_CREDITS", "50")
    conn, lead_id = _lead(tmp_path)
    seen: list[list[str]] = []

    def enrich(ids: list[str]) -> list[zoominfo.ZoomInfoContactDetail]:
        """Record the normalized set sent to the paid client."""
        seen.append(ids)
        return [_detail()]

    monkeypatch.setattr(zoominfo, "enrich_contacts", enrich)
    applied = zoominfo_enrichment.apply_for_lead(conn, lead_id, ["1", " 1 ", "1"])

    assert seen == [["1"]]
    assert applied.billed == 1
    assert zoominfo_credits.remaining(conn) == 49
    conn.close()
