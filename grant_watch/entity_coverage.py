"""Shared typed storage for large Census-backed entity research universes.

Why: school-district and incorporated-place universes are too large for one CSV
and have many-to-many relationships with source publishers. This module keeps
namespaced identities, evidence links, research-state derivation, deterministic
sharding, atomic refreshes, and the constitutional line cap consistent.
"""

from __future__ import annotations

import csv
import re
import shutil
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, fields
from pathlib import Path
from urllib.parse import urlparse


LINE_CAP = 1_000
RESEARCH_STATUSES = frozenset(
    {"candidate_found", "not_applicable", "not_researched", "researched_not_found"}
)
LINK_RELATIONS = frozenset(
    {
        "cooperative_member",
        "direct_publisher",
        "required_portal",
        "shared_administrator",
    }
)


@dataclass(frozen=True)
class EntityKey:
    """Collision-safe identity for a GEOID within one Census namespace."""

    entity_namespace: str
    geoid: str


@dataclass(frozen=True)
class CoverageEntity:
    """One entity from a pinned authoritative geographic universe."""

    entity_namespace: str
    geoid: str
    state: str
    entity_name: str
    entity_kind: str
    universe_vintage: str
    entity_disposition: str

    @property
    def key(self) -> EntityKey:
        """Return the collision-safe key used by tasks and source links."""
        return EntityKey(self.entity_namespace, self.geoid)


@dataclass(frozen=True)
class EntitySourceLink:
    """One reviewed many-to-many relationship between an entity and source."""

    entity_namespace: str
    geoid: str
    source_id: str
    relation: str
    evidence_url: str
    evidence_checked_on: str
    link_method: str
    notes: str

    @property
    def entity_key(self) -> EntityKey:
        """Return the namespaced entity key referenced by this link."""
        return EntityKey(self.entity_namespace, self.geoid)


@dataclass(frozen=True)
class EntityCoverageTask:
    """Durable research state for one namespaced geographic entity."""

    entity_namespace: str
    geoid: str
    state: str
    entity_name: str
    entity_kind: str
    universe_vintage: str
    entity_disposition: str
    research_status: str
    last_checked_on: str
    notes: str

    @property
    def key(self) -> EntityKey:
        """Return the collision-safe key used to derive linked status."""
        return EntityKey(self.entity_namespace, self.geoid)


@dataclass(frozen=True)
class StructuralStatus:
    """Evidence that one entity cannot have its own procurement source."""

    research_status: str
    checked_on: str
    notes: str


def _fieldnames(model: type[object]) -> tuple[str, ...]:
    """Return a stable dataclass column contract."""
    return tuple(field.name for field in fields(model))


def _has_linebreak(value: str) -> bool:
    """Return whether a CSV value could violate the physical line cap."""
    return "\n" in value or "\r" in value


def load_source_links(path: Path, geoid_length: int = 7) -> list[EntitySourceLink]:
    """Load reviewed many-to-many links and reject malformed evidence."""
    fieldnames = _fieldnames(EntitySourceLink)
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    links: list[EntitySourceLink] = []
    for row_number, row in enumerate(rows, start=2):
        if set(row) != set(fieldnames):
            raise ValueError(f"{path.name} row {row_number}: columns mismatch")
        link = EntitySourceLink(**{name: row[name].strip() for name in fieldnames})
        if any(_has_linebreak(getattr(link, name)) for name in fieldnames):
            raise ValueError(f"{path.name} row {row_number}: embedded line break")
        if not link.geoid.isdigit() or len(link.geoid) != geoid_length:
            raise ValueError(f"{path.name} row {row_number}: invalid geoid")
        if not re.fullmatch(r"[a-z][a-z0-9_]+", link.entity_namespace):
            raise ValueError(f"{path.name} row {row_number}: invalid namespace")
        if link.relation not in LINK_RELATIONS:
            raise ValueError(f"{path.name} row {row_number}: invalid relation")
        evidence = urlparse(link.evidence_url)
        if evidence.scheme != "https" or not evidence.netloc:
            raise ValueError(f"{path.name} row {row_number}: invalid evidence URL")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", link.evidence_checked_on):
            raise ValueError(f"{path.name} row {row_number}: invalid evidence date")
        if not link.source_id or not link.link_method or not link.notes:
            raise ValueError(f"{path.name} row {row_number}: incomplete link evidence")
        links.append(link)
    unique_keys = {
        (link.entity_namespace, link.geoid, link.source_id, link.relation)
        for link in links
    }
    if len(unique_keys) != len(links):
        raise ValueError(f"{path.name}: duplicate source relationship")
    return links


