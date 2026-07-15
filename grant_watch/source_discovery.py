"""Typed, immutable evidence records for source-discovery research checks.

Why: a Firecrawl search result is useful research memory but is not a working poller.
This module validates secret-free evidence (query, selected result, retrieval date,
and hashes) and links each check to either a catalog source or an evidenced coverage
exception. It never auto-promotes or enables a source.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from dataclasses import dataclass, fields
from pathlib import Path
from urllib.parse import urlparse


CHECKS_PATH = (
    Path(__file__).resolve().parent.parent
    / "data"
    / "source_catalog"
    / "discovery_checks.csv"
)
ALLOWED_LEVELS = frozenset(
    {
        "city",
        "county",
        "education_service_agency",
        "federal",
        "multi_jurisdiction",
        "portal_family",
        "regional_government",
        "school_district",
        "special_district",
        "state",
    }
)


@dataclass(frozen=True)
class DiscoveryCheck:
    """One selected Firecrawl result with enough evidence to audit the claim."""

    check_id: str
    research_key: str
    state: str
    jurisdiction_level: str
    query: str
    checked_on: str
    transport: str
    result_rank: int
    result_url: str
    result_title: str
    result_snippet: str
    search_evidence_sha256: str
    content_sha256: str
    content_status: str
    notes: str


def _parse_check(row: dict[str, str], row_number: int) -> DiscoveryCheck:
    """Parse one discovery row and reject incomplete or malformed evidence."""
    expected = {field.name for field in fields(DiscoveryCheck)}
    if set(row) != expected:
        raise ValueError(f"discovery row {row_number}: columns mismatch")
    try:
        rank = int(row["result_rank"])
    except ValueError as exc:
        raise ValueError(f"discovery row {row_number}: invalid result_rank") from exc
    check = DiscoveryCheck(
        check_id=row["check_id"].strip(),
        research_key=row["research_key"].strip(),
        state=row["state"].strip().upper(),
        jurisdiction_level=row["jurisdiction_level"].strip(),
        query=row["query"].strip(),
        checked_on=row["checked_on"].strip(),
        transport=row["transport"].strip(),
        result_rank=rank,
        result_url=row["result_url"].strip(),
        result_title=row["result_title"].strip(),
        result_snippet=row["result_snippet"].strip(),
        search_evidence_sha256=row["search_evidence_sha256"].strip().lower(),
        content_sha256=row["content_sha256"].strip().lower(),
        content_status=row["content_status"].strip(),
        notes=row["notes"].strip(),
    )
    if not re.fullmatch(r"fc\.\d{8}\.[a-z0-9._-]+", check.check_id):
        raise ValueError(f"discovery row {row_number}: invalid check_id")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]+", check.research_key):
        raise ValueError(f"discovery row {row_number}: invalid research_key")
    if check.jurisdiction_level not in ALLOWED_LEVELS:
        raise ValueError(f"discovery row {row_number}: invalid jurisdiction_level")
    if check.state and not re.fullmatch(r"[A-Z]{2}", check.state):
        raise ValueError(f"discovery row {row_number}: invalid state")
    if check.jurisdiction_level != "portal_family" and not check.state:
        raise ValueError(f"discovery row {row_number}: state is required")
    if check.jurisdiction_level == "portal_family" and check.state:
        raise ValueError(
            f"discovery row {row_number}: portal family cannot claim one state"
        )
    if not check.query:
        raise ValueError(f"discovery row {row_number}: query is required")
    if check.transport != "firecrawl_search":
        raise ValueError(f"discovery row {row_number}: unsupported transport")
    if check.result_rank < 1:
        raise ValueError(f"discovery row {row_number}: result_rank must be positive")
    parsed_url = urlparse(check.result_url)
    if parsed_url.scheme != "https" or not parsed_url.netloc:
        raise ValueError(f"discovery row {row_number}: result URL must be HTTPS")
    if not check.result_title or not check.result_snippet:
        raise ValueError(
            f"discovery row {row_number}: selected result evidence is required"
        )
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", check.checked_on):
        raise ValueError(f"discovery row {row_number}: invalid checked_on")
    for label, value in (
        ("search_evidence_sha256", check.search_evidence_sha256),
        ("content_sha256", check.content_sha256),
    ):
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError(f"discovery row {row_number}: invalid {label}")
    evidence_payload = json.dumps(
        [
            check.query,
            check.result_rank,
            check.result_url,
            check.result_title,
            check.result_snippet,
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    expected_evidence_hash = hashlib.sha256(evidence_payload.encode()).hexdigest()
    if check.search_evidence_sha256 != expected_evidence_hash:
        raise ValueError(f"discovery row {row_number}: search evidence hash mismatch")
    if check.content_status != "scraped":
        raise ValueError(f"discovery row {row_number}: unsupported content_status")
    return check


def load_discovery_checks(path: Path = CHECKS_PATH) -> list[DiscoveryCheck]:
    """Load immutable discovery evidence and reject duplicate check identifiers."""
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    checks = [_parse_check(row, number) for number, row in enumerate(rows, start=2)]
    ids = [check.check_id for check in checks]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate discovery check IDs")
    return checks


def validate_research_links(
    checks: list[DiscoveryCheck], catalog_ids: set[str], coverage_keys: set[str]
) -> None:
    """Require every research check to link to a source or a coverage exception."""
    valid_keys = catalog_ids | coverage_keys
    missing = sorted({check.research_key for check in checks} - valid_keys)
    if missing:
        raise ValueError(f"unlinked discovery research keys: {missing}")


def main(argv: list[str] | None = None) -> int:
    """Validate stored discovery evidence and its links to canonical catalog data."""
    parser = argparse.ArgumentParser(description="Validate source-discovery evidence")
    parser.add_argument("--checks", type=Path, default=CHECKS_PATH)
    args = parser.parse_args(argv)
    checks = load_discovery_checks(args.checks)
    from .source_catalog import load_catalog, load_coverage_exceptions

    entries = load_catalog()
    exceptions = load_coverage_exceptions()
    validate_research_links(
        checks,
        {entry.source_id for entry in entries},
        {
            f"coverage.{exception.state.lower()}.{exception.jurisdiction_level.value}"
            for exception in exceptions
        },
    )
    print(f"verified: validated {len(checks)} immutable discovery checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
