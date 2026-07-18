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
    assert "270 candidate sources catalogued" in text
    assert "29 reviewed by hand so far" in text
    assert "30 pages checked with saved evidence" in text
    assert "1 raw search batch stored" in text
    assert "Counties: 3,144 in total" in text
    assert "56 with a source link" in text
    assert "3,073 not yet researched" in text
    assert "School districts: 13,363 in total" in text
    assert "66 with a source link" in text
    assert "Incorporated places: 32,058 in total" in text
    assert "This is our research list, not the leads themselves" in text
    assert "5 live and finding matches" in text


def test_state_summary_counts_only_batches_that_include_that_state() -> None:
    """A state-filtered answer never inherits an unrelated nationwide batch count."""
    text = status.source_inventory_status(view="summary", state="NV")
    assert "Source discovery status for NV" in text
    assert "0 raw search batches stored" in text


def test_state_namespace_coverage_is_exact_and_does_not_claim_integration() -> None:
    """Natural state/layer filters retain candidate and structural distinctions."""
    text = status.source_inventory_status(
        view="coverage", state="CA", namespace="school_district"
    )
    assert "Source research coverage for CA" in text
    assert "School districts: 975 in total" in text
    assert "3 with a source link" in text
    assert "971 not yet researched" in text
    assert "1 no separate source needed" in text
    assert "not a working feed or a lead" in text
    assert "Counties:" not in text


def test_reviewed_sources_expose_only_safe_reviewed_catalog_fields() -> None:
    """Reviewed-source UI omits raw queries, snippets, hashes, notes, and credentials."""
    text = status.source_inventory_status(view="reviewed_sources", state="NH", limit=5)
    assert "the three sources we've reviewed in NH" in text
    # Internal catalog slugs never surface to the human-facing answer.
    assert "nh.strafford_county.bids" not in text
    assert "Access is open, no login (confirmed)" in text
    assert "we've confirmed access but haven't built a feed yet" in text
    assert "county source" in text
    assert "https://co.strafford.nh.us/" in text
    lowered = text.lower()
    for forbidden in (
        "query=",
        "snippet",
        "sha256",
        "credential",
        "firecrawl_api_key",
        "notes=",
        "access=",
        "integration=",
    ):
        assert forbidden not in lowered


def test_recent_batch_ui_labels_legacy_and_search_success_truthfully() -> None:
    """A completed raw search never becomes a reviewed/promoted-source claim."""
    text = status.source_inventory_status(view="recent_batches")
    assert "I completed one recent discovery search on July 16, 2026" in text
    assert "found 126 potential results across 27 searches" in text
    assert "raw search results, not reviewed sources" in text
    assert "doesn't mean a source was reviewed or added" in text


def test_recent_batch_state_and_namespace_filters_scope_the_counts() -> None:
    """Filtered batch UI derives counts from matching checkpoints, not the whole batch."""
    state_text = status.source_inventory_status(view="recent_batches", state="CA")
    county_text = status.source_inventory_status(
        view="recent_batches", namespace="county"
    )
    for text in (state_text, county_text):
        assert "found 45 potential results across nine searches" in text
        assert "126" not in text


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
        ("show recent discovery batch for CA", "recent_batches", "CA", "all"),
        (
            "show recent discoveries in California",
            "reviewed_sources",
            "CA",
            "all",
        ),
        (
            "what did the raw discovery search find in California?",
            "recent_batches",
            "CA",
            "all",
        ),
        (
            "How much of California's school district research is done?",
            "coverage",
            "CA",
            "school_district",
        ),
        (
            "What has Grant actually reviewed in New Hampshire?",
            "reviewed_sources",
            "NH",
            "all",
        ),
        ("lemme see the reviewed NH sources", "reviewed_sources", "NH", "all"),
        (
            "How many Texas counties are still not researched?",
            "coverage",
            "TX",
            "county",
        ),
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


def test_read_only_search_wording_is_not_mistaken_for_paid_execution() -> None:
    """Questions about stored search evidence remain read-only inventory requests."""
    request = status.parse_status_request(
        "What did the raw discovery search find in California?"
    )
    assert request is not None
    assert request.paid_execution_requested is False


@pytest.mark.parametrize(
    "message",
    [
        "Run source discovery for California",
        "Start source research in California",
        "Find new sources in California",
        "Search for new sources in California",
        "Run Firecrawl for California",
        "Launch discovery in California",
        "Go run Firecrawl source discovery for California right now",
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
    assert "270 candidate sources catalogued" in result["reply"]


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
    assert "Counties:" in text


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
    assert "I ran one recent discovery search" in text
    assert "found no results across three searches" in text
    assert "one came back empty" in text
    assert "one failed" in text
    assert "one still running" in text
