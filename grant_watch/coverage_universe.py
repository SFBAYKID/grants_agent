"""Authoritative county-equivalent universe and per-entity research queue.

Why: one county source per state cannot prove nationwide county research. Census's
public 2025 Gazetteer supplies stable county-equivalent GEOIDs and names without an
API key. This module parses that snapshot, links known catalog sources explicitly,
and writes one small state shard so every entity retains an honest research status.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import tempfile
from dataclasses import dataclass, fields
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import requests


ROOT = Path(__file__).resolve().parent.parent
COUNTY_UNIVERSE_URL = (
    "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/"
    "2025_Gazetteer/2025_Gaz_counties_national.zip"
)
COUNTY_UNIVERSE_SHA256 = (
    "4c90d0f805779923b5958ab13d0c1e9b99fe4932b786bfcf75dd739bb2dcb4ea"
)
COUNTY_UNIVERSE_VINTAGE = "2025"
COUNTY_ENTITY_COUNT = 3_144
TASK_ROOT = ROOT / "data" / "source_catalog" / "coverage_tasks" / "counties"
LINKS_PATH = ROOT / "data" / "source_catalog" / "county_source_links.csv"
RESEARCH_STATUSES = frozenset(
    {"candidate_found", "not_applicable", "not_researched", "researched_not_found"}
)


@dataclass(frozen=True)
class CountyEntity:
    """One 2025 Census county or county-equivalent entity."""

    entity_id: str
    state: str
    entity_name: str


@dataclass(frozen=True)
class CountySourceLink:
    """Explicit relationship between one county entity and catalog source candidate."""

    entity_id: str
    source_id: str
    linked_on: str
    link_method: str
    notes: str


@dataclass(frozen=True)
class CountyCoverageTask:
    """Durable per-county research status derived from the universe and explicit links."""

    entity_id: str
    state: str
    entity_name: str
    entity_kind: str
    universe_vintage: str
    research_status: str
    source_id: str
    last_checked_on: str
    notes: str


def parse_county_gazetteer(zip_bytes: bytes) -> list[CountyEntity]:
    """Parse the official national Gazetteer ZIP and retain the 50 states plus DC."""
    from .source_catalog import US_JURISDICTIONS

    with ZipFile(BytesIO(zip_bytes)) as archive:
        members = archive.namelist()
        if len(members) != 1:
            raise ValueError("county Gazetteer ZIP must contain exactly one member")
        text = archive.read(members[0]).decode("utf-8-sig")
    rows = list(csv.DictReader(text.splitlines(), delimiter="|"))
    required = {"USPS", "GEOID", "NAME"}
    if not rows or not required <= set(rows[0]):
        raise ValueError("county Gazetteer columns are missing")
    entities = [
        CountyEntity(
            entity_id=row["GEOID"].strip(),
            state=row["USPS"].strip().upper(),
            entity_name=row["NAME"].strip(),
        )
        for row in rows
        if row["USPS"].strip().upper() in US_JURISDICTIONS
    ]
    if any(
        not entity.entity_id.isdigit() or len(entity.entity_id) != 5
        for entity in entities
    ):
        raise ValueError("county Gazetteer contains an invalid GEOID")
    ids = [entity.entity_id for entity in entities]
    if len(ids) != len(set(ids)):
        raise ValueError("county Gazetteer contains duplicate GEOIDs")
    return sorted(entities, key=lambda entity: (entity.state, entity.entity_id))


def fetch_county_universe() -> list[CountyEntity]:
    """Fetch the pinned no-key Census snapshot and fail loudly if its bytes drift."""
    response = requests.get(COUNTY_UNIVERSE_URL, timeout=60)
    response.raise_for_status()
    digest = hashlib.sha256(response.content).hexdigest()
    if digest != COUNTY_UNIVERSE_SHA256:
        raise ValueError(
            "Census county universe hash changed; review the new snapshot before promotion"
        )
    entities = parse_county_gazetteer(response.content)
    if len(entities) != COUNTY_ENTITY_COUNT:
        raise ValueError(
            f"Census county universe count changed: {len(entities)} != {COUNTY_ENTITY_COUNT}"
        )
    return entities


def load_county_links(path: Path = LINKS_PATH) -> list[CountySourceLink]:
    """Load manually reviewed county-to-source links and reject duplicate entities."""
    expected = {field.name for field in fields(CountySourceLink)}
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    links: list[CountySourceLink] = []
    for row_number, row in enumerate(rows, start=2):
        if set(row) != expected:
            raise ValueError(f"county link row {row_number}: columns mismatch")
        link = CountySourceLink(**{name: row[name].strip() for name in expected})
        if not link.entity_id.isdigit() or len(link.entity_id) != 5:
            raise ValueError(f"county link row {row_number}: invalid entity_id")
        if not link.source_id or not link.linked_on:
            raise ValueError(f"county link row {row_number}: incomplete link evidence")
        links.append(link)
    entity_ids = [link.entity_id for link in links]
    if len(entity_ids) != len(set(entity_ids)):
        raise ValueError("duplicate county source links")
    return links


def build_county_tasks(
    entities: list[CountyEntity],
    links: list[CountySourceLink],
    structural_exceptions: dict[str, tuple[str, str]],
) -> list[CountyCoverageTask]:
    """Build exact per-entity statuses without promoting unresearched counties."""
    entity_ids = {entity.entity_id for entity in entities}
    orphan_links = sorted({link.entity_id for link in links} - entity_ids)
    if orphan_links:
        raise ValueError(f"county links reference unknown GEOIDs: {orphan_links}")
    link_map = {link.entity_id: link for link in links}
    tasks: list[CountyCoverageTask] = []
    for entity in entities:
        link = link_map.get(entity.entity_id)
        exception = structural_exceptions.get(entity.state)
        if link is not None:
            status = "candidate_found"
            source_id = link.source_id
            checked_on = link.linked_on
            notes = link.notes
        elif exception is not None:
            status, checked_on = exception
            source_id = ""
            notes = "State-layer structural exception applies to this county-equivalent"
        else:
            status = "not_researched"
            source_id = ""
            checked_on = ""
            notes = ""
        tasks.append(
            CountyCoverageTask(
                entity_id=entity.entity_id,
                state=entity.state,
                entity_name=entity.entity_name,
                entity_kind="county_equivalent",
                universe_vintage=COUNTY_UNIVERSE_VINTAGE,
                research_status=status,
                source_id=source_id,
                last_checked_on=checked_on,
                notes=notes,
            )
        )
    return tasks


def _write_rows(path: Path, tasks: list[CountyCoverageTask]) -> None:
    """Write one deterministic county-task shard with the typed column contract."""
    fieldnames = [field.name for field in fields(CountyCoverageTask)]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for task in sorted(tasks, key=lambda item: item.entity_id):
            writer.writerow({name: getattr(task, name) for name in fieldnames})


def write_county_tasks(tasks: list[CountyCoverageTask], root: Path = TASK_ROOT) -> None:
    """Write one sub-1000-line CSV per state and remove retired state shards."""
    root.mkdir(parents=True, exist_ok=True)
    states = sorted({task.state for task in tasks})
    for state in states:
        _write_rows(
            root / f"{state}.csv", [task for task in tasks if task.state == state]
        )
    for path in root.glob("*.csv"):
        if path.stem not in states:
            path.unlink()


def load_county_tasks(root: Path = TASK_ROOT) -> list[CountyCoverageTask]:
    """Load and validate all state shards as one deterministic coverage universe."""
    expected = {field.name for field in fields(CountyCoverageTask)}
    tasks: list[CountyCoverageTask] = []
    for path in sorted(root.glob("*.csv")):
        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        for row_number, row in enumerate(rows, start=2):
            if set(row) != expected:
                raise ValueError(f"{path.name} row {row_number}: columns mismatch")
            task = CountyCoverageTask(**{name: row[name].strip() for name in expected})
            if task.state != path.stem:
                raise ValueError(f"{path.name} row {row_number}: state/shard mismatch")
            if task.research_status not in RESEARCH_STATUSES:
                raise ValueError(
                    f"{path.name} row {row_number}: invalid research status"
                )
            if (task.research_status == "candidate_found") != bool(task.source_id):
                raise ValueError(
                    f"{path.name} row {row_number}: source/status mismatch"
                )
            tasks.append(task)
    ids = [task.entity_id for task in tasks]
    if len(ids) != len(set(ids)):
        raise ValueError("county task shards contain duplicate GEOIDs")
    return tasks


def validate_county_tasks(
    tasks: list[CountyCoverageTask],
    links: list[CountySourceLink],
    catalog_ids: set[str],
) -> None:
    """Validate universe completeness and every source link against the catalog."""
    if len(tasks) != COUNTY_ENTITY_COUNT:
        raise ValueError(f"county task count mismatch: {len(tasks)}")
    linked_ids = {link.entity_id for link in links}
    task_links = {task.entity_id for task in tasks if task.source_id}
    if task_links != linked_ids:
        raise ValueError("county task shards do not match reviewed source links")
    missing_sources = sorted({link.source_id for link in links} - catalog_ids)
    if missing_sources:
        raise ValueError(
            f"county links reference missing catalog sources: {missing_sources}"
        )


def task_drift(expected: list[CountyCoverageTask], root: Path = TASK_ROOT) -> list[str]:
    """Compare generated state shards without modifying the repository."""
    with tempfile.TemporaryDirectory(prefix="county-coverage-") as temp_name:
        expected_root = Path(temp_name)
        write_county_tasks(expected, expected_root)
        expected_names = {path.name for path in expected_root.glob("*.csv")}
        actual_names = {path.name for path in root.glob("*.csv")}
        drift = [f"missing:{name}" for name in sorted(expected_names - actual_names)]
        drift.extend(
            f"unexpected:{name}" for name in sorted(actual_names - expected_names)
        )
        for name in sorted(expected_names & actual_names):
            if (expected_root / name).read_bytes() != (root / name).read_bytes():
                drift.append(f"stale:{name}")
    return drift


def _exception_map() -> dict[str, tuple[str, str]]:
    """Return only structural county exceptions that apply to every entity in a state."""
    from .source_catalog import JurisdictionLevel, load_coverage_exceptions

    return {
        exception.state: (exception.status.value, exception.checked_on)
        for exception in load_coverage_exceptions()
        if exception.jurisdiction_level == JurisdictionLevel.COUNTY
        and exception.status.value == "not_applicable"
    }


def main(argv: list[str] | None = None) -> int:
    """Refresh from Census or validate the stored county research universe offline."""
    parser = argparse.ArgumentParser(
        description="Manage county-equivalent coverage tasks"
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="fetch the pinned Census snapshot and rewrite shards",
    )
    args = parser.parse_args(argv)
    from .source_catalog import load_catalog

    links = load_county_links()
    catalog_ids = {entry.source_id for entry in load_catalog()}
    if args.refresh:
        entities = fetch_county_universe()
        expected = build_county_tasks(entities, links, _exception_map())
        write_county_tasks(expected)
    tasks = load_county_tasks()
    validate_county_tasks(tasks, links, catalog_ids)
    counts = {status: 0 for status in sorted(RESEARCH_STATUSES)}
    for task in tasks:
        counts[task.research_status] += 1
    print(
        f"verified: validated {len(tasks)} county-equivalent tasks; "
        + ", ".join(f"{status}={count}" for status, count in counts.items())
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
