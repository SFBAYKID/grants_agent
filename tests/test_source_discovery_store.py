"""Tests for atomic restartable Firecrawl raw-evidence batch storage."""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

import grant_watch.source_discovery_store as store
from grant_watch.firecrawl_client import (
    SearchOutcome,
    SearchResultEvidence,
    canonical_json_hash,
)
from grant_watch.source_discovery_store import (
    BatchLock,
    BatchLockedError,
    ExecutionLock,
    initialize_batch,
    load_checkpoints,
    load_manifest,
    replace_checkpoint,
    validate_batch,
    validate_checkpoint,
)
from grant_watch.source_discovery_models import (
    SCHEMA_VERSION,
    BatchManifest,
    DiscoveryCheckpoint,
    append_attempt,
    begin_attempt,
    complete_attempt,
    make_task_id,
    new_checkpoint,
    recover_in_flight,
)


def _bundle(
    *, max_attempts: int = 2, namespaces: tuple[str, ...] = ("county",)
) -> tuple[BatchManifest, list[DiscoveryCheckpoint]]:
    """Build a valid one-or-more-task manifest/checkpoint fixture."""
    provisional = BatchManifest(
        schema_version=SCHEMA_VERSION,
        batch_id="20260715T120000Z",
        created_at_utc="2026-07-15T12:00:00Z",
        query_template_id="rfp-grants-v1",
        selection_seed="seed",
        namespaces=namespaces,
        states=("CA",),
        task_limit=len(namespaces),
        result_limit=2,
        requests_per_minute=10,
        max_attempts=max_attempts,
        task_fingerprints=(),
    )
    checkpoints = [
        new_checkpoint(
            manifest=provisional,
            entity_namespace=namespace,
            geoid="06001" if namespace == "county" else "0600001",
            state="CA",
            entity_name=f"Test {namespace}",
            entity_kind=namespace,
            universe_vintage="2025",
            query=f"query for {namespace}",
        )
        for namespace in namespaces
    ]
    manifest = replace(
        provisional,
        task_fingerprints=tuple(
            sorted(checkpoint.task_id for checkpoint in checkpoints)
        ),
    )
    validate_batch(manifest, checkpoints)
    return manifest, checkpoints


def _outcome(outcome: str = "success", *, retryable: bool = False) -> SearchOutcome:
    """Build one secret-free transport outcome for checkpoint tests."""
    successful = outcome in {"success", "zero_results"}
    results = (
        (
            SearchResultEvidence(
                rank=1,
                metadata={
                    "url": "https://example.gov/bids",
                    "title": "Bids",
                    "nested": {"kind": "official"},
                },
            ),
        )
        if outcome == "success"
        else ()
    )
    metadata = {"success": True} if successful else {}
    payload = {**metadata, "data": [result.metadata for result in results]}
    http_status = (
        200
        if successful
        else 429
        if outcome == "rate_limited"
        else 0
        if outcome == "timeout"
        else 500
    )
    return SearchOutcome(
        outcome=outcome,
        http_status=http_status,
        retry_after_seconds=0.0,
        response_sha256=canonical_json_hash(payload) if successful else "",
        response_metadata=metadata,
        results=results,
        error_code="" if successful else "http_500",
        sanitized_error="" if successful else "http_500",
        retryable=retryable,
        systemic=False,
    )


def test_batch_snapshot_round_trips_namespaced_collisions(tmp_path: Path) -> None:
    """County and seven-digit namespaces remain distinct in deterministic shards."""
    manifest, checkpoints = _bundle(
        namespaces=("county", "school_district", "incorporated_place")
    )
    batch_dir = initialize_batch(tmp_path, manifest, checkpoints)
    assert load_manifest(batch_dir) == manifest
    assert load_checkpoints(batch_dir) == sorted(
        checkpoints, key=lambda item: item.task_id
    )
    assert all(b"\r\n" not in path.read_bytes() for path in batch_dir.rglob("*.jsonl"))


def test_reusing_batch_id_with_changed_manifest_fails_before_rewrite(
    tmp_path: Path,
) -> None:
    """An existing batch cannot silently adopt different paid-call arguments."""
    manifest, checkpoints = _bundle()
    initialize_batch(tmp_path, manifest, checkpoints)
    changed = replace(manifest, requests_per_minute=11)
    with pytest.raises(ValueError, match="different manifest"):
        initialize_batch(tmp_path, changed, checkpoints)


