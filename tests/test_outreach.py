"""Phase-2 + Persequor-client tests: the anti-hallucination gate, the rep roster,
and the test-mode brief. All offline."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import requests

from grant_watch import db, persequor_client
from grant_watch.enrich.finder import verify_on_page
from grant_watch.enrich.salesforce import SFMatch, distinctive_term
from grant_watch.models import Lead, LeadGrade, RawItem

PAGE = """# Castle Rock School District — Staff Directory
Superintendent: Dr. Jane Doe — jdoe@crschools.org — (360) 555-0100
Technology Director: Sam Smith — ssmith@crschools.org
"""


# ------------------------------------------------------------ the gate
def test_gate_accepts_verbatim_email_and_name() -> None:
    """Verify gate accepts verbatim email and name."""
    assert verify_on_page(PAGE, "jdoe@crschools.org", "Jane Doe")


def test_gate_rejects_email_not_on_page() -> None:
    # The classic hallucination: plausible address, never fetched.
    """Verify gate rejects email not on page."""
    assert not verify_on_page(PAGE, "jane.doe@crschools.org", "Jane Doe")


def test_gate_rejects_name_not_on_page() -> None:
    """Verify gate rejects name not on page."""
    assert not verify_on_page(PAGE, "jdoe@crschools.org", "John Roe")


def test_gate_rejects_malformed_email() -> None:
    """Verify gate rejects malformed email."""
    assert not verify_on_page(PAGE, "not-an-email", "Jane Doe")


# ------------------------------------------------------------ salesforce (offline bits)
def test_distinctive_term_strips_punct_and_generic_words() -> None:
    # SOSL chokes on punctuation; and dropping generic org words lets name
    # variations match (ABC Schools <-> ABC School District).
    """Verify distinctive term strips punct and generic words."""
    term = distinctive_term("Mt. Morris Consolidated Schools (401) & District")
    assert "(" not in term and "&" not in term and "." not in term
    assert "Morris" in term
    # generic words removed so variations still match
    for generic in ("Consolidated", "Schools", "District"):
        assert generic not in term


def test_distinctive_term_variation_matching() -> None:
    # "ABC Schools" and "ABC School District" must reduce to the same distinctive term.
    """Verify distinctive term variation matching."""
    assert distinctive_term("ABC Schools") == distinctive_term("ABC School District")


def test_sf_match_carries_lightning_link() -> None:
    """Verify sf match carries lightning link."""
    m = SFMatch(
        sobject="Account",
        record_id="001x",
        name="Monarch",
        company="",
        owner="Chase",
        link="https://x/lightning/r/Account/001x/view",
        confidence="high",
    )
    assert "/lightning/r/Account/001x/view" in m.link


# ------------------------------------------------------------ roster
def test_rep_roster_derives_send_as() -> None:
    """Verify rep roster derives send as."""
    assert persequor_client.rep_email_for("U01DPJVURHU") == "chase@monarchconnected.com"
    assert persequor_client.rep_email_for("U_NOT_A_REP") is None


# ------------------------------------------------------------ brief
def _lead_row(tmp_path: Path) -> tuple[sqlite3.Connection, sqlite3.Row]:
    """Create one award row for outreach and contact-gate tests."""
    conn = db.connect(tmp_path / "t.db")
    db.upsert_lead(
        conn,
        Lead(
            item=RawItem(
                source="usaspending:16.071",
                item_id="A1",
                title="SVPP",
                entity="Castle Rock School District 401",
                state="WA",
                program="SVPP",
                amount=500_000.0,
                start="2025-10-01",
                end="2028-09-30",
                url="https://x.gov/a",
                raw={},
            ),
            grade=LeadGrade.GOLD,
        ),
    )
    row = db.get_lead(conn, 1)
    assert row is not None
    return conn, row


def test_brief_test_mode_overrides_recipient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify brief test mode overrides recipient."""
    monkeypatch.setenv("OUTREACH_TEST_EMAIL", "chase@monarchconnected.com")
    conn, row = _lead_row(tmp_path)
    cid = db.save_contact(
        conn,
        row["id"],
        "Jane Doe",
        "Superintendent",
        "jdoe@crschools.org",
        "",
        "https://crschools.org/staff",
        "high",
    )
    contact = conn.execute("SELECT * FROM contacts WHERE id=?", (cid,)).fetchone()
    brief = persequor_client.build_brief(
        row, contact, "U01DPJVURHU", "chase@monarchconnected.com"
    )
    assert brief is not None
    assert brief["contact_email"] == "chase@monarchconnected.com"  # test override
    assert "jdoe@crschools.org" in brief["rep_notes"]  # truth preserved
    assert "TEST MODE" in brief["rep_notes"]
    assert brief["amount_usd"] == 500000
    assert brief["expires_at"] == "2028-09-30"
    assert brief["request_id"].startswith(f"grant-{row['id']}-")


