"""Atomic, restartable raw-evidence storage for Firecrawl discovery batches.

Why: raw search results are neither catalog sources nor reviewed discovery checks.
This module stores an immutable batch manifest and one JSONL checkpoint per entity,
with exclusive locking and same-filesystem replacement after every attempt. It
never writes the canonical catalog, source links, or coverage-task shards.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import TextIO

from .firecrawl_client import (
    JsonValue,
    canonical_json_hash,
    redact_json,
)
from .source_discovery_models import (
    SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    AttemptEvidence,
    BatchManifest,
    DiscoveryCheckpoint,
    canonical_json,
    make_request_sha256,
    make_task_id,
)


LINE_CAP = 1_000
ALLOWED_NAMESPACES = frozenset({"county", "school_district", "incorporated_place"})
TERMINAL_STATUSES = frozenset(
    {"success", "zero_results", "non_retryable_failure", "attempts_exhausted"}
)
ALL_STATUSES = TERMINAL_STATUSES | {"pending", "retryable_failure", "in_flight"}
TRANSPORT_OUTCOMES = frozenset(
    {
        "success",
        "zero_results",
        "timeout",
        "http_error",
        "rate_limited",
        "oversized_response",
        "malformed_response",
        "in_flight",
        "indeterminate",
    }
)
UTC_TIMESTAMP_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


class BatchLockedError(RuntimeError):
    """Raised when another process already owns a discovery batch lock."""


class BatchLock:
    """Non-blocking advisory lock that prevents duplicate paid batch calls."""

    def __init__(self, root: Path, batch_id: str) -> None:
        """Prepare a lock path without acquiring it during object construction."""
        self._root = root
        self._batch_id = batch_id
        self._handle: TextIO | None = None

    def __enter__(self) -> BatchLock:
        """Acquire the batch lock or fail before the caller reaches Firecrawl."""
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._root / f".{self._batch_id}.lock"
        handle = path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise BatchLockedError(
                f"batch is already running: {self._batch_id}"
            ) from exc
        self._handle = handle
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Release the advisory lock even when orchestration raises."""
        del exc_type, exc_value, traceback
        handle = self._handle
        if handle is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        self._handle = None


class ExecutionLock(BatchLock):
    """Root-wide lock preventing overlapping Firecrawl batches and rate windows."""

    def __init__(self, root: Path) -> None:
        """Use one stable lock inode shared by every batch ID under this root."""
        super().__init__(root, "execution")


def _canonical_json(value: object) -> str:
    """Serialize one evidence object deterministically on a single physical line."""
    return canonical_json(value)


def _validated_json_object(value: object, label: str) -> dict[str, JsonValue]:
    """Validate stored JSON recursively and reject any value needing redaction."""
    normalized = redact_json(value)
    if not isinstance(normalized, dict) or normalized != value:
        raise ValueError(f"{label} is malformed or contains unredacted data")
    return normalized


def _required_string(value: object, label: str, *, allow_empty: bool = False) -> str:
    """Return an exact JSON string without coercing another scalar type."""
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ValueError(f"{label} must be a string")
    return value


def _required_int(value: object, label: str) -> int:
    """Return an exact JSON integer while rejecting booleans and strings."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value


def _required_float(value: object, label: str) -> float:
    """Return an exact finite JSON number while rejecting booleans and strings."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number")
    result = float(value)
    if result < 0 or result == float("inf") or result != result:
        raise ValueError(f"{label} must be a finite non-negative number")
    return result


def _required_bool(value: object, label: str) -> bool:
    """Return an exact JSON boolean without Python truthiness coercion."""
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a boolean")
    return value


def _required_string_list(value: object, label: str) -> tuple[str, ...]:
    """Return a tuple only when the JSON value is an array of non-empty strings."""
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    return tuple(_required_string(item, label) for item in value)


def _parse_timestamp(value: str, label: str) -> datetime:
    """Parse one exact second-precision UTC timestamp and reject invalid dates."""
    if not UTC_TIMESTAMP_PATTERN.fullmatch(value):
        raise ValueError(f"{label} must be a second-precision UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} is not a real UTC timestamp") from exc
    if parsed.tzinfo != UTC:
        raise ValueError(f"{label} is not UTC")
    return parsed


