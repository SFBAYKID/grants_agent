"""Typed source-catalog loading, validation, coverage, and report generation.

Why: discovery candidates are not leads and do not belong in source observations.
This catalog preserves where a source was found, its access boundary, geographic
coverage, and its integration maturity without claiming that discovery equals a
working poller. Generated reports keep public and credentialed lists consistent
with one canonical CSV.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import tempfile
from dataclasses import dataclass, fields
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlparse

from .source_discovery import load_discovery_checks, validate_research_links


CATALOG_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "source_catalog" / "sources.csv"
)
COVERAGE_EXCEPTIONS_PATH = (
    Path(__file__).resolve().parent.parent
    / "data"
    / "source_catalog"
    / "coverage_exceptions.csv"
)
REPORT_DIR = Path(__file__).resolve().parent.parent / "docs" / "source_inventory"
REPORT_FILENAMES = frozenset(
    {
        "account_or_paid_sources.csv",
        "api_key_sources.csv",
        "candidate_public_no_auth.csv",
        "credentialed_sources.csv",
        "state_coverage.csv",
        "unknown_or_manual_access.csv",
        "verified_public_no_auth.csv",
    }
)
US_JURISDICTIONS = frozenset(
    {
        "AL",
        "AK",
        "AZ",
        "AR",
        "CA",
        "CO",
        "CT",
        "DE",
        "DC",
        "FL",
        "GA",
        "HI",
        "ID",
        "IL",
        "IN",
        "IA",
        "KS",
        "KY",
        "LA",
        "ME",
        "MD",
        "MA",
        "MI",
        "MN",
        "MS",
        "MO",
        "MT",
        "NE",
        "NV",
        "NH",
        "NJ",
        "NM",
        "NY",
        "NC",
        "ND",
        "OH",
        "OK",
        "OR",
        "PA",
        "RI",
        "SC",
        "SD",
        "TN",
        "TX",
        "UT",
        "VT",
        "VA",
        "WA",
        "WV",
        "WI",
        "WY",
    }
)
SOURCE_KINDS = frozenset(
    {
        "contract_award",
        "grant_award",
        "grant_opportunity",
        "rfp",
        "rule_notice",
    }
)
LEAD_SIGNALS = frozenset({"gold", "silver", "watch"})
COVERAGE_RULES = frozenset({"optional", "required", "unknown"})
DISCOVERY_METHODS = frozenset(
    {
        "firecrawl_search",
        "live_api",
        "live_pdf",
        "live_web",
        "naspo_directory",
        "official_web",
        "web_research",
        "web_search",
    }
)
PORTAL_FAMILIES = frozenset(
    {
        "agate",
        "aspnet_web",
        "bidnet_direct",
        "bid_locker",
        "bonfire",
        "beacon_bid",
        "buy_speed",
        "civicengage",
        "ckan_csv",
        "commbuys",
        "custom",
        "custom_api",
        "custom_portal",
        "custom_web",
        "delaware_bids",
        "demandstar",
        "ecivis",
        "egrantsmanagement",
        "emacs",
        "ionwave",
        "jaggaer",
        "opengov",
        "oregonbuys_pdf",
        "peoplesoft",
        "planetbids",
        "press_release",
        "public_purchase",
        "published_pdf",
        "sigma_vss",
        "socrata",
        "survey_monkey_apply",
        "swift",
        "vendor_registry",
        "webgrants",
        "wvoasis",
    }
)


class AccessMode(StrEnum):
    """How a human or poller reaches source records."""

    PUBLIC_NO_AUTH = "public_no_auth"
    PUBLIC_API_KEY = "public_api_key"
    FREE_ACCOUNT = "free_account"
    SUPPLIER_ACCOUNT = "supplier_account"
    PAID = "paid"
    MANUAL_ONLY = "manual_only"
    UNKNOWN = "unknown"


class VerificationLabel(StrEnum):
    """Constitution-required evidence label for one independent claim axis."""

    VERIFIED = "verified"
    ASSUMED = "assumed"
    NEEDS_TESTING = "needs-testing"


class IntegrationStatus(StrEnum):
    """Furthest demonstrated integration stage for a source endpoint."""

    DISCOVERED = "discovered"
    ACCESS_CHECKED = "access_checked"
    PARSER_TESTED = "parser_tested"
    LIVE_ZERO_VERIFIED = "live_zero_verified"
    LIVE_POSITIVE_VERIFIED = "live_positive_verified"


class JurisdictionLevel(StrEnum):
    """Publisher/coverage level represented by one endpoint."""

    FEDERAL = "federal"
    STATE = "state"
    COUNTY = "county"
    CITY = "city"
    SCHOOL_DISTRICT = "school_district"
    SPECIAL_DISTRICT = "special_district"
    REGIONAL_GOVERNMENT = "regional_government"
    EDUCATION_SERVICE_AGENCY = "education_service_agency"
    MULTI_JURISDICTION = "multi_jurisdiction"
    PORTAL_FAMILY = "portal_family"


class CoverageResearchStatus(StrEnum):
    """Honest research state for a required jurisdiction/source-layer pair."""

    CANDIDATE_FOUND = "candidate_found"
    NOT_RESEARCHED = "not_researched"
    RESEARCHED_NOT_FOUND = "researched_not_found"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class SourceCatalogEntry:
    """One source endpoint with independent access and verification evidence."""

    source_id: str
    name: str
    jurisdiction_level: JurisdictionLevel
    state: str
    publisher: str
    source_kinds: str
    lead_signals: str
    url: str
    portal_family: str
    access_mode: AccessMode
    credential_env: str
    official_status: VerificationLabel
    access_status: VerificationLabel
    integration_status: IntegrationStatus
    discovered_on: str
    last_access_checked_on: str
    discovery_method: str
    evidence_url: str
    coverage_rule: str
    coverage_scope: str
    notes: str


@dataclass(frozen=True)
class CoverageException:
    """Evidence that an empty layer was researched or does not exist structurally."""

    state: str
    jurisdiction_level: JurisdictionLevel
    status: CoverageResearchStatus
    checked_on: str
    evidence_method: str
    evidence_url: str
    notes: str


@dataclass(frozen=True)
class CoverageRow:
    """Counts proving which source categories are present for one jurisdiction."""

    state: str
    state_sources: int
    county_sources: int
    city_sources: int
    district_sources: int
    rfp_sources: int
    grant_sources: int
    contract_award_sources: int
    county_research_status: CoverageResearchStatus
    district_research_status: CoverageResearchStatus
    grant_research_status: CoverageResearchStatus


def _parse_entry(row: dict[str, str], row_number: int) -> SourceCatalogEntry:
    """Parse and validate one CSV row, failing loudly on unsupported claims."""
    expected = {field.name for field in fields(SourceCatalogEntry)}
    if set(row) != expected:
        missing = sorted(expected - set(row))
        extra = sorted(set(row) - expected)
        raise ValueError(
            f"catalog row {row_number} columns mismatch: missing={missing}, extra={extra}"
        )
    try:
        entry = SourceCatalogEntry(
            source_id=row["source_id"].strip(),
            name=row["name"].strip(),
            jurisdiction_level=JurisdictionLevel(row["jurisdiction_level"].strip()),
            state=row["state"].strip().upper(),
            publisher=row["publisher"].strip(),
            source_kinds=row["source_kinds"].strip(),
            lead_signals=row["lead_signals"].strip(),
            url=row["url"].strip(),
            portal_family=row["portal_family"].strip(),
            access_mode=AccessMode(row["access_mode"].strip()),
            credential_env=row["credential_env"].strip(),
            official_status=VerificationLabel(row["official_status"].strip()),
            access_status=VerificationLabel(row["access_status"].strip()),
            integration_status=IntegrationStatus(row["integration_status"].strip()),
            discovered_on=row["discovered_on"].strip(),
            last_access_checked_on=row["last_access_checked_on"].strip(),
            discovery_method=row["discovery_method"].strip(),
            evidence_url=row["evidence_url"].strip(),
            coverage_rule=row["coverage_rule"].strip(),
            coverage_scope=row["coverage_scope"].strip(),
            notes=row["notes"].strip(),
        )
    except ValueError as exc:
        raise ValueError(f"catalog row {row_number}: {exc}") from exc
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]+", entry.source_id):
        raise ValueError(
            f"catalog row {row_number}: invalid source_id {entry.source_id!r}"
        )
    parsed_url = urlparse(entry.url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise ValueError(f"catalog row {row_number}: invalid source URL {entry.url!r}")
    if entry.evidence_url:
        parsed_evidence = urlparse(entry.evidence_url)
        if (
            parsed_evidence.scheme not in {"http", "https"}
            or not parsed_evidence.netloc
        ):
            raise ValueError(f"catalog row {row_number}: invalid evidence URL")
    source_kinds = entry.source_kinds.split("|")
    if not source_kinds or any(kind not in SOURCE_KINDS for kind in source_kinds):
        raise ValueError(f"catalog row {row_number}: invalid source_kinds")
    if len(source_kinds) != len(set(source_kinds)):
        raise ValueError(f"catalog row {row_number}: duplicate source_kinds")
    lead_signals = entry.lead_signals.split("|")
    if not lead_signals or any(signal not in LEAD_SIGNALS for signal in lead_signals):
        raise ValueError(f"catalog row {row_number}: invalid lead_signals")
    if len(lead_signals) != len(set(lead_signals)):
        raise ValueError(f"catalog row {row_number}: duplicate lead_signals")
    if entry.portal_family not in PORTAL_FAMILIES:
        raise ValueError(f"catalog row {row_number}: invalid portal_family")
    if entry.coverage_rule not in COVERAGE_RULES:
        raise ValueError(f"catalog row {row_number}: invalid coverage_rule")
    if entry.discovery_method not in DISCOVERY_METHODS:
        raise ValueError(f"catalog row {row_number}: invalid discovery_method")
    national_levels = {JurisdictionLevel.FEDERAL, JurisdictionLevel.PORTAL_FAMILY}
    if (
        entry.jurisdiction_level not in national_levels
        and entry.state not in US_JURISDICTIONS
    ):
        raise ValueError(
            f"catalog row {row_number}: state is required for non-federal source"
        )
    if entry.jurisdiction_level in national_levels and entry.state:
        raise ValueError(
            f"catalog row {row_number}: national source cannot claim one state"
        )
    if entry.access_mode == AccessMode.PUBLIC_API_KEY and not entry.credential_env:
        raise ValueError(f"catalog row {row_number}: keyed API requires credential_env")
    if entry.access_mode != AccessMode.PUBLIC_API_KEY and entry.credential_env:
        raise ValueError(
            f"catalog row {row_number}: credential_env is only valid for keyed APIs"
        )
    if entry.credential_env and not re.fullmatch(
        r"[A-Z][A-Z0-9_]+", entry.credential_env
    ):
        raise ValueError(
            f"catalog row {row_number}: credential_env must be an env-var name"
        )
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", entry.discovered_on):
        raise ValueError(f"catalog row {row_number}: discovered_on must be YYYY-MM-DD")
    if entry.last_access_checked_on and not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}", entry.last_access_checked_on
    ):
        raise ValueError(
            f"catalog row {row_number}: last_access_checked_on must be YYYY-MM-DD"
        )
    if (
        entry.integration_status != IntegrationStatus.DISCOVERED
        and not entry.last_access_checked_on
    ):
        raise ValueError(
            f"catalog row {row_number}: checked integration requires a check date"
        )
    if (
        entry.integration_status
        in {
            IntegrationStatus.LIVE_ZERO_VERIFIED,
            IntegrationStatus.LIVE_POSITIVE_VERIFIED,
        }
        and entry.access_status != VerificationLabel.VERIFIED
    ):
        raise ValueError(
            f"catalog row {row_number}: live integration requires verified access"
        )
    return entry


def load_catalog(path: Path = CATALOG_PATH) -> list[SourceCatalogEntry]:
    """Load the canonical catalog and reject duplicate IDs or malformed evidence."""
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    entries = [_parse_entry(row, number) for number, row in enumerate(rows, start=2)]
    ids = [entry.source_id for entry in entries]
    duplicates = sorted({source_id for source_id in ids if ids.count(source_id) > 1})
    if duplicates:
        raise ValueError(f"duplicate source IDs: {duplicates}")
    return entries


def load_coverage_exceptions(
    path: Path = COVERAGE_EXCEPTIONS_PATH,
) -> list[CoverageException]:
    """Load researched-empty/not-applicable coverage evidence without inventing sources."""
    expected = {field.name for field in fields(CoverageException)}
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    exceptions: list[CoverageException] = []
    for row_number, row in enumerate(rows, start=2):
        if set(row) != expected:
            raise ValueError(f"coverage exception row {row_number} columns mismatch")
        try:
            exception = CoverageException(
                state=row["state"].strip().upper(),
                jurisdiction_level=JurisdictionLevel(row["jurisdiction_level"].strip()),
                status=CoverageResearchStatus(row["status"].strip()),
                checked_on=row["checked_on"].strip(),
                evidence_method=row["evidence_method"].strip(),
                evidence_url=row["evidence_url"].strip(),
                notes=row["notes"].strip(),
            )
        except ValueError as exc:
            raise ValueError(f"coverage exception row {row_number}: {exc}") from exc
        if exception.state not in US_JURISDICTIONS:
            raise ValueError(f"coverage exception row {row_number}: invalid state")
        if exception.status not in {
            CoverageResearchStatus.RESEARCHED_NOT_FOUND,
            CoverageResearchStatus.NOT_APPLICABLE,
        }:
            raise ValueError(
                f"coverage exception row {row_number}: unsupported empty status"
            )
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", exception.checked_on):
            raise ValueError(f"coverage exception row {row_number}: invalid checked_on")
        evidence = urlparse(exception.evidence_url)
        if evidence.scheme not in {"http", "https"} or not evidence.netloc:
            raise ValueError(
                f"coverage exception row {row_number}: invalid evidence URL"
            )
        exceptions.append(exception)
    keys = [(item.state, item.jurisdiction_level) for item in exceptions]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate coverage exception")
    return exceptions


def coverage_rows(
    entries: list[SourceCatalogEntry], exceptions: list[CoverageException] | None = None
) -> list[CoverageRow]:
    """Summarize explicit source-category coverage for all 50 states plus DC."""
    exception_map = {
        (exception.state, exception.jurisdiction_level): exception.status
        for exception in (exceptions or [])
    }

    def status_for(
        state: str, level: JurisdictionLevel, count: int
    ) -> CoverageResearchStatus:
        """Return found/empty research status for one geographic source layer."""
        if count:
            return CoverageResearchStatus.CANDIDATE_FOUND
        return exception_map.get((state, level), CoverageResearchStatus.NOT_RESEARCHED)

    rows: list[CoverageRow] = []
    for state in sorted(US_JURISDICTIONS):
        scoped = [entry for entry in entries if entry.state == state]
        state_count = sum(
            entry.jurisdiction_level == JurisdictionLevel.STATE for entry in scoped
        )
        county_count = sum(
            entry.jurisdiction_level == JurisdictionLevel.COUNTY for entry in scoped
        )
        city_count = sum(
            entry.jurisdiction_level == JurisdictionLevel.CITY for entry in scoped
        )
        district_count = sum(
            entry.jurisdiction_level == JurisdictionLevel.SCHOOL_DISTRICT
            for entry in scoped
        )
        rfp_count = sum("rfp" in entry.source_kinds.split("|") for entry in scoped)
        grant_count = sum(
            bool(
                {"grant_opportunity", "grant_award"}
                & set(entry.source_kinds.split("|"))
            )
            for entry in scoped
        )
        contract_award_count = sum(
            "contract_award" in entry.source_kinds.split("|") for entry in scoped
        )
        rows.append(
            CoverageRow(
                state=state,
                state_sources=state_count,
                county_sources=county_count,
                city_sources=city_count,
                district_sources=district_count,
                rfp_sources=rfp_count,
                grant_sources=grant_count,
                contract_award_sources=contract_award_count,
                county_research_status=status_for(
                    state, JurisdictionLevel.COUNTY, county_count
                ),
                district_research_status=status_for(
                    state, JurisdictionLevel.SCHOOL_DISTRICT, district_count
                ),
                grant_research_status=(
                    CoverageResearchStatus.CANDIDATE_FOUND
                    if grant_count
                    else CoverageResearchStatus.NOT_RESEARCHED
                ),
            )
        )
    return rows


def _write_entries(path: Path, entries: list[SourceCatalogEntry]) -> None:
    """Write a deterministic CSV view from typed canonical entries."""
    fieldnames = [field.name for field in fields(SourceCatalogEntry)]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for entry in sorted(entries, key=lambda item: (item.state, item.source_id)):
            writer.writerow({name: getattr(entry, name) for name in fieldnames})


def generate_reports(
    entries: list[SourceCatalogEntry],
    report_dir: Path = REPORT_DIR,
    exceptions: list[CoverageException] | None = None,
) -> None:
    """Generate public, credentialed, unknown-access, and coverage CSV reports."""
    report_dir.mkdir(parents=True, exist_ok=True)
    _write_entries(
        report_dir / "verified_public_no_auth.csv",
        [
            entry
            for entry in entries
            if entry.access_mode == AccessMode.PUBLIC_NO_AUTH
            and entry.access_status == VerificationLabel.VERIFIED
        ],
    )
    _write_entries(
        report_dir / "candidate_public_no_auth.csv",
        [
            entry
            for entry in entries
            if entry.access_mode == AccessMode.PUBLIC_NO_AUTH
            and entry.access_status != VerificationLabel.VERIFIED
        ],
    )
    _write_entries(
        report_dir / "credentialed_sources.csv",
        [
            entry
            for entry in entries
            if entry.access_mode
            in {
                AccessMode.PUBLIC_API_KEY,
                AccessMode.FREE_ACCOUNT,
                AccessMode.SUPPLIER_ACCOUNT,
                AccessMode.PAID,
            }
        ],
    )
    _write_entries(
        report_dir / "api_key_sources.csv",
        [entry for entry in entries if entry.access_mode == AccessMode.PUBLIC_API_KEY],
    )
    _write_entries(
        report_dir / "account_or_paid_sources.csv",
        [
            entry
            for entry in entries
            if entry.access_mode
            in {
                AccessMode.FREE_ACCOUNT,
                AccessMode.SUPPLIER_ACCOUNT,
                AccessMode.PAID,
            }
        ],
    )
    _write_entries(
        report_dir / "unknown_or_manual_access.csv",
        [
            entry
            for entry in entries
            if entry.access_mode
            in {
                AccessMode.UNKNOWN,
                AccessMode.MANUAL_ONLY,
            }
        ],
    )
    coverage_path = report_dir / "state_coverage.csv"
    with coverage_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[field.name for field in fields(CoverageRow)],
            lineterminator="\n",
        )
        writer.writeheader()
        for row in coverage_rows(entries, exceptions):
            writer.writerow(
                {field.name: getattr(row, field.name) for field in fields(CoverageRow)}
            )


def report_drift(
    entries: list[SourceCatalogEntry],
    report_dir: Path = REPORT_DIR,
    exceptions: list[CoverageException] | None = None,
) -> list[str]:
    """Return missing, stale, or unexpected generated CSV names without rewriting them."""
    with tempfile.TemporaryDirectory(prefix="grant-source-reports-") as temp_name:
        expected_dir = Path(temp_name)
        generate_reports(entries, expected_dir, exceptions)
        drift: list[str] = []
        for filename in sorted(REPORT_FILENAMES):
            actual = report_dir / filename
            expected = expected_dir / filename
            if not actual.exists():
                drift.append(f"missing:{filename}")
            elif actual.read_bytes() != expected.read_bytes():
                drift.append(f"stale:{filename}")
        actual_names = {path.name for path in report_dir.glob("*.csv")}
        drift.extend(
            f"unexpected:{filename}"
            for filename in sorted(actual_names - REPORT_FILENAMES)
        )
    return drift


def main(argv: list[str] | None = None) -> int:
    """Validate the catalog and regenerate deterministic inventory reports."""
    parser = argparse.ArgumentParser(
        description="Validate and report the source catalog"
    )
    parser.add_argument("--catalog", type=Path, default=CATALOG_PATH)
    parser.add_argument("--report-dir", type=Path, default=REPORT_DIR)
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate that generated CSV reports are current without rewriting them",
    )
    args = parser.parse_args(argv)
    entries = load_catalog(args.catalog)
    exceptions = load_coverage_exceptions()
    checks = load_discovery_checks()
    validate_research_links(
        checks,
        {entry.source_id for entry in entries},
        {
            f"coverage.{exception.state.lower()}.{exception.jurisdiction_level.value}"
            for exception in exceptions
        },
    )
    if args.check:
        drift = report_drift(entries, args.report_dir, exceptions)
        if drift:
            print(
                f"needs-testing: generated report drift: {', '.join(drift)}",
                file=sys.stderr,
            )
            return 1
        print(
            f"verified: validated {len(entries)} source records, {len(checks)} discovery "
            "checks, and current reports"
        )
        return 0
    generate_reports(entries, args.report_dir, exceptions)
    print(
        f"verified: validated {len(entries)} source records, {len(checks)} discovery "
        "checks, and generated reports"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
