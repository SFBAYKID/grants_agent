"""Tests for the authoritative county-equivalent research universe."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest

from grant_watch.coverage_universe import (
    COUNTY_ENTITY_COUNT,
    CountyEntity,
    CountySourceLink,
    build_county_tasks,
    load_county_links,
    load_county_tasks,
    parse_county_gazetteer,
    task_drift,
    validate_county_tasks,
    write_county_tasks,
)
from grant_watch.source_catalog import US_JURISDICTIONS, load_catalog


def _gazetteer_zip(rows: list[str], header: str = "USPS|GEOID|NAME|") -> bytes:
    """Build a minimal in-memory Gazetteer ZIP for deterministic parser tests."""
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("counties.txt", "\n".join([header, *rows]))
    return buffer.getvalue()


def _entity(entity_id: str = "06001", state: str = "CA") -> CountyEntity:
    """Build one typed county entity for focused task-generation tests."""
    return CountyEntity(entity_id=entity_id, state=state, entity_name="Test County")


def _link(entity_id: str = "06001") -> CountySourceLink:
    """Build one typed reviewed source link for focused validation tests."""
    return CountySourceLink(
        entity_id=entity_id,
        source_id="ca.test.bids",
        linked_on="2026-07-15",
        link_method="publisher_exact_name",
        notes="Test County",
    )


def test_parse_county_gazetteer_filters_non_us_jurisdictions() -> None:
    """The parser retains the 50 states and DC but excludes territories."""
    entities = parse_county_gazetteer(
        _gazetteer_zip(
            [
                "CA|06001|Alameda County|",
                "DC|11001|District of Columbia|",
                "PR|72001|Adjuntas Municipio|",
            ]
        )
    )
    assert [(entity.state, entity.entity_id) for entity in entities] == [
        ("CA", "06001"),
        ("DC", "11001"),
    ]


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        (_gazetteer_zip(["CA|06001|Alameda County|"], "USPS|NAME|"), "columns"),
        (_gazetteer_zip(["CA|bad|Alameda County|"]), "invalid GEOID"),
    ],
)
def test_parse_county_gazetteer_rejects_malformed_data(
    payload: bytes, error: str
) -> None:
    """Malformed official-data shapes fail instead of creating incomplete tasks."""
    with pytest.raises(ValueError, match=error):
        parse_county_gazetteer(payload)


def test_build_county_tasks_preserves_evidence_boundaries() -> None:
    """Links, structural exceptions, and untouched entities receive distinct states."""
    entities = [
        _entity(),
        _entity("11001", "DC"),
        _entity("53033", "WA"),
    ]
    tasks = build_county_tasks(
        entities,
        [_link()],
        {"DC": ("not_applicable", "2026-07-15")},
    )
    assert [task.research_status for task in tasks] == [
        "candidate_found",
        "not_applicable",
        "not_researched",
    ]
    assert tasks[0].source_id == "ca.test.bids"
    assert tasks[1].last_checked_on == "2026-07-15"
    assert tasks[2].source_id == ""


def test_build_county_tasks_rejects_orphan_link() -> None:
    """A source link cannot silently refer to a GEOID outside the universe."""
    with pytest.raises(ValueError, match="unknown GEOIDs"):
        build_county_tasks([_entity()], [_link("99999")], {})


def test_county_task_shards_round_trip_and_detect_drift(tmp_path: Path) -> None:
    """Generated state shards round-trip and stale content is reported read-only."""
    tasks = build_county_tasks([_entity(), _entity("53033", "WA")], [_link()], {})
    write_county_tasks(tasks, tmp_path)
    assert load_county_tasks(tmp_path) == tasks
    assert task_drift(tasks, tmp_path) == []
    assert all(b"\r\n" not in path.read_bytes() for path in tmp_path.glob("*.csv"))
    (tmp_path / "CA.csv").write_text("stale\n", encoding="utf-8")
    assert task_drift(tasks, tmp_path) == ["stale:CA.csv"]


def test_validate_county_tasks_rejects_missing_catalog_source() -> None:
    """Reviewed entity links must resolve to a canonical source-catalog entry."""
    linked_task = build_county_tasks([_entity()], [_link()], {})[0]
    tasks = [replace(linked_task, entity_id="00000")]
    tasks.extend(
        replace(
            linked_task,
            entity_id=f"{index:05d}",
            research_status="not_researched",
            source_id="",
        )
        for index in range(1, COUNTY_ENTITY_COUNT)
    )
    links = [replace(_link(), entity_id="00000")]
    with pytest.raises(ValueError, match="missing catalog sources"):
        validate_county_tasks(tasks, links, set())


def test_canonical_county_universe_is_complete_and_linked() -> None:
    """Committed shards exactly match the pinned Census count and reviewed links."""
    tasks = load_county_tasks()
    links = load_county_links()
    entries = load_catalog()
    validate_county_tasks(tasks, links, {entry.source_id for entry in entries})
    assert len(tasks) == COUNTY_ENTITY_COUNT
    assert {task.state for task in tasks} == US_JURISDICTIONS
    assert Counter(task.research_status for task in tasks) == {
        "candidate_found": 56,
        "not_applicable": 15,
        "not_researched": 3_073,
    }
    assert len(links) == 56