def _attempt_from_dict(value: object) -> AttemptEvidence:
    """Parse one nested attempt from untrusted persisted JSON."""
    if not isinstance(value, dict):
        raise ValueError("checkpoint attempt must be an object")
    required = {
        "attempt_number",
        "started_at_utc",
        "completed_at_utc",
        "outcome",
        "http_status",
        "retry_after_seconds",
        "response_sha256",
        "response_metadata",
        "results",
        "error_code",
        "sanitized_error",
        "retryable",
        "systemic",
    }
    if set(value) != required:
        raise ValueError("checkpoint attempt columns mismatch")
    raw_results = value["results"]
    if not isinstance(raw_results, list):
        raise ValueError("checkpoint results must be a list")
    results = tuple(
        _validated_json_object(result, "checkpoint result") for result in raw_results
    )
    return AttemptEvidence(
        attempt_number=_required_int(value["attempt_number"], "attempt number"),
        started_at_utc=_required_string(value["started_at_utc"], "attempt start"),
        completed_at_utc=_required_string(
            value["completed_at_utc"], "attempt completion", allow_empty=True
        ),
        outcome=_required_string(value["outcome"], "attempt outcome"),
        http_status=_required_int(value["http_status"], "HTTP status"),
        retry_after_seconds=_required_float(
            value["retry_after_seconds"], "retry-after seconds"
        ),
        response_sha256=_required_string(
            value["response_sha256"], "response digest", allow_empty=True
        ),
        response_metadata=_validated_json_object(
            value["response_metadata"], "response metadata"
        ),
        results=results,
        error_code=_required_string(
            value["error_code"], "error code", allow_empty=True
        ),
        sanitized_error=_required_string(
            value["sanitized_error"], "sanitized error", allow_empty=True
        ),
        retryable=_required_bool(value["retryable"], "retryable"),
        systemic=_required_bool(value["systemic"], "systemic"),
    )


def _checkpoint_to_dict(checkpoint: DiscoveryCheckpoint) -> dict[str, object]:
    """Convert one typed checkpoint to its stable JSON record."""
    return {
        "task_id": checkpoint.task_id,
        "entity_namespace": checkpoint.entity_namespace,
        "geoid": checkpoint.geoid,
        "state": checkpoint.state,
        "entity_name": checkpoint.entity_name,
        "entity_kind": checkpoint.entity_kind,
        "universe_vintage": checkpoint.universe_vintage,
        "query": checkpoint.query,
        "request_sha256": checkpoint.request_sha256,
        "attempts": [asdict(attempt) for attempt in checkpoint.attempts],
        "terminal_status": checkpoint.terminal_status,
    }


def _checkpoint_from_dict(value: object) -> DiscoveryCheckpoint:
    """Parse and validate one checkpoint JSON record."""
    if not isinstance(value, dict):
        raise ValueError("checkpoint row must be an object")
    required = {
        "task_id",
        "entity_namespace",
        "geoid",
        "state",
        "entity_name",
        "entity_kind",
        "universe_vintage",
        "query",
        "request_sha256",
        "attempts",
        "terminal_status",
    }
    if set(value) != required or not isinstance(value["attempts"], list):
        raise ValueError("checkpoint row columns mismatch")
    checkpoint = DiscoveryCheckpoint(
        task_id=_required_string(value["task_id"], "task ID"),
        entity_namespace=_required_string(value["entity_namespace"], "namespace"),
        geoid=_required_string(value["geoid"], "GEOID"),
        state=_required_string(value["state"], "state"),
        entity_name=_required_string(value["entity_name"], "entity name"),
        entity_kind=_required_string(value["entity_kind"], "entity kind"),
        universe_vintage=_required_string(
            value["universe_vintage"], "universe vintage"
        ),
        query=_required_string(value["query"], "query"),
        request_sha256=_required_string(value["request_sha256"], "request digest"),
        attempts=tuple(_attempt_from_dict(item) for item in value["attempts"]),
        terminal_status=_required_string(value["terminal_status"], "terminal status"),
    )
    validate_checkpoint(checkpoint)
    return checkpoint


