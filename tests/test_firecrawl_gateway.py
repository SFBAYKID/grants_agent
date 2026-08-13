"""Durable budget, in-flight, and backoff guarantees for runtime Firecrawl calls."""

from __future__ import annotations

import multiprocessing
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import requests

from grant_watch import db
from grant_watch.enrich import firecrawl_gateway as gateway
from tests.paid_provider_support import (
    configure_authority,
    configure_firecrawl_runtime,
)


class _Response:
    """Minimal deterministic Firecrawl response."""

    def __init__(
        self,
        payload: dict[str, object],
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Store one response body and status."""
        self._payload = payload
        self.status_code = status
        self.headers = headers or {}

    def json(self) -> dict[str, object]:
        """Return the scripted JSON body."""
        return self._payload


def _configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: int = 10
) -> Path:
    """Set one host authority, standalone ledger, credential, and fixed ceiling."""
    return configure_firecrawl_runtime(tmp_path, monkeypatch, limit=limit)


def _ledger_rows(sql: str) -> list[sqlite3.Row]:
    """Read one query from the configured standalone test ledger."""
    ledger = gateway.connect_ledger()
    try:
        return list(ledger.execute(sql))
    finally:
        ledger.close()


def _claim_rate_slot_process(start: Any, results: Any) -> None:
    """Claim one real standalone-ledger slot from an independent process."""
    from grant_watch.enrich import firecrawl_gateway as process_gateway

    start.wait()
    ledger = process_gateway.connect_ledger()
    try:
        process_gateway._await_rate_slot(ledger, datetime.now(timezone.utc))
        results.put(time.monotonic())
    finally:
        ledger.close()


def test_in_flight_and_budget_are_committed_before_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The network boundary can observe its own precommitted durable reservation."""
    conn = db.connect(tmp_path / "gateway.db")
    ledger_path = _configured(tmp_path, monkeypatch)

    def _post(*_args: object, **_kwargs: object) -> _Response:
        """Assert durable state from a second connection before returning."""
        observer = sqlite3.connect(ledger_path)
        attempt = observer.execute(
            """SELECT state,request_hash,attempt_number,workflow,request_key
                 FROM firecrawl_runtime_attempts"""
        ).fetchone()
        assert attempt is not None
        assert attempt[:4] == ("in_flight", attempt[1], 1, "test_search")
        assert len(attempt[1]) == 64
        assert attempt[4].startswith("search:")
        assert "Alpha District" not in attempt[4]
        assert (
            observer.execute(
                "SELECT reserved_calls FROM firecrawl_runtime_periods"
            ).fetchone()[0]
            == 1
        )
        observer.close()
        return _Response({"data": [{"title": "Official result"}]})

    monkeypatch.setattr(gateway.requests, "post", _post)
    with gateway.bind_connection(conn, "test_search"):
        assert gateway.search("Alpha District", conn=conn)[0]["title"] == (
            "Official result"
        )
    row = _ledger_rows("SELECT state,http_status FROM firecrawl_runtime_attempts")[0]
    assert tuple(row) == ("completed", 200)
    observer = sqlite3.connect(ledger_path)
    dump = "\n".join(observer.iterdump())
    observer.close()
    assert "Alpha District" not in dump
    assert "fc-test-secret" not in dump


def test_timeout_stays_indeterminate_and_counts_against_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lost response is never relabeled failed or refunded automatically."""
    conn = db.connect(tmp_path / "gateway.db")
    _configured(tmp_path, monkeypatch)
    calls = 0

    def _timeout(*_args: object, **_kwargs: object) -> _Response:
        """Lose the response after the request could have reached Firecrawl."""
        nonlocal calls
        calls += 1
        raise requests.Timeout("lost response")

    monkeypatch.setattr(gateway.requests, "post", _timeout)
    with pytest.raises(gateway.FirecrawlUnavailable, match="indeterminate"):
        gateway.search("Alpha District", conn=conn)
    assert (
        _ledger_rows("SELECT state FROM firecrawl_runtime_attempts")[0][0]
        == "indeterminate"
    )
    assert calls == 1
    assert gateway.usage(conn)[0] == 1


def test_schema42_indeterminate_prefix_blocks_post_upgrade_repeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A legacy NULL full hash cannot silently reopen the same paid request."""
    conn = db.connect(tmp_path / "legacy.db")
    ledger_path = _configured(tmp_path, monkeypatch)
    at = datetime(2026, 8, 12, tzinfo=timezone.utc)
    body: dict[str, object] = {"query": "Alpha District", "limit": 5}
    digest = gateway._request_hash("search", body)
    ledger = sqlite3.connect(ledger_path)
    ledger.execute(
        """INSERT INTO firecrawl_runtime_periods
             (billing_period,call_limit,reserved_calls,created_at,updated_at)
           VALUES ('2026-08',10,1,?,?)""",
        (at.isoformat(), at.isoformat()),
    )
    ledger.execute(
        """INSERT INTO firecrawl_runtime_attempts
             (id,request_key,request_hash,attempt_number,workflow,operation,
              billing_period,state,started_at)
           VALUES ('legacy',?,NULL,1,'runtime','search','2026-08',
                   'indeterminate',?)""",
        (f"search:{digest[:24]}:old-uuid", at.isoformat()),
    )
    ledger.commit()
    ledger.close()
    monkeypatch.setattr(gateway, "_now", lambda: at)
    monkeypatch.setattr(
        gateway.requests,
        "post",
        lambda *_a, **_k: pytest.fail("legacy indeterminate request reached HTTP"),
    )

    with pytest.raises(gateway.FirecrawlIndeterminate, match="explicit retry"):
        gateway.search("Alpha District", conn=conn)

    assert gateway.usage(conn, at)[0] == 1
    assert _ledger_rows("SELECT COUNT(*) FROM firecrawl_runtime_attempts")[0][0] == 1
    with pytest.raises(gateway.FirecrawlIndeterminate, match="explicit retry"):
        gateway.search("Alpha District", conn=conn)
    assert gateway.usage(conn)[0] == 1


def test_explicit_indeterminate_retry_increments_attempt_and_then_resets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only explicit authority retries a lost exact request; success closes the chain."""
    conn = db.connect(tmp_path / "gateway.db")
    _configured(tmp_path, monkeypatch)
    calls = 0

    def _post(*_args: object, **_kwargs: object) -> _Response:
        """Lose the first response, then complete both explicitly authorized calls."""
        nonlocal calls
        calls += 1
        if calls == 1:
            raise requests.Timeout("lost response")
        return _Response({"data": []})

    monkeypatch.setattr(gateway.requests, "post", _post)
    with pytest.raises(gateway.FirecrawlIndeterminate):
        gateway.search("Alpha District", conn=conn)
    with gateway.allow_indeterminate_retry():
        assert gateway.search("Alpha District", conn=conn) == []
    # A later ordinary invocation is a new logical call, not an unbounded retry chain.
    assert gateway.search("Alpha District", conn=conn) == []

    attempts = _ledger_rows(
        """SELECT state,attempt_number,request_hash,request_key
             FROM firecrawl_runtime_attempts ORDER BY rowid"""
    )
    assert [(row["state"], row["attempt_number"]) for row in attempts] == [
        ("indeterminate", 1),
        ("completed", 2),
        ("completed", 1),
    ]
    assert len({row["request_hash"] for row in attempts}) == 1
    assert len({row["request_key"] for row in attempts}) == 3
    assert gateway.usage(conn)[0] == 3


def test_rate_limit_persists_shared_backoff_without_another_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One 429 stops concurrent/follow-up work until the durable window expires."""
    conn = db.connect(tmp_path / "gateway.db")
    _configured(tmp_path, monkeypatch)
    calls = 0

    def _limited(*_args: object, **_kwargs: object) -> _Response:
        """Return one explicit provider throttle."""
        nonlocal calls
        calls += 1
        return _Response({}, status=429, headers={"Retry-After": "120"})

    monkeypatch.setattr(gateway.requests, "post", _limited)
    with pytest.raises(gateway.FirecrawlRateLimited):
        gateway.search("first", conn=conn)
    with pytest.raises(gateway.FirecrawlRateLimited, match="persisted"):
        gateway.search("second", conn=conn)
    assert calls == 1
    assert gateway.usage(conn)[0] == 1


def test_exact_rate_limit_retries_are_bounded_after_persisted_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retry-After opens bounded exact retries and never an unlimited paid loop."""
    conn = db.connect(tmp_path / "gateway.db")
    _configured(tmp_path, monkeypatch, limit=10)
    current = datetime(2026, 8, 12, tzinfo=timezone.utc)
    calls = 0

    def _clock() -> datetime:
        """Return the controlled provider/accounting clock."""
        return current

    def _limited(*_args: object, **_kwargs: object) -> _Response:
        """Return a persisted one-minute provider throttle."""
        nonlocal calls
        calls += 1
        return _Response({}, status=429, headers={"Retry-After": "60"})

    monkeypatch.setattr(gateway, "_now", _clock)
    monkeypatch.setattr(gateway.requests, "post", _limited)
    for _ in range(gateway.MAX_RATE_LIMIT_ATTEMPTS):
        with pytest.raises(gateway.FirecrawlRateLimited):
            gateway.search("same exact query", conn=conn)
        current += timedelta(seconds=61)
    with pytest.raises(gateway.FirecrawlRateLimited, match="retry limit"):
        gateway.search("same exact query", conn=conn)

    assert calls == gateway.MAX_RATE_LIMIT_ATTEMPTS
    assert gateway.usage(conn)[0] == gateway.MAX_RATE_LIMIT_ATTEMPTS
    assert [
        row[0]
        for row in _ledger_rows(
            "SELECT attempt_number FROM firecrawl_runtime_attempts ORDER BY rowid"
        )
    ] == list(range(1, gateway.MAX_RATE_LIMIT_ATTEMPTS + 1))


def test_monthly_and_workflow_budgets_fail_before_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither ceiling can be crossed by a second paid request."""
    conn = db.connect(tmp_path / "gateway.db")
    _configured(tmp_path, monkeypatch, limit=1)
    calls = 0

    def _success(*_args: object, **_kwargs: object) -> _Response:
        """Count actual HTTP boundaries."""
        nonlocal calls
        calls += 1
        return _Response({"data": []})

    monkeypatch.setattr(gateway.requests, "post", _success)
    gateway.search("first", conn=conn)
    with pytest.raises(gateway.FirecrawlBudgetExhausted, match="runtime budget"):
        gateway.search("second", conn=conn)
    assert calls == 1

    other = db.connect(tmp_path / "workflow.db")
    _configured(tmp_path, monkeypatch, limit=10)
    budget = gateway.FirecrawlCallBudget(1)
    with gateway.use_call_budget(budget):
        gateway.search("first", conn=other)
        with pytest.raises(gateway.FirecrawlBudgetExhausted, match="workflow"):
            gateway.search("second", conn=other)
    assert calls == 2


def test_eight_independent_app_databases_share_one_atomic_monthly_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parallel workers cannot each treat the account-wide allowance as private."""
    _configured(tmp_path, monkeypatch, limit=3)
    calls = 0
    lock = threading.Lock()

    def _success(*_args: object, **_kwargs: object) -> _Response:
        """Count only calls that won the conditional durable reservation."""
        nonlocal calls
        with lock:
            calls += 1
        return _Response({"data": []})

    def _worker(number: int) -> str:
        """Run one exact request through its own production-shaped connection."""
        conn = db.connect(tmp_path / f"independent-app-{number}.db")
        try:
            gateway.search(f"worker {number}", conn=conn)
        except gateway.FirecrawlBudgetExhausted:
            return "blocked"
        finally:
            conn.close()
        return "completed"

    monkeypatch.setattr(gateway.requests, "post", _success)
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(_worker, range(8)))

    observer = db.connect(tmp_path / "observer-app.db")
    assert outcomes.count("completed") == 3
    assert outcomes.count("blocked") == 5
    assert calls == 3
    assert gateway.usage(observer) == (3, 3)
    assert _ledger_rows("SELECT COUNT(*) FROM firecrawl_runtime_attempts")[0][0] == 3


def test_independent_processes_share_proactive_request_spacing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The authority ledger spaces processes, not merely threads in one listener."""
    _configured(tmp_path, monkeypatch, limit=10)
    monkeypatch.setenv(gateway.RATE_LIMIT_ENV, "120")
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(target=_claim_rate_slot_process, args=(start, results))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    try:
        start.set()
        timestamps = sorted(results.get(timeout=15) for _ in processes)
        for process in processes:
            process.join(timeout=15)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)
    assert timestamps[1] - timestamps[0] >= 0.35


