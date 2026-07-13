"""Storage tests: schema creation, dedup upserts, idempotent CSV seeding, run logging.
Each test builds its own throwaway DB (tmp_path) — no shared state (CLAUDE.md rule 3)."""

from __future__ import annotations

from pathlib import Path

from grant_watch import db
from grant_watch.models import Lead, LeadGrade, RawItem, RunStats

SEED_CSV = Path(__file__).resolve().parent.parent / "data" / "svpp_active_awards_CA_MI_PA_WA.csv"


def _lead(item_id: str = "A1", source: str = "usaspending:16.071") -> Lead:
    return Lead(
        item=RawItem(source=source, item_id=item_id, title="SVPP award",
                     entity="Castle Rock SD 401", state="WA", program="SVPP",
                     amount=500_000.0, start="2025-10-01", end="2028-09-30",
                     url="https://example.gov/a1", raw={"k": "v"}),
        grade=LeadGrade.GOLD,
    )


def test_upsert_dedups_on_source_and_item_id(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "t.db")
    assert db.upsert_lead(conn, _lead()) is True      # first sight -> new
    assert db.upsert_lead(conn, _lead()) is False     # same item -> dedup
    # Same item id under a DIFFERENT source must NOT collide (the CFDA-split rule).
    assert db.upsert_lead(conn, _lead(source="usaspending:16.710")) is True
    assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 2


def test_upsert_refreshes_last_seen(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "t.db")
    db.upsert_lead(conn, _lead())
    first = conn.execute("SELECT first_seen, last_seen FROM leads").fetchone()
    db.upsert_lead(conn, _lead())
    second = conn.execute("SELECT first_seen, last_seen FROM leads").fetchone()
    assert second[0] == first[0]          # first_seen never moves
    assert second[1] >= first[1]          # last_seen refreshed


def test_seed_is_idempotent(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "t.db")
    rows, new = db.seed_from_csv(conn, SEED_CSV)
    assert rows == 75 and new == 75       # the verified CSV: 75 GOLD awards
    rows2, new2 = db.seed_from_csv(conn, SEED_CSV)
    assert rows2 == 75 and new2 == 0      # re-seeding inserts nothing
    grades = {g for (g,) in conn.execute("SELECT DISTINCT lead_grade FROM leads")}
    assert grades == {"gold"}


def test_run_logging(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "t.db")
    db.log_run(conn, "2026-07-13T00:00:00+00:00",
               RunStats(source="TestSource", items_seen=5, items_new=2, errors=""))
    row = conn.execute("SELECT source, items_seen, items_new FROM runs").fetchone()
    assert row == ("TestSource", 5, 2)
