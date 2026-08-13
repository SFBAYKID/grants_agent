"""Durable, budgeted boundary for every operational Firecrawl search and scrape.

The nationwide source-discovery collector keeps its own immutable batch store, fixed
attempt budget, and root-wide lock, but its HTTP crosses this same account boundary.
Every Firecrawl caller therefore commits ``in_flight`` before HTTP, reserves from one
fixed monthly ceiling, shares proactive rate/backoff state, and never retries an
indeterminate exact request implicitly.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from ..firecrawl_client import MAX_RESPONSE_BYTES
from ..paid_provider_authority import (
    PaidProviderAuthorityError,
    require_call_authority,
)
from . import firecrawl_runtime_ledger

SEARCH_URL = "https://api.firecrawl.dev/v1/search"
SCRAPE_URL = "https://api.firecrawl.dev/v1/scrape"
MONTHLY_LIMIT_ENV = "FIRECRAWL_RUNTIME_MONTHLY_CALL_LIMIT"
RATE_LIMIT_ENV = "FIRECRAWL_RUNTIME_REQUESTS_PER_MINUTE"
LEDGER_PATH_ENV = firecrawl_runtime_ledger.LEDGER_PATH_ENV
MAX_RATE_LIMIT_ATTEMPTS = 3
MAX_REQUESTS_PER_MINUTE = 600
MAX_PROACTIVE_RATE_WAIT_SECONDS = 30.0
_WORKFLOW_RE = re.compile(r"[a-z0-9_.:-]{1,80}")
_WORKFLOW: contextvars.ContextVar[str] = contextvars.ContextVar(
    "firecrawl_workflow", default="runtime"
)
_RETRY_INDETERMINATE: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "firecrawl_retry_indeterminate", default=False
)


class FirecrawlUnavailable(RuntimeError):
    """The provider could not return a definite usable response."""


class FirecrawlRateLimited(FirecrawlUnavailable):
    """A persisted provider backoff window currently blocks new calls."""

    def __init__(self, message: str, *, retry_after_seconds: float = 0.0) -> None:
        """Retain only the safe numeric wait needed by a batch scheduler."""
        super().__init__(message)
        self.retry_after_seconds = max(0.0, retry_after_seconds)


class FirecrawlRateWaitExceeded(FirecrawlRateLimited):
    """The account-wide proactive limiter cannot grant a bounded slot in time."""


class FirecrawlIndeterminate(FirecrawlUnavailable):
    """An exact prior request may have executed and needs operator reconciliation."""


class FirecrawlCredentialRejected(FirecrawlUnavailable):
    """The account credential or billing state blocks all further batch work."""


class FirecrawlBudgetNotConfigured(RuntimeError):
    """Paid runtime calls are disabled until an operator sets a fixed ceiling."""


class FirecrawlBudgetExhausted(RuntimeError):
    """The fixed monthly or per-workflow call ceiling has been reached."""


@dataclass
class FirecrawlCallBudget:
    """Thread-safe call ceiling shared by one higher-level workflow."""

    limit: int
    used: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        """Reject a non-positive budget before any worker starts."""
        if self.limit <= 0:
            raise ValueError("Firecrawl call budget must be positive")

    def claim(self) -> bool:
        """Atomically reserve one workflow call, returning false at the ceiling."""
        with self._lock:
            if self.used >= self.limit:
                return False
            self.used += 1
            return True

    def release(self) -> None:
        """Return a claim when durable reservation failed before HTTP."""
        with self._lock:
            if self.used > 0:
                self.used -= 1


_CALL_BUDGET: contextvars.ContextVar[FirecrawlCallBudget | None] = (
    contextvars.ContextVar("firecrawl_call_budget", default=None)
)


@contextlib.contextmanager
def bind_connection(conn: sqlite3.Connection, workflow: str) -> Iterator[None]:
    """Bind a safe workflow label; the app connection is never spend authority."""
    del conn
    normalized = workflow.strip().lower()
    if _WORKFLOW_RE.fullmatch(normalized) is None:
        raise ValueError("invalid Firecrawl workflow label")
    workflow_token = _WORKFLOW.set(normalized)
    try:
        yield
    finally:
        _WORKFLOW.reset(workflow_token)


@contextlib.contextmanager
def bind_workflow(workflow: str) -> Iterator[None]:
    """Label a call while allowing the gateway to own its durable connection."""
    normalized = workflow.strip().lower()
    if _WORKFLOW_RE.fullmatch(normalized) is None:
        raise ValueError("invalid Firecrawl workflow label")
    token = _WORKFLOW.set(normalized)
    try:
        yield
    finally:
        _WORKFLOW.reset(token)


@contextlib.contextmanager
def use_call_budget(budget: FirecrawlCallBudget) -> Iterator[None]:
    """Apply one shared call ceiling to all gateway calls in this context."""
    token = _CALL_BUDGET.set(budget)
    try:
        yield
    finally:
        _CALL_BUDGET.reset(token)


@contextlib.contextmanager
def allow_indeterminate_retry(enabled: bool = True) -> Iterator[None]:
    """Authorize an explicit retry of one exact request with an unknown outcome.

    Only an operator-facing path that already exposes an explicit retry control should
    enter this context. Keeping the authority in a context variable prevents ordinary
    scheduled and Slack calls from accidentally forwarding a permissive boolean.
    """
    if not enabled:
        yield
        return
    token = _RETRY_INDETERMINATE.set(True)
    try:
        yield
    finally:
        _RETRY_INDETERMINATE.reset(token)


def _now() -> datetime:
    """Return an aware UTC timestamp."""
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    """Serialize an aware timestamp for SQLite comparison and audit."""
    return value.astimezone(timezone.utc).isoformat()


def _period(at: datetime) -> str:
    """Return the UTC calendar-month billing key."""
    return at.astimezone(timezone.utc).strftime("%Y-%m")


def _configured_monthly_limit() -> int:
    """Return the fail-closed fixed runtime ceiling."""
    raw = os.environ.get(MONTHLY_LIMIT_ENV, "").strip()
    try:
        limit = int(raw)
    except ValueError as exc:
        raise FirecrawlBudgetNotConfigured(
            f"{MONTHLY_LIMIT_ENV} must be a positive integer"
        ) from exc
    if limit <= 0:
        raise FirecrawlBudgetNotConfigured(
            f"{MONTHLY_LIMIT_ENV} must be configured before paid runtime calls"
        )
    return limit


def _configured_rate_interval() -> float:
    """Return the reviewed minimum spacing between account-wide HTTP calls."""
    raw = os.environ.get(RATE_LIMIT_ENV, "").strip()
    try:
        requests_per_minute = int(raw)
    except ValueError as exc:
        raise FirecrawlBudgetNotConfigured(
            f"{RATE_LIMIT_ENV} must be a positive integer"
        ) from exc
    if not 1 <= requests_per_minute <= MAX_REQUESTS_PER_MINUTE:
        raise FirecrawlBudgetNotConfigured(
            f"{RATE_LIMIT_ENV} must be between 1 and {MAX_REQUESTS_PER_MINUTE}"
        )
    return 60.0 / requests_per_minute


@contextlib.contextmanager
def _connection(explicit: sqlite3.Connection | None) -> Iterator[sqlite3.Connection]:
    """Open only the host-bound standalone account ledger for paid runtime calls."""
    del explicit
    owned = connect_ledger()
    try:
        yield owned
    finally:
        owned.close()


def connect_ledger() -> sqlite3.Connection:
    """Open the configured account ledger for diagnostics and tested operations."""
    _configured_monthly_limit()
    _configured_rate_interval()
    try:
        binding = require_call_authority("firecrawl", ("FIRECRAWL_API_KEY",))
        return firecrawl_runtime_ledger.connect_runtime(binding)
    except (
        PaidProviderAuthorityError,
        firecrawl_runtime_ledger.FirecrawlLedgerError,
    ) as exc:
        raise FirecrawlBudgetNotConfigured(str(exc)) from exc


def _request_hash(operation: str, body: dict[str, object]) -> str:
    """Return the deterministic full digest of one canonical provider request."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{operation}|{canonical}".encode()).hexdigest()


