"""What Grant fills into a lead, and what it is allowed to CLAIM about it.

Split from test_reminders.py at the 1,000-line cap (CLAUDE.md rule 4). The boundary
is real: these are about LEAD DATA and the durable claims made from it — the
organization sweep that populates addresses, and the evidence sentence written into a
Salesforce record — while the file they left is about follow-ups and email.

The sentence tests matter most. That string outlives every Slack thread, so it is the
most durable claim Grant makes about a person.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from grant_watch import db

CHANNEL = "C0TEST"
REP = "U0REP"
THREAD = "1700000000.000100"


def _conn(tmp_path: Path) -> sqlite3.Connection:
    """A throwaway migrated database."""
    return db.connect(tmp_path / "leads.db")


# --- Organization backfill ---------------------------------------------------------


def test_the_backfill_targets_leads_that_actually_need_it(tmp_path: Path) -> None:
    """A lead whose profile is already `found` must not be paid for twice.

    `enrich_org_profile` short-circuits on `found`, so including those would list
    work that does nothing. `unreachable` IS included, because that outcome is
    explicitly retryable.
    """
    from grant_watch import org_backfill

    conn = _conn(tmp_path)
    for index, (status, grade) in enumerate(
        [("found", "gold"), ("unreachable", "gold"), ("", "gold"), ("", "watch")]
    ):
        conn.execute(
            "INSERT INTO leads (source,source_item_id,entity_name,state,detail_url,"
            "lead_grade,org_profile_status,amount) "
            "VALUES ('s',?,?,'CA','u',?,?,?)",
            (str(index), f"Org {index}", grade, status or None, 1000 - index),
        )
    conn.commit()
    names = [row["entity_name"] for row in org_backfill.candidates(conn, grade="gold")]
    assert names == ["Org 1", "Org 2"], f"picked the wrong leads: {names}"
    conn.close()


def test_one_unreachable_site_does_not_end_the_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same wedge that let one bad reminder silence the whole queue.

    A corpus sweep meets broken sites constantly; if the first one aborts the run,
    the backfill never reaches the leads behind it.
    """
    from grant_watch import org_backfill

    conn = _conn(tmp_path)
    for index in range(3):
        conn.execute(
            "INSERT INTO leads (source,source_item_id,entity_name,state,detail_url,"
            "lead_grade,amount) VALUES ('s',?,?,'CA','u','gold',?)",
            (str(index), f"Org {index}", 1000 - index),
        )
    conn.commit()

    class _Profile:
        """A profile that found something."""

        street = "1 Main St"
        website = "https://x.test"
        phone = ""

    def flaky(_conn: object, lead_id: int, *_a: object, **_k: object) -> object:
        """Explode on the first lead, succeed afterwards."""
        if lead_id == 1:
            raise RuntimeError("site unreachable")
        return _Profile()

    monkeypatch.setattr(org_backfill, "enrich_org_profile", flaky)
    outcome = org_backfill.run(conn, grade="gold", dry_run=False)
    assert outcome.considered == 3
    assert outcome.failed == 1
    assert outcome.enriched == 2, "the sweep stopped at the first broken site"
    conn.close()