def build_entity_tasks(
    entities: list[CoverageEntity],
    links: list[EntitySourceLink],
    structural_statuses: dict[EntityKey, StructuralStatus] | None = None,
) -> list[EntityCoverageTask]:
    """Derive task states from any reviewed links and structural evidence."""
    entity_keys = {entity.key for entity in entities}
    link_keys = {link.entity_key for link in links}
    orphan_links = sorted(
        link_keys - entity_keys, key=lambda key: (key.entity_namespace, key.geoid)
    )
    if orphan_links:
        raise ValueError(f"source links reference unknown entities: {orphan_links}")
    structural = structural_statuses or {}
    orphan_structural = sorted(
        set(structural) - entity_keys,
        key=lambda key: (key.entity_namespace, key.geoid),
    )
    if orphan_structural:
        raise ValueError(
            f"structural statuses reference unknown entities: {orphan_structural}"
        )
    links_by_entity: dict[EntityKey, list[EntitySourceLink]] = defaultdict(list)
    for link in links:
        links_by_entity[link.entity_key].append(link)
    tasks: list[EntityCoverageTask] = []
    for entity in entities:
        entity_links = links_by_entity.get(entity.key, [])
        exception = structural.get(entity.key)
        if entity_links:
            status = "candidate_found"
            checked_on = max(link.evidence_checked_on for link in entity_links)
            notes = f"{len(entity_links)} reviewed source link(s)"
        elif exception is not None:
            status = exception.research_status
            checked_on = exception.checked_on
            notes = exception.notes
        else:
            status = "not_researched"
            checked_on = ""
            notes = ""
        tasks.append(
            EntityCoverageTask(
                entity_namespace=entity.entity_namespace,
                geoid=entity.geoid,
                state=entity.state,
                entity_name=entity.entity_name,
                entity_kind=entity.entity_kind,
                universe_vintage=entity.universe_vintage,
                entity_disposition=entity.entity_disposition,
                research_status=status,
                last_checked_on=checked_on,
                notes=notes,
            )
        )
    return sorted(tasks, key=lambda task: (task.state, task.geoid))


def _relative_shard(task: EntityCoverageTask) -> Path:
    """Return a stable state/local-GEOID-prefix shard path for one task."""
    if len(task.geoid) < 3 or not task.geoid.isdigit():
        raise ValueError(f"invalid GEOID for sharding: {task.geoid}")
    return Path(task.state) / f"{task.geoid[2]}.csv"


def _write_task_rows(path: Path, tasks: list[EntityCoverageTask]) -> None:
    """Write one deterministic LF-only task shard under the physical line cap."""
    if len(tasks) + 1 > LINE_CAP:
        raise ValueError(f"task shard would exceed {LINE_CAP} lines: {path}")
    fieldnames = _fieldnames(EntityCoverageTask)
    for task in tasks:
        if any(_has_linebreak(getattr(task, name)) for name in fieldnames):
            raise ValueError(f"task contains embedded line break: {task.key}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for task in sorted(tasks, key=lambda item: item.geoid):
            writer.writerow({name: getattr(task, name) for name in fieldnames})


def write_entity_tasks(tasks: list[EntityCoverageTask], root: Path) -> None:
    """Write deterministic shards and remove only retired generated CSV shards."""
    grouped: dict[Path, list[EntityCoverageTask]] = defaultdict(list)
    for task in tasks:
        grouped[_relative_shard(task)].append(task)
    expected_paths = {root / relative for relative in grouped}
    root.mkdir(parents=True, exist_ok=True)
    for relative, shard_tasks in sorted(grouped.items(), key=lambda item: str(item[0])):
        _write_task_rows(root / relative, shard_tasks)
    for path in sorted(root.rglob("*.csv")):
        if path not in expected_paths:
            path.unlink()
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()


def replace_entity_tasks(tasks: list[EntityCoverageTask], root: Path) -> None:
    """Atomically replace a complete universe after all staged shards validate."""
    root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{root.name}-", dir=root.parent
    ) as temp_name:
        staged_root = Path(temp_name) / root.name
        write_entity_tasks(tasks, staged_root)
        loaded = load_entity_tasks(staged_root)
        if loaded != sorted(tasks, key=lambda task: (task.state, task.geoid)):
            raise ValueError("staged task universe failed round-trip validation")
        backup = root.parent / f".{root.name}.backup"
        if backup.exists():
            shutil.rmtree(backup)
        if root.exists():
            root.rename(backup)
        try:
            staged_root.rename(root)
        except Exception:
            if backup.exists() and not root.exists():
                backup.rename(root)
            raise
        if backup.exists():
            shutil.rmtree(backup)


