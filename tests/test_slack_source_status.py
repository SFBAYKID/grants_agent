"""Offline UI-contract tests for Grant's read-only source-discovery Slack surface."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import grant_watch.slack.conversation as conversation
import grant_watch.slack.source_status as status
import grant_watch.slack.tools as tools
from grant_watch.firecrawl_client import SearchOutcome, canonical_json_hash
from grant_watch.source_catalog import (
    AccessMode,
    IntegrationStatus,
    JurisdictionLevel,
    SourceCatalogEntry,
    VerificationLabel,
)
from grant_watch.source_discovery import DiscoveryCheck
from grant_watch.source_discovery_batch import ResearchTarget, plan_batch
from grant_watch.source_discovery_models import append_attempt, begin_attempt
from grant_watch.source_discovery_store import initialize_batch, replace_checkpoint


def test_canonical_summary_preserves_inventory_lead_and_poller_boundaries() -> None:
    """Slack summary exposes exact aggregates without collapsing evidence layers."""
    text = status.source_inventory_status()
    assert "catalog sources: 270" in text
    assert "manually reviewed catalog sources: 29" in text
    assert "selected-result evidence checks: 30" in text
    assert "validated raw batches stored: 1" in text
    assert "counties: 3144 total; 56 candidate_found; 3073 not_researched" in text
    assert "school districts: 13363 total; 66 candidate_found" in text
    assert "incorporated places: 32058 total; 14 candidate_found" in text
    assert "Research inventory is not the lead database" in text
    assert "live_positive_verified=5" in text


def test_state_summary_counts_only_batches_that_include_that_state() -> None:
    """A state-filtered answer never inherits an unrelated nationwide batch count."""
    text = status.source_inventory_status(view="summary", state="NV")
    assert "Source discovery summary for NV" in text
    assert "validated raw batches stored: 0" in text


def test_state_namespace_coverage_is_exact_and_does_not_claim_integration() -> None:
    """Natural state/layer filters retain candidate and structural distinctions."""
    text = status.source_inventory_status(
        view="coverage", state="CA", namespace="school_district"
    )
    assert "Source research coverage for CA" in text
    assert "school districts: 975 total" in text
    assert "3 candidate_found" in text
    assert "971 not_researched" in text
    assert "1 not_applicable" in text
    assert "does not mean a working poller or lead" in text
    assert "counties:" not in text


def test_reviewed_sources_expose_only_safe_reviewed_catalog_fields() -> None:
    """Reviewed-source UI omits raw queries, snippets, hashes, notes, and credentials."""
    text = status.source_inventory_status(view="reviewed_sources", state="NH", limit=5)
    assert "showing 3 of 3" in text
    assert "nh.strafford_county.bids" in text
    assert "access=public_no_auth/verified" in text
    assert "integration=access_checked" in text
    assert "https://co.strafford.nh.us/" in text
    lowered = text.lower()
    for forbidden in (
        "query=",
        "snippet",
        "sha256",
        "credential",
        "firecrawl_api_key",
        "notes=",
    ):
        assert forbidden not in lowered


def test_recent_batch_ui_labels_legacy_and_search_success_truthfully() -> None:
    """A completed raw search never becomes a reviewed/promoted-source claim."""
    text = status.source_inventory_status(view="recent_batches")
    assert "20260716T004633Z" in text
    assert "schema v1 (validation-only legacy)" in text
    assert "tasks=27; attempts=27; results=126; success=27" in text
    assert "search completed" in text
    assert "does not mean a source was reviewed or promoted" in text


def test_recent_batch_state_and_namespace_filters_scope_the_counts() -> None:
    """Filtered batch UI derives counts from matching checkpoints, not the whole batch."""
    state_text = status.source_inventory_status(view="recent_batches", state="CA")
    county_text = status.source_inventory_status(
        view="recent_batches", namespace="county"
    )
    for text in (state_text, county_text):
        assert "tasks=9; attempts=9; results=45; success=9" in text
        assert "tasks=27" not in text


@pytest.mark.parametrize(
    ("message", "view", "state_code", "namespace"),
    [
        ("show source discovery status", "summary", "", "all"),
        (
            "show source inventory coverage for school districts in California",
            "coverage",
            "CA",
            "school_district",
        ),
        ("list reviewed sources in NH", "reviewed_sources", "NH", "all"),
        ("show the last 5 discovery batches", "recent_batches", "", "all"),
    ],
)
def test_natural_language_status_requests_parse_deterministically(
    message: str, view: str, state_code: str, namespace: str
) -> None:
    """Supported Slack phrasings map to one bounded read-only request."""
    request = status.parse_status_request(message)
    assert request is not None
    assert request.view == view
    assert request.state == state_code
    assert request.namespace == namespace


def test_source_status_followup_uses_thread_context() -> None:
    """A plain state follow-up remains in the source-inventory conversation."""
    request = status.parse_status_request(
        "What about Texas?", ["Grant: Source discovery summary nationwide"]
    )
    assert request is not None
    assert request.state == "TX"
    assert request.view == "summary"


@pytest.mark.parametrize(
    "message",
    [
        "Run source discovery for California",
        "Start source research in California",
        "Find new sources in California",
        "Search for new sources in California",
        "Run Firecrawl for California",
        "Launch discovery in California",
    ],
)
def test_paid_discovery_request_is_disabled_before_any_network_tool(
    monkeypatch: pytest.MonkeyPatch, message: str
) -> None:
    """Slack cannot turn typed words into paid Firecrawl work or API-key access."""

    def forbidden_anthropic() -> None:
        """Fail if deterministic inventory routing reaches the model client."""
        raise AssertionError("Anthropic should not be constructed")

    def forbidden_web_search(
        query: str, on_progress: tools.Progress | None = None
    ) -> str:
        """Fail if an internal status request reaches paid public-web search."""
        del query, on_progress
        raise AssertionError("web_search should not run")

    monkeypatch.setattr(conversation, "Anthropic", forbidden_anthropic)
    monkeypatch.setattr(tools, "web_search", forbidden_web_search)
    monkeypatch.setenv("FIRECRAWL_API_KEY", "must-not-be-read")
    result = conversation.respond(message, None)
    assert result["intent"] == "question"
    assert "paid discovery runs are disabled" in result["reply"]
    assert result["files"] == []


def test_status_request_bypasses_anthropic_and_web_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inventory UI is locally deterministic even when network tools are unavailable."""

    def forbidden() -> None:
        """Fail if a local read-only Slack request reaches an external client."""
        raise AssertionError("external client should not run")

    monkeypatch.setattr(conversation, "Anthropic", forbidden)
    result = conversation.respond("Show source discovery status", None)
    assert result["intent"] == "question"
    assert "catalog sources: 270" in result["reply"]


