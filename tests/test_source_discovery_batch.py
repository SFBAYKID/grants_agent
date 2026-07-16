"""Tests for deterministic bounded Firecrawl entity-discovery orchestration."""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

import grant_watch.source_discovery_batch as batch
from grant_watch.firecrawl_client import (
    SearchOutcome,
    SearchResultEvidence,
    canonical_json_hash,
)
from grant_watch.source_discovery_batch import (
    QUERY_TEMPLATE_ID,
    ResearchTarget,
    build_query,
    completed_request_keys,
    execute_batch,
    load_research_targets,
    main,
    plan_batch,
    select_targets,
    summarize_batch,
    validate_stored_batches,
)
from grant_watch.source_discovery_models import make_task_id
from grant_watch.source_discovery_store import load_checkpoints


@dataclass
class FakeClient:
    """Return queued typed outcomes and record every paid-call equivalent."""

    outcomes: list[SearchOutcome]

    def __post_init__(self) -> None:
        """Initialize a mutable call log outside the dataclass constructor."""
        self.calls: list[tuple[str, int]] = []

    def search_once(self, query: str, result_limit: int) -> SearchOutcome:
        """Record one call and pop its deterministic response."""
        self.calls.append((query, result_limit))
        if not self.outcomes:
            raise AssertionError("unexpected Firecrawl call")
        return self.outcomes.pop(0)


class CrashingClient:
    """Simulate a process failure after orchestration records an in-flight call."""

    def search_once(self, query: str, result_limit: int) -> SearchOutcome:
        """Raise at the transport boundary where paid-call completion is unknown."""
        del query, result_limit
        raise RuntimeError("simulated process crash")


def _target(
    namespace: str = "county",
    geoid: str = "06001",
    state: str = "CA",
) -> ResearchTarget:
    """Build one typed queue target with a namespace-appropriate GEOID."""
    return ResearchTarget(
        entity_namespace=namespace,
        geoid=geoid,
        state=state,
        entity_name=f"Test {namespace} {geoid}",
        entity_kind=namespace,
        universe_vintage="2025",
    )


def _success(url: str = "https://example.gov/bids") -> SearchOutcome:
    """Build one successful secret-free search outcome."""
    metadata = {"url": url, "title": "Bids", "description": "Open RFPs"}
    payload = {"success": True, "data": [metadata]}
    return SearchOutcome(
        outcome="success",
        http_status=200,
        retry_after_seconds=0,
        response_sha256=canonical_json_hash(payload),
        response_metadata={"success": True},
        results=(SearchResultEvidence(1, metadata),),
        error_code="",
        sanitized_error="",
        retryable=False,
        systemic=False,
    )


def _failure(
    outcome: str = "timeout",
    *,
    retryable: bool = True,
    systemic: bool = False,
    retry_after: float = 0,
) -> SearchOutcome:
    """Build one metadata-only failure for retry and stop tests."""
    http_status = (
        401
        if systemic
        else 429
        if outcome == "rate_limited"
        else 0
        if outcome == "timeout"
        else 500
    )
    return SearchOutcome(
        outcome=outcome,
        http_status=http_status,
        retry_after_seconds=retry_after,
        response_sha256="",
        response_metadata={},
        results=(),
        error_code="http_401" if systemic else "requests_timeout",
        sanitized_error="http_401" if systemic else "requests_timeout",
        retryable=retryable,
        systemic=systemic,
    )


def _fixed_now() -> datetime:
    """Return one deterministic timezone-aware clock value for batch tests."""
    return datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


def _plan(
    targets: list[ResearchTarget], *, max_attempts: int = 2
) -> tuple[batch.BatchManifest, list[batch.DiscoveryCheckpoint]]:
    """Build one fixed-time batch fixture through production planning."""
    return plan_batch(
        targets,
        batch_id="20260715T120000Z",
        created_at_utc="2026-07-15T12:00:00Z",
        result_limit=2,
        requests_per_minute=10,
        max_attempts=max_attempts,
        selection_seed="seed",
    )


