"""A phone number must never be attributed to a person who does not own it.

Grant collects two different phone facts: a named person's direct line, verified
verbatim on the page it came from, and the organization's main switchboard. They
were previously merged — the Salesforce Lead payload silently fell back to the org
line with no disclosure, so an SDR opening that record dialled a district
switchboard believing it was the named person's number. A LinkedIn-sourced person
never has a phone of their own, so the fallback fired precisely on the contacts
whose identity was least verified.

Search output had the mirror-image bug: it carried no phone at all, which is why
Grant told a rep its sources "never carry phone numbers".
"""

from __future__ import annotations

from grant_watch.enrich.salesforce_contact_records import choose_phone
from grant_watch.slack.search_enrichment import (
    _CONTACT_COLUMNS,
    _contact_cell,
)
from grant_watch.slack.search_presentation import contact_suffix


def test_every_cell_shape_matches_the_column_header() -> None:
    """A short cell would shift every later column in the exported spreadsheet."""
    for cell in (
        _contact_cell(status="error"),
        _contact_cell(status="not checked (time budget)"),
        _contact_cell(name="A", title="B", email="c@d.test", status="verified"),
        _contact_cell(
            status="verified", phone="503-555-0100", org_phone="503-555-0000"
        ),
    ):
        assert len(cell) == len(_CONTACT_COLUMNS)


def test_a_verified_contacts_direct_line_reaches_the_summary() -> None:
    """The phone Grant already collected must actually be shown to the rep.

    This is the assertion that catches a widened column being silently dropped by
    a positional unpack of the first four cells.
    """
    cell = _contact_cell(
        name="Dana Reyes",
        title="Director of Technology",
        email="dreyes@district.test",
        status="verified",
        phone="503-555-0100",
    )
    suffix = contact_suffix(cell)
    assert "503-555-0100" in suffix
    assert "direct" in suffix


def test_an_org_switchboard_is_never_rendered_as_a_linkedin_persons_number() -> None:
    """The person is named and the number is the org's — the wording must say so."""
    cell = _contact_cell(
        name="Dana Reyes",
        title="Director of Technology",
        status="linkedin_only",
        org_phone="503-555-0000",
    )
    suffix = contact_suffix(cell)
    assert "Dana Reyes" in suffix
    assert "main line 503-555-0000" in suffix
    # The person has no direct line, so nothing may present one.
    assert "direct" not in suffix


def test_a_verified_person_can_carry_both_numbers_distinctly() -> None:
    """Both facts are true at once and must stay distinguishable."""
    suffix = contact_suffix(
        _contact_cell(
            name="Dana Reyes",
            title="IT Director",
            email="d@x.test",
            status="verified",
            phone="503-555-0100",
            org_phone="503-555-0000",
        )
    )
    assert "direct 503-555-0100" in suffix
    assert "main line 503-555-0000" in suffix


def test_choose_phone_labels_a_direct_line_and_an_org_fallback() -> None:
    """The Salesforce payload must know which kind of number it is writing."""
    person = {"phone": "503-555-0100", "name": "Dana Reyes"}
    org_only = {"phone": "", "name": "Dana Reyes"}
    # The alias exists only when a current typed evidence row proves the projection.
    lead = {
        "org_phone": "503-555-0000",
        "evidenced_org_phone": "503-555-0000",
        "org_profile_status": "found",
    }
    assert choose_phone(person, lead) == ("503-555-0100", "direct")
    assert choose_phone(org_only, lead) == ("503-555-0000", "org_general")
    assert choose_phone(
        org_only,
        {"org_phone": "503-555-9999", "org_profile_status": "found"},
    ) == ("", "")
