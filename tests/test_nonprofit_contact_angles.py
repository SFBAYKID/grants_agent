"""A synagogue is not searched for a superintendent.

NSGP recipients — congregations, camps, museums, community centers — became a lead
segment on 2026-09-01. The contact finder knew only two kinds of organization, school
and city, and defaulted to school. Asking "The Postville Shul IA superintendent contact
email" spends four real Firecrawl searches to find nobody: a bill for a guaranteed
not_found, on the segment that now supplies the freshest awards in the product.
"""

from __future__ import annotations

import pytest

from grant_watch.enrich.finder import _angles_for, _org_kind


@pytest.mark.parametrize(
    "entity",
    [
        "THE POSTVILLE SHUL",
        "LIFE CHURCH",
        "Chabad Lubavitch of the Quad Cities, Inc.",
        "Islamic and Culture Center Bosniak of Des Moines",
        "ST. MARY CHURCH OF SOLON, IOWA",
        "Jewish Federation of Delaware Inc",
        "Christ the King Roman Catholic Church of Fort Des Moines",
        "Valley Evangelical Free Church of West Des Moines",
    ],
)
def test_real_nsgp_recipients_are_recognised_as_nonprofits(entity: str) -> None:
    """Every one of these is a real production entity name from the freshest 25."""
    assert _org_kind(entity) == "nonprofit"


@pytest.mark.parametrize(
    "entity",
    ["SAINT ANNES EPISCOPAL SCHOOL", "CHICAGO JEWISH DAY SCHOOL"],
)
def test_a_religious_day_school_is_a_nonprofit_not_a_district(entity: str) -> None:
    """THE ORDERING TEST, and the reason nonprofit is checked first.

    Both names match the school pattern. Both are NSGP recipients whose security
    contact is an administrator, not a superintendent — so a school word must not win
    the tie the way it correctly does for "X County School District".
    """
    assert _org_kind(entity) == "nonprofit"


@pytest.mark.parametrize(
    ("entity", "kind"),
    [
        ("GOBLES PUBLIC SCHOOLS", "school"),
        ("Castle Rock School District 401", "school"),
        ("MOUNT HOREB AREA SCHOOL DISTRICT", "school"),
        ("CITY OF TACOMA", "city"),
        ("Village of Oak Park", "city"),
    ],
)
def test_districts_and_cities_are_unchanged(entity: str, kind: str) -> None:
    """THE CONTROL. A pattern that swallowed real districts would pass every test
    above while silently sending every school lead down the wrong search angles."""
    assert _org_kind(entity) == kind


def test_each_kind_gets_its_own_search_angles() -> None:
    """The routing, not just the label — the label alone spends nothing differently."""
    nonprofit = _angles_for("THE POSTVILLE SHUL")
    school = _angles_for("GOBLES PUBLIC SCHOOLS")
    city = _angles_for("CITY OF TACOMA")
    assert "executive director" in nonprofit[0]
    assert "technology director" in school[0]
    assert "city manager" in city[0]
    assert len({nonprofit, school, city}) == 3


def test_a_nonprofit_is_never_asked_for_a_superintendent() -> None:
    """The specific waste this fixes, asserted on the emitted queries."""
    angles = " ".join(_angles_for("Christ the King Roman Catholic Church"))
    assert "superintendent" not in angles
    assert "principal" not in angles