def test_bounded_rate_wait_failure_is_finalized_without_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A saturated queue leaves a definite failed/no-HTTP attempt, not in-flight."""
    _configured(tmp_path, monkeypatch, limit=10)
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(gateway, "_now", lambda: now)
    monkeypatch.setattr(gateway, "_configured_rate_interval", lambda: 1.0)
    ledger = gateway.connect_ledger()
    with ledger:
        ledger.execute(
            """INSERT INTO firecrawl_runtime_rate_state
                 (singleton,next_call_at,updated_at) VALUES (1,?,?)""",
            (
                (now + timedelta(seconds=31)).isoformat(),
                now.isoformat(),
            ),
        )
    ledger.close()
    monkeypatch.setattr(
        gateway.requests,
        "post",
        lambda *_a, **_k: pytest.fail("bounded rate wait reached HTTP"),
    )

    with pytest.raises(gateway.FirecrawlRateWaitExceeded, match="bounded wait"):
        gateway.search("queued request")

    row = _ledger_rows(
        "SELECT state,finished_at,error_code FROM firecrawl_runtime_attempts"
    )[0]
    assert row["state"] == "failed"
    assert row["finished_at"]
    assert row["error_code"] == "proactive_rate_wait_exceeded_no_http"


def test_malformed_rate_timestamp_is_a_definite_no_http_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Corrupt rate state cannot strand a reserved attempt as indeterminate."""
    _configured(tmp_path, monkeypatch, limit=10)
    ledger = gateway.connect_ledger()
    with ledger:
        ledger.execute(
            """INSERT INTO firecrawl_runtime_rate_state
                 (singleton,next_call_at,updated_at)
               VALUES (1,'not-a-timestamp','2026-08-12T00:00:00+00:00')"""
        )
    ledger.close()
    monkeypatch.setattr(
        gateway.requests,
        "post",
        lambda *_a, **_k: pytest.fail("malformed rate state reached HTTP"),
    )

    with pytest.raises(gateway.FirecrawlUnavailable, match="malformed"):
        gateway.search("malformed rate state")

    row = _ledger_rows("SELECT state,error_code FROM firecrawl_runtime_attempts")[0]
    assert tuple(row) == (
        "failed",
        "proactive_rate_gate_firecrawlunavailable_no_http",
    )


