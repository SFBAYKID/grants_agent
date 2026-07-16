"""Pinned Census school-district universe and per-entity source research queue.

Why: one district source per state cannot establish nationwide school coverage.
The 2025 Census Gazetteer publishes elementary, secondary, unified, and special
administrative district layers without an API key. This module validates all four
snapshots as one atomic vintage and preserves every district's research status.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from io import BytesIO
from pathlib import Path
from zipfile import BadZipFile, ZipFile

import requests

from .entity_coverage import (
    CoverageEntity,
    EntityCoverageTask,
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
SCHOOL_NAMESPACE = "school_district"
SCHOOL_UNIVERSE_VINTAGE = "2025"
SCHOOL_ENTITY_COUNT = 13_363
PLACEHOLDER_COUNT = 19
TASK_ROOT = ROOT / "data" / "source_catalog" / "coverage_tasks" / "school_districts"
LINKS_PATH = ROOT / "data" / "source_catalog" / "school_district_source_links.csv"
BASE_URL = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2025_Gazetteer"


@dataclass(frozen=True)
class SchoolGazetteerSpec:
    """Pinned contract for one official Census school-district Gazetteer layer."""

    code: str
    entity_kind: str
    url: str
    sha256: str
    expected_count: int


@dataclass(frozen=True)
class SchoolLinkCandidate:
    """One ranked Census candidate for manual source-link review."""

    source_id: str
    publisher: str
    geoid: str
    entity_name: str
    entity_kind: str
    similarity: float


SCHOOL_SPECS = (
    SchoolGazetteerSpec(
        code="elsd",
        entity_kind="elementary_school_district",
        url=f"{BASE_URL}/2025_Gaz_elsd_national.zip",
        sha256="fe5adfe0588e418fecac84c60303e8d18b75abb7c9712b0c9058f69e5ea0d8c9",
        expected_count=1_971,
    ),
    SchoolGazetteerSpec(
        code="scsd",
        entity_kind="secondary_school_district",
        url=f"{BASE_URL}/2025_Gaz_scsd_national.zip",
        sha256="24026d5ff622aef46af595c7724ac42372327767eaa66d601bf4dc8bf2e52a3f",
        expected_count=478,
    ),
    SchoolGazetteerSpec(
        code="unsd",
        entity_kind="unified_school_district",
        url=f"{BASE_URL}/2025_Gaz_unsd_national.zip",
        sha256="72fe1cc606aa9bfe6d95b246c22aff9fdac2215b2f9cb286ba432b3177193e3f",
        expected_count=10_862,
    ),
    SchoolGazetteerSpec(
        code="sdadm",
        entity_kind="school_administrative_area",
        url=f"{BASE_URL}/2025_Gaz_sdadm_national.zip",
        sha256="a86de88012021c3c3a52fa0807faa0399bd20e3cc0ac3ca801d8199c6400935e",
        expected_count=52,
    ),
)
SCHOOL_KINDS = frozenset(spec.entity_kind for spec in SCHOOL_SPECS)


def parse_school_gazetteer(
    zip_bytes: bytes, spec: SchoolGazetteerSpec
) -> list[CoverageEntity]:
    """Parse and validate one official school layer for the 50 states plus DC."""
    from .source_catalog import US_JURISDICTIONS

    try:
        with ZipFile(BytesIO(zip_bytes)) as archive:
            members = archive.namelist()
            if len(members) != 1:
                raise ValueError(f"{spec.code} ZIP must contain exactly one member")
            member = members[0]
            if Path(member).name != member or not member.endswith(".txt"):
                raise ValueError(f"{spec.code} ZIP contains an unsafe member")
            text = archive.read(member).decode("utf-8-sig")
    except BadZipFile as exc:
        raise ValueError(f"{spec.code} payload is not a ZIP") from exc
    rows = list(csv.DictReader(text.splitlines(), delimiter="|"))
    required = {"USPS", "GEOID", "NAME"}
    if not rows or not required <= set(rows[0]):
        raise ValueError(f"{spec.code} Gazetteer columns are missing")
    entities = [
        CoverageEntity(
            entity_namespace=SCHOOL_NAMESPACE,
            geoid=row["GEOID"].strip(),
            state=row["USPS"].strip().upper(),
            entity_name=row["NAME"].strip(),
            entity_kind=spec.entity_kind,
            universe_vintage=SCHOOL_UNIVERSE_VINTAGE,
            entity_disposition=(
                "statistical_placeholder"
                if row["NAME"].strip() == "School District Not Defined"
                else "researchable"
            ),
        )
        for row in rows
        if row["USPS"].strip().upper() in US_JURISDICTIONS
    ]
    if len(entities) != spec.expected_count:
        raise ValueError(
            f"{spec.code} entity count changed: {len(entities)} != {spec.expected_count}"
        )
    if any(not entity.geoid.isdigit() or len(entity.geoid) != 7 for entity in entities):
        raise ValueError(f"{spec.code} Gazetteer contains an invalid GEOID")
    keys = [entity.key for entity in entities]
    if len(keys) != len(set(keys)):
        raise ValueError(f"{spec.code} Gazetteer contains duplicate GEOIDs")
    return sorted(entities, key=lambda entity: (entity.state, entity.geoid))


def fetch_school_universe() -> list[CoverageEntity]:
    """Fetch and verify all four pinned school layers as one complete vintage."""
    entities: list[CoverageEntity] = []
    for spec in SCHOOL_SPECS:
        response = requests.get(spec.url, timeout=60)
        response.raise_for_status()
        digest = hashlib.sha256(response.content).hexdigest()
        if digest != spec.sha256:
            raise ValueError(
                f"{spec.code} Gazetteer hash changed; manual review required"
            )
        entities.extend(parse_school_gazetteer(response.content, spec))
    keys = [entity.key for entity in entities]
    if len(entities) != SCHOOL_ENTITY_COUNT or len(keys) != len(set(keys)):
        raise ValueError("combined school universe count or uniqueness changed")
    return sorted(entities, key=lambda entity: (entity.state, entity.geoid))


def school_structural_statuses(
    entities: list[CoverageEntity],
) -> dict[EntityKey, StructuralStatus]:
    """Mark Census's explicit undefined-district placeholders as not applicable."""
    statuses = {
        entity.key: StructuralStatus(
            research_status="not_applicable",
            checked_on="2026-07-15",
            notes="Census Gazetteer row is School District Not Defined",
        )
        for entity in entities
        if entity.entity_disposition == "statistical_placeholder"
    }
    if len(statuses) != PLACEHOLDER_COUNT:
        raise ValueError(f"school placeholder count changed: {len(statuses)}")
    return statuses


