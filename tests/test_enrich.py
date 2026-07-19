"""Contact-enrichment honesty (Constitution rule 1): an unreachable source must NEVER
be recorded as not_found, and a genuine not_found must be recorded truthfully. All
offline — finder's network calls are monkeypatched, no Firecrawl/Anthropic hit."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import requests

from grant_watch import db
from grant_watch.enrich import finder
from grant_watch.enrich.finder import ContactCandidate, SourceUnreachable
from grant_watch.models import Lead, LeadGrade, RawItem
from grant_watch.slack import tools


def _lead(tmp_path: Path) -> tuple[sqlite3.Connection, int]:
    """One award lead to enrich against."""
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
    return conn, int(conn.execute("SELECT id FROM leads").fetchone()["id"])


@pytest.mark.parametrize(
    "entity, kind",
    [
        ("City of East Providence", "city"),
        ("City of Salmon", "city"),
        ("Town of Kemah", "city"),
        ("Jefferson County", "city"),
        ("Tallapoosa Co School District", "school"),
        ("Alief ISD", "school"),
        ("Birmingham Community Charter High School", "school"),
        ("Dekalb County School District", "school"),  # school words win the tie
        ("Mars Hill Bible School", "school"),
    ],
)
def test_org_kind_classifies_city_vs_school(entity: str, kind: str) -> None:
    """City awards must not be treated as schools when picking a contact."""
    assert finder._org_kind(entity) == kind
    titles = finder._titles_for(entity)
    if kind == "city":
        assert "city manager" in titles and "superintendent" not in titles
    else:
        assert "superintendent" in titles


def test_linkedin_person_targets_city_roles_and_skips_school_people(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A city lead searches city roles and never attaches a school person.

    Live 2026-07-18: East Providence (a city award) surfaced the school district's
    IT director; a city award should reach a city official instead."""
    captured: dict[str, str] = {}

    def _fake_search(query: str, limit: int = 5) -> list[dict[str, str]]:
        """Return a school person first, then a city official."""
        captured["query"] = query
        return [
            {
                "url": "https://www.linkedin.com/in/sam-super",
                "title": "Sam Super - Superintendent - East Providence "
                "School District | LinkedIn",
            },
            {
                "url": "https://www.linkedin.com/in/pat-citymgr",
                "title": "Pat Manager - City Manager - City of East "
                "Providence | LinkedIn",
            },
        ]

    monkeypatch.setattr(finder, "_search", _fake_search)
    out = finder.linkedin_person("City of East Providence", "RI")
    assert "city manager" in captured["query"].lower()
    assert out is not None
    assert out["name"] == "Pat Manager"  # the school superintendent was skipped