def load_entity_tasks(root: Path, geoid_length: int = 7) -> list[EntityCoverageTask]:
    """Load and validate every state/prefix task shard under one universe root."""
    fieldnames = _fieldnames(EntityCoverageTask)
    tasks: list[EntityCoverageTask] = []
    for path in sorted(root.glob("*/*.csv")):
        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        if len(path.read_text(encoding="utf-8").splitlines()) > LINE_CAP:
            raise ValueError(f"{path}: exceeds {LINE_CAP} physical lines")
        for row_number, row in enumerate(rows, start=2):
            if set(row) != set(fieldnames):
                raise ValueError(f"{path} row {row_number}: columns mismatch")
            task = EntityCoverageTask(
                **{name: row[name].strip() for name in fieldnames}
            )
            if any(_has_linebreak(getattr(task, name)) for name in fieldnames):
                raise ValueError(f"{path} row {row_number}: embedded line break")
            if not task.geoid.isdigit() or len(task.geoid) != geoid_length:
                raise ValueError(f"{path} row {row_number}: invalid geoid")
            if task.state != path.parent.name:
                raise ValueError(f"{path} row {row_number}: state/shard mismatch")
            if task.geoid[2] != path.stem:
                raise ValueError(f"{path} row {row_number}: prefix/shard mismatch")
            if task.research_status not in RESEARCH_STATUSES:
                raise ValueError(f"{path} row {row_number}: invalid research status")
            tasks.append(task)
    keys = [task.key for task in tasks]
    if len(keys) != len(set(keys)):
        raise ValueError(f"{root}: duplicate entity task keys")
    return sorted(tasks, key=lambda task: (task.state, task.geoid))


def validate_entity_tasks(
    tasks: list[EntityCoverageTask],
    links: list[EntitySourceLink],
    catalog_ids: set[str],
    expected_count: int,
    allowed_kinds: frozenset[str],
    expected_namespace: str,
) -> None:
    """Validate universe size, namespace, kinds, links, and catalog relationships."""
    if len(tasks) != expected_count:
        raise ValueError(
            f"entity task count mismatch: {len(tasks)} != {expected_count}"
        )
    if {task.entity_namespace for task in tasks} != {expected_namespace}:
        raise ValueError("task universe has an unexpected namespace")
    invalid_kinds = sorted({task.entity_kind for task in tasks} - allowed_kinds)
    if invalid_kinds:
        raise ValueError(f"unexpected entity kinds: {invalid_kinds}")
    linked_keys = {link.entity_key for link in links}
    candidate_keys = {
        task.key for task in tasks if task.research_status == "candidate_found"
    }
    if candidate_keys != linked_keys:
        raise ValueError("candidate task states do not match reviewed source links")
    missing_sources = sorted({link.source_id for link in links} - catalog_ids)
    if missing_sources:
        raise ValueError(f"links reference missing catalog sources: {missing_sources}")
    for task in tasks:
        if task.research_status in {"researched_not_found", "not_applicable"}:
            if not task.last_checked_on or not task.notes:
                raise ValueError(f"evidenced empty task is incomplete: {task.key}")


def entity_task_drift(expected: list[EntityCoverageTask], root: Path) -> list[str]:
    """Compare generated task shards without modifying the repository."""
    with tempfile.TemporaryDirectory(prefix="entity-coverage-") as temp_name:
        expected_root = Path(temp_name)
        write_entity_tasks(expected, expected_root)
        expected_files = {
            path.relative_to(expected_root): path
            for path in expected_root.rglob("*.csv")
        }
        actual_files = {path.relative_to(root): path for path in root.rglob("*.csv")}
        drift = [
            f"missing:{path}" for path in sorted(expected_files.keys() - actual_files)
        ]
        drift.extend(
            f"unexpected:{path}"
            for path in sorted(actual_files.keys() - expected_files)
        )
        for relative in sorted(expected_files.keys() & actual_files):
            if (
                expected_files[relative].read_bytes()
                != actual_files[relative].read_bytes()
            ):
                drift.append(f"stale:{relative}")
    return drift


def research_status_counts(tasks: list[EntityCoverageTask]) -> Counter[str]:
    """Return research-status counts for CLI and documentation output."""
    return Counter(task.research_status for task in tasks)
