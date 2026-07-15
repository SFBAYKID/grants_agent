"""Validation tests for immutable Firecrawl source-discovery evidence."""

from __future__ import annotations

import csv
from dataclasses import fields, replace
from pathlib import Path

import pytest

from grant_watch.source_catalog import load_catalog, load_coverage_exceptions
from grant_watch.source_discovery import (
    DiscoveryCheck,
    load_discovery_checks,
    validate_research_links,
)


def _write_checks(path: Path, checks: list[DiscoveryCheck]) -> None:
    """Write discovery records using the production CSV column contract."""
    fieldnames = [field.name for field in fields(DiscoveryCheck)]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for check in checks:
            writer.writerow({name: getattr(check, name) for name in fieldnames})


def test_canonical_discovery_checks_link_to_sources_or_exceptions() -> None:
    """Every stored Firecrawl check resolves to durable canonical research state."""
    entries = load_catalog()
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
    assert len(checks) == 12
    assert all(check.transport == "firecrawl_search" for check in checks)


def test_discovery_check_rejects_invalid_hash(tmp_path: Path) -> None:
    """A malformed content/evidence hash cannot masquerade as immutable evidence."""
    path = tmp_path / "checks.csv"
    check = replace(load_discovery_checks()[0], content_sha256="not-a-hash")
    _write_checks(path, [check])
    with pytest.raises(ValueError, match="invalid content_sha256"):
        load_discovery_checks(path)


def test_discovery_check_recomputes_selected_result_hash(tmp_path: Path) -> None:
    """Edited search evidence fails its deterministic content-derived hash."""
    path = tmp_path / "checks.csv"
    check = replace(load_discovery_checks()[0], result_title="Altered title")
    _write_checks(path, [check])
    with pytest.raises(ValueError, match="search evidence hash mismatch"):
        load_discovery_checks(path)


def test_discovery_check_rejects_unlinked_research_key() -> None:
    """Evidence cannot become an orphan detached from source or coverage state."""
    check = replace(load_discovery_checks()[0], research_key="missing.source")
    with pytest.raises(ValueError, match="unlinked discovery research keys"):
        validate_research_links([check], set(), set())