def test_tool_schema_and_dispatch_expose_only_read_only_status() -> None:
    """The model tool layer can read inventory but has no paid execution tool."""
    names = {schema["name"] for schema in tools.TOOL_SCHEMAS}
    assert "source_inventory_status" in names
    assert "run_source_discovery" not in names
    text, artifact = tools.run_tool(
        "source_inventory_status",
        {"view": "coverage", "state": "CA", "namespace": "county"},
    )
    assert artifact is None
    assert "Source research coverage for CA" in text
    assert "counties:" in text


def _unsafe_entry() -> SourceCatalogEntry:
    """Build one reviewed entry containing fields that must never reach Slack raw."""
    return SourceCatalogEntry(
        source_id="ca.unsafe.source",
        name="Unsafe <@U123> & source",
        jurisdiction_level=JurisdictionLevel.COUNTY,
        state="CA",
        publisher="Publisher",
        source_kinds="rfp",
        lead_signals="silver",
        url="https://example.gov/bids?api_key=SUPER_SECRET_URL_VALUE",
        portal_family="custom",
        access_mode=AccessMode.PUBLIC_API_KEY,
        credential_env="SUPER_SECRET_API_KEY",
        official_status=VerificationLabel.VERIFIED,
        access_status=VerificationLabel.VERIFIED,
        integration_status=IntegrationStatus.DISCOVERED,
        discovered_on="2026-07-15",
        last_access_checked_on="2026-07-15",
        discovery_method="firecrawl_search",
        evidence_url="https://example.gov/evidence",
        coverage_rule="optional",
        coverage_scope="Unsafe",
        notes="private operator note",
    )