def _request_key(operation: str) -> str:
    """Return an opaque unique audit key carrying no query, URL, or digest text."""
    return f"{operation}:{uuid.uuid4().hex}"


def _blocked_until(row: sqlite3.Row | None, at: datetime) -> None:
    """Raise while a persisted provider backoff/circuit window is active."""
    if row is None or not row["blocked_until"]:
        return
    try:
        blocked = datetime.fromisoformat(str(row["blocked_until"]))
    except ValueError:
        raise FirecrawlUnavailable("Firecrawl provider state is malformed") from None
    if blocked > at:
        reason = str(row["reason"] or "provider_backoff")
        if reason == "rate_limited":
            raise FirecrawlRateLimited(
                "Firecrawl is in a persisted rate-limit backoff",
                retry_after_seconds=(blocked - at).total_seconds(),
            )
        if reason == "credential_or_billing":
            raise FirecrawlCredentialRejected(
                "Firecrawl credential or billing circuit is temporarily open"
            )
        raise FirecrawlUnavailable("Firecrawl provider circuit is temporarily open")


def _reserve(
    conn: sqlite3.Connection,
    operation: str,
    body: dict[str, object],
    at: datetime,
) -> str:
    """Reserve budget and commit an in-flight row before the HTTP boundary."""
    configured_limit = _configured_monthly_limit()
    period = _period(at)
    attempt_id = uuid.uuid4().hex
    request_hash = _request_hash(operation, body)
    request_key = _request_key(operation)
    started = _timestamp(at)
    exhausted = False
    with conn:
        # This upsert is deliberately the first operation. It obtains SQLite's write
        # lock before the exact-request lookup, so two workers cannot both observe no
        # in-flight attempt and cross the HTTP boundary for the same request.
        conn.execute(
            """INSERT INTO firecrawl_runtime_periods
                 (billing_period,call_limit,reserved_calls,created_at,updated_at)
               VALUES (?,?,0,?,?)
               ON CONFLICT(billing_period) DO UPDATE SET
                 call_limit=MAX(firecrawl_runtime_periods.reserved_calls,
                                excluded.call_limit),
                 updated_at=excluded.updated_at""",
            (period, configured_limit, started, started),
        )
        _blocked_until(
            conn.execute(
                """SELECT blocked_until,reason FROM firecrawl_runtime_provider_state
                   WHERE provider='firecrawl'"""
            ).fetchone(),
            at,
        )
        # Schema-42 keys contain the first 24 hex characters of this same canonical
        # digest. Migration 45 cannot honestly fabricate the missing suffix, so NULL
        # legacy hashes are matched by that retained prefix and treated fail-closed.
        # A 96-bit collision can only cause a conservative operator-review refusal;
        # it can never authorize a duplicate call.
        prior = conn.execute(
            """SELECT id,state,attempt_number
                 FROM firecrawl_runtime_attempts
                WHERE billing_period=? AND (
                  request_hash=? OR (
                    request_hash IS NULL AND request_key LIKE ?
                  )
                )
                ORDER BY started_at DESC,rowid DESC LIMIT 1""",
            (period, request_hash, f"{operation}:{request_hash[:24]}:%"),
        ).fetchone()
        attempt_number = 1
        if prior is not None:
            prior_state = str(prior["state"])
            prior_number = max(1, int(prior["attempt_number"] or 1))
            if prior_state in {"in_flight", "indeterminate"}:
                if not _RETRY_INDETERMINATE.get():
                    raise FirecrawlIndeterminate(
                        "prior exact Firecrawl request is indeterminate; "
                        "explicit retry required"
                    )
                attempt_number = prior_number + 1
                if prior_state == "in_flight":
                    conn.execute(
                        """UPDATE firecrawl_runtime_attempts
                              SET state='indeterminate',finished_at=?,
                                  error_code=COALESCE(
                                    error_code,'operator_declared_interrupted'
                                  )
                            WHERE id=? AND state='in_flight'""",
                        (started, prior["id"]),
                    )
            elif prior_state == "rate_limited":
                if prior_number >= MAX_RATE_LIMIT_ATTEMPTS:
                    raise FirecrawlRateLimited(
                        "Firecrawl exact-request retry limit is exhausted"
                    )
                attempt_number = prior_number + 1
        updated = conn.execute(
            """UPDATE firecrawl_runtime_periods
               SET reserved_calls=reserved_calls+1,updated_at=?
               WHERE billing_period=? AND reserved_calls < call_limit""",
            (started, period),
        )
        if updated.rowcount != 1:
            exhausted = True
        else:
            conn.execute(
                """INSERT INTO firecrawl_runtime_attempts
                     (id,request_key,request_hash,attempt_number,workflow,operation,
                      billing_period,state,started_at)
                   VALUES (?,?,?,?,?,?,?,'in_flight',?)""",
                (
                    attempt_id,
                    request_key,
                    request_hash,
                    attempt_number,
                    _WORKFLOW.get(),
                    operation,
                    period,
                    started,
                ),
            )
    if exhausted:
        raise FirecrawlBudgetExhausted(
            f"Firecrawl runtime budget is exhausted for {period}"
        )
    return attempt_id