def validate_manifest(manifest: BatchManifest) -> None:
    """Reject manifests that could cause an unbounded or ambiguous paid run."""
    if manifest.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise ValueError("unsupported Firecrawl batch schema")
    if not re.fullmatch(r"\d{8}T\d{6}Z", manifest.batch_id):
        raise ValueError("invalid Firecrawl batch ID")
    try:
        _parse_timestamp(manifest.created_at_utc, "manifest creation")
    except ValueError as exc:
        raise ValueError("batch manifest timestamp/template is incomplete") from exc
    if not manifest.query_template_id or not manifest.selection_seed:
        raise ValueError("batch manifest timestamp/template is incomplete")
    if not manifest.namespaces or set(manifest.namespaces) - ALLOWED_NAMESPACES:
        raise ValueError("batch manifest namespaces are invalid")
    if len(set(manifest.namespaces)) != len(manifest.namespaces):
        raise ValueError("batch manifest namespaces contain duplicates")
    if not manifest.states or any(
        not re.fullmatch(r"[A-Z]{2}", state) for state in manifest.states
    ):
        raise ValueError("batch manifest states are invalid")
    if tuple(sorted(set(manifest.states))) != manifest.states:
        raise ValueError("batch manifest states must be unique and sorted")
    if not 1 <= manifest.task_limit <= 100 or not 1 <= manifest.result_limit <= 5:
        raise ValueError("batch manifest task/result limits are invalid")
    if (
        not 1 <= manifest.requests_per_minute <= 60
        or not 1 <= manifest.max_attempts <= 3
    ):
        raise ValueError("batch manifest request/attempt bounds are invalid")
    if len(manifest.task_fingerprints) != manifest.task_limit:
        raise ValueError("batch manifest task fingerprints do not match task limit")
    if tuple(sorted(set(manifest.task_fingerprints))) != manifest.task_fingerprints:
        raise ValueError("batch manifest task fingerprints are not unique and sorted")
    if any(
        not re.fullmatch(r"[0-9a-f]{64}", digest)
        for digest in manifest.task_fingerprints
    ):
        raise ValueError("batch manifest task fingerprint is invalid")


