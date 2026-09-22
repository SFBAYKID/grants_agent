"""Award-style district names bind to NCES names only when the match is unique.

On 2026-09-22 only 5 of 16 Oregon award districts matched NCES exactly, so 11
school districts had no official website and the contact search could not find
their staff pages. Names below are the live NCES names read that day. Offline.
"""

from __future__ import annotations

import pytest

from grant_watch.enrich import nces


def _or(*names: str) -> list[nces.NCESDistrict]:
    """Build a fake Oregon district list from NCES-style names."""
    return [
        nces.NCESDistrict(f"41{index:05d}", name, "OR", "", 100)
        for index, name in enumerate(names)
    ]


OREGON = _or(
    "Harrisburg SD 7J",
    "Central Linn SD 552",
    "Winston-Dillard SD 116",
    "Lane ESD",
    "Scio SD 95",
    "Sheridan SD 48J",
    "Molalla River SD 35",
    "Riddle SD 70",
    "Colton SD 53",
    "Central SD 13J",
    "Central Curry SD 1",
)


@pytest.mark.parametrize(
    ("award_name", "nces_name"),
    [
        ("HARRISBURG SCHOOL DISTRICT 7", "Harrisburg SD 7J"),
        ("CENTRAL LINN SCHOOL DISTRICT 552C", "Central Linn SD 552"),
        ("WINSTON DILLARD SCHOOL DISTRICT", "Winston-Dillard SD 116"),
        ("LANE EDUCATION SERVICE DISTRICT", "Lane ESD"),
        ("SCIO SCHOOL DISTRICT", "Scio SD 95"),
        ("SHERIDAN SCHOOL DISTRICT", "Sheridan SD 48J"),
        ("MOLALLA RIVER SCHOOL DISTRICT", "Molalla River SD 35"),
        ("RIDDLE SCHOOL DISTRICT 70", "Riddle SD 70"),
    ],
)
def test_award_spellings_bind_to_the_one_nces_district(
    award_name: str, nces_name: str
) -> None:
    """Joint letters, ESD spelling and an unstated number are not identity."""
    match = nces.match_district(award_name, OREGON)
    assert match is not None and match.name == nces_name


@pytest.mark.parametrize(
    "award_name",
    [
        # A stated number must match: Riddle is 70, not 71.
        "RIDDLE SCHOOL DISTRICT 71",
        # County legal names are not NCES place names (Colton is SD 53); no guess.
        "CLACKAMAS COUNTY SCHOOL DISTRICT NO 53",
        # A private school is not the public district of the same place.
        "SHERIDAN SCHOOL",
        "SCIO CHRISTIAN SCHOOL",
    ],
)
def test_near_misses_bind_nothing(award_name: str) -> None:
    """Every looser tier still refuses anything it cannot pin to one district."""
    assert nces.match_district(award_name, OREGON) is None


def test_two_districts_sharing_a_place_name_bind_neither() -> None:
    """Uniqueness is per tier: two Sheridans with different numbers is no match."""
    both = _or("Sheridan SD 48J", "Sheridan SD 5")
    assert nces.match_district("SHERIDAN SCHOOL DISTRICT", both) is None
