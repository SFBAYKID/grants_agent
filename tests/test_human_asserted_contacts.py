"""What a rep tells Grant is recorded, attributed — and can never pass as verified.

Grant used to refuse a phone number a rep typed into chat because it had not come
from a source Grant pulled. That was the honesty rule pointed at the wrong case: it
exists to stop Grant INVENTING a contact and calling it discovered, not to stop a
person telling Grant something true.

The first attempt at the fix was worse than the refusal. It filled empty fields on
the contact the rep named, which left that row still reading `contact_status
='verified'` while carrying a value nobody had checked — and `grant.py` selects the
Persequor outreach brief's contact on exactly that status. A rep-typed email would
have been emailed to a school administrator as though Grant had verified it. These
tests pin the corrected shape: a supplied fact is always its OWN row.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from grant_watch import db
from grant_watch.slack import tools
from tests.contact_support import verified_contact_evidence

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
    contact_id, written = db.save_human_asserted_contact(
        conn, lead_id, name="Dana Reyes", phone="308-555-0142", asserted_by=REP
    )
    assert set(written) == {"name", "phone"}
    row = conn.execute("SELECT * FROM contacts WHERE id=?", (contact_id,)).fetchone()
    assert row["phone"] == "308-555-0142"
    assert row["asserted_by_slack_user"] == REP
    assert row["asserted_at"]
    conn.close()


def test_a_supplied_fact_can_never_reach_the_outreach_brief(tmp_path: Path) -> None:
    """THE exploit: a rep-typed email emailed to a school as Grant's own finding.

    A rep may supply a different email from memory. When that filled an existing row
    in place, the row stayed `verified` and grant.py's brief selection picked it up.
    The supplied fact must land somewhere that query cannot see or alter.
    """
    conn, lead_id = _lead(tmp_path)
    db.save_contact(
        conn,
        lead_id,
        "Dana Reyes",
        "IT Director",
        "office@district.test",
        "308-555-0100",
        "https://district.test/staff",
        "high",
        field_evidence=verified_contact_evidence(
            "Dana Reyes",
            "office@district.test",
            "https://district.test/staff",
            title="IT Director",
            phone="308-555-0100",
        ),
    )
    db.save_human_asserted_contact(
        conn, lead_id, email="dana@district.test", asserted_by=REP
    )

    # Exactly the query grant.py uses to choose the contact an email is built from.
    brief_contacts = [
        row
        for row in conn.execute("SELECT * FROM contacts WHERE lead_id=?", (lead_id,))
        if row["contact_status"] == "verified"
    ]
    assert len(brief_contacts) == 1
    assert brief_contacts[0]["email"] == "office@district.test", (
        "a rep-typed email reached the outreach brief as a verified contact"
    )
    conn.close()


def test_page_verified_evidence_is_never_altered(tmp_path: Path) -> None:
    """A typo cannot destroy what Grant checked on the organization's own page."""
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
        field_evidence=verified_contact_evidence(
            "Dana Reyes",
            "dana@district.test",
            "https://district.test/staff",
            title="IT Director",
            phone="308-555-0100",
        ),
    )
    db.save_human_asserted_contact(
        conn, lead_id, email="wrong@district.test", phone="999", asserted_by=REP
    )
    original = conn.execute(
        "SELECT * FROM contacts WHERE id=?", (verified_id,)
    ).fetchone()
    assert original["email"] == "dana@district.test"
    assert original["phone"] == "308-555-0100"
    assert original["contact_status"] == "verified"
    assert original["asserted_by_slack_user"] is None
    conn.close()


def test_every_writer_records_a_provenance(tmp_path: Path) -> None:
    """The completeness invariant — this is the test that would have caught it.

    Provenance was populated by a ONE-SHOT migration backfill, so every contact
    created afterwards had it NULL. The single guard that reads it then computed
    "not page-verified" for genuinely verified rows, which is how a typo could have
    overwritten real evidence on anything the daily enrichment produced.
    """
    conn, lead_id = _lead(tmp_path)
    db.save_contact(
        conn,
        lead_id,
        "Alice Able",
        "Director",
        "alice@b.test",
        "308-555-0100",
        "https://b.test/staff",
        "high",
        field_evidence=verified_contact_evidence(
            "Alice Able",
            "alice@b.test",
            "https://b.test/staff",
            title="Director",
            phone="308-555-0100",
        ),
    )
    db.save_vendor_contact(
        conn, lead_id, "B", "t", "b@c.test", "2", "v1", do_not_call=False
    )
    db.save_linkedin_contact(conn, lead_id, "C", "t", "https://linkedin.test/in/c")
    db.save_human_asserted_contact(conn, lead_id, phone="3", asserted_by=REP)

    rows = list(conn.execute("SELECT contact_status,provenance FROM contacts"))
    assert len(rows) == 4
    assert all(row["provenance"] for row in rows), (
        f"a writer left provenance NULL: {[dict(r) for r in rows]}"
    )
    assert {row["provenance"] for row in rows} == {
        "page_verified",
        "vendor_licensed",
        "linkedin_claimed",
        "human_asserted",
    }
    conn.close()


def test_the_reply_reports_only_what_was_actually_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Grant must not claim to have stored a field the database dropped.

    An earlier version built its confirmation from the ARGUMENTS, so a field the
    write silently discarded was still reported as recorded — a fabricated success
    inside the one feature whose justification is honest attribution.
    """
    conn, lead_id = _lead(tmp_path)
    conn.close()
    _redirect(monkeypatch, tmp_path / "h.db")
    out = tools.record_contact_fact(
        lead_id, REP, name="Dana Reyes", phone="308-555-0142"
    )
    assert "Dana Reyes" in out
    assert "308-555-0142" in out
    # It names the organisation, not an opaque internal id the rep cannot check.
    assert "Scottsbluff Public School" in out
    assert f"<@{REP}>" in out

    check = db.connect(tmp_path / "h.db")
    row = check.execute("SELECT * FROM contacts").fetchone()
    assert row["name"] == "Dana Reyes"
    assert row["phone"] == "308-555-0142"
    check.close()


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
    check = db.connect(tmp_path / "h.db")
    assert check.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 0
    check.close()


@pytest.mark.parametrize(
    "ask",
    [
        "delete that campaign and add the new one",
        "can you fix this — remove those members",
        "update: please delete the lead",
        "take her off the campaign, and set the other one live",
        "also delete that campaign",
    ],
)
def test_one_additive_word_no_longer_disarms_the_removal_refusal(ask: str) -> None:
    """The guard was message-wide, so any additive word anywhere switched it off.

    That reopened the exact failure its own comment names: a soothing "done, I
    removed that" for something that never happened — made likelier by the new
    prompt telling Grant to work out intent and act.
    """
    from grant_watch.slack.intent_router import removal_request

    assert removal_request(ask) is True