def test_brief_strips_honorific_from_contact_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The honorific never reaches contact_name, so Persequor greets by first name.

    Live 2026-07-18: a site listing 'Mr. Joel Padgett' produced a 'Hi Mr.,' draft."""
    monkeypatch.setenv("OUTREACH_TEST_EMAIL", "chase@monarchconnected.com")
    conn, row = _lead_row(tmp_path)
    cid = db.save_contact(
        conn,
        row["id"],
        "Mr. Joel Padgett",
        "Director of Technology",
        "joel.padgett@x.org",
        "",
        "https://x.org/staff",
        "high",
    )
    contact = conn.execute("SELECT * FROM contacts WHERE id=?", (cid,)).fetchone()
    brief = persequor_client.build_brief(
        row, contact, "U01DPJVURHU", "chase@monarchconnected.com"
    )
    assert brief is not None
    assert brief["contact_name"] == "Joel Padgett"


def test_brief_uses_current_event_source_not_stale_projection(tmp_path: Path) -> None:
    """Persequor receives the exact event record URL used by Grant's detail reply."""
    conn, row = _lead_row(tmp_path)
    conn.execute("UPDATE leads SET detail_url='https://generic.example/dataset'")
    conn.commit()
    row = db.get_lead(conn, int(row["id"]))
    assert row is not None
    contact_id = db.save_contact(
        conn,
        int(row["id"]),
        "Jane Doe",
        "Superintendent",
        "jdoe@crschools.org",
        "",
        "https://crschools.org/staff",
        "high",
    )
    contact = conn.execute(
        "SELECT * FROM contacts WHERE id=?", (contact_id,)
    ).fetchone()
    brief = persequor_client.build_brief(
        row, contact, "U01DPJVURHU", "chase@monarchconnected.com"
    )
    assert brief is not None
    assert brief["source_url"] == "https://x.gov/a"


def test_brief_live_mode_requires_verified_contact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify brief live mode requires verified contact."""
    monkeypatch.delenv("OUTREACH_TEST_EMAIL", raising=False)
    conn, row = _lead_row(tmp_path)
    assert (
        persequor_client.build_brief(
            row, None, "U01DPJVURHU", "chase@monarchconnected.com"
        )
        is None
    )


def test_submit_persists_before_post_and_reports_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify submit persists before post and reports unreachable."""
    monkeypatch.setenv("PERSEQUOR_API_URL", "http://127.0.0.1:1")  # nothing listens
    monkeypatch.setenv("OUTREACH_TEST_EMAIL", "chase@monarchconnected.com")
    conn, row = _lead_row(tmp_path)
    brief = persequor_client.build_brief(
        row, None, "U01DPJVURHU", "chase@monarchconnected.com"
    )
    state, msg = persequor_client.submit_brief(conn, row["id"], brief)
    assert state == "unreachable" and "queued" in msg
    # the request was persisted BEFORE the failed POST (idempotency anchor)
    saved = conn.execute(
        "SELECT draft FROM outreach WHERE channel='persequor'"
    ).fetchone()
    assert saved is not None and brief["request_id"] in saved["draft"]


class _AcceptedResponse:
    """Minimal requests-compatible accepted response for offline handoff tests."""

    status_code = 202
    text = "accepted"


def test_retry_reuses_request_id_and_does_not_duplicate_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed POST is retried from one row with the original idempotency key."""
    monkeypatch.setenv("OUTREACH_TEST_EMAIL", "chase@monarchconnected.com")
    conn, row = _lead_row(tmp_path)
    brief = persequor_client.build_brief(
        row, None, "U01DPJVURHU", "chase@monarchconnected.com"
    )
    assert brief is not None

    def fail_post(*_args: object, **_kwargs: object) -> object:
        """Simulate a transient connection failure without network access."""
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(persequor_client.requests, "post", fail_post)
    state, _message = persequor_client.submit_brief(conn, int(row["id"]), brief)
    assert state == "unreachable"
    conn.execute("UPDATE outreach SET next_attempt_at='2000-01-01T00:00:00+00:00'")
    conn.commit()

    sent_ids: list[str] = []

    def accept_post(_url: str, json: object, **_kwargs: object) -> _AcceptedResponse:
        """Capture the retried id and simulate Persequor accepting it."""
        assert isinstance(json, dict)
        sent_ids.append(str(json["request_id"]))
        return _AcceptedResponse()

    monkeypatch.setattr(persequor_client.requests, "post", accept_post)
    summary = persequor_client.retry_pending(conn)
    saved = conn.execute("SELECT * FROM outreach").fetchall()
    assert summary.submitted == 1
    assert sent_ids == [brief["request_id"]]
    assert len(saved) == 1 and saved[0]["status"] == "submitted"


def test_request_id_is_stable_per_slack_request(tmp_path: Path) -> None:
    """Redelivery is stable while a later explicit request gets a fresh draft key."""
    conn, row = _lead_row(tmp_path)
    first = persequor_client.request_id_for(row, "U1", "C1", "100.1", "101.1")
    second = persequor_client.request_id_for(row, "U1", "C1", "100.1", "101.1")
    redraft = persequor_client.request_id_for(row, "U1", "C1", "100.1", "102.1")
    assert first == second
    assert first != redraft


def test_repeated_submit_does_not_make_second_http_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A queued/sending persisted key is the local idempotency boundary."""
    monkeypatch.setenv("OUTREACH_TEST_EMAIL", "chase@monarchconnected.com")
    conn, row = _lead_row(tmp_path)
    request_id = persequor_client.request_id_for(row, "U1", "C1", "100.1", "101.1")
    brief = persequor_client.build_brief(
        row, None, "U1", "chase@monarchconnected.com", request_id=request_id
    )
    assert brief is not None
    calls = 0

    def fail_post(*_args: object, **_kwargs: object) -> object:
        """Provide test-local behavior for fail post."""
        nonlocal calls
        calls += 1
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(persequor_client.requests, "post", fail_post)
    first, _ = persequor_client.submit_brief(conn, int(row["id"]), brief)
    second, message = persequor_client.submit_brief(conn, int(row["id"]), brief)
    assert first == "unreachable" and second == "unreachable"
    assert "did not create another copy" in message
    assert calls == 1