def _await_rate_slot(conn: sqlite3.Connection, at: datetime) -> float:
    """Claim and await one cross-process HTTP slot in the standalone ledger.

    The write-first transaction serializes every process sharing this provider
    authority. A claimed timestamp remains consumed if a process crashes during the
    wait, which is conservative and prevents the next worker from bursting across
    the vendor boundary.
    """
    interval = _configured_rate_interval()
    at_text = _timestamp(at)
    with conn:
        conn.execute(
            """INSERT INTO firecrawl_runtime_rate_state
                 (singleton,next_call_at,updated_at) VALUES (1,?,?)
               ON CONFLICT(singleton) DO NOTHING""",
            (at_text, at_text),
        )
        row = conn.execute(
            "SELECT next_call_at FROM firecrawl_runtime_rate_state WHERE singleton=1"
        ).fetchone()
        if row is None:
            raise FirecrawlUnavailable("Firecrawl rate state is missing")
        try:
            next_call = datetime.fromisoformat(str(row[0]))
        except ValueError as exc:
            raise FirecrawlUnavailable("Firecrawl rate state is malformed") from exc
        if next_call.tzinfo is None:
            next_call = next_call.replace(tzinfo=timezone.utc)
        scheduled = max(at.astimezone(timezone.utc), next_call.astimezone(timezone.utc))
        wait_seconds = max(0.0, (scheduled - at).total_seconds())
        if wait_seconds > MAX_PROACTIVE_RATE_WAIT_SECONDS:
            raise FirecrawlRateWaitExceeded(
                "Firecrawl proactive rate-limit queue exceeds the bounded wait"
            )
        conn.execute(
            """UPDATE firecrawl_runtime_rate_state
                  SET next_call_at=?,updated_at=? WHERE singleton=1""",
            (
                _timestamp(scheduled + timedelta(seconds=interval)),
                at_text,
            ),
        )
    if wait_seconds:
        time.sleep(wait_seconds)
    return wait_seconds