def validate_checkpoint(checkpoint: DiscoveryCheckpoint) -> None:
    """Validate identifiers, evidence hashes, sequence, and terminal-state truth."""
    if checkpoint.entity_namespace not in ALLOWED_NAMESPACES:
        raise ValueError("checkpoint namespace is invalid")
    expected_geoid_length = 5 if checkpoint.entity_namespace == "county" else 7
    if not checkpoint.geoid.isdigit() or len(checkpoint.geoid) != expected_geoid_length:
        raise ValueError("checkpoint GEOID is invalid")
    if not re.fullmatch(r"[A-Z]{2}", checkpoint.state):
        raise ValueError("checkpoint state is invalid")
    if not checkpoint.query or checkpoint.terminal_status not in ALL_STATUSES:
        raise ValueError("checkpoint query/status is invalid")
    for digest in (checkpoint.task_id, checkpoint.request_sha256):
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("checkpoint digest is invalid")
    numbers = [attempt.attempt_number for attempt in checkpoint.attempts]
    if numbers != list(range(1, len(numbers) + 1)):
        raise ValueError("checkpoint attempt numbers are not contiguous")
    if checkpoint.terminal_status == "pending" and checkpoint.attempts:
        raise ValueError("pending checkpoint cannot contain attempts")
    if checkpoint.terminal_status in TERMINAL_STATUSES and not checkpoint.attempts:
        raise ValueError("terminal checkpoint requires attempt evidence")
    previous_completion: datetime | None = None
    for index, attempt in enumerate(checkpoint.attempts):
        if attempt.outcome not in TRANSPORT_OUTCOMES:
            raise ValueError("checkpoint attempt outcome is invalid")
        started = _parse_timestamp(attempt.started_at_utc, "attempt start")
        if previous_completion is not None and started < previous_completion:
            raise ValueError("attempt sequence overlaps earlier evidence")
        if attempt.outcome == "in_flight":
            if attempt.completed_at_utc:
                raise ValueError("in-flight attempt cannot have a completion time")
            if index != len(checkpoint.attempts) - 1:
                raise ValueError("only the final attempt may remain in flight")
        else:
            completed = _parse_timestamp(attempt.completed_at_utc, "attempt completion")
            if completed < started:
                raise ValueError("attempt completion precedes its start")
            previous_completion = completed
        if not 0 <= attempt.http_status <= 599:
            raise ValueError("attempt HTTP status is invalid")
        if attempt.outcome in {"success", "zero_results"}:
            if not re.fullmatch(r"[0-9a-f]{64}", attempt.response_sha256):
                raise ValueError("successful attempt requires a response hash")
            ranks: list[int] = []
            raw_results: list[JsonValue] = []
            for result in attempt.results:
                if set(result) != {"rank", "metadata"}:
                    raise ValueError("stored result fields are invalid")
                rank = result["rank"]
                metadata = result["metadata"]
                if (
                    isinstance(rank, bool)
                    or not isinstance(rank, int)
                    or not isinstance(metadata, dict)
                ):
                    raise ValueError("stored result rank/metadata is invalid")
                ranks.append(rank)
                raw_results.append(metadata)
            if ranks != list(range(1, len(ranks) + 1)):
                raise ValueError("stored result ranks are not contiguous")
            if attempt.outcome == "success" and not ranks:
                raise ValueError("successful attempt requires at least one result")
            if attempt.outcome == "zero_results" and ranks:
                raise ValueError("zero-results attempt cannot contain results")
            payload: dict[str, JsonValue] = dict(attempt.response_metadata)
            payload["data"] = raw_results
            if canonical_json_hash(payload) != attempt.response_sha256:
                raise ValueError("stored result metadata does not match response hash")
            if attempt.retryable or attempt.systemic:
                raise ValueError("successful attempt has invalid failure flags")
            if not 200 <= attempt.http_status < 300:
                raise ValueError("successful attempt requires a 2xx response")
            if attempt.error_code or attempt.sanitized_error:
                raise ValueError("successful attempt cannot contain an error")
        else:
            if attempt.response_sha256 or attempt.response_metadata or attempt.results:
                raise ValueError("failed attempt cannot claim response evidence")
            if attempt.outcome == "in_flight":
                if (
                    attempt.http_status
                    or attempt.retry_after_seconds
                    or attempt.error_code
                    or attempt.sanitized_error
                    or attempt.retryable
                    or attempt.systemic
                ):
                    raise ValueError(
                        "in-flight attempt contains premature outcome data"
                    )
            elif attempt.outcome == "indeterminate":
                if (
                    attempt.http_status
                    or not attempt.retryable
                    or attempt.systemic
                    or attempt.error_code != "process_interrupted_after_attempt_start"
                    or attempt.sanitized_error != attempt.error_code
                ):
                    raise ValueError(
                        "indeterminate attempt recovery evidence is invalid"
                    )
            elif (
                not attempt.error_code or attempt.sanitized_error != attempt.error_code
            ):
                raise ValueError("failed attempt requires one sanitized error code")
            elif attempt.outcome == "timeout" and (
                attempt.http_status or not attempt.retryable or attempt.systemic
            ):
                raise ValueError("timeout outcome flags are invalid")
            elif attempt.outcome == "rate_limited" and (
                attempt.http_status != 429 or not attempt.retryable or attempt.systemic
            ):
                raise ValueError("rate-limit outcome flags are invalid")
            elif attempt.outcome in {"oversized_response", "malformed_response"} and (
                not 200 <= attempt.http_status < 300
                or attempt.retryable
                or attempt.systemic
            ):
                raise ValueError("successful-HTTP failure flags are invalid")
            elif attempt.outcome == "http_error":
                if attempt.systemic != (attempt.http_status in {401, 402, 403}):
                    raise ValueError("HTTP failure systemic flag is invalid")
                expected_retryable = (
                    attempt.http_status == 0 or attempt.http_status >= 500
                )
                if attempt.retryable != expected_retryable:
                    raise ValueError("HTTP failure retry flag is invalid")
    if checkpoint.attempts:
        last = checkpoint.attempts[-1]
        expected_status = (
            last.outcome
            if last.outcome in {"success", "zero_results"}
            else "non_retryable_failure"
            if not last.retryable
            else checkpoint.terminal_status
        )
        if (
            checkpoint.terminal_status
            in {
                "success",
                "zero_results",
                "non_retryable_failure",
            }
            and checkpoint.terminal_status != expected_status
        ):
            raise ValueError("checkpoint terminal status contradicts final attempt")
        if checkpoint.terminal_status == "retryable_failure" and not last.retryable:
            raise ValueError("retryable checkpoint lacks retryable final evidence")
        if checkpoint.terminal_status == "in_flight" and last.outcome != "in_flight":
            raise ValueError("in-flight checkpoint lacks an in-flight final attempt")


