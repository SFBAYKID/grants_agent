"""What may become a Salesforce field, and whether a campaign Lead names a person.

Both defects here shipped real records on 2026-08-11: a Lead titled "Corkscrew
Salesman and Finder" taken from a LinkedIn profile whose ownership was never
established, and an organization mailbox announced to a rep as "Email (direct)".
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from grant_watch import db
from grant_watch.enrich.salesforce_campaign_gateway import SalesforceRecordRef
from grant_watch.enrich.salesforce_campaign_ownership import campaign_lead_payload
from grant_watch.enrich.salesforce_contact_fields import (
    choose_email,
    email_is_general,
    title_is_writable,
)
from grant_watch.enrich.salesforce_contact_records import contact_lead_payload
from grant_watch.models import FundingEventType, Lead, LeadGrade, RawItem

OWNER = SalesforceRecordRef(
    "User", "005000000000001", "Test Rep", "https://writer.salesforce.test/u"
)


def _lead_row(
    conn: sqlite3.Connection, entity: str = "YESHIVA OHR ELCHONON"
) -> sqlite3.Row:
    """Insert one award lead and return its row."""
    db.upsert_lead(
        conn,
        Lead(
            item=RawItem(
                source="provenance-test",
                item_id=f"item-{entity}",
                title="verified security grant",
                entity=entity,
                state="CA",
                program="NSGP",
                amount=150_000,
                start="2026-01-01",
                end="2027-01-01",
                url="https://source.test/1",
                raw={},
                event_type=FundingEventType.AWARD_OBLIGATED,
            ),
            grade=LeadGrade.GOLD,
        ),
    )
    return conn.execute("SELECT * FROM leads ORDER BY id DESC LIMIT 1").fetchone()


def _contact(
    conn: sqlite3.Connection,
    lead_id: int,
    *,
    name: str,
    title: str,
    email: str,
    status: str,
) -> sqlite3.Row:
    """Insert one contact row with an explicit status the finder gate would set."""
    conn.execute(
        """INSERT INTO contacts
             (lead_id,name,title,email,phone,source_url,confidence,contact_status,
              provenance)
           VALUES (?,?,?,?,'','https://evidence.test','high',?,?)""",
        (
            lead_id,
            name,
            title,
            email,
            status,
            "linkedin_claimed" if status == "linkedin_only" else "page_verified",
        ),
    )
    conn.commit()
    return conn.execute(
        "SELECT * FROM contacts WHERE lead_id=? ORDER BY id DESC LIMIT 1", (lead_id,)
    ).fetchone()


def test_a_linkedin_title_never_becomes_a_salesforce_title(tmp_path: Path) -> None:
    """The exact record created on 2026-08-11: Title "Corkscrew Salesman and Finder"."""
    conn = db.connect(tmp_path / "title.db")
    lead = _lead_row(conn)
    contact = _contact(
        conn,
        int(lead["id"]),
        name="Levi Chein",
        title="Corkscrew Salesman and Finder",
        email="",
        status="linkedin_only",
    )
    assert title_is_writable(contact, str(lead["entity_name"])) is False
    payload = contact_lead_payload(lead, contact, "UREP", "action-1", OWNER)
    assert "Title" not in payload
    # The finding itself is not suppressed — only its promotion to a CRM fact.
    assert "LinkedIn__c" not in payload or payload.get("LastName") == "Chein"


def test_a_verified_title_is_still_written(tmp_path: Path) -> None:
    """The guard must not cost the honest case; this is the control."""
    conn = db.connect(tmp_path / "title-ok.db")
    lead = _lead_row(conn)
    contact = _contact(
        conn,
        int(lead["id"]),
        name="Dana Reyes",
        title="Director of Technology",
        email="dreyes@example.org",
        status="verified",
    )
    assert title_is_writable(contact, str(lead["entity_name"])) is True
    assert (
        contact_lead_payload(lead, contact, "UREP", "action-1", OWNER)["Title"]
        == "Director of Technology"
    )


@pytest.mark.parametrize(
    "email,general",
    [
        ("office@ygla.org", True),
        ("info@district.k12.ca.us", True),
        ("superintendent@montebello.k12.ca.us", False),
        ("v.chalabian@bcchs.net", False),
    ],
)
def test_an_organization_mailbox_is_recognised_wherever_it_was_stored(
    email: str, general: bool
) -> None:
    """`office@` is the organization's, whichever column it happens to sit in."""
    assert email_is_general(email) is general


def test_a_general_mailbox_on_the_contact_row_is_labelled_general(
    tmp_path: Path,
) -> None:
    """Grant told a rep "Email (direct): office@ygla.org" on 2026-08-11.

    The general branch only fired when the CONTACT had no email at all, so a
    switchboard address scraped onto the contact row was announced as the named
    person's own.
    """
    conn = db.connect(tmp_path / "email.db")
    lead = _lead_row(conn)
    contact = _contact(
        conn,
        int(lead["id"]),
        name="Rabbi Yossi Gross",
        title="Executive Director",
        email="office@ygla.org",
        status="verified",
    )
    assert choose_email(contact, lead) == ("office@ygla.org", "general")


def test_a_campaign_lead_carries_the_person_when_grant_has_one(tmp_path: Path) -> None:
    """A campaign built from org-only Leads cannot contain a POC by construction.

    Nelly asked for "company name, POC title, name and contact information" and the
    bulk path could only ever produce `LastName = <ORGANIZATION>` with every person
    field blank — even where Grant had already verified someone.
    """
    conn = db.connect(tmp_path / "person.db")
    lead = _lead_row(conn, entity="IMPERIAL UNIFIED SCHOOL DISTRICT")
    _contact(
        conn,
        int(lead["id"]),
        name="Dana Reyes",
        title="Director of Information Technology",
        email="dreyes@imperialusd.org",
        status="verified",
    )
    payload, note, person = campaign_lead_payload(conn, lead, "UREP", "act", OWNER)
    assert person == "Dana Reyes"
    assert payload["FirstName"] == "Dana"
    assert payload["LastName"] == "Reyes"
    assert payload["Title"] == "Director of Information Technology"
    assert payload["Email"] == "dreyes@imperialusd.org"
    assert payload["Company"] == "IMPERIAL UNIFIED SCHOOL DISTRICT"
    assert "Dana Reyes" in note


def test_a_campaign_lead_stays_organization_only_without_a_verified_person(
    tmp_path: Path,
) -> None:
    """No contact, and an unverified one, must both stay organization-only.

    Creating a hundred Leads named after LinkedIn profiles is a different risk from
    one named person on a card a human reads, so the bulk path takes `verified` only.
    """
    conn = db.connect(tmp_path / "orgonly.db")
    bare = _lead_row(conn, entity="SAVANNA SCHOOL DISTRICT")
    payload, _note, person = campaign_lead_payload(conn, bare, "UREP", "act", OWNER)
    assert person == ""
    assert payload["LastName"] == "SAVANNA SCHOOL DISTRICT"
    assert "FirstName" not in payload

    linked = _lead_row(conn, entity="CORNING UNION HIGH SCHOOL DISTRICT")
    _contact(
        conn,
        int(linked["id"]),
        name="Dave Messmer",
        title="Retired Director of Technology",
        email="",
        status="linkedin_only",
    )
    payload, _note, person = campaign_lead_payload(conn, linked, "UREP", "act", OWNER)
    assert person == ""
    assert payload["LastName"] == "CORNING UNION HIGH SCHOOL DISTRICT"
    assert "Title" not in payload
