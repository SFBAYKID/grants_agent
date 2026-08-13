"""Offline tests for the organization-profile verbatim verifiers."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from grant_watch import db
from grant_watch.enrich import organization_profile as op
from grant_watch.enrich import evidence
from grant_watch.models import FundingEventType, Lead, LeadGrade, RawItem


def _lead(tmp_path: Path) -> tuple[sqlite3.Connection, int]:
    """Create one isolated lead for organization-profile integration tests."""
    conn = db.connect(tmp_path / "org-profile.db")
    db.upsert_lead(
        conn,
        Lead(
            RawItem(
                "test",
                "org-1",
                "award",
                "Alpha School District",
                "CA",
                "SVPP",
                100_000,
                "2026-01-01",
                "2027-01-01",
                "https://example.gov/award",
                {},
                event_type=FundingEventType.AWARD_OBLIGATED,
            ),
            LeadGrade.GOLD,
        ),
    )
    row = conn.execute("SELECT id FROM leads WHERE source_item_id='org-1'").fetchone()
    assert row is not None
    return conn, int(row["id"])


def _site() -> op.SiteCandidate:
    """Return one state-bound site candidate for offline integration tests."""
    return op.SiteCandidate(
        "https://alpha.org",
        "alpha.org",
        evidence.recorded_match(
            "website_candidate",
            "https://alpha.org",
            "https://search.example/result",
            "Alpha School District — California",
        ),
    )


def test_general_email_requires_shared_mailbox_and_verbatim() -> None:
    """A general email must be a shared mailbox AND appear verbatim on the page."""
    page = "Contact us at info@alpha.org or call the office."
    assert op._general_email_on_page(page, "info@alpha.org")
    # A personal-looking address is not treated as the org's general mailbox.
    assert not op._general_email_on_page(page, "jane.smith@alpha.org")
    # Not on the page → rejected even if it looks general.
    assert not op._general_email_on_page(page, "office@alpha.org")
    assert not op._general_email_on_page(
        "Contact adminsmith@alpha.org", "adminsmith@alpha.org"
    )
    assert not op._general_email_on_page("Contact info@alpha.org.au", "info@alpha.org")


def test_merge_only_accepts_page_verified_values() -> None:
    """_merge fills a field only when the claimed value is verbatim on the page."""
    page = "Alpha School, 1 Alpha Way, Sacramento, 95814. Phone 555-999-1000."
    profile = op.OrgProfile()
    op._merge(
        profile,
        page,
        {
            "general_email": "info@alpha.org",  # NOT on this page → dropped
            "phone": "555-999-1000",
            "street": "1 Alpha Way",
            "city": "Sacramento",
            "postal_code": "95814",
            "state": "invented state",  # not on page → dropped
        },
        "https://alpha.org/contact",
    )
    assert profile.phone == "555-999-1000"
    assert profile.street == "1 Alpha Way"
    assert profile.city == "Sacramento"
    assert profile.postal_code == "95814"
    assert profile.general_email == ""  # not on page
    assert profile.state == ""  # not on page
    assert profile.source_url == "https://alpha.org/contact"


def test_merge_does_not_treat_a_lowercase_conjunction_as_oregon() -> None:
    """A model-produced ``OR`` value needs an uppercase code or full state name."""
    profile = op.OrgProfile()
    op._merge(
        profile,
        "Call or email the district office in Springfield.",
        {"state": "OR"},
        "https://alpha.org/contact",
    )
    assert profile.state == ""
    assert "state" not in profile.field_evidence


def test_merge_refuses_to_blend_fields_from_two_pages() -> None:
    """One legacy source URL must never be made to prove facts from another page."""
    profile = op.OrgProfile()
    op._merge(
        profile,
        "Call 555-999-1000",
        {"phone": "555-999-1000"},
        "https://alpha.org/",
    )
    with pytest.raises(ValueError, match="across pages"):
        op._merge(
            profile,
            "Email info@alpha.org",
            {"general_email": "info@alpha.org"},
            "https://alpha.org/contact",
        )


def test_phone_only_profile_is_found_without_promoting_a_search_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A verbatim switchboard alone is a found org profile, not a false negative."""
    conn, lead_id = _lead(tmp_path)
    monkeypatch.setattr(op, "_resolve_site", lambda *_a, **_k: _site())
    monkeypatch.setattr(
        op, "_scrape", lambda _url, **_kwargs: "Main office: 555-222-1000"
    )
    monkeypatch.setattr(
        op,
        "_extract_org",
        lambda *_a, **_k: {"phone": "555-222-1000"},
    )

    profile = op.enrich_org_profile(conn, lead_id)

    assert profile.status == "found"
    assert profile.phone == "555-222-1000"
    assert profile.website == ""
    assert profile.website_candidate == "https://alpha.org"
    stored = db.get_lead(conn, lead_id)
    assert stored is not None
    assert stored["org_profile_status"] == "found"
    assert stored["org_website"] is None
    assert stored["org_website_candidate"] == "https://alpha.org"