def _manifest_to_dict(manifest: BatchManifest) -> dict[str, object]:
    """Convert tuples to JSON arrays while preserving the immutable field set."""
    value = asdict(manifest)
    value["namespaces"] = list(manifest.namespaces)
    value["states"] = list(manifest.states)
    value["task_fingerprints"] = list(manifest.task_fingerprints)
    return value


def _manifest_from_dict(value: object) -> BatchManifest:
    """Parse an immutable manifest and reject unknown fields."""
    if not isinstance(value, dict):
        raise ValueError("batch manifest must be an object")
    required = set(BatchManifest.__dataclass_fields__)
    if set(value) != required:
        raise ValueError("batch manifest fields mismatch")
    manifest = BatchManifest(
        schema_version=_required_int(value["schema_version"], "schema version"),
        batch_id=_required_string(value["batch_id"], "batch ID"),
        created_at_utc=_required_string(value["created_at_utc"], "creation time"),
        query_template_id=_required_string(
            value["query_template_id"], "query template"
        ),
        selection_seed=_required_string(value["selection_seed"], "selection seed"),
        namespaces=_required_string_list(value["namespaces"], "manifest namespaces"),
        states=_required_string_list(value["states"], "manifest states"),
        task_limit=_required_int(value["task_limit"], "task limit"),
        result_limit=_required_int(value["result_limit"], "result limit"),
        requests_per_minute=_required_int(
            value["requests_per_minute"], "requests per minute"
        ),
        max_attempts=_required_int(value["max_attempts"], "max attempts"),
        task_fingerprints=_required_string_list(
            value["task_fingerprints"], "task fingerprints"
        ),
    )
    validate_manifest(manifest)
    return manifest


def _shard_relative(checkpoint: DiscoveryCheckpoint) -> Path:
    """Return the namespace/state/local-GEOID-prefix checkpoint shard."""
    return (
        Path("results")
        / checkpoint.entity_namespace
        / checkpoint.state
        / f"{checkpoint.geoid[2]}.jsonl"
    )