def test_the_backfill_spends_nothing_without_execute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each lead is a live scrape, so the default must be inert."""
    from grant_watch import org_backfill

    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO leads (source,source_item_id,entity_name,state,detail_url,"
        "lead_grade,amount) VALUES ('s','1','Org','CA','u','gold',10)"
    )
    conn.commit()

    def explode(*_a: object, **_k: object) -> object:
        """Any call here means a dry run spent money."""
        raise AssertionError("a dry run performed a paid scrape")

    monkeypatch.setattr(org_backfill, "enrich_org_profile", explode)
    outcome = org_backfill.run(conn, grade="gold", dry_run=True)
    # `failed` is the load-bearing assertion. The sweep catches broad exceptions on
    # purpose, so it SWALLOWS the AssertionError above — asserting only on
    # considered/enriched passed against a version that had spent the money and
    # counted the resulting error. Caught by mutation testing, not by reading.
    assert (outcome.considered, outcome.enriched, outcome.failed) == (1, 0, 0)
    conn.close()


# --- What Salesforce is told about a contact's evidence ----------------------------


@pytest.mark.parametrize(
    ("writer", "kwargs", "must_say", "must_not_say"),
    [
        (
            "save_vendor_contact",
            {
                "name": "Vic Chalabian",
                "title": "IT Manager",
                "email": "v@x.test",
                "phone": "",
                "vendor_person_id": "1",
                "do_not_call": False,
            },
            "Supplied by ZoomInfo",
            "verified verbatim",
        ),
        (
            "save_linkedin_contact",
            {
                "name": "Dana Reyes",
                "title": "Director",
                "profile_url": "https://linkedin.test/in/dr",
            },
            "ownership not verified",
            "verified verbatim",
        ),
    ],
)
def test_salesforce_is_never_told_a_contact_was_verified_when_it_was_not(
    tmp_path: Path,
    writer: str,
    kwargs: dict[str, object],
    must_say: str,
    must_not_say: str,
) -> None:
    """This sentence is written into a CRM record and outlives every thread.

    It special-cased only LinkedIn and fell through to "Contact verified verbatim on
    {source}" for everything else — so a ZoomInfo contact, which has no source URL,
    was filed as "Contact verified verbatim on unknown source": a claim of
    verification, citing nothing, about data nobody checked.
    """
    from grant_watch.enrich.salesforce_contact_records import _contact_evidence

    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO leads (source,source_item_id,entity_name,state,detail_url) "
        "VALUES ('s','1','Test District','CA','u')"
    )
    conn.commit()
    lead_id = int(conn.execute("SELECT id FROM leads").fetchone()["id"])
    getattr(db, writer)(conn, lead_id, **kwargs)
    contact = conn.execute(
        "SELECT * FROM contacts WHERE lead_id=?", (lead_id,)
    ).fetchone()

    sentence = _contact_evidence(contact)
    assert must_say in sentence, sentence
    assert must_not_say not in sentence, (
        f"claimed verification it does not have: {sentence}"
    )
    conn.close()


def test_a_contact_with_no_source_page_does_not_claim_verification(
    tmp_path: Path,
) -> None:
    """ "Contact verified verbatim on unknown source" was a real string it produced."""
    from grant_watch.enrich.salesforce_contact_records import _contact_evidence

    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO leads (source,source_item_id,entity_name,state,detail_url) "
        "VALUES ('s','1','Test District','CA','u')"
    )
    conn.commit()
    lead_id = int(conn.execute("SELECT id FROM leads").fetchone()["id"])
    db.save_contact(conn, lead_id, "Dana", "Director", "d@x.test", "", "", "medium")
    contact = conn.execute(
        "SELECT * FROM contacts WHERE lead_id=?", (lead_id,)
    ).fetchone()

    sentence = _contact_evidence(contact)
    assert "verified verbatim" not in sentence
    assert "unknown source" not in sentence
    conn.close()


def test_the_sweep_pays_for_each_organization_once(tmp_path: Path) -> None:
    """The first production run bought the same page three times.

    Gold holds ~30 duplicated entity names, and the sweep pays per LEAD row, so
    Modesto City Schools was scraped twice and Mt. Morris three times in a single
    25-lead batch. Each organization should cost one fetch.
    """
    from grant_watch import org_backfill

    conn = _conn(tmp_path)
    for index, name in enumerate(
        [
            "MODESTO CITY SCHOOLS",
            "MODESTO CITY SCHOOLS",
            "MT MORRIS",
            "MT MORRIS",
            "MT MORRIS",
            "GALT JOINT UNION",
        ]
    ):
        conn.execute(
            "INSERT INTO leads (source,source_item_id,entity_name,state,detail_url,"
            "lead_grade,canonical_entity_key,amount) VALUES ('s',?,?,'CA','u','gold',?,?)",
            (str(index), name, name.lower().replace(" ", "") + "|ca", 100),
        )
    conn.commit()
    picked = org_backfill.candidates(conn, grade="gold")
    names = [row["entity_name"] for row in picked]
    assert len(names) == len(set(names)) == 3, f"paid for a duplicate: {names}"
    conn.close()


def _linked(conn: sqlite3.Connection, lead_id: int, sf_id: str, item: int) -> None:
    """Record that a Grant lead corresponds to a Salesforce Lead.

    The parent `crm_actions` row is required: `crm_action_items.action_id` carries a
    foreign key, and inserting items without it silently tested nothing.
    """
    conn.execute(
        "INSERT OR IGNORE INTO crm_actions (id,action_type,workspace,channel,"
        "thread_ts,requested_by,state,payload_json,payload_hash,nonce_hash,"
        "expires_at,attempts,external_write_started,created_at,updated_at) "
        "VALUES (?,'add_campaign_members','T1','C1','1.1','U0REP','complete','{}',"
        "'h','n','2026-08-02',0,0,'2026-08-01','2026-08-01')",
        (f"act-{item}",),
    )
    conn.execute(
        "INSERT INTO crm_action_items (id,action_id,lead_id,canonical_entity_key,"
        "operation,proposed_json,state,verification_state,salesforce_id) "
        "VALUES (?,?,?,'k','add_member','{}','complete','verified',?)",
        (item, f"act-{item}", lead_id, sf_id),
    )


def test_a_linkedin_title_never_becomes_a_salesforce_field(tmp_path: Path) -> None:
    """`linkedin_only` means ownership is UNPROVEN, and a Salesforce Title says nothing.

    Writing an unverified title into a structured CRM field launders a guess into a
    fact that outlives every thread, and nobody downstream can tell it from a checked
    one. Grant still surfaces LinkedIn findings in Slack, where they are labelled.
    """
    from grant_watch import salesforce_lead_fill

    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO leads (source,source_item_id,entity_name,state,detail_url,"
        "lead_grade) VALUES ('s','1','Test District','CA','u','gold')"
    )
    conn.commit()
    lead_id = int(conn.execute("SELECT id FROM leads").fetchone()["id"])
    db.save_linkedin_contact(
        conn, lead_id, "Dana Reyes", "Director of Technology", "https://li.test/in/dr"
    )
    conn.commit()

    offered = salesforce_lead_fill.proposed_fields(conn, lead_id)
    assert "Title" not in offered, (
        f"an unverified LinkedIn title was offered to Salesforce: {offered}"
    )
    conn.close()


def test_a_verified_title_is_offered(tmp_path: Path) -> None:
    """The other direction: a page-verified contact SHOULD complete the record."""
    from grant_watch import salesforce_lead_fill

    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO leads (source,source_item_id,entity_name,state,detail_url,"
        "lead_grade) VALUES ('s','1','Test District','CA','u','gold')"
    )
    conn.commit()
    lead_id = int(conn.execute("SELECT id FROM leads").fetchone()["id"])
    db.save_contact(
        conn,
        lead_id,
        "Dana Reyes",
        "Director of Technology",
        "d@x.test",
        "555-0100",
        "https://district.test/staff",
        "high",
    )
    conn.commit()

    offered = salesforce_lead_fill.proposed_fields(conn, lead_id)
    assert offered.get("Title") == "Director of Technology"
    assert offered.get("Email") == "d@x.test"
    conn.close()


def test_one_grant_lead_yields_one_salesforce_target(tmp_path: Path) -> None:
    """Lead #231 maps to TWO Salesforce records and appeared twice.

    The same values would have gone to both, and --limit would have bounded rows
    rather than leads. Merging two CRM records for one organization is a human's
    call, not something a sweep should do by writing to both.
    """
    from grant_watch import salesforce_lead_fill

    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO leads (source,source_item_id,entity_name,state,detail_url,"
        "lead_grade) VALUES ('s','1','Birmingham Community Charter','CA','u','gold')"
    )
    conn.commit()
    lead_id = int(conn.execute("SELECT id FROM leads").fetchone()["id"])
    _linked(conn, lead_id, "00Q000000000001", 1)
    _linked(conn, lead_id, "00Q000000000002", 2)
    conn.commit()

    rows = salesforce_lead_fill.linked_leads(conn)
    assert len(rows) == 1, f"one lead produced {len(rows)} write targets"
    assert rows[0]["salesforce_id"] == "00Q000000000001"
    conn.close()
