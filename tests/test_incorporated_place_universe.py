"""Tests for the pinned Census incorporated-place research universe."""

from __future__ import annotations

from collections import Counter
from io import BytesIO
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from grant_watch.entity_coverage import load_entity_tasks, load_source_links
from grant_watch.incorporated_place_universe import (
    FUNCTIONAL_STATUS_COUNTS,
    GAPS_PATH,
    LINKS_PATH,
    PLACE_ENTITY_COUNT,
    PLACE_NAMESPACE,
    PLACE_UNIVERSE_SHA256,
    TASK_ROOT,
    fetch_place_universe,
    load_place_gaps,
    parse_place_gazetteer,
)
from grant_watch.source_catalog import JurisdictionLevel, US_JURISDICTIONS, load_catalog


def _place_zip(
    rows: list[str],
    header: str = "USPS|GEOID|NAME|LSAD|FUNCSTAT|",
    member: str = "places.txt",
) -> bytes:
    """Build a minimal in-memory place Gazetteer ZIP."""
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr(member, "\n".join([header, *rows]))
    return buffer.getvalue()


def test_place_parser_classifies_every_functional_status() -> None:
    """Only A/B places are researchable; S/F/I/N remain explicit structural rows."""
    entities = parse_place_gazetteer(
        _place_zip(
            [
                "CA|0600001|Active city|25|A|",
                "LA|2200001|Partial city|25|B|",
                "TX|4800001|Example CDP|57|S|",
                "GA|1300001|Balance|00|F|",
                "CO|0800001|Inactive town|43|I|",
                "DC|1100001|Nonfunctioning city|25|N|",
                "PR|7200001|Territory city|25|A|",
            ]
        ),
        expected_count=6,
        expected_status_counts={"A": 1, "B": 1, "F": 1, "I": 1, "N": 1, "S": 1},
    )
    assert Counter(entity.entity_kind for entity in entities) == {
        "census_designated_place": 1,
        "fictitious_place": 1,
        "inactive_incorporated_place": 1,
        "incorporated_place": 2,
        "nonfunctioning_incorporated_place": 1,
    }
    assert Counter(entity.entity_disposition for entity in entities) == {
        "researchable": 2,
        "structural_F": 1,
        "structural_I": 1,
        "structural_N": 1,
        "structural_S": 1,
    }


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        (b"not-a-zip", "not a ZIP"),
        (_place_zip(["CA|0600001|Test|25|A|"], "USPS|NAME|"), "columns"),
        (_place_zip(["CA|bad|Test|25|A|"]), "invalid GEOID"),
        (_place_zip(["CA|0600001|Test|25|A|"], member="../place.txt"), "unsafe"),
    ],
)
def test_place_parser_rejects_malformed_payloads(payload: bytes, error: str) -> None:
    """Malformed archives and rows fail before becoming research tasks."""
    with pytest.raises(ValueError, match=error):
        parse_place_gazetteer(
            payload, expected_count=1, expected_status_counts={"A": 1}
        )


def test_place_parser_rejects_multiple_members_and_duplicate_geoids() -> None:
    """Ambiguous archives and duplicate place identities fail validation."""
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr(
            "one.txt", "USPS|GEOID|NAME|LSAD|FUNCSTAT|\nCA|0600001|One|25|A|"
        )
        archive.writestr(
            "two.txt", "USPS|GEOID|NAME|LSAD|FUNCSTAT|\nCA|0600002|Two|25|A|"
        )
    with pytest.raises(ValueError, match="exactly one"):
        parse_place_gazetteer(
            buffer.getvalue(), expected_count=2, expected_status_counts={"A": 2}
        )
    with pytest.raises(ValueError, match="duplicate GEOIDs"):
        parse_place_gazetteer(
            _place_zip(
                [
                    "CA|0600001|One|25|A|",
                    "CA|0600001|One again|25|A|",
                ]
            ),
            expected_count=2,
            expected_status_counts={"A": 2},
        )


def test_fetch_rejects_changed_place_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    """Byte drift halts a place refresh before task replacement."""
    response = SimpleNamespace(content=b"changed", raise_for_status=lambda: None)
    monkeypatch.setattr(
        "grant_watch.incorporated_place_universe.requests.get",
        lambda *_args, **_kwargs: response,
    )
    with pytest.raises(ValueError, match="hash changed"):
        fetch_place_universe()


def test_brewster_town_gap_does_not_link_the_statistical_cdp() -> None:
    """The active Massachusetts town stays a future MCD task, not a false CDP link."""
    tasks = {task.geoid: task for task in load_entity_tasks(TASK_ROOT)}
    links = load_source_links(LINKS_PATH)
    gaps = load_place_gaps(GAPS_PATH)
    assert tasks["2507945"].entity_name == "Brewster CDP"
    assert tasks["2507945"].research_status == "not_applicable"
    assert all(link.geoid != "2507945" for link in links)
    assert [(gap.source_id, gap.gap_type) for gap in gaps] == [
        ("ma.brewster.procurement", "minor_civil_division_not_in_place_universe")
    ]


def test_canonical_place_universe_matches_all_pins_and_source_partitions() -> None:
    """Committed tasks preserve exact Census classifications and catalog coverage."""
    tasks = load_entity_tasks(TASK_ROOT)
    links = load_source_links(LINKS_PATH)
    gaps = load_place_gaps()
    assert len(tasks) == PLACE_ENTITY_COUNT
    assert {task.entity_namespace for task in tasks} == {PLACE_NAMESPACE}
    assert {task.state for task in tasks} == US_JURISDICTIONS
    assert Counter(task.entity_disposition for task in tasks) == {
        "researchable": 19_471,
        "structural_F": 8,
        "structural_I": 35,
        "structural_N": 4,
        "structural_S": 12_540,
    }
    assert Counter(task.research_status for task in tasks) == {
        "candidate_found": 14,
        "not_applicable": 12_587,
        "not_researched": 19_457,
    }
    city_source_ids = {
        entry.source_id
        for entry in load_catalog()
        if entry.jurisdiction_level == JurisdictionLevel.CITY
    }
    assert {link.source_id for link in links} | {gap.source_id for gap in gaps} == (
        city_source_ids
    )
    assert (
        max(
            len(path.read_text(encoding="utf-8").splitlines())
            for path in TASK_ROOT.rglob("*.csv")
        )
        == 299
    )
    assert PLACE_UNIVERSE_SHA256 == (
        "49644173a453469d9bd77fb7a493b027f87567e209edaf2078aac7543ac2ee29"
    )
    assert FUNCTIONAL_STATUS_COUNTS == {
        "A": 19_469,
        "B": 2,
        "F": 8,
        "I": 35,
        "N": 4,
        "S": 12_540,
    }
