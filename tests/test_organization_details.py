"""Verified official-site answers for Grant lead-thread location questions."""

from __future__ import annotations

import pytest

from grant_watch.enrich.finder import OfficialSite
from grant_watch.enrich.organization_profile import OrganizationProfile
from grant_watch.slack import organization


def test_verified_location_answer_uses_only_profile_fields(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Grant presents the code-verified address, phone, website, and exact source."""
    monkeypatch.setattr(
        organization.finder, "find_official_site",
        lambda *_args, **_kwargs: OfficialSite(
            "birminghamcharter.com", "https://www.birminghamcharter.com/contact",
            "official school result"))
    monkeypatch.setattr(
        organization, "fetch_profile", lambda *_args: OrganizationProfile(
            website="https://birminghamcharter.com/", street="17000 Haynes Street",
            city="Van Nuys", state="CA", postal_code="91406",
            main_phone="818-758-5200",
            source_url="https://www.birminghamcharter.com/contact"))
    reply = organization.find_organization_details(
        "BIRMINGHAM COMMUNITY CHARTER HIGH SCHOOL", "CA")
    assert "Birmingham Community Charter High School" in reply
    assert "17000 Haynes Street, Van Nuys, CA, 91406" in reply
    assert "818-758-5200" in reply
    assert "https://www.birminghamcharter.com/contact" in reply


def test_unreadable_official_page_does_not_invent_address(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """An official root may be shown while an unverified address remains absent."""
    monkeypatch.setattr(
        organization.finder, "find_official_site",
        lambda *_args, **_kwargs: OfficialSite(
            "birminghamcharter.com", "https://www.birminghamcharter.com/", "official"))
    monkeypatch.setattr(
        organization, "fetch_profile",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("blocked")))
    reply = organization.find_organization_details("Birmingham School", "CA")
    assert "Birmingham School" in reply and "couldn’t verify a street address" in reply
    assert "17000" not in reply
