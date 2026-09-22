"""A LinkedIn profile is evidence about an organization only if it names it.

On 2026-09-22 the LinkedIn fallback saved four clearly wrong people for Oregon
awardees, because the first human-shaped search result won. The result titles
below are the shapes seen that day. Offline.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from grant_watch.enrich import finder


def _only(result: dict[str, str]) -> Callable[..., list[dict[str, str]]]:
    """A search stub that returns exactly one result."""

    def _search(query: str, limit: int = 5) -> list[dict[str, str]]:
        """Return the one canned LinkedIn result."""
        return [result]

    return _search


@pytest.mark.parametrize(
    ("entity", "title"),
    [
        ("ASANTE", "Jo Reyes - ALABASTER CITY SCHOOL DISTRICT | LinkedIn"),
        (
            "B'NAI BRITH MENS CAMP ASSOCIATION",
            "Sam Cohen - Camp Director - Camp Bnai Brith of Montreal | LinkedIn",
        ),
        (
            "MULTNOMAH COUNTY SCHOOL DISTRICT U2-20 JT",
            "Lee Park - Director of Development at Osprey Productions | LinkedIn",
        ),
        (
            "ARCH CAPE-FALCON COVE BEACH COMMUNITY CLUB, INC",
            "Ana Bell - Creative Director at Cannabis Wave | LinkedIn",
        ),
    ],
)
def test_a_profile_that_does_not_name_the_org_is_rejected(
    monkeypatch: pytest.MonkeyPatch, entity: str, title: str
) -> None:
    """Each of the four live mismatches must now find nobody."""
    monkeypatch.setattr(
        finder,
        "_search",
        _only({"url": "https://www.linkedin.com/in/x", "title": title}),
    )
    assert finder.linkedin_person(entity, "OR") is None


def test_a_profile_that_names_the_org_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    """Numbers and filler words are ignored; the place name must be there."""
    monkeypatch.setattr(
        finder,
        "_search",
        _only(
            {
                "url": "https://www.linkedin.com/in/kim-tech",
                "title": "Kim Tran - Technology Director - Harrisburg School "
                "District | LinkedIn",
            }
        ),
    )
    found = finder.linkedin_person("HARRISBURG SCHOOL DISTRICT 7", "OR")
    assert found is not None and found["name"] == "Kim Tran"


def test_the_org_may_be_named_in_the_snippet_rather_than_the_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LinkedIn often puts the employer in the description, not the title."""
    monkeypatch.setattr(
        finder,
        "_search",
        _only(
            {
                "url": "https://www.linkedin.com/in/dana-it",
                "title": "Dana Lee - IT Director | LinkedIn",
                "description": "Experience: Asante · Medford, Oregon",
            }
        ),
    )
    found = finder.linkedin_person("ASANTE", "OR")
    assert found is not None and found["name"] == "Dana Lee"
