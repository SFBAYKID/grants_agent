"""A verified official website is searched first and its pages count as official.

On 2026-09-22 four Oregon districts got 4 searches each and zero pages read: every
result's snippet failed the name-in-snippet test, though NCES had published each
district's own website. Offline.
"""

from __future__ import annotations

import pytest

from grant_watch.enrich import finder

SITE = "https://www.scio.k12.or.us"


def _page_result(url: str) -> list[dict[str, str]]:
    """One search result whose snippet does NOT name the district."""
    return [{"url": url, "title": "Staff Directory", "description": "Our team"}]


def test_the_official_site_is_searched_first_and_its_page_is_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A snippet that never names the district is still read on its own site."""
    queries: list[str] = []
    scraped: list[str] = []

    def search(query: str, limit: int = 4) -> list[dict[str, str]]:
        """Record the query; return a bare page on the official site."""
        queries.append(query)
        return _page_result("https://www.scio.k12.or.us/staff")

    def scrape(url: str) -> str:
        """Record the page read."""
        scraped.append(url)
        return "x" * 400

    candidate = finder.ContactCandidate(
        "Pat Lee", "Technology Director", "plee@scio.k12.or.us", "", SITE, "high"
    )
    monkeypatch.setattr(finder, "_search", search)
    monkeypatch.setattr(finder, "_scrape", scrape)
    monkeypatch.setattr(finder, "_extract", lambda *_a, **_k: candidate)
    found = finder.find_contact("SCIO SCHOOL DISTRICT", "OR", official_site=SITE)
    assert queries[0] == "site:scio.k12.or.us technology director"
    assert scraped == ["https://www.scio.k12.or.us/staff"]
    assert found is candidate


def test_without_an_official_site_the_same_snippet_is_still_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control: nothing is loosened for a lead with no verified website."""
    monkeypatch.setattr(
        finder,
        "_search",
        lambda *_a, **_k: _page_result("https://www.scio.k12.or.us/x"),
    )
    monkeypatch.setattr(finder, "_scrape", lambda *_a, **_k: pytest.fail("read"))
    with pytest.raises(finder.SourceUnreachable):
        finder.find_contact("SCIO SCHOOL DISTRICT", "OR")


def test_a_different_site_still_needs_the_name_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The known site lifts the test for ITS pages only, never a stranger's."""
    monkeypatch.setattr(
        finder, "_search", lambda *_a, **_k: _page_result("https://directory.test/x")
    )
    monkeypatch.setattr(finder, "_scrape", lambda *_a, **_k: pytest.fail("read"))
    with pytest.raises(finder.SourceUnreachable):
        finder.find_contact("SCIO SCHOOL DISTRICT", "OR", official_site=SITE)


def test_only_a_verified_nces_website_is_handed_to_the_search(
    tmp_path: pytest.TempPathFactory,
) -> None:
    """An unavailable or unchecked NCES website must not lift the name test."""
    from pathlib import Path

    from grant_watch import db
    from grant_watch.models import FundingEventType, Lead, LeadGrade, RawItem
    from grant_watch.slack.contact_enrichment import _verified_nces_website

    conn = db.connect(Path(str(tmp_path)) / "t.db")
    db.upsert_lead(
        conn,
        Lead(
            item=RawItem(
                source="t",
                item_id="1",
                title="award",
                entity="SCIO SCHOOL DISTRICT",
                state="OR",
                program="SVPP",
                amount=1,
                start="",
                end="",
                url="https://source.test/1",
                raw={},
                event_type=FundingEventType.AWARD_OBLIGATED,
            ),
            grade=LeadGrade.GOLD,
        ),
    )
    lead_id = int(conn.execute("SELECT id FROM leads").fetchone()[0])
    conn.execute(
        "UPDATE leads SET nces_website=?, nces_website_status='unavailable'", (SITE,)
    )
    assert _verified_nces_website(db.get_lead(conn, lead_id)) == ""
    conn.execute("UPDATE leads SET nces_website_status='verified'")
    assert _verified_nces_website(db.get_lead(conn, lead_id)) == SITE