def test_query_includes_rfp_contract_award_and_grant_discovery_terms() -> None:
    """The fixed template searches both procurement and funding-source surfaces."""
    query = build_query(_target("school_district", "0600001"))
    assert "procurement bids RFP" in query
    assert "contract awards" in query
    assert "grant awards funding" in query
    assert "school district" in query


def test_target_adapters_only_return_canonical_not_researched_rows() -> None:
    """Canonical adapters exclude linked, structural, and completed coverage tasks."""
    targets = load_research_targets()
    assert targets
    assert {target.entity_namespace for target in targets} == {
        "county",
        "school_district",
        "incorporated_place",
    }
    assert len({target.key for target in targets}) == len(targets)
    assert all(target.universe_vintage == "2025" for target in targets)


def test_selection_is_deterministic_balanced_and_namespace_safe() -> None:
    """Hash ranking balances layers while preserving colliding numeric identities."""
    targets = [_target("county", f"06{index:03d}") for index in range(1, 6)]
    targets.extend(
        _target("school_district", f"06{index:05d}") for index in range(1, 6)
    )
    targets.extend(
        _target("incorporated_place", f"06{index:05d}") for index in range(1, 6)
    )
    first = select_targets(
        targets,
        states=frozenset({"CA"}),
        namespaces=frozenset({"county", "school_district", "incorporated_place"}),
        per_namespace_state=2,
        task_limit=6,
        selection_seed="seed",
    )
    second = select_targets(
        list(reversed(targets)),
        states=frozenset({"CA"}),
        namespaces=frozenset({"county", "school_district", "incorporated_place"}),
        per_namespace_state=2,
        task_limit=6,
        selection_seed="seed",
    )
    assert first == second
    assert Counter(target.entity_namespace for target in first) == {
        "county": 2,
        "school_district": 2,
        "incorporated_place": 2,
    }
    skipped = select_targets(
        targets,
        states=frozenset({"CA"}),
        namespaces=frozenset({"county"}),
        per_namespace_state=5,
        task_limit=5,
        selection_seed="seed",
        completed_requests=frozenset({first[0].request_key(5)}),
    )
    assert first[0] not in skipped
    changed_vintage = [
        ResearchTarget(
            entity_namespace=first[0].entity_namespace,
            geoid=first[0].geoid,
            state=first[0].state,
            entity_name=first[0].entity_name,
            entity_kind=first[0].entity_kind,
            universe_vintage="2026",
        )
    ]
    assert (
        select_targets(
            changed_vintage,
            states=frozenset({"CA"}),
            namespaces=frozenset({first[0].entity_namespace}),
            per_namespace_state=1,
            task_limit=1,
            selection_seed="seed",
            completed_requests=frozenset({first[0].request_key(5)}),
        )
        == changed_vintage
    )


def test_plan_has_deterministic_fingerprints_and_fixed_bounds() -> None:
    """The manifest exactly fingerprints every planned namespaced request."""
    manifest, checkpoints = _plan([_target(), _target("school_district", "0600001")])
    assert manifest.query_template_id == QUERY_TEMPLATE_ID
    assert manifest.task_limit == 2
    assert manifest.task_fingerprints == tuple(
        sorted(checkpoint.task_id for checkpoint in checkpoints)
    )
    assert len({checkpoint.task_id for checkpoint in checkpoints}) == 2


def test_execute_rejects_validation_only_schema_v1(tmp_path: Path) -> None:
    """Direct callers cannot downgrade a paid run to the legacy identity schema."""
    manifest, checkpoints = _plan([_target()])
    checkpoint = checkpoints[0]
    legacy_id = make_task_id(
        manifest.batch_id,
        checkpoint.entity_namespace,
        checkpoint.geoid,
        manifest.query_template_id,
        checkpoint.query,
        manifest.result_limit,
        schema_version=1,
    )
    legacy_manifest = replace(
        manifest,
        schema_version=1,
        task_fingerprints=(legacy_id,),
    )
    legacy_checkpoint = replace(checkpoint, task_id=legacy_id)
    with pytest.raises(ValueError, match="current evidence schema"):
        execute_batch(
            tmp_path,
            legacy_manifest,
            [legacy_checkpoint],
            FakeClient([_success()]),
            now=_fixed_now,
            sleeper=lambda seconds: None,
        )
    assert not list(tmp_path.iterdir())