def test_sqlite_rate_gate_failure_is_finalized_and_wrapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ordinary ledger error before HTTP is definite and user-facing as unavailable."""
    _configured(tmp_path, monkeypatch, limit=10)

    def _database_failure(_conn: sqlite3.Connection, _at: datetime) -> float:
        """Fail after reservation but before any simulated provider request."""
        raise sqlite3.OperationalError("simulated rate-state read failure")

    monkeypatch.setattr(gateway, "_await_rate_slot", _database_failure)
    monkeypatch.setattr(
        gateway.requests,
        "post",
        lambda *_a, **_k: pytest.fail("SQLite rate-gate failure reached HTTP"),
    )

    with pytest.raises(gateway.FirecrawlUnavailable, match="before HTTP"):
        gateway.search("sqlite rate failure")

    row = _ledger_rows("SELECT state,error_code FROM firecrawl_runtime_attempts")[0]
    assert tuple(row) == (
        "failed",
        "proactive_rate_gate_operationalerror_no_http",
    )


def test_oversized_success_response_is_bounded_and_finalized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Discovery/runtime evidence cannot read or persist an unbounded vendor body."""
    conn = db.connect(tmp_path / "oversized.db")
    _configured(tmp_path, monkeypatch)
    response = _Response(
        {"success": True, "data": []},
        headers={"Content-Length": str(gateway.MAX_RESPONSE_BYTES + 1)},
    )
    monkeypatch.setattr(gateway.requests, "post", lambda *_a, **_k: response)

    with pytest.raises(gateway.FirecrawlUnavailable, match="evidence cap"):
        gateway.search("oversized", conn=conn)

    row = _ledger_rows(
        "SELECT state,error_code,http_status FROM firecrawl_runtime_attempts"
    )[0]
    assert tuple(row) == ("completed", "response_too_large", 200)
    conn.close()