def _finalize(
    conn: sqlite3.Connection,
    attempt_id: str,
    state: str,
    *,
    status: int = 0,
    error_code: str = "",
    retry_after: float = 0.0,
    block_for: float = 0.0,
    block_reason: str = "",
) -> None:
    """Persist the exact known outcome and optional shared provider backoff."""
    finished = _now()
    with conn:
        conn.execute(
            """UPDATE firecrawl_runtime_attempts
               SET state=?,finished_at=?,http_status=?,retry_after_seconds=?,error_code=?
               WHERE id=? AND state='in_flight'""",
            (
                state,
                _timestamp(finished),
                status or None,
                retry_after or None,
                error_code or None,
                attempt_id,
            ),
        )
        if block_for > 0:
            conn.execute(
                """INSERT INTO firecrawl_runtime_provider_state
                     (provider,blocked_until,reason,updated_at)
                   VALUES ('firecrawl',?,?,?)
                   ON CONFLICT(provider) DO UPDATE SET
                     blocked_until=excluded.blocked_until,
                     reason=excluded.reason,
                     updated_at=excluded.updated_at""",
                (
                    _timestamp(finished + timedelta(seconds=block_for)),
                    block_reason,
                    _timestamp(finished),
                ),
            )


def _retry_after(response: requests.Response) -> float:
    """Parse a numeric Retry-After header without trusting arbitrary text."""
    headers = getattr(response, "headers", {}) or {}
    try:
        return max(0.0, float(headers.get("Retry-After", "0")))
    except (TypeError, ValueError):
        return 0.0


