"""Pinned Census place universe and incorporated-place source research queue.

Why: city procurement discovery needs an explicit national denominator. The 2025
Census place Gazetteer contains active incorporated governments as well as CDPs,
fictitious balances, inactive places, and nonfunctioning places. All rows remain
auditable, but only active/partially consolidated governments enter research.
New England towns outside this universe are recorded as explicit coverage gaps.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import re
from collections import Counter
from dataclasses import dataclass, fields
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse
from zipfile import BadZipFile, ZipFile

import requests

from .entity_coverage import (
    CoverageEntity,
    EntityKey,
    EntitySourceLink,
    StructuralStatus,
    build_entity_tasks,
    load_entity_tasks,
    load_source_links,
    replace_entity_tasks,
    research_status_counts,
    validate_entity_tasks,
)


ROOT = Path(__file__).resolve().parent.parent
PLACE_NAMESPACE = "incorporated_place"
PLACE_UNIVERSE_VINTAGE = "2025"
PLACE_UNIVERSE_URL = (
    "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/"
    "2025_Gazetteer/2025_Gaz_place_national.zip"
)
PLACE_UNIVERSE_SHA256 = (
    "49644173a453469d9bd77fb7a493b027f87567e209edaf2078aac7543ac2ee29"
)
PLACE_ENTITY_COUNT = 32_058
FUNCTIONAL_STATUS_COUNTS = {
    "A": 19_469,
    "B": 2,
    "F": 8,
    "I": 35,
    "N": 4,
    "S": 12_540,
}
RESEARCHABLE_FUNCTIONAL_STATUSES = frozenset({"A", "B"})
TASK_ROOT = ROOT / "data" / "source_catalog" / "coverage_tasks" / "incorporated_places"
LINKS_PATH = ROOT / "data" / "source_catalog" / "incorporated_place_source_links.csv"
GAPS_PATH = ROOT / "data" / "source_catalog" / "incorporated_place_source_gaps.csv"
PLACE_KINDS = frozenset(
    {
        "census_designated_place",
        "fictitious_place",
        "inactive_incorporated_place",
        "incorporated_place",
        "nonfunctioning_incorporated_place",
    }
)


@dataclass(frozen=True)
class PlaceUniverseGap:
    """One catalog city source that belongs to a different Census namespace."""

    source_id: str
    state: str
    gap_type: str
    evidence_url: str
    checked_on: str
    notes: str


def _entity_kind(functional_status: str) -> str:
    """Map official FUNCSTAT values to explicit place entity kinds."""
    return {
        "A": "incorporated_place",
        "B": "incorporated_place",
        "F": "fictitious_place",
        "I": "inactive_incorporated_place",
        "N": "nonfunctioning_incorporated_place",
        "S": "census_designated_place",
    }[functional_status]


def parse_place_gazetteer(
    zip_bytes: bytes,
    expected_count: int = PLACE_ENTITY_COUNT,
    expected_status_counts: dict[str, int] | None = None,
) -> list[CoverageEntity]:
    """Parse US/DC places and validate the expected functional classifications."""
    from .source_catalog import US_JURISDICTIONS

    try:
        with ZipFile(BytesIO(zip_bytes)) as archive:
            members = archive.namelist()
            if len(members) != 1:
                raise ValueError("place ZIP must contain exactly one member")
            member = members[0]
            if Path(member).name != member or not member.endswith(".txt"):
                raise ValueError("place ZIP contains an unsafe member")
            text = archive.read(member).decode("utf-8-sig")
    except BadZipFile as exc:
        raise ValueError("place payload is not a ZIP") from exc
    rows = list(csv.DictReader(text.splitlines(), delimiter="|"))
    required = {"USPS", "GEOID", "NAME", "FUNCSTAT", "LSAD"}
    if not rows or not required <= set(rows[0]):
        raise ValueError("place Gazetteer columns are missing")
    selected = [row for row in rows if row["USPS"].strip().upper() in US_JURISDICTIONS]
    counts = Counter(row["FUNCSTAT"].strip() for row in selected)
    expected_statuses = expected_status_counts or FUNCTIONAL_STATUS_COUNTS
    if dict(sorted(counts.items())) != dict(sorted(expected_statuses.items())):
        raise ValueError(f"place functional-status counts changed: {dict(counts)}")
    entities = [
        CoverageEntity(
            entity_namespace=PLACE_NAMESPACE,
            geoid=row["GEOID"].strip(),
            state=row["USPS"].strip().upper(),
            entity_name=row["NAME"].strip(),
            entity_kind=_entity_kind(row["FUNCSTAT"].strip()),
            universe_vintage=PLACE_UNIVERSE_VINTAGE,
            entity_disposition=(
                "researchable"
                if row["FUNCSTAT"].strip() in RESEARCHABLE_FUNCTIONAL_STATUSES
                else f"structural_{row['FUNCSTAT'].strip()}"
            ),
        )
        for row in selected
    ]
    if len(entities) != expected_count:
        raise ValueError(f"place entity count changed: {len(entities)}")
    if any(not entity.geoid.isdigit() or len(entity.geoid) != 7 for entity in entities):
        raise ValueError("place Gazetteer contains an invalid GEOID")
    keys = [entity.key for entity in entities]
    if len(keys) != len(set(keys)):
        raise ValueError("place Gazetteer contains duplicate GEOIDs")
    return sorted(entities, key=lambda entity: (entity.state, entity.geoid))


def fetch_place_universe() -> list[CoverageEntity]:
    """Fetch the pinned no-key place snapshot and reject byte-level drift."""
    response = requests.get(PLACE_UNIVERSE_URL, timeout=60)
    response.raise_for_status()
    digest = hashlib.sha256(response.content).hexdigest()
    if digest != PLACE_UNIVERSE_SHA256:
        raise ValueError("place Gazetteer hash changed; manual review required")
    return parse_place_gazetteer(response.content)


def place_structural_statuses(
    entities: list[CoverageEntity],
) -> dict[EntityKey, StructuralStatus]:
    """Mark every statistical, fictitious, inactive, or nonfunctioning place empty."""
    statuses = {
        entity.key: StructuralStatus(
            research_status="not_applicable",
            checked_on="2026-07-15",
            notes=f"Census FUNCSTAT disposition is {entity.entity_disposition}",
        )
        for entity in entities
        if entity.entity_disposition != "researchable"
    }
    expected = PLACE_ENTITY_COUNT - sum(
        FUNCTIONAL_STATUS_COUNTS[code] for code in RESEARCHABLE_FUNCTIONAL_STATUSES
    )
    if len(statuses) != expected:
        raise ValueError(
            f"place structural count changed: {len(statuses)} != {expected}"
        )
    return statuses


def load_place_gaps(path: Path = GAPS_PATH) -> list[PlaceUniverseGap]:
    """Load evidenced city-source gaps that require another Census namespace."""
    fieldnames = tuple(field.name for field in fields(PlaceUniverseGap))
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    gaps: list[PlaceUniverseGap] = []
    for row_number, row in enumerate(rows, start=2):
        if set(row) != set(fieldnames):
            raise ValueError(f"place gap row {row_number}: columns mismatch")
        gap = PlaceUniverseGap(**{name: row[name].strip() for name in fieldnames})
        evidence = urlparse(gap.evidence_url)
        if evidence.scheme != "https" or not evidence.netloc:
            raise ValueError(f"place gap row {row_number}: invalid evidence URL")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", gap.checked_on):
            raise ValueError(f"place gap row {row_number}: invalid checked_on")
        if gap.gap_type != "minor_civil_division_not_in_place_universe":
            raise ValueError(f"place gap row {row_number}: invalid gap_type")
        if not gap.source_id or not gap.state or not gap.notes:
            raise ValueError(f"place gap row {row_number}: incomplete evidence")
        gaps.append(gap)
    source_ids = [gap.source_id for gap in gaps]
    if len(source_ids) != len(set(source_ids)):
        raise ValueError("duplicate incorporated-place source gap")
    return gaps


def _validate_place_coverage(
    links: list[EntitySourceLink], gaps: list[PlaceUniverseGap]
) -> set[str]:
    """Partition every catalog city source into exact place links or evidenced gaps."""
    from .source_catalog import JurisdictionLevel, load_catalog

    city_ids = {
        entry.source_id
        for entry in load_catalog()
        if entry.jurisdiction_level == JurisdictionLevel.CITY
    }
    linked_ids = {link.source_id for link in links}
    gap_ids = {gap.source_id for gap in gaps}
    if linked_ids & gap_ids:
        raise ValueError("city source cannot be both linked and a universe gap")
    if linked_ids | gap_ids != city_ids:
        raise ValueError(
            "city source/place coverage mismatch: "
            f"missing={sorted(city_ids - linked_ids - gap_ids)}"
        )
    if {link.entity_namespace for link in links} != {PLACE_NAMESPACE}:
        raise ValueError("place links contain an unexpected namespace")
    return city_ids


def main(argv: list[str] | None = None) -> int:
    """Refresh from Census or validate stored incorporated-place tasks offline."""
    parser = argparse.ArgumentParser(
        description="Manage incorporated-place coverage tasks"
    )
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args(argv)
    links = load_source_links(LINKS_PATH)
    gaps = load_place_gaps()
    catalog_ids = _validate_place_coverage(links, gaps)
    if args.refresh:
        entities = fetch_place_universe()
        tasks = build_entity_tasks(entities, links, place_structural_statuses(entities))
        replace_entity_tasks(tasks, TASK_ROOT)
    tasks = load_entity_tasks(TASK_ROOT)
    validate_entity_tasks(
        tasks,
        links,
        catalog_ids,
        PLACE_ENTITY_COUNT,
        PLACE_KINDS,
        PLACE_NAMESPACE,
    )
    counts = research_status_counts(tasks)
    print(
        f"verified: validated {len(tasks)} incorporated-place tasks; "
        + ", ".join(f"{status}={counts.get(status, 0)}" for status in sorted(counts))
        + f"; universe_gaps={len(gaps)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
