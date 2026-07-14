"""Phase-2 + Persequor-client tests: the anti-hallucination gate, the rep roster,
and the test-mode brief. All offline."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

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
    assert verify_on_page(PAGE, "jdoe@crschools.org", "Jane Doe")


def test_gate_rejects_email_not_on_page() -> None:
    # The classic hallucination: plausible address, never fetched.
    assert not verify_on_page(PAGE, "jane.doe@crschools.org", "Jane Doe")


def test_gate_rejects_name_not_on_page() -> None:
    assert not verify_on_page(PAGE, "jdoe@crschools.org", "John Roe")


def test_gate_rejects_malformed_email() -> None:
    assert not verify_on_page(PAGE, "not-an-email", "Jane Doe")


# ------------------------------------------------------------ salesforce (offline bits)
def test_distinctive_term_strips_punct_and_generic_words() -> None:
    # SOSL chokes on punctuation; and dropping generic org words lets name
    # variations match (ABC Schools <-> ABC School District).
    term = distinctive_term("Mt. Morris Consolidated Schools (401) & District")
    assert "(" not in term and "&" not in term and "." not in term
    assert "Morris" in term
    # generic words removed so variations still match
    for generic in ("Consolidated", "Schools", "District"):
        assert generic not in term


def test_distinctive_term_variation_matching() -> None:
    # "ABC Schools" and "ABC School District" must reduce to the same distinctive term.
    assert distinctive_term("ABC Schools") == distinctive_term("ABC School District")


def test_sf_match_carries_lightning_link() -> None:
    m = SFMatch(sobject="Account", record_id="001x", name="Monarch", company="",
                owner="Chase", link="https://x/lightning/r/Account/001x/view",
                confidence="high")
    assert "/lightning/r/Account/001x/view" in m.link


# ------------------------------------------------------------ roster
def test_rep_roster_derives_send_as() -> None:
    assert persequor_client.rep_email_for("U01DPJVURHU") == "chase@monarchconnected.com"
    assert persequor_client.rep_email_for("U_NOT_A_REP") is None


# ------------------------------------------------------------ brief
def _lead_row(tmp_path: Path) -> tuple[sqlite3.Connection, sqlite3.Row]:
    """Create one award row for outreach and contact-gate tests."""
    conn = db.connect(tmp_path / "t.db")
    db.upsert_lead(conn, Lead(
        item=RawItem(source="usaspending:16.071", item_id="A1", title="SVPP",
                     entity="Castle Rock School District 401", state="WA",
                     program="SVPP", amount=500_000.0, start="2025-10-01",
                     end="2028-09-30", url="https://x.gov/a", raw={}),
        grade=LeadGrade.GOLD))
    row = conn.execute("SELECT * FROM leads").fetchone()
    return conn, row


def test_brief_test_mode_overrides_recipient(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OUTREACH_TEST_EMAIL", "chase@monarchconnected.com")
    conn, row = _lead_row(tmp_path)
    cid = db.save_contact(conn, row["id"], "Jane Doe", "Superintendent",
                          "jdoe@crschools.org", "", "https://crschools.org/staff", "high")
    contact = conn.execute("SELECT * FROM contacts WHERE id=?", (cid,)).fetchone()
    brief = persequor_client.build_brief(row, contact, "U01DPJVURHU",
                                         "chase@monarchconnected.com")
    assert brief is not None
    assert brief["contact_email"] == "chase@monarchconnected.com"   # test override
    assert "jdoe@crschools.org" in brief["rep_notes"]               # truth preserved
    assert "TEST MODE" in brief["rep_notes"]
    assert brief["amount_usd"] == 500000
    assert brief["expires_at"] == "2028-09-30"
    assert brief["request_id"].startswith(f"grant-{row['id']}-")


def test_brief_live_mode_requires_verified_contact(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OUTREACH_TEST_EMAIL", raising=False)
    conn, row = _lead_row(tmp_path)
    assert persequor_client.build_brief(row, None, "U01DPJVURHU",
                                        "chase@monarchconnected.com") is None


def test_submit_persists_before_post_and_reports_unreachable(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSEQUOR_API_URL", "http://127.0.0.1:1")  # nothing listens
    monkeypatch.setenv("OUTREACH_TEST_EMAIL", "chase@monarchconnected.com")
    conn, row = _lead_row(tmp_path)
    brief = persequor_client.build_brief(row, None, "U01DPJVURHU",
                                         "chase@monarchconnected.com")
    state, msg = persequor_client.submit_brief(conn, row["id"], brief)
    assert state == "unreachable" and "queued" in msg
    # the request was persisted BEFORE the failed POST (idempotency anchor)
    saved = conn.execute("SELECT draft FROM outreach WHERE channel='persequor'").fetchone()
    assert saved is not None and brief["request_id"] in saved["draft"]


# ------------------------------------------------------------ Google Sheet handoff
def test_google_sheet_handoff_is_complete_and_formula_safe(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Persequor receives every row and formula-like external strings as literal text."""
    captured: dict[str, object] = {}

    class FakeResponse:
        """Successful Persequor create-sheet response with a real URL field."""

        status_code = 201

        def json(self) -> dict[str, str]:
            """Return the created sheet URL."""
            return {"url": "https://docs.google.com/spreadsheets/d/sheet-id"}

    def fake_post(url: str, **kwargs: object) -> FakeResponse:
        """Capture the outgoing JSON without making a network request."""
        captured["url"] = url
        captured["json"] = kwargs["json"]
        return FakeResponse()

    monkeypatch.setattr(persequor_client.requests, "post", fake_post)
    rows: list[list[object]] = [[f"District {i}", i] for i in range(2_001)]
    rows[0][0] = " \t=IMPORTXML('https://example.test')"
    state, message = persequor_client.create_google_sheet(
        "Complete export", ["entity", "amount"], rows,
        "U01DPJVURHU", "chase@monarchconnected.com")

    assert state == "created" and message.startswith("https://docs.google.com/")
    payload = captured["json"]
    assert isinstance(payload, dict)
    assert len(payload["rows"]) == 2_001
    assert payload["rows"][0][0].startswith("'")
    assert payload["rows"][1][1] == 1


def test_google_sheet_requires_mapped_rep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing roster identity fails before any request can reach Persequor."""
    called = False

    def fake_post(_url: str, **_kwargs: object) -> object:
        """Record an unexpected network call."""
        nonlocal called
        called = True
        return object()

    monkeypatch.setattr(persequor_client.requests, "post", fake_post)
    state, message = persequor_client.create_google_sheet(
        "Export", ["entity"], [["District"]], "U_UNKNOWN", "")
    assert state == "error" and "mapped" in message
    assert called is False


# ------------------------------------------------------------ contact storage
def test_not_found_is_recorded_honestly(tmp_path: Path) -> None:
    conn, row = _lead_row(tmp_path)
    db.mark_contact_not_found(conn, row["id"])
    rows = db.contacts_for_lead(conn, row["id"])
    assert rows[0]["contact_status"] == "not_found"
    assert rows[0]["email"] is None  # nothing invented
