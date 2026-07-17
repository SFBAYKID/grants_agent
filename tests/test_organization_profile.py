"""Offline tests for the organization-profile verbatim verifiers."""

from __future__ import annotations

from grant_watch.enrich import organization_profile as op


def test_general_email_requires_shared_mailbox_and_verbatim() -> None:
    """A general email must be a shared mailbox AND appear verbatim on the page."""
    page = "Contact us at info@alpha.org or call the office."
    assert op._general_email_on_page(page, "info@alpha.org")
    # A personal-looking address is not treated as the org's general mailbox.
    assert not op._general_email_on_page(page, "jane.smith@alpha.org")
    # Not on the page → rejected even if it looks general.
    assert not op._general_email_on_page(page, "office@alpha.org")


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
