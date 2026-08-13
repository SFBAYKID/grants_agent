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
