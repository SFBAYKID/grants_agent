"""Typed state and deterministic identities for raw source-discovery evidence.

Why: Firecrawl transport results must become explicit, restartable evidence without
mixing persistence mechanics into state transitions. These immutable models and
pure helpers define manifests, checkpoints, paid-attempt markers, and honest crash
recovery independently from the JSONL store.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace

from .firecrawl_client import JsonValue, SearchOutcome


LEGACY_SCHEMA_VERSION = 1
SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS = frozenset({LEGACY_SCHEMA_VERSION, SCHEMA_VERSION})


@dataclass(frozen=True)
class BatchManifest:
    """Immutable selection and request contract for one paid discovery batch."""

    schema_version: int
    batch_id: str
    created_at_utc: str
    query_template_id: str
    selection_seed: str
    namespaces: tuple[str, ...]
    states: tuple[str, ...]
    task_limit: int
    result_limit: int
    requests_per_minute: int
    max_attempts: int
    task_fingerprints: tuple[str, ...]


@dataclass(frozen=True)
class AttemptEvidence:
    """One immutable, secret-free API attempt nested in an entity checkpoint."""

    attempt_number: int
    started_at_utc: str
    completed_at_utc: str
    outcome: str
    http_status: int
    retry_after_seconds: float
    response_sha256: str
    response_metadata: dict[str, JsonValue]
    results: tuple[dict[str, JsonValue], ...]
    error_code: str
    sanitized_error: str
    retryable: bool
    systemic: bool


@dataclass(frozen=True)
class DiscoveryCheckpoint:
    """Restartable raw research state for one namespaced geographic entity."""

    task_id: str
    entity_namespace: str
    geoid: str
    state: str
    entity_name: str
    entity_kind: str
    universe_vintage: str
    query: str
    request_sha256: str
    attempts: tuple[AttemptEvidence, ...]
    terminal_status: str


def canonical_json(value: object) -> str:
    """Serialize one evidence object deterministically on a single physical line."""
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _sha256_text(value: str) -> str:
    """Return a lowercase SHA-256 digest for deterministic task identities."""
    return hashlib.sha256(value.encode()).hexdigest()


def make_task_id(
    batch_id: str,
    entity_namespace: str,
    geoid: str,
    query_template_id: str,
    query: str,
    result_limit: int,
    *,
    schema_version: int = SCHEMA_VERSION,
    state: str = "",
    entity_name: str = "",
    entity_kind: str = "",
    universe_vintage: str = "",
) -> str:
    """Bind a task ID to its request and, in schema v2, full target snapshot."""
    parts = (
        batch_id,
        entity_namespace,
        geoid,
        query_template_id,
        query,
        str(result_limit),
    )
    if schema_version == LEGACY_SCHEMA_VERSION:
        return _sha256_text("|".join(parts))
    if schema_version != SCHEMA_VERSION or not all(
        (state, entity_name, entity_kind, universe_vintage)
    ):
        raise ValueError("schema-v2 task identity requires a complete target snapshot")
    return _sha256_text(
        "|".join(
            (
                *parts,
                state,
                entity_name,
                entity_kind,
                universe_vintage,
            )
        )
    )


def make_request_sha256(query: str, result_limit: int) -> str:
    """Fingerprint the secret-free Firecrawl request body."""
    return _sha256_text(canonical_json({"limit": result_limit, "query": query}))


def new_checkpoint(
    *,
    manifest: BatchManifest,
    entity_namespace: str,
    geoid: str,
    state: str,
    entity_name: str,
    entity_kind: str,
    universe_vintage: str,
    query: str,
) -> DiscoveryCheckpoint:
    """Create a pending checkpoint that exactly matches an immutable manifest."""
    if manifest.schema_version != SCHEMA_VERSION:
        raise ValueError("new discovery checkpoints require the current schema")
    task_id = make_task_id(
        manifest.batch_id,
        entity_namespace,
        geoid,
        manifest.query_template_id,
        query,
        manifest.result_limit,
        schema_version=manifest.schema_version,
        state=state,
        entity_name=entity_name,
        entity_kind=entity_kind,
        universe_vintage=universe_vintage,
    )
    return DiscoveryCheckpoint(
        task_id=task_id,
        entity_namespace=entity_namespace,
        geoid=geoid,
        state=state,
        entity_name=entity_name,
        entity_kind=entity_kind,
        universe_vintage=universe_vintage,
        query=query,
        request_sha256=make_request_sha256(query, manifest.result_limit),
        attempts=(),
        terminal_status="pending",
    )


def append_attempt(
    checkpoint: DiscoveryCheckpoint,
    outcome: SearchOutcome,
    started_at_utc: str,
    completed_at_utc: str,
    max_attempts: int,
) -> DiscoveryCheckpoint:
    """Append one outcome and derive a truthful restart/terminal status."""
    attempt_number = len(checkpoint.attempts) + 1
    if attempt_number > max_attempts:
        raise ValueError("checkpoint has exhausted its fixed attempt budget")
    results = tuple(
        {"rank": result.rank, "metadata": result.metadata} for result in outcome.results
    )
    attempt = AttemptEvidence(
        attempt_number=attempt_number,
        started_at_utc=started_at_utc,
        completed_at_utc=completed_at_utc,
        outcome=outcome.outcome,
        http_status=outcome.http_status,
        retry_after_seconds=outcome.retry_after_seconds,
        response_sha256=outcome.response_sha256,
        response_metadata=outcome.response_metadata,
        results=results,
        error_code=outcome.error_code,
        sanitized_error=outcome.sanitized_error,
        retryable=outcome.retryable,
        systemic=outcome.systemic,
    )
    if outcome.outcome in {"success", "zero_results"}:
        status = outcome.outcome
    elif not outcome.retryable:
        status = "non_retryable_failure"
    elif attempt_number >= max_attempts:
        status = "attempts_exhausted"
    else:
        status = "retryable_failure"
    return replace(
        checkpoint,
        attempts=(*checkpoint.attempts, attempt),
        terminal_status=status,
    )


def begin_attempt(
    checkpoint: DiscoveryCheckpoint,
    started_at_utc: str,
    max_attempts: int,
) -> DiscoveryCheckpoint:
    """Persist an in-flight marker before a possibly paid HTTP request begins."""
    attempt_number = len(checkpoint.attempts) + 1
    if attempt_number > max_attempts:
        raise ValueError("checkpoint has exhausted its fixed attempt budget")
    attempt = AttemptEvidence(
        attempt_number=attempt_number,
        started_at_utc=started_at_utc,
        completed_at_utc="",
        outcome="in_flight",
        http_status=0,
        retry_after_seconds=0.0,
        response_sha256="",
        response_metadata={},
        results=(),
        error_code="",
        sanitized_error="",
        retryable=False,
        systemic=False,
    )
    return replace(
        checkpoint,
        attempts=(*checkpoint.attempts, attempt),
        terminal_status="in_flight",
    )


def complete_attempt(
    checkpoint: DiscoveryCheckpoint,
    outcome: SearchOutcome,
    completed_at_utc: str,
    max_attempts: int,
) -> DiscoveryCheckpoint:
    """Replace the final in-flight marker with the returned transport outcome."""
    if not checkpoint.attempts or checkpoint.terminal_status != "in_flight":
        raise ValueError("checkpoint has no in-flight attempt to complete")
    pending = checkpoint.attempts[-1]
    base = replace(
        checkpoint, attempts=checkpoint.attempts[:-1], terminal_status="pending"
    )
    return append_attempt(
        base,
        outcome,
        pending.started_at_utc,
        completed_at_utc,
        max_attempts,
    )


def recover_in_flight(
    checkpoint: DiscoveryCheckpoint,
    recovered_at_utc: str,
    max_attempts: int,
) -> DiscoveryCheckpoint:
    """Explicitly record an interrupted call as indeterminate before any retry."""
    if not checkpoint.attempts or checkpoint.terminal_status != "in_flight":
        raise ValueError("checkpoint has no in-flight attempt to recover")
    pending = checkpoint.attempts[-1]
    recovered = replace(
        pending,
        completed_at_utc=recovered_at_utc,
        outcome="indeterminate",
        error_code="process_interrupted_after_attempt_start",
        sanitized_error="process_interrupted_after_attempt_start",
        retryable=True,
    )
    status = (
        "attempts_exhausted"
        if len(checkpoint.attempts) >= max_attempts
        else "retryable_failure"
    )
    return replace(
        checkpoint,
        attempts=(*checkpoint.attempts[:-1], recovered),
        terminal_status=status,
    )