def test_execute_retries_rate_limit_then_resumes_without_duplicate_calls(
    tmp_path: Path,
) -> None:
    """Retryable evidence is checkpointed and completed runs make no repeat calls."""
    manifest, checkpoints = _plan([_target()], max_attempts=2)
    client = FakeClient([_failure("rate_limited", retry_after=9), _success()])
    sleeps: list[float] = []
    summary = execute_batch(
        tmp_path,
        manifest,
        checkpoints,
        client,
        now=_fixed_now,
        sleeper=sleeps.append,
    )
    assert len(client.calls) == 2
    assert sleeps == [9]
    assert summary.statuses == Counter({"success": 1})
    assert summary.attempt_count == 2
    resumed = FakeClient([])
    second = execute_batch(
        tmp_path,
        manifest,
        checkpoints,
        resumed,
        now=_fixed_now,
        sleeper=sleeps.append,
    )
    assert resumed.calls == []
    assert second == summary


def test_systemic_auth_failure_stops_batch_after_durable_checkpoint(
    tmp_path: Path,
) -> None:
    """401/402/403 evidence is stored once and stops further credit spending."""
    manifest, checkpoints = _plan([_target(), _target("county", "06003")])
    client = FakeClient([_failure("http_error", retryable=False, systemic=True)])
    with pytest.raises(RuntimeError, match="authorization or billing"):
        execute_batch(
            tmp_path,
            manifest,
            checkpoints,
            client,
            now=_fixed_now,
            sleeper=lambda seconds: None,
        )
    assert len(client.calls) == 1
    stored = load_checkpoints(tmp_path / manifest.batch_id)
    assert Counter(item.terminal_status for item in stored) == {
        "non_retryable_failure": 1,
        "pending": 1,
    }


def test_crash_after_precall_checkpoint_requires_explicit_retry(tmp_path: Path) -> None:
    """An unknown paid-call outcome is never retried silently after restart."""
    manifest, checkpoints = _plan([_target()], max_attempts=2)
    with pytest.raises(RuntimeError, match="simulated process crash"):
        execute_batch(
            tmp_path,
            manifest,
            checkpoints,
            CrashingClient(),
            now=_fixed_now,
            sleeper=lambda seconds: None,
        )
    stored = load_checkpoints(tmp_path / manifest.batch_id)
    assert stored[0].terminal_status == "in_flight"

    no_retry = FakeClient([_success()])
    with pytest.raises(RuntimeError, match="--retry-indeterminate"):
        execute_batch(
            tmp_path,
            manifest,
            checkpoints,
            no_retry,
            now=_fixed_now,
            sleeper=lambda seconds: None,
        )
    assert no_retry.calls == []

    retried = FakeClient([_success()])
    summary = execute_batch(
        tmp_path,
        manifest,
        checkpoints,
        retried,
        now=_fixed_now,
        sleeper=lambda seconds: None,
        retry_indeterminate=True,
    )
    final = load_checkpoints(tmp_path / manifest.batch_id)[0]
    assert summary.statuses == Counter({"success": 1})
    assert [attempt.outcome for attempt in final.attempts] == [
        "indeterminate",
        "success",
    ]


def test_rate_window_is_shared_across_different_batch_ids(tmp_path: Path) -> None:
    """A new batch waits for the prior batch's persisted completion window."""
    first_manifest, first_checkpoints = _plan([_target()])
    execute_batch(
        tmp_path,
        first_manifest,
        first_checkpoints,
        FakeClient([_success()]),
        now=_fixed_now,
        sleeper=lambda seconds: None,
    )
    second_manifest, second_checkpoints = plan_batch(
        [_target("county", "06003")],
        batch_id="20260715T120001Z",
        created_at_utc="2026-07-15T12:00:01Z",
        result_limit=2,
        requests_per_minute=10,
        max_attempts=2,
        selection_seed="seed",
    )
    sleeps: list[float] = []
    execute_batch(
        tmp_path,
        second_manifest,
        second_checkpoints,
        FakeClient([_success()]),
        now=_fixed_now,
        sleeper=sleeps.append,
    )
    assert sleeps == [6.0]