def test_later_explicit_redraft_creates_fresh_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two distinct Slack messages may request two human-reviewed Gmail drafts."""
    monkeypatch.setenv("OUTREACH_TEST_EMAIL", "chase@monarchconnected.com")
    conn, row = _lead_row(tmp_path)
    calls: list[str] = []

    def accept_post(_url: str, json: object, **_kwargs: object) -> _AcceptedResponse:
        """Provide test-local behavior for accept post."""
        assert isinstance(json, dict)
        calls.append(str(json["request_id"]))
        return _AcceptedResponse()

    monkeypatch.setattr(persequor_client.requests, "post", accept_post)
    for request_token in ("101.1", "102.1"):
        request_id = persequor_client.request_id_for(
            row, "U1", "C1", "100.1", request_token
        )
        brief = persequor_client.build_brief(
            row, None, "U1", "chase@monarchconnected.com", request_id=request_id
        )
        assert brief is not None
        state, message = persequor_client.submit_brief(conn, int(row["id"]), brief)
        assert state == "submitted" and "Nothing was sent" in message

    saved = conn.execute(
        "SELECT submitted_at,sent_at,approved_by FROM outreach ORDER BY id"
    ).fetchall()
    assert len(saved) == 2 and len(set(calls)) == 2
    assert all(item["submitted_at"] for item in saved)
    assert all(
        item["sent_at"] is None and item["approved_by"] is None for item in saved
    )


def test_retry_compare_and_set_skips_already_sending_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two retry workers cannot both POST the same queued outbox row."""
    conn, row = _lead_row(tmp_path)
    conn.execute(
        """INSERT INTO outreach
             (lead_id,channel,draft,request_id,status,attempts,created_at)
           VALUES (?,'persequor','{}','req-race','sending',1,
                   '2000-01-01T00:00:00+00:00')""",
        (int(row["id"]),),
    )
    conn.commit()

    def unexpected(*_args: object, **_kwargs: object) -> object:
        """Provide test-local behavior for unexpected."""
        raise AssertionError("already-sending row posted twice")

    monkeypatch.setattr(persequor_client.requests, "post", unexpected)
    saved = conn.execute(
        "SELECT * FROM outreach WHERE request_id='req-race'"
    ).fetchone()
    state, message = persequor_client._attempt_saved(conn, saved)
    assert state == "unreachable"
    assert "already being processed" in message


def test_retry_dry_run_makes_no_request_or_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retry dry-run only counts due rows and leaves attempts untouched."""
    conn, row = _lead_row(tmp_path)
    conn.execute(
        """INSERT INTO outreach
             (lead_id,channel,draft,request_id,status,attempts,created_at,next_attempt_at)
           VALUES (?,'persequor','{}','req-dry','queued',1,
                   '2000-01-01T00:00:00+00:00','2000-01-01T00:00:00+00:00')""",
        (int(row["id"]),),
    )
    conn.commit()

    def unexpected_post(*_args: object, **_kwargs: object) -> object:
        """Fail the test if dry-run performs external I/O."""
        raise AssertionError("dry-run posted")

    monkeypatch.setattr(persequor_client.requests, "post", unexpected_post)
    summary = persequor_client.retry_pending(conn, dry_run=True)
    attempts = conn.execute(
        "SELECT attempts FROM outreach WHERE request_id='req-dry'"
    ).fetchone()[0]
    assert summary == persequor_client.RetrySummary(1, 0, 1, 0)
    assert attempts == 1


# ------------------------------------------------------------ contact storage
def test_not_found_is_recorded_honestly(tmp_path: Path) -> None:
    """Verify not found is recorded honestly."""
    conn, row = _lead_row(tmp_path)
    db.mark_contact_not_found(conn, row["id"])
    rows = db.contacts_for_lead(conn, row["id"])
    assert rows[0]["contact_status"] == "not_found"
    assert rows[0]["email"] is None  # nothing invented
