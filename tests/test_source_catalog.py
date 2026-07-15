"""Catalog validation and generated nationwide source-report tests."""

from __future__ import annotations

import csv
from collections import Counter
from dataclasses import fields
from pathlib import Path

import pytest

from grant_watch.source_catalog import (
    AccessMode,
    CoverageResearchStatus,
    IntegrationStatus,
    JurisdictionLevel,
    SourceCatalogEntry,
    US_JURISDICTIONS,
    VerificationLabel,
    coverage_rows,
    generate_reports,
    load_catalog,
    load_coverage_exceptions,
    report_drift,
)


def _entry(**overrides: object) -> SourceCatalogEntry:
    """Build one fully typed valid source entry for focused failure tests."""
    values: dict[str, object] = {
        "source_id": "test.source",
        "name": "Test Source",
        "jurisdiction_level": JurisdictionLevel.STATE,
        "state": "CA",
        "publisher": "Test Publisher",
        "source_kinds": "rfp",
        "lead_signals": "silver",
        "url": "https://example.gov/bids",
        "portal_family": "custom",
        "access_mode": AccessMode.PUBLIC_NO_AUTH,
        "credential_env": "",
        "official_status": VerificationLabel.VERIFIED,
        "access_status": VerificationLabel.VERIFIED,
        "integration_status": IntegrationStatus.ACCESS_CHECKED,
        "discovered_on": "2026-07-15",
        "last_access_checked_on": "2026-07-15",
        "discovery_method": "live_web",
        "evidence_url": "https://example.gov/bids",
        "coverage_rule": "unknown",
        "coverage_scope": "State agencies",
        "notes": "",
    }
    values.update(overrides)
    return SourceCatalogEntry(**values)  # type: ignore[arg-type]  # Test helper accepts overrides.


def _write_catalog(path: Path, entries: list[SourceCatalogEntry]) -> None:
    """Write test entries using the production catalog column contract."""
    fieldnames = [field.name for field in fields(SourceCatalogEntry)]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for entry in entries:
            writer.writerow({name: getattr(entry, name) for name in fieldnames})


def test_canonical_catalog_has_all_states_and_unique_ids() -> None:
    """The committed catalog must explicitly cover every state plus DC."""
    entries = load_catalog()
    assert {entry.state for entry in entries if entry.state} == US_JURISDICTIONS
    assert len({entry.source_id for entry in entries}) == len(entries)
    assert all(entry.discovered_on for entry in entries)
    assert load_coverage_exceptions()


def test_canonical_counts_and_exact_coverage_match_published_inventory() -> None:
    """Published counts and honest exact-layer gaps are pinned to canonical data."""
    entries = load_catalog()
    assert Counter(entry.jurisdiction_level for entry in entries) == {
        JurisdictionLevel.CITY: 7,
        JurisdictionLevel.COUNTY: 53,
        JurisdictionLevel.EDUCATION_SERVICE_AGENCY: 3,
        JurisdictionLevel.FEDERAL: 19,
        JurisdictionLevel.MULTI_JURISDICTION: 1,
        JurisdictionLevel.PORTAL_FAMILY: 10,
        JurisdictionLevel.REGIONAL_GOVERNMENT: 1,
        JurisdictionLevel.SCHOOL_DISTRICT: 52,
        JurisdictionLevel.SPECIAL_DISTRICT: 1,
        JurisdictionLevel.STATE: 105,
    }
    rows = coverage_rows(entries, load_coverage_exceptions())
    assert all(row.state_sources > 0 and row.grant_sources > 0 for row in rows)
    assert {row.state for row in rows if row.county_sources == 0} == {
        "CT",
        "DC",
        "RI",
        "VT",
    }
    assert all(row.district_sources > 0 for row in rows)
    access_counts = Counter(entry.access_mode for entry in entries)
    assert access_counts == {
        AccessMode.FREE_ACCOUNT: 14,
        AccessMode.PUBLIC_API_KEY: 2,
        AccessMode.PUBLIC_NO_AUTH: 28,
        AccessMode.SUPPLIER_ACCOUNT: 4,
        AccessMode.UNKNOWN: 204,
    }
    assert (
        sum(
            entry.access_mode == AccessMode.PUBLIC_NO_AUTH
            and entry.access_status == VerificationLabel.VERIFIED
            for entry in entries
        )
        == 17
    )


def test_generated_access_lists_partition_every_source(tmp_path: Path) -> None:
    """Public, credentialed, and unknown/manual views cannot omit or duplicate rows."""
    entries = [
        _entry(source_id="public", access_mode=AccessMode.PUBLIC_NO_AUTH),
        _entry(
            source_id="keyed",
            access_mode=AccessMode.PUBLIC_API_KEY,
            credential_env="SAM_API_KEY",
        ),
        _entry(source_id="account", access_mode=AccessMode.FREE_ACCOUNT),
        _entry(source_id="unknown", access_mode=AccessMode.UNKNOWN),
        _entry(source_id="manual", access_mode=AccessMode.MANUAL_ONLY),
    ]
    generate_reports(entries, tmp_path)
    ids: list[str] = []
    for filename in (
        "verified_public_no_auth.csv",
        "candidate_public_no_auth.csv",
        "api_key_sources.csv",
        "account_or_paid_sources.csv",
        "unknown_or_manual_access.csv",
    ):
        with (tmp_path / filename).open(newline="", encoding="utf-8") as handle:
            ids.extend(row["source_id"] for row in csv.DictReader(handle))
    assert sorted(ids) == sorted(entry.source_id for entry in entries)
    assert len(ids) == len(set(ids))
    assert all(b"\r\n" not in path.read_bytes() for path in tmp_path.glob("*.csv"))