def _normalized_name(value: str) -> str:
    """Normalize publisher/district names only for human-reviewed suggestions."""
    normalized = value.lower().replace("&", " and ")
    normalized = re.sub(r"\bst[.]?\b", "saint", normalized)
    ignored = (
        r"\bindependent\b|\bunified\b|\bconsolidated\b|\bpublic\b|\bcity\b|"
        r"\bcounty\b|\bparish\b|\bborough\b|\bcommunity\b|\bmetropolitan\b|"
        r"\bmunicipal\b|\bschools?\b|\bdistrict\b|\bunit\b|\bsystem\b|"
        r"\bdepartment of education\b"
    )
    normalized = re.sub(ignored, " ", normalized)
    return " ".join(re.findall(r"[a-z0-9]+", normalized))


def suggest_school_links(
    tasks: list[EntityCoverageTask], source_id: str, publisher: str, state: str
) -> list[SchoolLinkCandidate]:
    """Rank same-state Census names for manual review without creating a link."""
    publisher_name = _normalized_name(publisher)
    candidates = [task for task in tasks if task.state == state]
    ranked = sorted(
        (
            SchoolLinkCandidate(
                source_id=source_id,
                publisher=publisher,
                geoid=task.geoid,
                entity_name=task.entity_name,
                entity_kind=task.entity_kind,
                similarity=SequenceMatcher(
                    None, publisher_name, _normalized_name(task.entity_name)
                ).ratio(),
            )
            for task in candidates
        ),
        key=lambda candidate: (-candidate.similarity, candidate.geoid),
    )
    return ranked[:3]


def _validate_school_links(
    links: list[EntitySourceLink], catalog_entries: list[object]
) -> set[str]:
    """Require every catalog school-district source to have reviewed entity links."""
    from .source_catalog import JurisdictionLevel, SourceCatalogEntry

    entries = [
        entry
        for entry in catalog_entries
        if isinstance(entry, SourceCatalogEntry)
        and entry.jurisdiction_level == JurisdictionLevel.SCHOOL_DISTRICT
    ]
    source_ids = {entry.source_id for entry in entries}
    linked_source_ids = {link.source_id for link in links}
    if linked_source_ids != source_ids:
        missing = sorted(source_ids - linked_source_ids)
        unexpected = sorted(linked_source_ids - source_ids)
        raise ValueError(
            f"school source/link mismatch; missing={missing}, unexpected={unexpected}"
        )
    if {link.entity_namespace for link in links} != {SCHOOL_NAMESPACE}:
        raise ValueError("school links contain an unexpected namespace")
    return source_ids


def main(argv: list[str] | None = None) -> int:
    """Refresh, validate, or print link suggestions for the school universe."""
    parser = argparse.ArgumentParser(
        description="Manage school-district coverage tasks"
    )
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--suggest-links", action="store_true")
    args = parser.parse_args(argv)
    from .source_catalog import JurisdictionLevel, load_catalog

    entries = load_catalog()
    if args.refresh:
        entities = fetch_school_universe()
        links = load_source_links(LINKS_PATH)
        tasks = build_entity_tasks(
            entities, links, school_structural_statuses(entities)
        )
        replace_entity_tasks(tasks, TASK_ROOT)
    tasks = load_entity_tasks(TASK_ROOT)
    if args.suggest_links:
        for entry in entries:
            if entry.jurisdiction_level != JurisdictionLevel.SCHOOL_DISTRICT:
                continue
            for candidate in suggest_school_links(
                tasks, entry.source_id, entry.publisher, entry.state
            ):
                print(
                    f"assumed: {candidate.source_id}\t{candidate.geoid}\t"
                    f"{candidate.similarity:.3f}\t{candidate.entity_kind}\t"
                    f"{candidate.entity_name}"
                )
        return 0
    links = load_source_links(LINKS_PATH)
    catalog_ids = _validate_school_links(links, list(entries))
    validate_entity_tasks(
        tasks,
        links,
        catalog_ids,
        SCHOOL_ENTITY_COUNT,
        SCHOOL_KINDS,
        SCHOOL_NAMESPACE,
    )
    counts = research_status_counts(tasks)
    print(
        f"verified: validated {len(tasks)} school-district tasks; "
        + ", ".join(f"{status}={counts.get(status, 0)}" for status in sorted(counts))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