def test_linkedin_person_rejects_role_titled_card_over_a_person(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A title-led card ('IT Director - City of Kemah') must never become a person
    named 'IT Director'; a later real-person card is preferred (H2)."""

    def _fake_search(query: str, limit: int = 5) -> list[dict[str, str]]:
        """A role-titled result first, then a genuine person."""
        return [
            {
                "url": "https://www.linkedin.com/in/kemah-it",
                "title": "IT Director - City of Kemah | LinkedIn",
            },
            {
                "url": "https://www.linkedin.com/in/jane-doe",
                "title": "Jane Doe - City Manager - City of Kemah | LinkedIn",
            },
        ]

    monkeypatch.setattr(finder, "_search", _fake_search)
    out = finder.linkedin_person("City of Kemah", "TX")
    assert out is not None
    assert out["name"] == "Jane Doe"  # the role-titled card was rejected, not split


def test_linkedin_person_returns_none_when_only_role_cards_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When every result is a role/org card, return None rather than a fabricated
    person Lead (H2)."""

    def _fake_search(query: str, limit: int = 5) -> list[dict[str, str]]:
        """Only role/org-titled cards, no real person name."""
        return [
            {
                "url": "https://www.linkedin.com/in/kemah-it",
                "title": "IT Director - City of Kemah | LinkedIn",
            },
            {
                "url": "https://www.linkedin.com/in/kemah-pw",
                "title": "Public Works Director - City of Kemah | LinkedIn",
            },
        ]

    monkeypatch.setattr(finder, "_search", _fake_search)
    assert finder.linkedin_person("City of Kemah", "TX") is None


def test_find_contact_reports_org_address_for_linkedin_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A LinkedIn-only result still reports the org address that was enriched.

    Live bug 2026-07-18: City of Salmon / East Providence had the address stored
    (200 Main Street / 145 Taunton Ave.) but the reply said "no mailing address"."""
    from grant_watch.enrich import organization_profile
    from grant_watch.slack.contact_enrichment import ContactOutcome

    monkeypatch.setattr(tools.db, "connect", lambda *_a, **_k: sqlite3.connect(":memory:"))
    monkeypatch.setattr(
        tools,
        "enrich_lead_contact",
        lambda *_a, **_k: ContactOutcome(
            "linkedin_only", "Jane Roe", "IT Director", "", "",
            "https://www.linkedin.com/in/jane-roe",
        ),
    )
    monkeypatch.setattr(
        organization_profile,
        "org_enrichment_summary",
        lambda *_a, **_k: (
            " From the organization's website I also added phone 208-756-3214; "
            "address 200 Main Street, Salmon, 83467."
        ),
    )
    out = tools.find_contact(3035)
    assert "Jane Roe" in out  # the LinkedIn person is still reported
    assert "200 Main Street, Salmon, 83467" in out  # the address is no longer dropped


def test_scrape_keeps_footer_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """The scrape requests full-page content (onlyMainContent=false) so an org's
    address / general email / phone in the FOOTER are not dropped (live 2026-07-18:
    City of Melrose's street address was stripped by Firecrawl main-content mode)."""
    captured: dict[str, object] = {}

    class _Resp:
        """Minimal Firecrawl response stub."""

        def raise_for_status(self) -> None:
            """No HTTP error."""

        def json(self) -> dict[str, object]:
            """Return markdown that includes footer text."""
            return {"data": {"markdown": "562 Main Street, Melrose, MA 02176"}}

    def _fake_post(
        _url: str,
        headers: object = None,
        json: dict[str, object] | None = None,
        timeout: int = 0,
    ) -> _Resp:
        """Capture the request body Firecrawl would receive."""
        captured.update(json or {})
        return _Resp()

    monkeypatch.setattr(finder, "_fc_headers", lambda: {})
    monkeypatch.setattr(finder.requests, "post", _fake_post)
    out = finder._scrape("https://cityofmelrose.org/")
    assert captured["onlyMainContent"] is False
    assert "562 Main Street" in out


# ------------------------------------------------------------ finder: reach vs not-found
def test_finder_raises_unreachable_when_search_never_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every search angle erroring means we could not look — raise, don't return None."""

    def boom(*_a: object, **_k: object) -> list[dict]:
        """Provide test-local behavior for boom."""
        raise requests.RequestException("down")

    monkeypatch.setattr(finder, "_search", boom)
    with pytest.raises(SourceUnreachable):
        finder.find_contact("Castle Rock School District", "WA")


def test_finder_raises_unreachable_when_no_page_is_readable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Search works but every page is blocked/empty — still 'could not look'."""
    monkeypatch.setattr(
        finder,
        "_search",
        lambda *_a, **_k: [
            {
                "url": "https://crschools.org/staff",
                "title": "Castle Rock School District staff",
            }
        ],
    )
    monkeypatch.setattr(finder, "_scrape", lambda *_a, **_k: "")  # blocked page
    with pytest.raises(SourceUnreachable):
        finder.find_contact("Castle Rock School District", "WA")


def test_finder_returns_none_when_pages_read_but_nothing_verifiable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real page that yields no verifiable contact is a TRUTHFUL not_found (None)."""
    monkeypatch.setattr(
        finder,
        "_search",
        lambda *_a, **_k: [
            {
                "url": "https://crschools.org/staff",
                "title": "Castle Rock School District staff",
            }
        ],
    )
    monkeypatch.setattr(finder, "_scrape", lambda *_a, **_k: "x" * 400)  # real content
    monkeypatch.setattr(finder, "_extract", lambda *_a, **_k: None)  # clean negative
    assert finder.find_contact("Castle Rock School District", "WA") is None


def test_contact_fields_require_independent_page_evidence() -> None:
    """A verified email cannot smuggle an invented title or phone into storage."""
    page = "Jane Doe — jdoe@crschools.org — Technology Director — (360) 555-0100"
    assert finder._text_field_on_page(page, "Technology Director") is True
    assert finder._text_field_on_page(page, "Chief Security Officer") is False
    assert finder._phone_on_page(page, "360-555-0100") is True
    assert finder._phone_on_page(page, "360-555-9999") is False


def test_search_result_must_bind_to_named_entity() -> None:
    """A directory/near-name result cannot become the organization's official site."""
    assert (
        finder._looks_official(
            "Castle Rock School District",
            "WA",
            {
                "url": "https://crschools.org/staff",
                "title": "Castle Rock School District staff",
            },
        )
        is True
    )
    assert (
        finder._looks_official(
            "Orange Unified School District",
            "CA",
            {
                "url": "https://orangecountywater.example/staff",
                "title": "Orange County Water Authority",
            },
        )
        is False
    )


# ------------------------------------------------------------ enrich_lead_contact honesty
def test_enrich_unreachable_records_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An outage returns 'unreachable' and writes NO contact row — a retry can re-look."""
    conn, lead_id = _lead(tmp_path)

    def raise_unreachable(*_a: object, **_k: object) -> ContactCandidate:
        """Provide test-local behavior for raise unreachable."""
        raise SourceUnreachable("down")

    monkeypatch.setattr(finder, "find_contact", raise_unreachable)
    outcome = tools.enrich_lead_contact(conn, lead_id)
    assert outcome.status == "unreachable"
    assert (
        db.contacts_for_lead(conn, lead_id) == []
    )  # nothing fabricated, nothing final


def _stub_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
    person: dict[str, str] | None,
    general_email: str,
) -> None:
    """Script the fallback chain: LinkedIn person and org-profile mailbox."""
    from grant_watch.enrich import organization_profile

    monkeypatch.setattr(finder, "linkedin_person", lambda *_a, **_k: person)
    profile = organization_profile.OrgProfile(
        general_email=general_email,
        source_url="https://example.org/contact" if general_email else "",
        status="found" if general_email else "not_found",
    )
    monkeypatch.setattr(
        organization_profile, "enrich_org_profile", lambda *_a, **_k: profile
    )


def test_enrich_genuine_miss_records_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only after site, LinkedIn, AND org mailbox all miss is the lead not_found."""
    conn, lead_id = _lead(tmp_path)
    monkeypatch.setattr(finder, "find_contact", lambda *_a, **_k: None)
    _stub_fallbacks(monkeypatch, person=None, general_email="")
    outcome = tools.enrich_lead_contact(conn, lead_id)
    assert outcome.status == "not_found"
    rows = db.contacts_for_lead(conn, lead_id)
    assert len(rows) == 1 and rows[0]["contact_status"] == "not_found"
    assert rows[0]["email"] is None


def test_enrich_falls_back_to_linkedin_and_org_mailbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No on-site person -> a LinkedIn name plus the org's general mailbox.

    Chase's rule: every school and city has an email somewhere — a bare
    not_found without trying LinkedIn and the org mailbox is a failed lookup."""
    conn, lead_id = _lead(tmp_path)
    monkeypatch.setattr(finder, "find_contact", lambda *_a, **_k: None)
    _stub_fallbacks(
        monkeypatch,
        person={
            "name": "Dana Roe",
            "title": "Technology Director",
            "url": "https://www.linkedin.com/in/dana-roe",
        },
        general_email="info@example.org",
    )
    outcome = tools.enrich_lead_contact(conn, lead_id)
    assert outcome.status == "linkedin_org_email"
    assert outcome.name == "Dana Roe"
    assert outcome.email == "info@example.org"
    saved = db.contacts_for_lead(conn, lead_id)
    assert any(c["contact_status"] == "linkedin_only" for c in saved)


def test_enrich_falls_back_to_org_mailbox_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No person anywhere -> the org's verified general mailbox, clearly labeled."""
    conn, lead_id = _lead(tmp_path)
    monkeypatch.setattr(finder, "find_contact", lambda *_a, **_k: None)
    _stub_fallbacks(monkeypatch, person=None, general_email="office@example.org")
    outcome = tools.enrich_lead_contact(conn, lead_id)
    assert outcome.status == "org_email"
    assert outcome.email == "office@example.org"
    assert outcome.name == ""
    # Not marked not_found: a usable mailbox was honestly found.
    rows = db.contacts_for_lead(conn, lead_id)
    assert not any(c["contact_status"] == "not_found" for c in rows)


def test_enrich_verified_saves_contact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A verified candidate is persisted and returned with its real fields."""
    conn, lead_id = _lead(tmp_path)
    cand = ContactCandidate(
        name="Jane Doe",
        title="Technology Director",
        email="jdoe@crschools.org",
        phone="",
        source_url="https://crschools.org/staff",
        confidence="high",
    )
    monkeypatch.setattr(finder, "find_contact", lambda *_a, **_k: cand)
    outcome = tools.enrich_lead_contact(conn, lead_id)
    assert outcome.status == "verified" and outcome.email == "jdoe@crschools.org"
    rows = db.contacts_for_lead(conn, lead_id)
    assert rows[0]["contact_status"] == "verified"
    assert rows[0]["email"] == "jdoe@crschools.org"


def test_enrich_reuses_existing_verified_without_researching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An already-verified contact is returned as-is; finder is never called again."""
    conn, lead_id = _lead(tmp_path)
    db.save_contact(
        conn,
        lead_id,
        "Sam Smith",
        "IT Director",
        "ssmith@crschools.org",
        "",
        "https://crschools.org/it",
        "high",
    )

    def fail(*_a: object, **_k: object) -> ContactCandidate:
        """Provide test-local behavior for fail."""
        raise AssertionError("finder must not run when a verified contact exists")

    monkeypatch.setattr(finder, "find_contact", fail)
    outcome = tools.enrich_lead_contact(conn, lead_id)
    assert outcome.status == "verified" and outcome.email == "ssmith@crschools.org"
