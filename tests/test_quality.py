"""Tests for the two live-digest fixes of 2026-07-13: seed-vs-live duplicate
reconciliation, and the quality-gate ranking (strongest leads first)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from grant_watch import db
from grant_watch.models import Lead, LeadGrade, RawItem
from grant_watch.scoring import lead_score
from grant_watch.slack.digest import _rank

TODAY = date(2026, 7, 13)


def _mk(conn, source: str, iid: str, entity: str, amount: float | None,
        start: str, end: str, grade_: LeadGrade = LeadGrade.GOLD,
        program: str = "SVPP") -> None:
    db.upsert_lead(conn, Lead(
        item=RawItem(source=source, item_id=iid, title="t", entity=entity,
                     state="WA", program=program, amount=amount, start=start,
                     end=end, url="", raw={}),
        grade=grade_))


# ------------------------------------------------------------ reconcile duplicates
def test_reconcile_retires_seed_twin_keeps_live(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "t.db")
    _mk(conn, "seed:svpp_csv", "castle~FY25", "Castle Rock SD 401", 500_000.0,
        "2025-10-01", "2028-09-30")
    _mk(conn, "usaspending:16.071", "AWD1", "CASTLE ROCK SD 401", 500_000.0,
        "2025-10-01", "2028-09-30")  # same award, live source, different case
    assert db.reconcile_seed_duplicates(conn) == 1
    seed = conn.execute("SELECT status, status_note FROM leads "
                        "WHERE source='seed:svpp_csv'").fetchone()
    live = conn.execute("SELECT status FROM leads "
                        "WHERE source='usaspending:16.071'").fetchone()
    assert seed["status"] == "dead" and "superseded" in seed["status_note"]
    assert live["status"] == "new"                      # live row untouched
    assert db.reconcile_seed_duplicates(conn) == 0      # idempotent


def test_reconcile_spares_seed_without_live_twin(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "t.db")
    _mk(conn, "seed:svpp_csv", "lonely~FY25", "Lonely District", 250_000.0,
        "2025-10-01", "2028-09-30")
    _mk(conn, "usaspending:16.071", "AWD2", "Different District", 250_000.0,
        "2025-10-01", "2028-09-30")  # same money, different entity -> no match
    assert db.reconcile_seed_duplicates(conn) == 0
    assert conn.execute("SELECT status FROM leads WHERE source='seed:svpp_csv'"
                        ).fetchone()["status"] == "new"


def test_retired_seed_leaves_digest(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "t.db")
    _mk(conn, "seed:svpp_csv", "castle~FY25", "Castle Rock SD 401", 500_000.0,
        "2025-10-01", "2026-09-30")                     # would hit 'expiring' bucket
    _mk(conn, "usaspending:16.071", "AWD1", "Castle Rock SD 401", 500_000.0,
        "2025-10-01", "2026-09-30")
    db.reconcile_seed_duplicates(conn)
    expiring = db.digest_leads(conn)["expiring"]
    assert len(expiring) == 1                           # the live row only
    assert expiring[0]["source"] == "usaspending:16.071"


# ------------------------------------------------------------ quality-gate ranking
def test_freshness_dominates_dollars() -> None:
    # Chase: 'freshness is everything' — old $500K must rank below fresh $100K.
    old_big = lead_score("SVPP", 500_000.0, "2023-07-01", TODAY)
    fresh_small = lead_score("SVPP", 100_000.0, "2026-06-01", TODAY)
    assert fresh_small > old_big


def test_program_fit_downranks_software_heavy() -> None:
    svpp = lead_score("SVPP", 500_000.0, "2026-06-01", TODAY)
    stop = lead_score("STOP", 500_000.0, "2026-06-01", TODAY)
    assert svpp > stop  # STOP skews software/threat-assessment (FINDINGS)


def test_rank_orders_digest_rows(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "t.db")
    _mk(conn, "usaspending:16.071", "OLD", "Old Big District", 500_000.0,
        "2022-10-01", "2028-09-30")
    _mk(conn, "usaspending:16.071", "FRESH", "Fresh District", 150_000.0,
        "2026-05-01", "2029-09-30")
    ranked = _rank(db.digest_leads(conn)["gold"])
    assert ranked[0]["entity_name"] == "Fresh District"


def test_unknown_start_never_outranks_known_fresh() -> None:
    assert lead_score("SVPP", 500_000.0, "", TODAY) < lead_score(
        "SVPP", 500_000.0, "2026-06-01", TODAY)