def test_append_attempt_preserves_zero_failure_and_retry_states() -> None:
    """Zero results and failure classes never collapse into researched-not-found."""
    manifest, checkpoints = _bundle(max_attempts=2)
    first = append_attempt(
        checkpoints[0],
        _outcome("http_error", retryable=True),
        "2026-07-15T12:00:00Z",
        "2026-07-15T12:00:01Z",
        manifest.max_attempts,
    )
    assert first.terminal_status == "retryable_failure"
    exhausted = append_attempt(
        first,
        _outcome("timeout", retryable=True),
        "2026-07-15T12:00:02Z",
        "2026-07-15T12:00:03Z",
        manifest.max_attempts,
    )
    assert exhausted.terminal_status == "attempts_exhausted"
    zero = append_attempt(
        checkpoints[0],
        _outcome("zero_results"),
        "2026-07-15T12:00:00Z",
        "2026-07-15T12:00:01Z",
        manifest.max_attempts,
    )
    assert zero.terminal_status == "zero_results"


def test_replace_checkpoint_round_trips_nested_result_metadata(
    tmp_path: Path,
) -> None:
    """Nested metadata survives an atomic checkpoint replacement without truncation."""
    manifest, checkpoints = _bundle()
    batch_dir = initialize_batch(tmp_path, manifest, checkpoints)
    completed = append_attempt(
        checkpoints[0],
        _outcome(),
        "2026-07-15T12:00:00Z",
        "2026-07-15T12:00:01Z",
        manifest.max_attempts,
    )
    replace_checkpoint(batch_dir, completed)
    stored = load_checkpoints(batch_dir)[0]
    assert stored == completed
    assert stored.attempts[0].results[0]["metadata"] == {
        "url": "https://example.gov/bids",
        "title": "Bids",
        "nested": {"kind": "official"},
    }


