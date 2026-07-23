"""Completion-gated confirmation freshness (Chase A1).

The rich card's freshness rule must not rest on `observed_at` (first-sighting, because
observations are write-once) or `last_seen`. Instead a lead's `last_confirmed_at`
advances ONLY when a run that re-confirmed it is durably marked complete AND successful.
A failed/partial/interrupted/dry run must never advance it.
"""

from __future__ import annotations

from pathlib import Path

from grant_watch import db
from grant_watch.models import RunStats


def _lead(conn: object, iid: str, source: str = "usaspending:16.071") -> int:
    """Insert a minimal lead and return its id."""
    conn.execute(
        "INSERT INTO leads (source, source_item_id, entity_name, status) "
        "VALUES (?,?,?, 'new')",
        (source, iid, f"District {iid}"),
    )
    conn.commit()
    return int(
        conn.execute(
            "SELECT id FROM leads WHERE source=? AND source_item_id=?", (source, iid)
        ).fetchone()["id"]
    )


def test_complete_run_advances_confirmation_for_seen_leads(tmp_path: Path) -> None:
    """A complete, successful run stamps last_confirmed_run_id/at on exactly the leads
    it re-confirmed."""
    conn = db.connect(tmp_path / "t.db")
    lid = _lead(conn, "A1")
    run_id = db.begin_run(conn, "usaspending:16.071", "2026-07-22T00:00:00+00:00")
    # while pending, freshness is NOT advanced
    assert conn.execute(
        "SELECT last_confirmed_at FROM leads WHERE id=?", (lid,)
    ).fetchone()[0] is None
    db.complete_run(
        conn, run_id, RunStats(source="usaspending:16.071", items_seen=1),
        [("usaspending:16.071", "A1")],
    )
    row = conn.execute(
        "SELECT last_confirmed_run_id, last_confirmed_at FROM leads WHERE id=?", (lid,)
    ).fetchone()
    assert row["last_confirmed_run_id"] == run_id and row["last_confirmed_at"]
    assert conn.execute(
        "SELECT state FROM runs WHERE id=?", (run_id,)
    ).fetchone()[0] == "complete"


def test_failed_run_never_advances_confirmation(tmp_path: Path) -> None:
    """A failed/partial run marks the run failed and leaves freshness untouched."""
    conn = db.connect(tmp_path / "t.db")
    lid = _lead(conn, "A1")
    run_id = db.begin_run(conn, "usaspending:16.071", "2026-07-22T00:00:00+00:00")
    db.fail_run(
        conn, run_id,
        RunStats(source="usaspending:16.071", complete=False, error_code="HTTPError"),
    )
    assert conn.execute(
        "SELECT last_confirmed_at FROM leads WHERE id=?", (lid,)
    ).fetchone()[0] is None
    assert conn.execute(
        "SELECT state FROM runs WHERE id=?", (run_id,)
    ).fetchone()[0] == "failed"


def test_dry_run_poll_writes_no_runs_and_no_confirmation(tmp_path: Path) -> None:
    """A --dry-run poll must not open a run row or advance any freshness."""
    from grant_watch import cli

    # Seed a real DB so we can inspect it, but run cmd_poll in dry-run (conn=None).
    path = tmp_path / "t.db"
    seed = db.connect(path)
    _lead(seed, "A1")
    seed.close()
    cli.cmd_poll(only_source="nonexistent-source-xyz", dry_run=True)
    conn = db.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    assert conn.execute(
        "SELECT last_confirmed_at FROM leads WHERE source_item_id='A1'"
    ).fetchone()[0] is None


def test_reconfirmation_by_a_later_complete_run_advances_freshness(
    tmp_path: Path,
) -> None:
    """An UNCHANGED lead still present in a later complete run gets re-confirmed — the
    whole point of A1 (write-once observations can't signal this)."""
    conn = db.connect(tmp_path / "t.db")
    lid = _lead(conn, "A1")
    r1 = db.begin_run(conn, "usaspending:16.071", "2026-07-20T00:00:00+00:00")
    db.complete_run(conn, r1, RunStats(source="usaspending:16.071"),
                    [("usaspending:16.071", "A1")])
    first = conn.execute(
        "SELECT last_confirmed_run_id FROM leads WHERE id=?", (lid,)
    ).fetchone()[0]
    r2 = db.begin_run(conn, "usaspending:16.071", "2026-07-22T00:00:00+00:00")
    db.complete_run(conn, r2, RunStats(source="usaspending:16.071"),
                    [("usaspending:16.071", "A1")])
    second = conn.execute(
        "SELECT last_confirmed_run_id FROM leads WHERE id=?", (lid,)
    ).fetchone()[0]
    assert first == r1 and second == r2 and r2 > r1