def test_report_check_detects_stale_and_unexpected_files(tmp_path: Path) -> None:
    """Check mode detects drift without silently replacing report evidence."""
    entries = [_entry()]
    generate_reports(entries, tmp_path)
    assert report_drift(entries, tmp_path) == []
    (tmp_path / "state_coverage.csv").write_text("stale\n")
    (tmp_path / "retired.csv").write_text("old\n")
    assert report_drift(entries, tmp_path) == [
        "stale:state_coverage.csv",
        "unexpected:retired.csv",
    ]


def test_portal_family_is_national_without_claiming_geographic_coverage() -> None:
    """A shared vendor platform is not counted as one state's source coverage."""
    entry = _entry(jurisdiction_level=JurisdictionLevel.PORTAL_FAMILY, state="")
    assert all(row.state_sources == 0 for row in coverage_rows([entry]))


def test_specialized_entities_do_not_claim_county_or_district_coverage() -> None:
    """Regional and service entities stay outside exact county/district counts."""
    entries = [
        _entry(
            source_id="regional",
            jurisdiction_level=JurisdictionLevel.REGIONAL_GOVERNMENT,
        ),
        _entry(
            source_id="special", jurisdiction_level=JurisdictionLevel.SPECIAL_DISTRICT
        ),
        _entry(
            source_id="esa",
            jurisdiction_level=JurisdictionLevel.EDUCATION_SERVICE_AGENCY,
        ),
    ]
    california = next(row for row in coverage_rows(entries) if row.state == "CA")
    assert california.county_sources == 0
    assert california.district_sources == 0


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("source_kinds", "rfpp", "invalid source_kinds"),
        ("lead_signals", "bronze", "invalid lead_signals"),
        ("portal_family", "typo_portal", "invalid portal_family"),
        ("coverage_rule", "sometimes", "invalid coverage_rule"),
        ("discovery_method", "memory", "invalid discovery_method"),
    ],
)
def test_catalog_semantic_vocabularies_reject_typos(
    tmp_path: Path, field: str, value: str, error: str
) -> None:
    """Semantic typos fail validation instead of silently changing coverage."""
    path = tmp_path / "catalog.csv"
    _write_catalog(path, [_entry(**{field: value})])
    with pytest.raises(ValueError, match=error):
        load_catalog(path)


def test_live_status_requires_verified_access_and_check_date(tmp_path: Path) -> None:
    """A live integration claim requires dated and verified access evidence."""
    path = tmp_path / "catalog.csv"
    _write_catalog(
        path,
        [
            _entry(
                integration_status=IntegrationStatus.LIVE_POSITIVE_VERIFIED,
                access_status=VerificationLabel.NEEDS_TESTING,
                last_access_checked_on="",
            )
        ],
    )
    with pytest.raises(ValueError, match="requires a check date"):
        load_catalog(path)
    _write_catalog(
        path,
        [
            _entry(
                integration_status=IntegrationStatus.LIVE_POSITIVE_VERIFIED,
                access_status=VerificationLabel.NEEDS_TESTING,
            )
        ],
    )
    with pytest.raises(ValueError, match="requires verified access"):
        load_catalog(path)


def test_coverage_distinguishes_state_county_and_district_sources() -> None:
    """A state portal must not masquerade as county or school-district coverage."""
    entries = [
        _entry(
            source_id="ca.state",
            jurisdiction_level=JurisdictionLevel.STATE,
            source_kinds="rfp|contract_award",
        ),
        _entry(source_id="ca.county", jurisdiction_level=JurisdictionLevel.COUNTY),
        _entry(
            source_id="ca.district",
            jurisdiction_level=JurisdictionLevel.SCHOOL_DISTRICT,
            source_kinds="rfp|grant_award",
        ),
    ]
    california = next(row for row in coverage_rows(entries) if row.state == "CA")
    assert california.state_sources == 1
    assert california.county_sources == 1
    assert california.district_sources == 1
    assert california.rfp_sources == 3
    assert california.grant_sources == 1
    assert california.contract_award_sources == 1
    assert california.county_research_status == CoverageResearchStatus.CANDIDATE_FOUND


def test_coverage_preserves_not_applicable_and_researched_not_found() -> None:
    """Empty coverage uses explicit evidence instead of pretending a source exists."""
    exceptions = load_coverage_exceptions()
    rows = coverage_rows([], exceptions)
    dc = next(row for row in rows if row.state == "DC")
    vermont = next(row for row in rows if row.state == "VT")
    assert dc.county_research_status == CoverageResearchStatus.NOT_APPLICABLE
    assert vermont.county_research_status == CoverageResearchStatus.RESEARCHED_NOT_FOUND


def test_keyed_api_requires_env_var_name_without_secret_value(tmp_path: Path) -> None:
    """A keyed source records only an env-var name and rejects a missing key reference."""
    path = tmp_path / "catalog.csv"
    _write_catalog(
        path, [_entry(access_mode=AccessMode.PUBLIC_API_KEY, credential_env="")]
    )
    with pytest.raises(ValueError, match="requires credential_env"):
        load_catalog(path)


def test_duplicate_source_ids_fail_loudly(tmp_path: Path) -> None:
    """Stable source IDs cannot collide across publishers or portal endpoints."""
    path = tmp_path / "catalog.csv"
    _write_catalog(path, [_entry(), _entry(name="Different Source")])
    with pytest.raises(ValueError, match="duplicate source IDs"):
        load_catalog(path)