def test_completed_keys_and_validator_use_only_success_or_zero(tmp_path: Path) -> None:
    """Raw terminal evidence advances selection without changing coverage tasks."""
    manifest, checkpoints = _plan([_target()])
    execute_batch(
        tmp_path,
        manifest,
        checkpoints,
        FakeClient([_success()]),
        now=_fixed_now,
        sleeper=lambda seconds: None,
    )
    assert completed_request_keys(tmp_path) == frozenset(
        {
            (
                "county",
                "06001",
                "2025",
                checkpoints[0].request_sha256,
            )
        }
    )
    summaries = validate_stored_batches(tmp_path)
    assert len(summaries) == 1
    assert summaries[0].result_count == 1


def test_batch_writes_cannot_modify_catalog_or_coverage_files(tmp_path: Path) -> None:
    """A raw batch leaves every canonical promotion-controlled artifact unchanged."""
    catalog_root = batch.ROOT / "data/source_catalog"
    protected = [
        path
        for path in catalog_root.rglob("*")
        if path.is_file() and "firecrawl_batches" not in path.parts
    ]
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in protected}
    manifest, checkpoints = _plan([_target()])
    execute_batch(
        tmp_path,
        manifest,
        checkpoints,
        FakeClient([_success()]),
        now=_fixed_now,
        sleeper=lambda seconds: None,
    )
    after = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in protected}
    assert after == before


def test_dry_run_reads_no_key_makes_no_call_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Dry run performs selection only and has no credential/network/write effects."""

    def forbidden() -> None:
        """Fail if dry-run orchestration tries to load environment credentials."""
        raise AssertionError("dry run loaded dotenv")

    monkeypatch.setattr(batch, "load_dotenv", forbidden)
    result = main(
        [
            "--states",
            "CA",
            "--namespaces",
            "county",
            "--per-namespace-state",
            "1",
            "--task-limit",
            "1",
            "--batch-id",
            "20260715T120000Z",
            "--root",
            str(tmp_path),
            "--dry-run",
        ]
    )
    assert result == 0
    assert not list(tmp_path.iterdir())
    assert (
        "no key read, no network call made, no file written" in capsys.readouterr().out
    )


def test_summary_distinguishes_pending_zero_and_success() -> None:
    """Derived reports never imply all planned tasks completed when one is pending."""
    manifest, checkpoints = _plan(
        [_target(), _target("county", "06003"), _target("county", "06005")]
    )
    from grant_watch.source_discovery_models import append_attempt

    completed = append_attempt(
        checkpoints[0],
        _success(),
        "2026-07-15T12:00:00Z",
        "2026-07-15T12:00:01Z",
        manifest.max_attempts,
    )
    zero_outcome = SearchOutcome(
        outcome="zero_results",
        http_status=200,
        retry_after_seconds=0,
        response_sha256=canonical_json_hash({"success": True, "data": []}),
        response_metadata={"success": True},
        results=(),
        error_code="",
        sanitized_error="",
        retryable=False,
        systemic=False,
    )
    zero = append_attempt(
        checkpoints[1],
        zero_outcome,
        "2026-07-15T12:00:00Z",
        "2026-07-15T12:00:01Z",
        manifest.max_attempts,
    )
    summary = summarize_batch(manifest, [completed, zero, checkpoints[2]])
    assert summary.statuses == Counter({"success": 1, "zero_results": 1, "pending": 1})


def test_canonical_raw_batch_evidence_is_complete_and_valid() -> None:
    """Committed raw evidence retains the exact live batch denominator and results."""
    summaries = validate_stored_batches()
    assert len(summaries) == 1
    assert summaries[0].batch_id == "20260716T004633Z"
    assert summaries[0].task_count == 27
    assert summaries[0].attempt_count == 27
    assert summaries[0].result_count == 126
    assert summaries[0].statuses == Counter({"success": 27})