def test_credential_rejection_opens_shared_circuit_before_followup_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One definite account rejection stops unrelated workers during the window."""
    conn = db.connect(tmp_path / "circuit.db")
    _configured(tmp_path, monkeypatch)
    calls = 0

    def _rejected(*_args: object, **_kwargs: object) -> _Response:
        """Return a definite credential rejection once."""
        nonlocal calls
        calls += 1
        return _Response({}, status=401)

    monkeypatch.setattr(gateway.requests, "post", _rejected)
    with pytest.raises(gateway.FirecrawlUnavailable, match="credential or billing"):
        gateway.search("first", conn=conn)
    with pytest.raises(gateway.FirecrawlUnavailable, match="temporarily open"):
        gateway.search("unrelated second", conn=conn)

    state = _ledger_rows(
        """SELECT reason,blocked_until FROM firecrawl_runtime_provider_state
             WHERE provider='firecrawl'"""
    )[0]
    assert state["reason"] == "credential_or_billing"
    assert state["blocked_until"]
    assert calls == 1
    assert gateway.usage(conn)[0] == 1


def test_lowered_monthly_ceiling_takes_effect_without_erasing_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator can lower the cap; prior calls remain counted and stop new HTTP."""
    conn = db.connect(tmp_path / "gateway.db")
    _configured(tmp_path, monkeypatch, limit=10)
    calls = 0

    def _success(*_args: object, **_kwargs: object) -> _Response:
        """Count only requests that cross the durable authorization boundary."""
        nonlocal calls
        calls += 1
        return _Response({"data": []})

    monkeypatch.setattr(gateway.requests, "post", _success)
    gateway.search("first", conn=conn)
    _configured(tmp_path, monkeypatch, limit=1)

    with pytest.raises(gateway.FirecrawlBudgetExhausted):
        gateway.search("second", conn=conn)

    assert calls == 1
    assert gateway.usage(conn) == (1, 1)