class _ResponseTooLarge(ValueError):
    """A successful response exceeded the bounded evidence payload size."""


def _bounded_response_json(response: requests.Response) -> object:
    """Decode at most the reviewed response cap, including streamed bodies."""
    headers = getattr(response, "headers", {}) or {}
    try:
        content_length = int(headers.get("Content-Length", "0"))
    except (TypeError, ValueError):
        content_length = 0
    if content_length > MAX_RESPONSE_BYTES:
        raise _ResponseTooLarge("declared Firecrawl response is too large")
    iterator = getattr(response, "iter_content", None)
    if callable(iterator):
        body = bytearray()
        for chunk in iterator(chunk_size=65_536):
            if not isinstance(chunk, (bytes, bytearray)):
                raise ValueError("Firecrawl response stream returned non-bytes")
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise _ResponseTooLarge("streamed Firecrawl response is too large")
        return json.loads(body.decode("utf-8"))
    content = getattr(response, "content", None)
    if isinstance(content, (bytes, bytearray)):
        if len(content) > MAX_RESPONSE_BYTES:
            raise _ResponseTooLarge("Firecrawl response is too large")
        if content:
            return json.loads(bytes(content).decode("utf-8"))
    return response.json()


def _post(
    operation: str,
    body: dict[str, object],
    timeout: float,
    conn: sqlite3.Connection | None,
) -> dict[str, Any]:
    """Execute one pre-reserved request and classify it without implicit retry."""
    api_key = os.environ.get("FIRECRAWL_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("FIRECRAWL_API_KEY not configured")
    budget = _CALL_BUDGET.get()
    if budget is not None and not budget.claim():
        raise FirecrawlBudgetExhausted("Firecrawl workflow call budget is exhausted")
    endpoint = SEARCH_URL if operation == "search" else SCRAPE_URL
    with _connection(conn) as ledger:
        try:
            attempt_id = _reserve(ledger, operation, body, _now())
        except Exception:
            if budget is not None:
                budget.release()
            raise
        try:
            _await_rate_slot(ledger, _now())
        except Exception as exc:
            error_code = (
                "proactive_rate_wait_exceeded_no_http"
                if isinstance(exc, FirecrawlRateWaitExceeded)
                else f"proactive_rate_gate_{type(exc).__name__.lower()}_no_http"
            )
            _finalize(
                ledger,
                attempt_id,
                "failed",
                error_code=error_code,
            )
            if isinstance(exc, FirecrawlUnavailable):
                raise
            raise FirecrawlUnavailable(
                "Firecrawl proactive rate gate failed before HTTP"
            ) from exc
        try:
            response = requests.post(
                endpoint,
                headers={"Authorization": f"Bearer {api_key}"},
                json=body,
                timeout=timeout,
                stream=True,
            )
        except requests.RequestException as exc:
            _finalize(
                ledger,
                attempt_id,
                "indeterminate",
                error_code=type(exc).__name__,
            )
            raise FirecrawlIndeterminate(
                "Firecrawl request outcome is indeterminate"
            ) from exc
        try:
            status = int(getattr(response, "status_code", 200))
            if status == 429:
                retry_after = _retry_after(response) or 60.0
                _finalize(
                    ledger,
                    attempt_id,
                    "rate_limited",
                    status=status,
                    error_code="http_429",
                    retry_after=retry_after,
                    block_for=retry_after,
                    block_reason="rate_limited",
                )
                raise FirecrawlRateLimited(
                    "Firecrawl rate limit reached", retry_after_seconds=retry_after
                )
            if status in {401, 402, 403}:
                _finalize(
                    ledger,
                    attempt_id,
                    "failed",
                    status=status,
                    error_code=f"http_{status}",
                    block_for=900.0,
                    block_reason="credential_or_billing",
                )
                raise FirecrawlCredentialRejected(
                    "Firecrawl rejected its credential or billing"
                )
            if status >= 500:
                _finalize(
                    ledger,
                    attempt_id,
                    "indeterminate",
                    status=status,
                    error_code=f"http_{status}",
                )
                raise FirecrawlIndeterminate("Firecrawl returned a server error")
            if status >= 400:
                _finalize(
                    ledger,
                    attempt_id,
                    "failed",
                    status=status,
                    error_code=f"http_{status}",
                )
                raise FirecrawlUnavailable(
                    f"Firecrawl rejected the {operation} request"
                )
            try:
                payload = _bounded_response_json(response)
            except _ResponseTooLarge as exc:
                _finalize(
                    ledger,
                    attempt_id,
                    "completed",
                    status=status,
                    error_code="response_too_large",
                )
                raise FirecrawlUnavailable(
                    "Firecrawl response exceeded the evidence cap"
                ) from exc
            except requests.RequestException as exc:
                _finalize(
                    ledger,
                    attempt_id,
                    "indeterminate",
                    status=status,
                    error_code="response_stream_error",
                )
                raise FirecrawlIndeterminate(
                    "Firecrawl response stream became indeterminate"
                ) from exc
            except (TypeError, ValueError, UnicodeDecodeError) as exc:
                _finalize(
                    ledger,
                    attempt_id,
                    "completed",
                    status=status,
                    error_code="invalid_json",
                )
                raise FirecrawlUnavailable("Firecrawl returned malformed JSON") from exc
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                # The full outcome is already classified; a cleanup-only close error
                # must not replace it or leave a known response marked in-flight.
                with contextlib.suppress(Exception):  # noqa: BLE001
                    close()
        if not isinstance(payload, dict):
            _finalize(
                ledger,
                attempt_id,
                "completed",
                status=status,
                error_code="invalid_shape",
            )
            raise FirecrawlUnavailable("Firecrawl returned an invalid response shape")
        data = payload.get("data")
        invalid_data = (
            operation == "search"
            and (
                not isinstance(data, list)
                or any(not isinstance(item, dict) for item in data)
            )
        ) or (
            operation == "scrape"
            and (
                not isinstance(data, dict)
                or not isinstance(data.get("markdown", ""), str)
            )
        )
        if invalid_data:
            _finalize(
                ledger,
                attempt_id,
                "completed",
                status=status,
                error_code="invalid_data_shape",
            )
            raise FirecrawlUnavailable(
                f"Firecrawl {operation} data has an invalid shape"
            )
        _finalize(ledger, attempt_id, "completed", status=status)
        return payload


def search(
    query: str,
    limit: int = 5,
    *,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """Return one bounded search result list through the durable gateway."""
    if not query.strip() or not 1 <= limit <= 5:
        raise ValueError("Firecrawl search requires a query and limit from 1 to 5")
    payload = search_payload(query, limit, conn=conn)
    data = payload.get("data")
    assert isinstance(data, list)
    return data[:limit]


def search_payload(
    query: str,
    limit: int = 5,
    *,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Return one full metered search payload for secret-redacted evidence capture."""
    if not query.strip() or not 1 <= limit <= 5:
        raise ValueError("Firecrawl search requires a query and limit from 1 to 5")
    return _post("search", {"query": query, "limit": limit}, 30.0, conn)


def scrape(url: str, *, conn: sqlite3.Connection | None = None) -> str:
    """Return full-page markdown through the durable gateway."""
    if not url.startswith(("https://", "http://")):
        raise ValueError("Firecrawl scrape requires an HTTP(S) URL")
    payload = _post(
        "scrape",
        {"url": url, "formats": ["markdown"], "onlyMainContent": False},
        60.0,
        conn,
    )
    data = payload.get("data")
    assert isinstance(data, dict)
    return str(data.get("markdown") or "")


def usage(conn: sqlite3.Connection, at: datetime | None = None) -> tuple[int, int]:
    """Return reserved and fixed-limit runtime calls for the current UTC month."""
    del conn
    with _connection(None) as ledger:
        row = ledger.execute(
            """SELECT reserved_calls,call_limit FROM firecrawl_runtime_periods
               WHERE billing_period=?""",
            (_period(at or _now()),),
        ).fetchone()
    return (int(row[0]), int(row[1])) if row is not None else (0, 0)


initialize_empty_ledger = firecrawl_runtime_ledger.initialize_empty_ledger