def test_unproven_site_remains_candidate_and_is_not_reported_official(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A search hit without any verified on-page fact is never an official site."""
    conn, lead_id = _lead(tmp_path)
    monkeypatch.setattr(op, "_resolve_site", lambda *_a, **_k: _site())
    monkeypatch.setattr(
        op, "_scrape", lambda _url, **_kwargs: "Welcome to an unrelated page"
    )
    monkeypatch.setattr(op, "_extract_org", lambda *_a, **_k: {})

    profile = op.enrich_org_profile(conn, lead_id)

    assert profile.status == "not_found"
    assert profile.website == ""
    assert profile.website_candidate == "https://alpha.org"
    stored = db.get_lead(conn, lead_id)
    assert stored is not None
    assert stored["org_website"] is None
    assert stored["org_website_candidate"] == "https://alpha.org"


def test_profile_selects_one_page_and_persists_field_level_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every projected fact cites the one page that actually proved the profile."""
    conn, lead_id = _lead(tmp_path)
    monkeypatch.setattr(op, "_resolve_site", lambda *_a, **_k: _site())

    def _scrape(url: str, **_kwargs: object) -> str:
        """Return a weaker homepage and a complete contact page."""
        if url.endswith("/contact"):
            return (
                "Contact info@alpha.org, 1 Alpha Way, Sacramento, California 95814. "
                "Phone 555-222-1000."
            )
        return "Main office phone 555-111-0000."

    def _extract(_text: str, _entity: str, url: str) -> dict[str, str]:
        """Model output remains page-specific for the verifier to check."""
        if url.endswith("/contact"):
            return {
                "general_email": "info@alpha.org",
                "phone": "555-222-1000",
                "street": "1 Alpha Way",
                "city": "Sacramento",
                "state": "California",
                "postal_code": "95814",
            }
        return {"phone": "555-111-0000"}

    monkeypatch.setattr(op, "_scrape", _scrape)
    monkeypatch.setattr(op, "_extract_org", _extract)

    profile = op.enrich_org_profile(conn, lead_id)

    assert profile.source_url == "https://alpha.org/contact"
    assert profile.phone == "555-222-1000"
    rows = list(
        conn.execute(
            """SELECT field_name,source_url FROM organization_field_evidence
               WHERE lead_id=? AND status='current'""",
            (lead_id,),
        )
    )
    sources = {str(row["field_name"]): str(row["source_url"]) for row in rows}
    assert sources["phone"] == "https://alpha.org/contact"
    assert sources["street"] == "https://alpha.org/contact"
    assert "website" not in sources


def test_profile_combines_pages_without_losing_field_specific_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Facts from separate pages retain separate provenance after projection."""
    conn, lead_id = _lead(tmp_path)
    monkeypatch.setattr(op, "_resolve_site", lambda *_a, **_k: _site())

    def _scrape(url: str, **_kwargs: object) -> str:
        """Split the switchboard and mailbox across two official pages."""
        if url.endswith("/contact"):
            return "Contact info@alpha.org at 1 Alpha Way, Sacramento, CA 95814."
        return "Main office phone 555-111-0000."

    def _extract(_text: str, _entity: str, url: str) -> dict[str, str]:
        """Return only values present on each page."""
        if url.endswith("/contact"):
            return {
                "general_email": "info@alpha.org",
                "street": "1 Alpha Way",
                "city": "Sacramento",
                "state": "CA",
                "postal_code": "95814",
            }
        return {"phone": "555-111-0000"}

    monkeypatch.setattr(op, "_scrape", _scrape)
    monkeypatch.setattr(op, "_extract_org", _extract)

    profile = op.enrich_org_profile(conn, lead_id)

    assert profile.general_email == "info@alpha.org"
    assert profile.phone == "555-111-0000"
    sources = {
        str(row["field_name"]): str(row["source_url"])
        for row in conn.execute(
            """SELECT field_name,source_url FROM organization_field_evidence
               WHERE lead_id=? AND status='current'""",
            (lead_id,),
        )
    }
    assert sources["general_email"] == "https://alpha.org/contact"
    assert sources["phone"] == "https://alpha.org"
    assert "website" not in sources


def test_profile_never_combines_two_pages_into_one_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Conflicting page addresses remain one page's tuple, never a synthetic mix."""
    conn, lead_id = _lead(tmp_path)
    monkeypatch.setattr(op, "_resolve_site", lambda *_a, **_k: _site())

    def _scrape(url: str, **_kwargs: object) -> str:
        """Return two complete but conflicting office addresses."""
        if url.endswith("/contact"):
            return "District office: 2 Beta Rd, Oakland, CA 94601."
        return "District office: 1 Alpha Way, Sacramento, CA 95814."

    def _extract(_text: str, _entity: str, url: str) -> dict[str, str]:
        """Extract each address exactly from its own page."""
        if url.endswith("/contact"):
            return {
                "street": "2 Beta Rd",
                "city": "Oakland",
                "state": "CA",
                "postal_code": "94601",
            }
        return {
            "street": "1 Alpha Way",
            "city": "Sacramento",
            "state": "CA",
            "postal_code": "95814",
        }

    monkeypatch.setattr(op, "_scrape", _scrape)
    monkeypatch.setattr(op, "_extract_org", _extract)
    profile = op.enrich_org_profile(conn, lead_id)

    assert (profile.street, profile.city, profile.state, profile.postal_code) in {
        ("1 Alpha Way", "Sacramento", "CA", "95814"),
        ("2 Beta Rd", "Oakland", "CA", "94601"),
    }
    sources = {
        str(row["source_url"])
        for row in conn.execute(
            """SELECT source_url FROM organization_field_evidence
                WHERE lead_id=? AND field_name IN
                  ('street','city','state','postal_code') AND status='current'""",
            (lead_id,),
        )
    }
    assert len(sources) == 1


def test_address_components_outside_the_street_block_are_not_joined() -> None:
    """A distant footer ZIP/state cannot be attached to a different street."""
    profile = op.OrgProfile()
    page = (
        "District office: 1 Alpha Way, Sacramento. "
        + ("unrelated content " * 80)
        + "Vendor remittance address: Texas 99999."
    )
    op._merge(
        profile,
        page,
        {
            "street": "1 Alpha Way",
            "city": "Sacramento",
            "state": "Texas",
            "postal_code": "99999",
        },
        "https://alpha.org/contact",
    )
    assert profile.street == "1 Alpha Way"
    assert profile.city == "Sacramento"
    assert profile.state == ""
    assert profile.postal_code == ""


@pytest.mark.parametrize(
    "second_address",
    (
        "2 Beta Rd, Austin, TX 99999",
        "PO Box 2, Austin, TX 99999",
        "2 Main Plaza, Austin, TX 99999",
        "2 County Route 5, Austin, TX 99999",
    ),
)
def test_adjacent_addresses_on_one_line_cannot_form_a_composite(
    second_address: str,
) -> None:
    """A nearby remittance address terminates the first address evidence block."""
    profile = op.OrgProfile()
    page = (
        "District office: 1 Alpha Way, Sacramento. "
        f"Vendor remittance: {second_address}."
    )
    op._merge(
        profile,
        page,
        {
            "street": "1 Alpha Way",
            "city": "Sacramento",
            "state": "TX",
            "postal_code": "99999",
        },
        "https://alpha.org/contact",
    )
    assert profile.street == "1 Alpha Way"
    assert profile.city == "Sacramento"
    assert profile.state == ""
    assert profile.postal_code == ""


def test_evidenced_profile_hides_legacy_and_mixed_projection_fields(
    tmp_path: Path,
) -> None:
    """Only exact current evidence is exposed; mixed-source addresses fail closed."""
    conn, lead_id = _lead(tmp_path)
    conn.execute(
        """UPDATE leads SET org_general_email='legacy@wrong.test',
                  org_phone='555-111-2222',org_street='1 Alpha Way',
                  org_city='Sacramento',org_state='CA',org_postal_code='95814',
                  org_profile_status='found' WHERE id=?""",
        (lead_id,),
    )
    phone_match = evidence.recorded_match(
        "phone",
        "555-111-2222",
        "https://alpha.org/contact",
        "Phone 555-111-2222",
    )
    db.save_org_profile(
        conn,
        lead_id,
        op.OrgProfile(
            phone="555-111-2222",
            status="found",
            source_url="https://alpha.org/contact",
            field_evidence={"phone": phone_match},
        ),
    )
    # Simulate a later direct legacy edit after the safe writer.
    conn.execute(
        """UPDATE leads SET org_general_email='legacy@wrong.test',
                  org_street='1 Alpha Way',org_city='Sacramento' WHERE id=?""",
        (lead_id,),
    )
    conn.commit()

    lead = db.get_lead(conn, lead_id)
    assert lead is not None
    profile = op.evidenced_profile(conn, lead)
    assert profile.phone == "555-111-2222"
    assert profile.general_email == ""
    assert profile.street == ""
    assert profile.city == ""


def test_unverified_legacy_contact_domain_cannot_seed_org_site(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wrong-state/untyped contact row is not an official-domain site anchor."""
    conn, lead_id = _lead(tmp_path)
    conn.execute(
        """INSERT INTO contacts
             (lead_id,name,email,source_url,official_domain,contact_status)
           VALUES (?,?,?,?,?,'unverified')""",
        (
            lead_id,
            "Wrong Person",
            "wrong@district.tx.us",
            "https://district.tx.us/staff",
            "district.tx.us",
        ),
    )
    conn.commit()
    monkeypatch.setattr(op, "_search", lambda *_a, **_k: [])
    lead = db.get_lead(conn, lead_id)
    assert lead is not None

    assert op._resolve_site(conn, lead) is None


def test_exact_nces_site_is_promoted_and_keeps_directory_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only an exact ID-bound directory website crosses candidate→official."""
    conn, lead_id = _lead(tmp_path)
    nces_source = (
        "https://nces.ed.gov/ccd/districtsearch/district_detail.asp?ID2=0600001"
    )
    conn.execute(
        """UPDATE leads SET nces_website='https://alpha.org',
                  nces_website_source_url=?,nces_website_status='verified'
           WHERE id=?""",
        (nces_source, lead_id),
    )
    conn.commit()
    monkeypatch.setattr(
        op,
        "_scrape",
        lambda _url, **_kwargs: "Alpha School District phone 555-111-0000",
    )
    monkeypatch.setattr(op, "_extract_org", lambda *_a, **_k: {"phone": "555-111-0000"})

    profile = op.enrich_org_profile(conn, lead_id)

    assert profile.website == "https://alpha.org"
    row = conn.execute(
        """SELECT field_value,source_url FROM organization_field_evidence
           WHERE lead_id=? AND field_name='website' AND status='current'""",
        (lead_id,),
    ).fetchone()
    assert row is not None
    assert tuple(row) == ("https://alpha.org", nces_source)


def test_persistence_refuses_unchecked_profile_fields_and_supersedes_old_facts(
    tmp_path: Path,
) -> None:
    """Constructing a profile cannot bypass evidence or leave stale facts current."""
    conn, lead_id = _lead(tmp_path)
    source_url = "https://alpha.org/contact"
    verified = op.OrgProfile(
        phone="555-111-0000",
        source_url=source_url,
        status="found",
        field_evidence={
            "phone": evidence.recorded_match(
                "phone", "555-111-0000", source_url, "Phone 555-111-0000"
            )
        },
    )
    db.save_org_profile(conn, lead_id, verified)
    db.save_org_profile(
        conn,
        lead_id,
        op.OrgProfile(
            phone="555-999-9999",
            general_email="invented@alpha.org",
            source_url=source_url,
            status="found",
        ),
    )

    stored = db.get_lead(conn, lead_id)
    assert stored is not None
    assert stored["org_phone"] is None
    assert stored["org_general_email"] is None
    assert stored["org_profile_status"] == "not_found"
    states = list(
        conn.execute(
            """SELECT field_value,status FROM organization_field_evidence
               WHERE lead_id=? ORDER BY verified_at""",
            (lead_id,),
        )
    )
    assert [tuple(row) for row in states] == [("555-111-0000", "superseded")]
