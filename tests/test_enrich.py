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
    db.upsert_lead(conn, Lead(
        item=RawItem(source="usaspending:16.071", item_id="A1", title="SVPP",
                     entity="Castle Rock School District 401", state="WA",
                     program="SVPP", amount=500_000.0, start="2025-10-01",
                     end="2028-09-30", url="https://x.gov/a", raw={}),
        grade=LeadGrade.GOLD))
    return conn, int(conn.execute("SELECT id FROM leads").fetchone()["id"])


# ------------------------------------------------------------ finder: reach vs not-found
def test_finder_raises_unreachable_when_search_never_returns(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Every search angle erroring means we could not look — raise, don't return None."""
    def boom(*_a: object, **_k: object) -> list[dict]:
        raise requests.RequestException("down")

    monkeypatch.setattr(finder, "_search", boom)
    with pytest.raises(SourceUnreachable):
        finder.find_contact("Castle Rock School District", "WA")


def test_finder_raises_unreachable_when_no_page_is_readable(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Search works but every page is blocked/empty — still 'could not look'."""
    monkeypatch.setattr(finder, "_search",
                        lambda *_a, **_k: [{
                            "url": "https://crschools.org/staff",
                            "title": "Castle Rock School District staff",
                        }])
    monkeypatch.setattr(finder, "_scrape", lambda *_a, **_k: "")  # blocked page
    with pytest.raises(SourceUnreachable):
        finder.find_contact("Castle Rock School District", "WA")


def test_finder_returns_none_when_pages_read_but_nothing_verifiable(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A real page that yields no verifiable contact is a TRUTHFUL not_found (None)."""
    monkeypatch.setattr(finder, "_search",
                        lambda *_a, **_k: [{
                            "url": "https://crschools.org/staff",
                            "title": "Castle Rock School District staff",
                        }])
    monkeypatch.setattr(finder, "_scrape", lambda *_a, **_k: "x" * 400)  # real content
    monkeypatch.setattr(finder, "_extract", lambda *_a, **_k: None)      # clean negative
    assert finder.find_contact("Castle Rock School District", "WA") is None


def test_finder_collects_multiple_distinct_official_contacts(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Finding IT does not stop the official-site search before Facilities is found."""
    results = [
        {"url": "https://district.example/technology",
         "title": "Example School District Washington technology"},
        {"url": "https://district.example/facilities",
         "title": "Example School District Washington facilities"},
    ]
    candidates = {
        "https://district.example/technology": ContactCandidate(
            "Taylor Tech", "Technology Director", "tech@district.example", "",
            "https://district.example/technology", "high"),
        "https://district.example/facilities": ContactCandidate(
            "Frank Facilities", "Facilities Director", "facilities@district.example", "",
            "https://district.example/facilities", "high"),
    }
    monkeypatch.setattr(finder, "_search", lambda *_a, **_k: results)
    monkeypatch.setattr(finder, "_scrape", lambda *_a, **_k: "x" * 400)
    monkeypatch.setattr(
        finder, "_extract", lambda _text, _entity, url: candidates[url])
    found = finder.find_contacts("Example School District", "WA")
    assert [candidate.email for candidate in found] == [
        "tech@district.example", "facilities@district.example"]


def test_contact_fields_require_independent_page_evidence() -> None:
    """A verified email cannot smuggle an invented title or phone into storage."""
    page = "Jane Doe — jdoe@crschools.org — Technology Director — (360) 555-0100"
    assert finder._text_field_on_page(page, "Technology Director") is True
    assert finder._text_field_on_page(page, "Chief Security Officer") is False
    assert finder._phone_on_page(page, "360-555-0100") is True
    assert finder._phone_on_page(page, "360-555-9999") is False


def test_search_result_must_bind_to_named_entity() -> None:
    """A directory/near-name result cannot become the organization's official site."""
    assert finder._looks_official(
        "Castle Rock School District", "WA",
        {"url": "https://crschools.org/staff",
         "title": "Castle Rock School District staff"},
    ) is True
    assert finder._looks_official(
        "Orange Unified School District", "CA",
        {"url": "https://orangecountywater.example/staff",
         "title": "Orange County Water Authority"},
    ) is False


def test_linkedin_result_must_name_requested_organization(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A LinkedIn profile for a same-name person at another school is rejected."""
    monkeypatch.setattr(finder, "_search", lambda *_args, **_kwargs: [
        {
            "url": "https://www.linkedin.com/in/pat-person",
            "title": "Pat Person - Principal | LinkedIn",
            "description": "Principal at Birmingham City Schools in Alabama",
        },
    ])
    assert finder.linkedin_person(
        "Birmingham Community Charter High School", "CA") is None


def test_linkedin_result_returns_typed_organization_match(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A matching search listing returns only its copied name, role, and URL."""
    monkeypatch.setattr(finder, "_search", lambda *_args, **_kwargs: [
        {
            "url": "https://www.linkedin.com/in/pat-person",
            "title": "Pat Person - Technology Director | LinkedIn",
            "description": "Birmingham Community Charter High School",
        },
    ])
    person = finder.linkedin_person(
        "Birmingham Community Charter High School", "CA")
    assert person == finder.LinkedInPerson(
        "Pat Person", "Technology Director", "https://www.linkedin.com/in/pat-person",
        "Pat Person - Technology Director | LinkedIn | Birmingham Community Charter High School")


def test_linkedin_timeout_returns_no_result_instead_of_hanging(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A bounded Firecrawl timeout becomes an honest non-result for the Slack turn."""
    def timeout(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
        raise requests.Timeout("slow")

    monkeypatch.setattr(finder, "_search", timeout)
    assert finder.linkedin_person(
        "Birmingham Community Charter High School", "CA") is None


# ------------------------------------------------------------ enrich_lead_contact honesty
def test_enrich_unreachable_records_nothing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An outage returns 'unreachable' and writes NO contact row — a retry can re-look."""
    conn, lead_id = _lead(tmp_path)

    def raise_unreachable(*_a: object, **_k: object) -> ContactCandidate:
        raise SourceUnreachable("down")

    monkeypatch.setattr(finder, "find_contact", raise_unreachable)
    outcome = tools.enrich_lead_contact(conn, lead_id)
    assert outcome.status == "unreachable"
    assert db.contacts_for_lead(conn, lead_id) == []  # nothing fabricated, nothing final


def test_enrich_genuine_miss_records_not_found(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A true miss (finder returned None) is recorded as not_found, honestly and finally."""
    conn, lead_id = _lead(tmp_path)
    monkeypatch.setattr(finder, "find_contact", lambda *_a, **_k: None)
    outcome = tools.enrich_lead_contact(conn, lead_id)
    assert outcome.status == "not_found"
    rows = db.contacts_for_lead(conn, lead_id)
    assert len(rows) == 1 and rows[0]["contact_status"] == "not_found"
    assert rows[0]["email"] is None


def test_enrich_verified_saves_contact(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A verified candidate is persisted and returned with its real fields."""
    conn, lead_id = _lead(tmp_path)
    cand = ContactCandidate(name="Jane Doe", title="Technology Director",
                            email="jdoe@crschools.org", phone="",
                            source_url="https://crschools.org/staff", confidence="high")
    monkeypatch.setattr(finder, "find_contact", lambda *_a, **_k: cand)
    outcome = tools.enrich_lead_contact(conn, lead_id)
    assert outcome.status == "verified" and outcome.email == "jdoe@crschools.org"
    rows = db.contacts_for_lead(conn, lead_id)
    assert rows[0]["contact_status"] == "verified"
    assert rows[0]["email"] == "jdoe@crschools.org"


def test_enrich_reuses_existing_verified_without_researching(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An already-verified contact is returned as-is; finder is never called again."""
    conn, lead_id = _lead(tmp_path)
    db.save_contact(conn, lead_id, "Sam Smith", "IT Director", "ssmith@crschools.org",
                    "", "https://crschools.org/it", "high")

    def fail(*_a: object, **_k: object) -> ContactCandidate:
        raise AssertionError("finder must not run when a verified contact exists")

    monkeypatch.setattr(finder, "find_contact", fail)
    outcome = tools.enrich_lead_contact(conn, lead_id)
    assert outcome.status == "verified" and outcome.email == "ssmith@crschools.org"
