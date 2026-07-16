"""Bounded Firecrawl discovery over Census-backed local-government queues.

Why: tens of thousands of county, school-district, and incorporated-place tasks
remain unresearched. This worker selects a deterministic slice, searches for RFP,
award, and grant-source pages, and stores raw secret-free evidence for later human
review. It never promotes catalog rows, source links, or coverage statuses.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from dotenv import load_dotenv

from .firecrawl_client import FirecrawlClient, SearchOutcome
from .source_discovery_store import (
    ALLOWED_NAMESPACES,
    BatchLock,
    ExecutionLock,
    initialize_batch,
    load_checkpoints,
    load_manifest,
    replace_checkpoint,
    validate_batch,
)
from .source_discovery_models import (
    SCHEMA_VERSION,
    BatchManifest,
    DiscoveryCheckpoint,
    begin_attempt,
    complete_attempt,
    make_request_sha256,
    make_task_id,
    new_checkpoint,
    recover_in_flight,
)


ROOT = Path(__file__).resolve().parent.parent
BATCH_ROOT = ROOT / "data" / "source_catalog" / "firecrawl_batches"
QUERY_TEMPLATE_ID = "rfp-grants-v1"
DEFAULT_SELECTION_SEED = "rfp-grants-v1"
NAMESPACE_ORDER = ("county", "school_district", "incorporated_place")
CompletedRequest = tuple[str, str, str, str]


@dataclass(frozen=True)
class ResearchTarget:
    """One untouched researchable entity adapted from a canonical task queue."""

    entity_namespace: str
    geoid: str
    state: str
    entity_name: str
    entity_kind: str
    universe_vintage: str

    @property
    def key(self) -> tuple[str, str]:
        """Return the namespaced identity used to skip prior terminal batches."""
        return (self.entity_namespace, self.geoid)

    def request_key(self, result_limit: int) -> CompletedRequest:
        """Return the vintage-aware fingerprint used to suppress exact repeats."""
        return (
            self.entity_namespace,
            self.geoid,
            self.universe_vintage,
            make_request_sha256(build_query(self), result_limit),
        )


@dataclass(frozen=True)
class BatchSummary:
    """Truthful derived counts for one stored discovery batch."""

    batch_id: str
    task_count: int
    attempt_count: int
    result_count: int
    statuses: Counter[str]


class SearchClient(Protocol):
    """Minimal injected Firecrawl interface used by orchestration and tests."""

    def search_once(self, query: str, result_limit: int) -> SearchOutcome:
        """Execute exactly one bounded search call."""
        ...


def utc_now() -> datetime:
    """Return a timezone-aware UTC clock value for evidence timestamps."""
    return datetime.now(UTC)


def _timestamp(value: datetime) -> str:
    """Format UTC evidence timestamps at stable second precision."""
    if value.tzinfo is None:
        raise ValueError("discovery timestamps must be timezone-aware")
    return (
        value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )


def _batch_id(value: datetime) -> str:
    """Format the default immutable batch identifier from a UTC clock."""
    return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def load_research_targets() -> list[ResearchTarget]:
    """Adapt only honest `not_researched` rows from all three task universes."""
    from .coverage_universe import load_county_tasks
    from .entity_coverage import load_entity_tasks
    from .incorporated_place_universe import TASK_ROOT as PLACE_TASK_ROOT
    from .school_district_universe import TASK_ROOT as SCHOOL_TASK_ROOT

    targets = [
        ResearchTarget(
            entity_namespace="county",
            geoid=task.entity_id,
            state=task.state,
            entity_name=task.entity_name,
            entity_kind=task.entity_kind,
            universe_vintage=task.universe_vintage,
        )
        for task in load_county_tasks()
        if task.research_status == "not_researched"
    ]
    for task in load_entity_tasks(SCHOOL_TASK_ROOT):
        if task.research_status == "not_researched":
            targets.append(
                ResearchTarget(
                    entity_namespace=task.entity_namespace,
                    geoid=task.geoid,
                    state=task.state,
                    entity_name=task.entity_name,
                    entity_kind=task.entity_kind,
                    universe_vintage=task.universe_vintage,
                )
            )
    for task in load_entity_tasks(PLACE_TASK_ROOT):
        if (
            task.research_status == "not_researched"
            and task.entity_disposition == "researchable"
        ):
            targets.append(
                ResearchTarget(
                    entity_namespace=task.entity_namespace,
                    geoid=task.geoid,
                    state=task.state,
                    entity_name=task.entity_name,
                    entity_kind=task.entity_kind,
                    universe_vintage=task.universe_vintage,
                )
            )
    keys = [target.key for target in targets]
    if len(keys) != len(set(keys)):
        raise ValueError("research target adapters produced duplicate identities")
    return sorted(
        targets,
        key=lambda item: (
            item.state,
            NAMESPACE_ORDER.index(item.entity_namespace),
            item.geoid,
        ),
    )


def build_query(target: ResearchTarget) -> str:
    """Build the fixed combined RFP/contract/grant discovery query."""
    subject = target.entity_name
    qualifier = {
        "county": "county",
        "school_district": "school district",
        "incorporated_place": "municipal city town",
    }[target.entity_namespace]
    return (
        f'"{subject}" "{target.state}" {qualifier} procurement bids RFP '
        "contract awards grant awards funding official"
    )


def _selection_rank(target: ResearchTarget, seed: str) -> tuple[str, str]:
    """Return a deterministic hash rank that avoids alphabetical selection bias."""
    digest = hashlib.sha256(
        f"{seed}|{target.entity_namespace}|{target.geoid}".encode()
    ).hexdigest()
    return (digest, target.geoid)


def select_targets(
    targets: list[ResearchTarget],
    *,
    states: frozenset[str],
    namespaces: frozenset[str],
    per_namespace_state: int,
    task_limit: int,
    selection_seed: str,
    result_limit: int = 5,
    completed_requests: frozenset[CompletedRequest] = frozenset(),
) -> list[ResearchTarget]:
    """Select a bounded, deterministic cross-state and cross-namespace slice."""
    if not states or not namespaces or namespaces - ALLOWED_NAMESPACES:
        raise ValueError("discovery states/namespaces are invalid")
    if per_namespace_state < 1 or not 1 <= task_limit <= 100:
        raise ValueError("discovery selection bounds are invalid")
    grouped: dict[tuple[str, str], list[ResearchTarget]] = defaultdict(list)
    for target in targets:
        if (
            target.state in states
            and target.entity_namespace in namespaces
            and target.request_key(result_limit) not in completed_requests
        ):
            grouped[(target.state, target.entity_namespace)].append(target)
    selected: list[ResearchTarget] = []
    for state in sorted(states):
        for namespace in NAMESPACE_ORDER:
            if namespace not in namespaces:
                continue
            ranked = sorted(
                grouped[(state, namespace)],
                key=lambda target: _selection_rank(target, selection_seed),
            )
            selected.extend(ranked[:per_namespace_state])
    return selected[:task_limit]


def completed_request_keys(
    root: Path = BATCH_ROOT, query_template_id: str = QUERY_TEMPLATE_ID
) -> frozenset[CompletedRequest]:
    """Return exact successful requests including vintage and request fingerprint."""
    completed: set[CompletedRequest] = set()
    if not root.exists():
        return frozenset()
    for batch_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        try:
            manifest = load_manifest(batch_dir)
            checkpoints = load_checkpoints(batch_dir)
            validate_batch(manifest, checkpoints)
        except (OSError, ValueError):
            # A malformed batch is not evidence and must be surfaced by the explicit
            # validator; it cannot silently exclude a target from future research.
            continue
        if manifest.query_template_id != query_template_id:
            continue
        completed.update(
            (
                checkpoint.entity_namespace,
                checkpoint.geoid,
                checkpoint.universe_vintage,
                checkpoint.request_sha256,
            )
            for checkpoint in checkpoints
            if checkpoint.terminal_status in {"success", "zero_results"}
        )
    return frozenset(completed)


def plan_batch(
    selected: list[ResearchTarget],
    *,
    batch_id: str,
    created_at_utc: str,
    result_limit: int,
    requests_per_minute: int,
    max_attempts: int,
    selection_seed: str,
) -> tuple[BatchManifest, list[DiscoveryCheckpoint]]:
    """Build an immutable manifest and its exact pending checkpoint membership."""
    if not selected:
        raise ValueError("discovery batch selection is empty")
    queries = [(target, build_query(target)) for target in selected]
    fingerprints = tuple(
        sorted(
            make_task_id(
                batch_id,
                target.entity_namespace,
                target.geoid,
                QUERY_TEMPLATE_ID,
                query,
                result_limit,
                schema_version=SCHEMA_VERSION,
                state=target.state,
                entity_name=target.entity_name,
                entity_kind=target.entity_kind,
                universe_vintage=target.universe_vintage,
            )
            for target, query in queries
        )
    )
    manifest = BatchManifest(
        schema_version=SCHEMA_VERSION,
        batch_id=batch_id,
        created_at_utc=created_at_utc,
        query_template_id=QUERY_TEMPLATE_ID,
        selection_seed=selection_seed,
        namespaces=tuple(
            namespace
            for namespace in NAMESPACE_ORDER
            if any(target.entity_namespace == namespace for target in selected)
        ),
        states=tuple(sorted({target.state for target in selected})),
        task_limit=len(selected),
        result_limit=result_limit,
        requests_per_minute=requests_per_minute,
        max_attempts=max_attempts,
        task_fingerprints=fingerprints,
    )
    checkpoints = [
        new_checkpoint(
            manifest=manifest,
            entity_namespace=target.entity_namespace,
            geoid=target.geoid,
            state=target.state,
            entity_name=target.entity_name,
            entity_kind=target.entity_kind,
            universe_vintage=target.universe_vintage,
            query=query,
        )
        for target, query in queries
    ]
    validate_batch(manifest, checkpoints)
    return manifest, checkpoints


def summarize_batch(
    manifest: BatchManifest, checkpoints: list[DiscoveryCheckpoint]
) -> BatchSummary:
    """Derive honest status/attempt/result counts from stored checkpoints."""
    validate_batch(manifest, checkpoints)
    return BatchSummary(
        batch_id=manifest.batch_id,
        task_count=len(checkpoints),
        attempt_count=sum(len(checkpoint.attempts) for checkpoint in checkpoints),
        result_count=sum(
            len(attempt.results)
            for checkpoint in checkpoints
            for attempt in checkpoint.attempts
        ),
        statuses=Counter(checkpoint.terminal_status for checkpoint in checkpoints),
    )


def _latest_persisted_completion(root: Path) -> datetime | None:
    """Return the latest validated attempt completion across every stored batch."""
    completions: list[datetime] = []
    for batch_path in sorted(path for path in root.iterdir() if path.is_dir()):
        manifest = load_manifest(batch_path)
        checkpoints = load_checkpoints(batch_path)
        validate_batch(manifest, checkpoints)
        completions.extend(
            datetime.fromisoformat(attempt.completed_at_utc.replace("Z", "+00:00"))
            for checkpoint in checkpoints
            for attempt in checkpoint.attempts
            if attempt.completed_at_utc
        )
    return max(completions, default=None)


def execute_batch(
    root: Path,
    manifest: BatchManifest,
    checkpoints: list[DiscoveryCheckpoint],
    client: SearchClient,
    *,
    now: Callable[[], datetime] = utc_now,
    sleeper: Callable[[float], None] = time.sleep,
    retry_indeterminate: bool = False,
) -> BatchSummary:
    """Run a globally locked batch with durable pre-call and retry evidence."""
    if manifest.schema_version != SCHEMA_VERSION:
        raise ValueError("Firecrawl execution requires the current evidence schema")
    interval = 60.0 / manifest.requests_per_minute
    with ExecutionLock(root), BatchLock(root, manifest.batch_id):
        batch_dir = initialize_batch(root, manifest, checkpoints)
        stored_manifest = load_manifest(batch_dir)
        if stored_manifest.schema_version != SCHEMA_VERSION:
            raise ValueError("stored legacy Firecrawl batches are validation-only")
        stored = load_checkpoints(batch_dir)
        validate_batch(stored_manifest, stored)
        last_completion = _latest_persisted_completion(root)
        retry_delay = 0.0
        for original in sorted(stored, key=lambda item: item.task_id):
            checkpoint = original
            if checkpoint.terminal_status == "in_flight":
                if not retry_indeterminate:
                    raise RuntimeError(
                        "batch contains an indeterminate in-flight attempt; "
                        "retry requires --retry-indeterminate"
                    )
                recovered_at = now()
                checkpoint = recover_in_flight(
                    checkpoint,
                    _timestamp(recovered_at),
                    stored_manifest.max_attempts,
                )
                replace_checkpoint(batch_dir, checkpoint)
                last_completion = max(
                    item for item in (last_completion, recovered_at) if item is not None
                )
            if checkpoint.terminal_status in {
                "success",
                "zero_results",
                "non_retryable_failure",
                "attempts_exhausted",
            }:
                continue
            while len(checkpoint.attempts) < stored_manifest.max_attempts:
                current = now()
                elapsed = (
                    (current - last_completion).total_seconds()
                    if last_completion is not None
                    else interval
                )
                delay_before = max(0.0, interval - elapsed, retry_delay)
                if delay_before:
                    sleeper(delay_before)
                started = _timestamp(now())
                checkpoint = begin_attempt(
                    checkpoint,
                    started,
                    stored_manifest.max_attempts,
                )
                replace_checkpoint(batch_dir, checkpoint)
                outcome = client.search_once(
                    checkpoint.query, stored_manifest.result_limit
                )
                completed_at = now()
                checkpoint = complete_attempt(
                    checkpoint,
                    outcome,
                    _timestamp(completed_at),
                    stored_manifest.max_attempts,
                )
                replace_checkpoint(batch_dir, checkpoint)
                last_completion = completed_at
                if outcome.systemic:
                    raise RuntimeError(
                        "Firecrawl authorization or billing rejected the batch"
                    )
                if checkpoint.terminal_status != "retryable_failure":
                    break
                exponential = float(2 ** (len(checkpoint.attempts) - 1))
                retry_delay = max(outcome.retry_after_seconds, exponential)
            retry_delay = 0.0
        final = load_checkpoints(batch_dir)
        validate_batch(stored_manifest, final)
        return summarize_batch(stored_manifest, final)


def validate_stored_batches(root: Path = BATCH_ROOT) -> list[BatchSummary]:
    """Validate every committed raw batch without making network calls."""
    summaries: list[BatchSummary] = []
    if not root.exists():
        return summaries
    for batch_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        manifest = load_manifest(batch_dir)
        checkpoints = load_checkpoints(batch_dir)
        validate_batch(manifest, checkpoints)
        summaries.append(summarize_batch(manifest, checkpoints))
    return summaries


def _csv_set(value: str, label: str) -> frozenset[str]:
    """Parse a comma-separated CLI set and reject empty entries."""
    items = frozenset(item.strip().upper() for item in value.split(",") if item.strip())
    if not items:
        raise ValueError(f"{label} cannot be empty")
    return items


def _execute_from_cli(
    root: Path,
    manifest: BatchManifest,
    checkpoints: list[DiscoveryCheckpoint],
    *,
    retry_indeterminate: bool,
) -> int:
    """Load the configured credential, execute one immutable plan, and report it."""
    load_dotenv()
    key = os.environ.get("FIRECRAWL_API_KEY", "")
    if not key:
        raise RuntimeError("FIRECRAWL_API_KEY not configured")
    summary = execute_batch(
        root,
        manifest,
        checkpoints,
        FirecrawlClient(key),
        retry_indeterminate=retry_indeterminate,
    )
    statuses = ", ".join(
        f"{status}={count}" for status, count in sorted(summary.statuses.items())
    )
    print(
        f"verified: Firecrawl batch {summary.batch_id}; tasks={summary.task_count}, "
        f"attempts={summary.attempt_count}, results={summary.result_count}; {statuses}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Plan, run/resume, or validate bounded raw Firecrawl discovery batches."""
    parser = argparse.ArgumentParser(description="Run raw entity source discovery")
    parser.add_argument("--states", default="CA,NH,TX")
    parser.add_argument("--namespaces", default=",".join(NAMESPACE_ORDER))
    parser.add_argument("--per-namespace-state", type=int, default=3)
    parser.add_argument("--task-limit", type=int, default=27)
    parser.add_argument("--result-limit", type=int, default=5)
    parser.add_argument("--requests-per-minute", type=int, default=10)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--selection-seed", default=DEFAULT_SELECTION_SEED)
    parser.add_argument("--batch-id", default="")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--retry-indeterminate", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--root", type=Path, default=BATCH_ROOT)
    args = parser.parse_args(argv)
    if args.validate:
        summaries = validate_stored_batches(args.root)
        print(
            f"verified: validated {len(summaries)} Firecrawl raw batch(es); "
            f"tasks={sum(summary.task_count for summary in summaries)}, "
            f"attempts={sum(summary.attempt_count for summary in summaries)}, "
            f"results={sum(summary.result_count for summary in summaries)}"
        )
        return 0

    existing_dir = args.root / args.batch_id if args.batch_id else None
    if existing_dir is not None and existing_dir.is_dir():
        manifest = load_manifest(existing_dir)
        checkpoints = load_checkpoints(existing_dir)
        validate_batch(manifest, checkpoints)
        if args.dry_run:
            summary = summarize_batch(manifest, checkpoints)
            print(
                f"verified: existing batch {summary.batch_id}; "
                f"tasks={summary.task_count}, attempts={summary.attempt_count}, "
                f"results={summary.result_count}; no key read, no network call made, "
                "no file written"
            )
            return 0
        return _execute_from_cli(
            args.root,
            manifest,
            checkpoints,
            retry_indeterminate=args.retry_indeterminate,
        )

    states = _csv_set(args.states, "states")
    namespaces = frozenset(
        item.lower() for item in _csv_set(args.namespaces, "namespaces")
    )
    prior = frozenset() if args.refresh else completed_request_keys(args.root)
    selected = select_targets(
        load_research_targets(),
        states=states,
        namespaces=namespaces,
        per_namespace_state=args.per_namespace_state,
        task_limit=args.task_limit,
        selection_seed=args.selection_seed,
        result_limit=args.result_limit,
        completed_requests=prior,
    )
    current = utc_now()
    batch_id = args.batch_id or _batch_id(current)
    manifest, checkpoints = plan_batch(
        selected,
        batch_id=batch_id,
        created_at_utc=_timestamp(current),
        result_limit=args.result_limit,
        requests_per_minute=args.requests_per_minute,
        max_attempts=args.max_attempts,
        selection_seed=args.selection_seed,
    )
    if args.dry_run:
        for checkpoint in sorted(
            checkpoints,
            key=lambda item: (item.state, item.entity_namespace, item.geoid),
        ):
            print(
                f"assumed: {checkpoint.entity_namespace}:{checkpoint.geoid}\t"
                f"{checkpoint.state}\t{checkpoint.entity_name}\t{checkpoint.query}"
            )
        print(
            f"assumed: dry run planned {len(checkpoints)} calls; "
            "no key read, no network call made, no file written"
        )
        return 0

    return _execute_from_cli(
        args.root,
        manifest,
        checkpoints,
        retry_indeterminate=args.retry_indeterminate,
    )


if __name__ == "__main__":
    raise SystemExit(main())