def _unsafe_check() -> DiscoveryCheck:
    """Build selected-result evidence whose raw research fields must remain hidden."""
    return DiscoveryCheck(
        check_id="fc.20260715.ca.unsafe",
        research_key="ca.unsafe.source",
        state="CA",
        jurisdiction_level="county",
        query="secret internal query",
        checked_on="2026-07-15",
        transport="firecrawl_search",
        result_rank=1,
        result_url="https://example.gov/result",
        result_title="raw <@U999> title",
        result_snippet="raw secret snippet",
        search_evidence_sha256="a" * 64,
        content_sha256="b" * 64,
        content_status="scraped",
        notes="raw note",
    )


def test_reviewed_source_renderer_escapes_markup_and_hides_sensitive_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even validated-model bypasses cannot inject mentions or credential metadata."""

    def load_unsafe_catalog(path: Path) -> list[SourceCatalogEntry]:
        """Return one adversarial typed catalog row regardless of the injected path."""
        del path
        return [_unsafe_entry()]

    def load_unsafe_checks(path: Path) -> list[DiscoveryCheck]:
        """Return one adversarial typed evidence row regardless of the injected path."""
        del path
        return [_unsafe_check()]

    monkeypatch.setattr(status, "load_catalog", load_unsafe_catalog)
    monkeypatch.setattr(status, "load_discovery_checks", load_unsafe_checks)
    text = status.source_inventory_status(view="reviewed_sources", state="CA")
    assert "&lt;@U123&gt; &amp; source" in text
    assert "(URL unavailable)" in text
    for hidden in (
        "SUPER_SECRET_API_KEY",
        "SUPER_SECRET_URL_VALUE",
        "private operator note",
        "secret internal query",
        "raw secret snippet",
        "raw <@U999> title",
    ):
        assert hidden not in text


def _outcome(outcome: str) -> SearchOutcome:
    """Build one strict transport outcome for batch-status UI testing."""
    if outcome == "zero_results":
        payload = {"success": True, "data": []}
        return SearchOutcome(
            outcome="zero_results",
            http_status=200,
            retry_after_seconds=0,
            response_sha256=canonical_json_hash(payload),
            response_metadata={"success": True},
            results=(),
            error_code="",
            sanitized_error="",
            retryable=False,
            systemic=False,
        )
    return SearchOutcome(
        outcome="http_error",
        http_status=404,
        retry_after_seconds=0,
        response_sha256="",
        response_metadata={},
        results=(),
        error_code="http_404",
        sanitized_error="http_404",
        retryable=False,
        systemic=False,
    )


def test_batch_renderer_preserves_zero_failure_and_inflight_states(
    tmp_path: Path,
) -> None:
    """Recent-batch UI reports truthful non-success and indeterminate denominators."""
    targets = [
        ResearchTarget("county", geoid, "CA", f"County {geoid}", "county", "2025")
        for geoid in ("06001", "06003", "06005")
    ]
    manifest, checkpoints = plan_batch(
        targets,
        batch_id="20260716T120000Z",
        created_at_utc="2026-07-16T12:00:00Z",
        result_limit=2,
        requests_per_minute=10,
        max_attempts=2,
        selection_seed="slack-test",
    )
    batch_dir = initialize_batch(tmp_path, manifest, checkpoints)
    zero = append_attempt(
        checkpoints[0],
        _outcome("zero_results"),
        "2026-07-16T12:00:00Z",
        "2026-07-16T12:00:01Z",
        manifest.max_attempts,
    )
    failed = append_attempt(
        checkpoints[1],
        _outcome("http_error"),
        "2026-07-16T12:00:02Z",
        "2026-07-16T12:00:03Z",
        manifest.max_attempts,
    )
    in_flight = begin_attempt(
        checkpoints[2], "2026-07-16T12:00:04Z", manifest.max_attempts
    )
    for checkpoint in (zero, failed, in_flight):
        replace_checkpoint(batch_dir, checkpoint)
    paths = replace(status.DiscoveryStatusPaths(), batches=tmp_path)
    text = status.source_inventory_status(view="recent_batches", paths=paths)
    assert "zero_results=1" in text
    assert "non_retryable_failure=1" in text
    assert "in_flight=1" in text
    assert "tasks=3; attempts=3; results=0" in text