def test_atomic_replace_failure_preserves_previous_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed os.replace leaves the previous complete shard authoritative."""
    manifest, checkpoints = _bundle()
    batch_dir = initialize_batch(tmp_path, manifest, checkpoints)
    before = load_checkpoints(batch_dir)
    completed = append_attempt(
        checkpoints[0],
        _outcome(),
        "2026-07-15T12:00:00Z",
        "2026-07-15T12:00:01Z",
        manifest.max_attempts,
    )

    def fail_replace(source: Path, destination: Path) -> None:
        """Simulate a same-filesystem replacement failure."""
        del source, destination
        raise OSError("simulated replace failure")

    monkeypatch.setattr(store.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated"):
        replace_checkpoint(batch_dir, completed)
    assert load_checkpoints(batch_dir) == before


def test_loader_rejects_unredacted_secret_and_wrong_shard(tmp_path: Path) -> None:
    """Foreign shard paths and secret-bearing nested metadata are invalid evidence."""
    manifest, checkpoints = _bundle()
    batch_dir = initialize_batch(tmp_path, manifest, checkpoints)
    path = next(batch_dir.rglob("*.jsonl"))
    record = json.loads(path.read_text(encoding="utf-8"))
    record["attempts"] = [
        {
            "attempt_number": 1,
            "started_at_utc": "2026-07-15T12:00:00Z",
            "completed_at_utc": "2026-07-15T12:00:01Z",
            "outcome": "success",
            "http_status": 200,
            "retry_after_seconds": 0,
            "response_sha256": "a" * 64,
            "response_metadata": {"api_key": "private"},
            "results": [],
            "error_code": "",
            "sanitized_error": "",
            "retryable": False,
            "systemic": False,
        }
    ]
    record["terminal_status"] = "success"
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unredacted"):
        load_checkpoints(batch_dir)


def test_batch_lock_rejects_concurrent_runner(tmp_path: Path) -> None:
    """A second process path fails before it can spend duplicate API credits."""
    with BatchLock(tmp_path, "20260715T120000Z"):
        with pytest.raises(BatchLockedError, match="already running"):
            with BatchLock(tmp_path, "20260715T120000Z"):
                pass


def test_execution_lock_rejects_different_concurrent_batch_ids(tmp_path: Path) -> None:
    """The root lock serializes paid calls even when batch IDs differ."""
    with ExecutionLock(tmp_path):
        with pytest.raises(BatchLockedError, match="already running"):
            with ExecutionLock(tmp_path):
                pass


def test_in_flight_attempt_requires_explicit_indeterminate_recovery() -> None:
    """A durable pre-call marker can be completed or honestly recovered after a crash."""
    manifest, checkpoints = _bundle(max_attempts=2)
    started = begin_attempt(
        checkpoints[0], "2026-07-15T12:00:00Z", manifest.max_attempts
    )
    assert started.terminal_status == "in_flight"
    completed = complete_attempt(
        started,
        _outcome(),
        "2026-07-15T12:00:01Z",
        manifest.max_attempts,
    )
    assert completed.terminal_status == "success"
    recovered = recover_in_flight(
        started, "2026-07-15T12:00:02Z", manifest.max_attempts
    )
    assert recovered.terminal_status == "retryable_failure"
    assert recovered.attempts[-1].outcome == "indeterminate"


def test_loaders_reject_json_scalar_coercion(tmp_path: Path) -> None:
    """Strings cannot masquerade as manifest integers or checkpoint booleans."""
    manifest, checkpoints = _bundle()
    batch_dir = initialize_batch(tmp_path, manifest, checkpoints)
    manifest_path = batch_dir / "manifest.json"
    raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw_manifest["task_limit"] = "1"
    manifest_path.write_text(json.dumps(raw_manifest) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="task limit must be an integer"):
        load_manifest(batch_dir)

    other_root = tmp_path / "other"
    batch_dir = initialize_batch(other_root, manifest, checkpoints)
    completed = append_attempt(
        checkpoints[0],
        _outcome(),
        "2026-07-15T12:00:00Z",
        "2026-07-15T12:00:01Z",
        manifest.max_attempts,
    )
    replace_checkpoint(batch_dir, completed)
    path = next(batch_dir.rglob("*.jsonl"))
    record = json.loads(path.read_text(encoding="utf-8"))
    record["attempts"][0]["retryable"] = "false"
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="retryable must be a boolean"):
        load_checkpoints(batch_dir)


def test_validation_rejects_outcome_timestamp_geoid_and_manifest_contradictions() -> (
    None
):
    """Stored evidence cannot claim contradictory status, time, or membership."""
    manifest, checkpoints = _bundle(max_attempts=2)
    completed = append_attempt(
        checkpoints[0],
        _outcome(),
        "2026-07-15T12:00:00Z",
        "2026-07-15T12:00:01Z",
        manifest.max_attempts,
    )
    wrong_outcome = replace(
        completed,
        attempts=(replace(completed.attempts[0], outcome="zero_results"),),
        terminal_status="zero_results",
    )
    with pytest.raises(ValueError, match="zero-results"):
        validate_checkpoint(wrong_outcome)
    bad_time = replace(
        completed,
        attempts=(
            replace(completed.attempts[0], completed_at_utc="2026-07-15T11:59:59Z"),
        ),
    )
    with pytest.raises(ValueError, match="precedes"):
        validate_checkpoint(bad_time)
    with pytest.raises(ValueError, match="GEOID"):
        validate_checkpoint(replace(checkpoints[0], entity_namespace="school_district"))
    with pytest.raises(ValueError, match="namespaces contradict"):
        validate_batch(replace(manifest, namespaces=("school_district",)), checkpoints)
    with pytest.raises(ValueError, match="attempt budget/outcome"):
        validate_batch(
            manifest,
            [replace(completed, terminal_status="attempts_exhausted")],
        )


@pytest.mark.parametrize(
    ("field_name", "changed_value"),
    [
        ("state", "TX"),
        ("entity_name", "Changed County"),
        ("entity_kind", "changed_kind"),
        ("universe_vintage", "2026"),
    ],
)
def test_schema_v2_task_identity_binds_complete_target_snapshot(
    field_name: str,
    changed_value: str,
) -> None:
    """Mutation of any non-request Census target field invalidates batch identity."""
    manifest, checkpoints = _bundle()
    changed = replace(checkpoints[0], **{field_name: changed_value})
    with pytest.raises(ValueError):
        validate_batch(manifest, [changed])


def test_schema_v1_is_validation_only_and_cannot_initialize_new_batch(
    tmp_path: Path,
) -> None:
    """Legacy evidence remains readable but cannot create new paid-work state."""
    manifest, checkpoints = _bundle()
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
    legacy_checkpoint = replace(checkpoint, task_id=legacy_id)
    legacy_manifest = replace(
        manifest,
        schema_version=1,
        task_fingerprints=(legacy_id,),
    )
    validate_batch(legacy_manifest, [legacy_checkpoint])
    with pytest.raises(ValueError, match="current schema"):
        new_checkpoint(
            manifest=legacy_manifest,
            entity_namespace="county",
            geoid="06001",
            state="CA",
            entity_name="Legacy County",
            entity_kind="county",
            universe_vintage="2025",
            query="legacy query",
        )
    with pytest.raises(ValueError, match="read-only"):
        initialize_batch(tmp_path, legacy_manifest, [legacy_checkpoint])
    assert not (tmp_path / legacy_manifest.batch_id).exists()


def test_schema_v1_checkpoint_evidence_cannot_be_rewritten(tmp_path: Path) -> None:
    """The low-level atomic replacer also preserves legacy evidence as read-only."""
    repository_root = Path(__file__).resolve().parent.parent
    source = repository_root / "data/source_catalog/firecrawl_batches/20260716T004633Z"
    copied = tmp_path / source.name
    shutil.copytree(source, copied)
    checkpoint = load_checkpoints(copied)[0]
    with pytest.raises(ValueError, match="read-only"):
        replace_checkpoint(copied, replace(checkpoint, entity_name="Changed Entity"))
    assert load_checkpoints(copied)[0] == checkpoint
