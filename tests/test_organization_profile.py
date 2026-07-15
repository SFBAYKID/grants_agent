"""Official organization profile extraction verification tests."""

from __future__ import annotations

from grant_watch.enrich import organization_profile as profile


PAGE = """
Dinuba Unified School District
1327 E. El Monte Way
Dinuba, CA 93618
Phone: 559-595-7200
"""


def test_profile_keeps_only_values_present_on_official_page() -> None:
    """Model candidates cannot pass the code gate unless the page contains them."""
    result = profile.extract_profile(PAGE, "dinuba.k12.ca.us", "https://dinuba.k12.ca.us", {
        "street": "1327 E. El Monte Way", "city": "Dinuba", "state": "CA",
        "postal_code": "93618", "country": "United States",
        "main_phone": "559-595-7200", "linkedin_url": "https://linkedin.com/in/invented",
    })
    assert result.website == "https://dinuba.k12.ca.us/"
    assert result.street == "1327 E. El Monte Way" and result.main_phone == "559-595-7200"
    assert result.country == "" and result.linkedin_url == ""


def test_profile_rejects_invalid_official_domain() -> None:
    """A missing or malformed official domain cannot become a CRM website."""
    try:
        profile.extract_profile(PAGE, "not a domain", "https://source", {})
    except ValueError as exc:
        assert "official domain" in str(exc)
    else:
        raise AssertionError("invalid domain was accepted")
