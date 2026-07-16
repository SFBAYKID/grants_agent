"""Tests for namespaced many-to-many entity coverage storage."""

from __future__ import annotations

import csv
from dataclasses import fields, replace
from pathlib import Path

import pytest

from grant_watch.entity_coverage import (
    CoverageEntity,
    EntityCoverageTask,
    EntityKey,
    EntitySourceLink,
    StructuralStatus,
    build_entity_tasks,
    entity_task_drift,
    load_entity_tasks,
    load_source_links,
    replace_entity_tasks,
    validate_entity_tasks,
    write_entity_tasks,
)


def _entity(geoid: str = "0600001") -> CoverageEntity:
    """Build one researchable namespaced entity for focused tests."""
    return CoverageEntity(
        entity_namespace="test_entity",
        geoid=geoid,
        state="CA",
        entity_name=f"Entity {geoid}",
        entity_kind="test_kind",
        universe_vintage="2025",
        entity_disposition="researchable",
    )


def _link(
    geoid: str = "0600001", source_id: str = "ca.test.source"
) -> EntitySourceLink:
    """Build one evidence-bearing entity/source relationship."""
    return EntitySourceLink(
        entity_namespace="test_entity",
        geoid=geoid,
        source_id=source_id,
        relation="direct_publisher",
        evidence_url="https://example.gov/procurement",
        evidence_checked_on="2026-07-15",
        link_method="publisher_exact_name",
        notes="Reviewed official publisher",
    )


def _write_links(path: Path, links: list[EntitySourceLink]) -> None:
    """Write link fixtures with the production column contract."""
    fieldnames = tuple(field.name for field in fields(EntitySourceLink))
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for link in links:
            writer.writerow({name: getattr(link, name) for name in fieldnames})


def test_many_to_many_links_derive_candidate_tasks() -> None:
    """One source may cover many entities and one entity may have many sources."""
    entities = [_entity(), _entity("0600002")]
    links = [
        _link(),
        _link(source_id="ca.test.secondary"),
        _link("0600002"),
    ]
    tasks = build_entity_tasks(entities, links)
    assert [task.research_status for task in tasks] == [
        "candidate_found",
        "candidate_found",
    ]
    assert tasks[0].notes == "2 reviewed source link(s)"
    validate_entity_tasks(
        tasks,
        links,
        {"ca.test.source", "ca.test.secondary"},
        2,
        frozenset({"test_kind"}),
        "test_entity",
    )


def test_structural_and_unresearched_states_remain_distinct() -> None:
    """Structural evidence does not turn untouched entities into researched rows."""
    entities = [_entity(), _entity("0600002")]
    tasks = build_entity_tasks(
        entities,
        [],
        {
            EntityKey("test_entity", "0600001"): StructuralStatus(
                "not_applicable", "2026-07-15", "Statistical placeholder"
            )
        },
    )
    assert [task.research_status for task in tasks] == [
        "not_applicable",
        "not_researched",
    ]


def test_link_loader_allows_cardinality_but_rejects_duplicate_relationship(
    tmp_path: Path,
) -> None:
    """Distinct relationships load while exact duplicate edges fail validation."""
    path = tmp_path / "links.csv"
    links = [_link(), _link(source_id="ca.test.secondary")]
    _write_links(path, links)
    assert load_source_links(path) == links
    _write_links(path, [links[0], links[0]])
    with pytest.raises(ValueError, match="duplicate source relationship"):
        load_source_links(path)


def test_link_loader_rejects_missing_evidence(tmp_path: Path) -> None:
    """A relationship without an HTTPS evidence URL cannot create coverage."""
    path = tmp_path / "links.csv"
    _write_links(path, [replace(_link(), evidence_url="http://example.gov")])
    with pytest.raises(ValueError, match="invalid evidence URL"):
        load_source_links(path)


def test_task_shards_round_trip_detect_drift_and_use_lf(tmp_path: Path) -> None:
    """State/prefix shards are deterministic, drift-aware, and LF-only."""
    tasks = build_entity_tasks([_entity(), _entity("0610002")], [_link()])
    write_entity_tasks(tasks, tmp_path)
    assert load_entity_tasks(tmp_path) == tasks
    assert entity_task_drift(tasks, tmp_path) == []
    assert all(b"\r\n" not in path.read_bytes() for path in tmp_path.rglob("*.csv"))
    path = tmp_path / "CA" / "0.csv"
    path.write_text("stale\n", encoding="utf-8")
    assert entity_task_drift(tasks, tmp_path) == ["stale:CA/0.csv"]


def test_task_loader_rejects_prefix_path_mismatch(tmp_path: Path) -> None:
    """Moving a shard under the wrong prefix cannot silently change identity."""
    tasks = build_entity_tasks([_entity()], [])
    write_entity_tasks(tasks, tmp_path)
    original = tmp_path / "CA" / "0.csv"
    original.rename(tmp_path / "CA" / "9.csv")
    with pytest.raises(ValueError, match="prefix/shard mismatch"):
        load_entity_tasks(tmp_path)


def test_writer_rejects_embedded_newline(tmp_path: Path) -> None:
    """Logical CSV values cannot consume extra physical lines."""
    task = replace(build_entity_tasks([_entity()], [])[0], notes="bad\nline")
    with pytest.raises(ValueError, match="embedded line break"):
        write_entity_tasks([task], tmp_path)


def test_atomic_replace_preserves_prior_universe_on_shard_failure(
    tmp_path: Path,
) -> None:
    """A failed oversized refresh leaves the previously complete universe intact."""
    root = tmp_path / "tasks"
    original = build_entity_tasks([_entity()], [])
    replace_entity_tasks(original, root)
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*.csv")}
    oversized = [
        EntityCoverageTask(
            entity_namespace="test_entity",
            geoid=f"060{index:04d}",
            state="CA",
            entity_name=f"Entity {index}",
            entity_kind="test_kind",
            universe_vintage="2025",
            entity_disposition="researchable",
            research_status="not_researched",
            last_checked_on="",
            notes="",
        )
        for index in range(1_000)
    ]
    with pytest.raises(ValueError, match="exceed"):
        replace_entity_tasks(oversized, root)
    after = {path.relative_to(root): path.read_bytes() for path in root.rglob("*.csv")}
    assert after == before


def test_validator_requires_evidence_for_empty_completed_state() -> None:
    """A completed empty status requires both a date and explanatory evidence."""
    task = replace(
        build_entity_tasks([_entity()], [])[0],
        research_status="researched_not_found",
    )
    with pytest.raises(ValueError, match="evidenced empty task is incomplete"):
        validate_entity_tasks(
            [task], [], set(), 1, frozenset({"test_kind"}), "test_entity"
        )
