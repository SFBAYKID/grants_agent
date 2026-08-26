"""Runtime validation for bounded batch contact-enrichment configuration."""

from __future__ import annotations

import importlib
import sqlite3
from pathlib import Path

import pytest

from grant_watch import db
from grant_watch.enrich import firecrawl_gateway
from grant_watch.slack import search_enrichment, tools
from grant_watch.slack.contact_enrichment import ContactOutcome
from tests.paid_provider_support import configure_firecrawl_runtime


def test_malformed_worker_env_does_not_crash_module_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bad deployment value fails at use with context, not during app import."""
    monkeypatch.setenv("GRANT_ENRICH_WORKERS", "many")
    imported = importlib.reload(search_enrichment)
    with pytest.raises(ValueError, match="integer from 1 to 8"):
        imported.configured_enrich_workers()


@pytest.mark.parametrize("raw", ["0", "9", "-1"])
def test_worker_count_is_bounded(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    """Configuration cannot silently create an unbounded provider burst."""
    monkeypatch.setenv("GRANT_ENRICH_WORKERS", raw)
    with pytest.raises(ValueError, match="integer from 1 to 8"):
        search_enrichment.configured_enrich_workers()


def test_batch_call_budget_caps_nested_provider_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two workers cannot expand one ask beyond its shared Firecrawl call ceiling."""
    target = tmp_path / "batch.db"
    conn = db.connect(target)
    rows = list(conn.execute("SELECT 1 AS id UNION ALL SELECT 2 AS id"))
    conn.close()
    configure_firecrawl_runtime(tmp_path, monkeypatch, limit=20)
    monkeypatch.setattr(search_enrichment, "MAX_FIRECRAWL_CALLS_PER_BATCH", 3)
    calls = 0

    class _Response:
        """One valid zero-result response."""

        status_code = 200
        headers: dict[str, str] = {}

        def json(self) -> dict[str, object]:
            """Return a valid search payload."""
            return {"data": []}

    def _post(*_args: object, **_kwargs: object) -> _Response:
        """Count actual provider boundaries."""
        nonlocal calls
        calls += 1
        return _Response()

    def _enrich(
        worker_conn: sqlite3.Connection,
        lead_id: int,
        *_args: object,
        **_kwargs: object,
    ) -> ContactOutcome:
        """Make two nested paid calls per organization."""
        firecrawl_gateway.search(f"lead {lead_id} first", conn=worker_conn)
        firecrawl_gateway.search(f"lead {lead_id} second", conn=worker_conn)
        return ContactOutcome("not_found")

    monkeypatch.setattr(firecrawl_gateway.requests, "post", _post)
    monkeypatch.setattr(tools, "enrich_lead_contact", _enrich)
    cells, note = search_enrichment._enrich_contacts(rows, target, 2, None)

    assert calls == 3
    assert sum(cell[3] == "not checked (call budget)" for cell in cells) == 1
    assert "fixed 3-call enrichment budget" in note


def test_time_budget_note_refuses_to_call_a_repeat_run_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The partial-batch note must state that a repeat run still costs money.

    The sentence is the ONLY thing the model has to reason from, and on 2026-08-26
    Grant compressed "cached ... retried properly" into "re-running costs nothing
    extra" for a rep. Completed lookups are cached; an UNREACHABLE source is filed
    retryable on purpose and is bought again. The note has to carry that itself.
    """
    target = tmp_path / "budget.db"
    conn = db.connect(target)
    rows = list(conn.execute("SELECT 1 AS id UNION ALL SELECT 2 AS id"))
    conn.close()
    # A negative budget puts the deadline in the past, so every row takes the
    # time-budget branch without any provider call.
    monkeypatch.setattr(search_enrichment, "ENRICH_TIME_BUDGET_S", -1.0)

    cells, note = search_enrichment._enrich_contacts(rows, target, 2, None)

    assert all(cell[3] == "not checked (time budget)" for cell in cells)
    assert "NOT free" in note
    assert "paid for again" in note


def test_a_complete_batch_makes_no_cost_disclosure_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CONTROL: nothing skipped means no note, so the wording above cannot be a
    phrase every batch carries regardless of what happened."""
    target = tmp_path / "complete.db"
    conn = db.connect(target)
    rows = list(conn.execute("SELECT 1 AS id UNION ALL SELECT 2 AS id"))
    conn.close()

    def _enrich(
        _worker_conn: sqlite3.Connection,
        _lead_id: int,
        *_args: object,
        **_kwargs: object,
    ) -> ContactOutcome:
        """Finish every organization inside the budget without a paid call."""
        return ContactOutcome("not_found")

    monkeypatch.setattr(tools, "enrich_lead_contact", _enrich)
    _cells, note = search_enrichment._enrich_contacts(rows, target, 2, None)

    assert note == ""