def test_unconfigured_budget_never_reaches_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An API key without an approved ceiling is not permission to spend."""
    conn = db.connect(tmp_path / "gateway.db")
    configure_authority(tmp_path, monkeypatch, "firecrawl")
    ledger_path = tmp_path / "firecrawl-runtime-ledger.db"
    gateway.initialize_empty_ledger(ledger_path)
    monkeypatch.setenv(gateway.LEDGER_PATH_ENV, str(ledger_path))
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-secret")
    monkeypatch.delenv(gateway.MONTHLY_LIMIT_ENV, raising=False)
    monkeypatch.setattr(
        gateway.requests,
        "post",
        lambda *_a, **_k: pytest.fail("HTTP must not run without a budget"),
    )
    with pytest.raises(gateway.FirecrawlBudgetNotConfigured):
        gateway.search("blocked", conn=conn)
    observer = sqlite3.connect(ledger_path)
    assert (
        observer.execute("SELECT COUNT(*) FROM firecrawl_runtime_attempts").fetchone()[
            0
        ]
        == 0
    )
    observer.close()


def test_runtime_firecrawl_endpoints_exist_only_at_reviewed_boundaries() -> None:
    """A new direct requests path cannot silently bypass durable state."""
    root = Path(__file__).resolve().parents[1] / "grant_watch"
    holders = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if "https://api.firecrawl.dev" in path.read_text(encoding="utf-8")
    }
    assert holders == {"enrich/firecrawl_gateway.py"}
