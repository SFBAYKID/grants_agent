"""What a rep tells Grant is recorded, attributed — and never laundered.

Grant used to refuse a phone number a rep typed into chat, on the grounds that it had
not come from a source Grant pulled. That was the honesty rule pointed at the wrong
case, and it was judged a product failure. The rule exists to stop Grant INVENTING a
contact and calling it discovered; it was never meant to stop a person telling Grant
something true.

So the invariant is not "refuse", it is "record who said it". These tests pin both
halves: the fact gets stored, and it can never be mistaken for something Grant
verified itself — including that it must not overwrite evidence Grant did verify.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from grant_watch import db
from grant_watch.slack import tools

REP = "U0REP"


def _lead(tmp_path: Path) -> tuple[sqlite3.Connection, int]:
    """One lead to attach contact facts to."""
    conn = db.connect(tmp_path / "h.db")
    conn.execute(
        "INSERT INTO leads (source,source_item_id,entity_name,state,detail_url) "
        "VALUES ('s','1','Scottsbluff Public School','NE','u')"
    )
    conn.commit()
    return conn, int(conn.execute("SELECT id FROM leads").fetchone()["id"])


def _redirect(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    """Send bare db.connect() calls to the throwaway file (see tests/conftest.py)."""
    real = db.connect

    def connect(db_path: object = None, *a: object, **k: object) -> object:
        """Open the throwaway file when no explicit path is given."""
        return real(path if db_path is None else db_path, *a, **k)

    monkeypatch.setattr(db, "connect", connect)


def test_a_supplied_phone_is_stored_and_attributed(tmp_path: Path) -> None:
    """The rep is the authority on what they type; record it and say who said it."""
    conn, lead_id = _lead(tmp_path)
    contact_id, refused = db.save_human_asserted_contact(
        conn,
        lead_id,
        name="Dana Reyes",
        phone="308-555-0142",
        asserted_by=REP,
    )
    assert refused == []
    row = conn.execute("SELECT * FROM contacts WHERE id=?", (contact_id,)).fetchone()
    assert row["phone"] == "308-555-0142"
    assert row["asserted_by_slack_user"] == REP
    assert row["asserted_at"]
    conn.close()


def test_a_supplied_fact_is_never_stored_as_verified(tmp_path: Path) -> None:
    """It must not reach an outreach brief or a rich card as Grant's own finding."""
    conn, lead_id = _lead(tmp_path)
    db.save_human_asserted_contact(
        conn, lead_id, email="dana@district.test", asserted_by=REP
    )
    row = conn.execute("SELECT * FROM contacts").fetchone()
    assert row["contact_status"] == "human_asserted"
    assert row["provenance"] == "human_asserted"
    assert row["contact_status"] != "verified"
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM contacts WHERE contact_status='verified'"
        ).fetchone()[0]
        == 0
    )
    conn.close()


def test_a_typo_cannot_destroy_page_verified_evidence(tmp_path: Path) -> None:
    """Grant checked that value on the organization's own page; a typed one cannot win.

    The refused field comes back by name so the caller can say out loud that it kept
    the old value — silently keeping it would be its own kind of dishonesty.
    """
    conn, lead_id = _lead(tmp_path)
    verified_id = db.save_contact(
        conn,
        lead_id,
        "Dana Reyes",
        "IT Director",
        "dana@district.test",
        "308-555-0100",
        "https://district.test/staff",
        "high",
    )
    conn.execute(
        "UPDATE contacts SET provenance='page_verified' WHERE id=?", (verified_id,)
    )
    conn.commit()

    _contact, refused = db.save_human_asserted_contact(
        conn,
        lead_id,
        phone="308-555-9999",
        email="wrong@district.test",
        asserted_by=REP,
        contact_id=verified_id,
    )
    assert set(refused) == {"phone", "email"}
    row = conn.execute("SELECT * FROM contacts WHERE id=?", (verified_id,)).fetchone()
    assert row["phone"] == "308-555-0100"
    assert row["email"] == "dana@district.test"
    conn.close()


def test_an_empty_field_on_an_existing_contact_is_filled(tmp_path: Path) -> None:
    """The common case: Grant found the person, the rep knows their number."""
    conn, lead_id = _lead(tmp_path)
    contact_id = db.save_linkedin_contact(
        conn, lead_id, "Dana Reyes", "IT Director", "https://linkedin.test/in/dana"
    )
    _contact, refused = db.save_human_asserted_contact(
        conn, lead_id, phone="308-555-0142", asserted_by=REP, contact_id=contact_id
    )
    assert refused == []
    row = conn.execute("SELECT * FROM contacts WHERE id=?", (contact_id,)).fetchone()
    assert row["phone"] == "308-555-0142"
    assert row["asserted_by_slack_user"] == REP
    conn.close()


def test_a_contact_from_another_lead_is_refused(tmp_path: Path) -> None:
    """Attaching a fact to someone else's lead would corrupt two records at once."""
    conn, lead_id = _lead(tmp_path)
    conn.execute(
        "INSERT INTO leads (source,source_item_id,entity_name,detail_url) "
        "VALUES ('s','2','Other District','u')"
    )
    other = int(
        conn.execute("SELECT id FROM leads WHERE source_item_id='2'").fetchone()["id"]
    )
    contact_id = db.save_linkedin_contact(conn, other, "Someone Else", "", "u")
    with pytest.raises(ValueError, match="does not belong"):
        db.save_human_asserted_contact(
            conn, lead_id, phone="1", asserted_by=REP, contact_id=contact_id
        )
    conn.close()


def test_the_tool_records_the_fact_and_names_the_rep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end through the tool a rep's message actually reaches."""
    conn, lead_id = _lead(tmp_path)
    conn.close()
    _redirect(monkeypatch, tmp_path / "h.db")
    out = tools.record_contact_fact(lead_id, REP, phone="308-555-0142")
    assert "Recorded on lead" in out
    assert f"<@{REP}>" in out
    assert "308-555-0142" in out


def test_the_tool_refuses_when_it_cannot_attribute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Attribution IS the honesty mechanism; without it there is nothing to record."""
    conn, lead_id = _lead(tmp_path)
    conn.close()
    _redirect(monkeypatch, tmp_path / "h.db")
    out = tools.record_contact_fact(lead_id, "", phone="308-555-0142")
    assert out.startswith("ERROR:")
    assert "who's asking" in out


def test_the_tool_needs_something_to_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty call must not create a blank contact row."""
    conn, lead_id = _lead(tmp_path)
    conn.close()
    _redirect(monkeypatch, tmp_path / "h.db")
    out = tools.record_contact_fact(lead_id, REP)
    assert out.startswith("ERROR:")
