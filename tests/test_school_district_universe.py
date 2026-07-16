"""Tests for the pinned nationwide school-district research universe."""

from __future__ import annotations

from collections import Counter
from io import BytesIO
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from grant_watch.entity_coverage import load_entity_tasks, load_source_links
from grant_watch.school_district_universe import (
    LINKS_PATH,
    PLACEHOLDER_COUNT,
    SCHOOL_ENTITY_COUNT,
    SCHOOL_NAMESPACE,
    SCHOOL_SPECS,
    TASK_ROOT,
    SchoolGazetteerSpec,
    fetch_school_universe,
    parse_school_gazetteer,
    suggest_school_links,
)
from grant_watch.source_catalog import JurisdictionLevel, US_JURISDICTIONS, load_catalog


def _school_zip(
    rows: list[str],
    header: str = "USPS|GEOID|NAME|",
    member: str = "school.txt",
) -> bytes:
    """Build a minimal in-memory school Gazetteer ZIP."""
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr(member, "\n".join([header, *rows]))
    return buffer.getvalue()


def _spec(expected_count: int = 2) -> SchoolGazetteerSpec:
    """Build one focused school-layer contract for parser tests."""
    return SchoolGazetteerSpec(
        code="test",
        entity_kind="unified_school_district",
        url="https://example.gov/test.zip",
        sha256="0" * 64,
        expected_count=expected_count,
    )


def test_school_parser_filters_territories_and_preserves_placeholder() -> None:
    """US/DC rows load while territories filter and undefined districts remain explicit."""
    entities = parse_school_gazetteer(
        _school_zip(
            [
                "CA|0600001|Test School District|",
                "TX|4899997|School District Not Defined|",
                "PR|7200001|Puerto Rico District|",
            ]
        ),
        _spec(),
    )
    assert [entity.geoid for entity in entities] == ["0600001", "4899997"]
    assert entities[0].entity_disposition == "researchable"
    assert entities[1].entity_disposition == "statistical_placeholder"


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        (b"not-a-zip", "not a ZIP"),
        (_school_zip(["CA|0600001|Test|"], "USPS|NAME|"), "columns"),
        (_school_zip(["CA|bad|Test|"]), "invalid GEOID"),
        (_school_zip(["CA|0600001|Test|"], member="../school.txt"), "unsafe"),
    ],
)
def test_school_parser_rejects_malformed_payloads(payload: bytes, error: str) -> None:
    """Malformed archives and rows fail before entering the research universe."""
    with pytest.raises(ValueError, match=error):
        parse_school_gazetteer(payload, _spec(1))


def test_school_parser_rejects_multiple_members_and_duplicates() -> None:
    """Ambiguous ZIPs and duplicate GEOIDs cannot create mixed universes."""
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("one.txt", "USPS|GEOID|NAME|\nCA|0600001|One|")
        archive.writestr("two.txt", "USPS|GEOID|NAME|\nCA|0600002|Two|")
    with pytest.raises(ValueError, match="exactly one"):
        parse_school_gazetteer(buffer.getvalue(), _spec())
    with pytest.raises(ValueError, match="duplicate GEOIDs"):
        parse_school_gazetteer(
            _school_zip(["CA|0600001|One|", "CA|0600001|One again|"]),
            _spec(),
        )


def test_fetch_rejects_changed_pinned_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    """Byte drift stops refresh before any changed layer is parsed or written."""
    response = SimpleNamespace(content=b"changed", raise_for_status=lambda: None)
    monkeypatch.setattr(
        "grant_watch.school_district_universe.requests.get",
        lambda *_args, **_kwargs: response,
    )
    with pytest.raises(ValueError, match="hash changed"):
        fetch_school_universe()


def test_link_suggestions_are_same_state_and_non_mutating() -> None:
    """Name ranking helps review but does not promote a source automatically."""
    tasks = load_entity_tasks(TASK_ROOT)
    candidates = suggest_school_links(
        tasks, "ca.test", "San Diego Unified School District", "CA"
    )
    assert candidates[0].geoid == "0634320"
    assert candidates[0].entity_name == "San Diego City Unified School District"
    assert all(candidate.geoid.startswith("06") for candidate in candidates)


def test_canonical_school_universe_matches_all_pins_and_links() -> None:
    """Committed tasks match the exact official counts and reviewed source mappings."""
    tasks = load_entity_tasks(TASK_ROOT)
    links = load_source_links(LINKS_PATH)
    assert len(tasks) == SCHOOL_ENTITY_COUNT
    assert {task.entity_namespace for task in tasks} == {SCHOOL_NAMESPACE}
    assert {task.state for task in tasks} == US_JURISDICTIONS
    assert Counter(task.entity_kind for task in tasks) == {
        "elementary_school_district": 1_971,
        "school_administrative_area": 52,
        "secondary_school_district": 478,
        "unified_school_district": 10_862,
    }
    assert Counter(task.research_status for task in tasks) == {
        "candidate_found": 66,
        "not_applicable": PLACEHOLDER_COUNT,
        "not_researched": 13_278,
    }
    assert (
        sum(task.entity_disposition == "statistical_placeholder" for task in tasks)
        == PLACEHOLDER_COUNT
    )
    school_source_ids = {
        entry.source_id
        for entry in load_catalog()
        if entry.jurisdiction_level == JurisdictionLevel.SCHOOL_DISTRICT
    }
    assert {link.source_id for link in links} == school_source_ids
    assert Counter(link.source_id for link in links)["nh.sau29.bids"] == 7
    assert (
        Counter(link.source_id for link in links)["mt.billings_schools.procurement"]
        == 2
    )
    assert (
        max(
            len(path.read_text(encoding="utf-8").splitlines())
            for path in TASK_ROOT.rglob("*.csv")
        )
        == 578
    )


def test_school_snapshot_constants_are_exact() -> None:
    """Every official layer retains its independently verified URL/hash/count pin."""
    assert [(spec.code, spec.expected_count, spec.sha256) for spec in SCHOOL_SPECS] == [
        (
            "elsd",
            1_971,
            "fe5adfe0588e418fecac84c60303e8d18b75abb7c9712b0c9058f69e5ea0d8c9",
        ),
        (
            "scsd",
            478,
            "24026d5ff622aef46af595c7724ac42372327767eaa66d601bf4dc8bf2e52a3f",
        ),
        (
            "unsd",
            10_862,
            "72fe1cc606aa9bfe6d95b246c22aff9fdac2215b2f9cb286ba432b3177193e3f",
        ),
        (
            "sdadm",
            52,
            "a86de88012021c3c3a52fa0807faa0399bd20e3cc0ac3ca801d8199c6400935e",
        ),
    ]