def _write_jsonl(path: Path, checkpoints: list[DiscoveryCheckpoint]) -> None:
    """Atomically rewrite one small LF-only checkpoint shard and fsync it."""
    if len(checkpoints) + 1 > LINE_CAP:
        raise ValueError("Firecrawl checkpoint shard exceeds the line cap")
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        _canonical_json(_checkpoint_to_dict(checkpoint)) + "\n"
        for checkpoint in sorted(checkpoints, key=lambda item: item.task_id)
    ]
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="", dir=path.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.writelines(rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _fsync_directory(path: Path) -> None:
    """Durably record a directory-entry replacement on the containing filesystem."""
    directory = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _write_manifest(path: Path, manifest: BatchManifest) -> None:
    """Create an immutable manifest and fsync it before any paid calls."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="") as handle:
        handle.write(_canonical_json(_manifest_to_dict(manifest)) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def load_manifest(batch_dir: Path) -> BatchManifest:
    """Load the single-line immutable manifest for one batch."""
    path = batch_dir / "manifest.json"
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) != 1:
        raise ValueError("batch manifest must contain exactly one line")
    return _manifest_from_dict(json.loads(lines[0]))


def load_checkpoints(batch_dir: Path) -> list[DiscoveryCheckpoint]:
    """Load all checkpoint shards and reject duplicate or foreign task IDs."""
    checkpoints: list[DiscoveryCheckpoint] = []
    for path in sorted((batch_dir / "results").glob("*/*/*.jsonl")):
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) > LINE_CAP:
            raise ValueError(f"checkpoint shard exceeds line cap: {path}")
        for line in lines:
            checkpoint = _checkpoint_from_dict(json.loads(line))
            if _shard_relative(checkpoint) != path.relative_to(batch_dir):
                raise ValueError("checkpoint is stored in the wrong shard")
            checkpoints.append(checkpoint)
    ids = [checkpoint.task_id for checkpoint in checkpoints]
    if len(ids) != len(set(ids)):
        raise ValueError("batch contains duplicate task checkpoints")
    return sorted(checkpoints, key=lambda item: item.task_id)


def validate_batch(
    manifest: BatchManifest, checkpoints: list[DiscoveryCheckpoint]
) -> None:
    """Require exact manifest/checkpoint membership and request fingerprints."""
    validate_manifest(manifest)
    fingerprints = tuple(sorted(checkpoint.task_id for checkpoint in checkpoints))
    if fingerprints != manifest.task_fingerprints:
        raise ValueError("batch checkpoints do not match immutable manifest")
    if {checkpoint.entity_namespace for checkpoint in checkpoints} != set(
        manifest.namespaces
    ):
        raise ValueError("batch checkpoint namespaces contradict manifest")
    if {checkpoint.state for checkpoint in checkpoints} != set(manifest.states):
        raise ValueError("batch checkpoint states contradict manifest")
    for checkpoint in checkpoints:
        validate_checkpoint(checkpoint)
        expected_request = make_request_sha256(checkpoint.query, manifest.result_limit)
        if checkpoint.request_sha256 != expected_request:
            raise ValueError("checkpoint request hash does not match manifest")
        expected_task_id = make_task_id(
            manifest.batch_id,
            checkpoint.entity_namespace,
            checkpoint.geoid,
            manifest.query_template_id,
            checkpoint.query,
            manifest.result_limit,
            schema_version=manifest.schema_version,
            state=checkpoint.state,
            entity_name=checkpoint.entity_name,
            entity_kind=checkpoint.entity_kind,
            universe_vintage=checkpoint.universe_vintage,
        )
        if checkpoint.task_id != expected_task_id:
            raise ValueError("checkpoint task ID does not match manifest")
        if len(checkpoint.attempts) > manifest.max_attempts:
            raise ValueError("checkpoint exceeds manifest attempt budget")
        if any(
            len(attempt.results) > manifest.result_limit
            for attempt in checkpoint.attempts
        ):
            raise ValueError("checkpoint results exceed manifest result limit")
        if not checkpoint.attempts:
            expected_status = "pending"
        else:
            final = checkpoint.attempts[-1]
            if final.outcome in {"success", "zero_results", "in_flight"}:
                expected_status = final.outcome
            elif not final.retryable:
                expected_status = "non_retryable_failure"
            elif len(checkpoint.attempts) >= manifest.max_attempts:
                expected_status = "attempts_exhausted"
            else:
                expected_status = "retryable_failure"
        if checkpoint.terminal_status != expected_status:
            raise ValueError("checkpoint status contradicts attempt budget/outcome")


def initialize_batch(
    root: Path,
    manifest: BatchManifest,
    checkpoints: list[DiscoveryCheckpoint],
) -> Path:
    """Create a complete batch snapshot or validate an identical existing one."""
    validate_batch(manifest, checkpoints)
    batch_dir = root / manifest.batch_id
    if batch_dir.exists():
        stored_manifest = load_manifest(batch_dir)
        stored_checkpoints = load_checkpoints(batch_dir)
        if stored_manifest != manifest:
            raise ValueError("batch ID already exists with a different manifest")
        validate_batch(stored_manifest, stored_checkpoints)
        return batch_dir
    if manifest.schema_version != SCHEMA_VERSION:
        raise ValueError("legacy Firecrawl schemas are read-only validation evidence")
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{manifest.batch_id}-", dir=root) as name:
        staged = Path(name) / manifest.batch_id
        _write_manifest(staged / "manifest.json", manifest)
        grouped: dict[Path, list[DiscoveryCheckpoint]] = {}
        for checkpoint in checkpoints:
            grouped.setdefault(_shard_relative(checkpoint), []).append(checkpoint)
        for relative, shard in grouped.items():
            _write_jsonl(staged / relative, shard)
        os.replace(staged, batch_dir)
        _fsync_directory(root)
    return batch_dir


def replace_checkpoint(batch_dir: Path, checkpoint: DiscoveryCheckpoint) -> None:
    """Validate a schema-v2 batch mutation, then atomically replace one entity row."""
    manifest = load_manifest(batch_dir)
    if manifest.schema_version != SCHEMA_VERSION:
        raise ValueError("legacy Firecrawl batch evidence is read-only")
    current = load_checkpoints(batch_dir)
    if checkpoint.task_id not in {item.task_id for item in current}:
        raise ValueError("replacement checkpoint is not a manifest task")
    proposed = [
        checkpoint if item.task_id == checkpoint.task_id else item for item in current
    ]
    validate_batch(manifest, proposed)
    relative = _shard_relative(checkpoint)
    path = batch_dir / relative
    existing: list[DiscoveryCheckpoint] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            parsed = _checkpoint_from_dict(json.loads(line))
            if parsed.task_id != checkpoint.task_id:
                existing.append(parsed)
    existing.append(checkpoint)
    _write_jsonl(path, existing)
