"""Tests for seed/live reconciliation and proactive quality-gate ranking."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from grant_watch import db
from grant_watch.models import (
    DatePrecision,
    FundingEventType,
    Lead,
    LeadGrade,
    RawItem,
    VerificationStatus,
)
from grant_watch.scoring import lead_score
from grant_watch.slack import drip

TODAY = date(2026, 7, 13)


def _mk(
    conn: sqlite3.Connection,
    source: str,
    iid: str,
    entity: str,
    amount: float | None,
    start: str,
    end: str,
    grade_: LeadGrade = LeadGrade.GOLD,
    program: str = "SVPP",
    event_date: str = "2026-06-01",
) -> None:
    """Provide test-local behavior for mk."""
    is_seed = source.startswith("seed:")
    db.upsert_lead(
        conn,
        Lead(
            item=RawItem(
                source=source,
                item_id=iid,
                title="t",
                entity=entity,
                state="WA",
                program=program,
                amount=amount,
                start=start,
                end=end,
                url="",
                raw={},
                event_type=(
                    FundingEventType.RECORD_OBSERVED
                    if is_seed
                    else FundingEventType.AWARD_OBLIGATED
                ),
                event_date="" if is_seed else event_date,
                date_precision=(
                    DatePrecision.UNKNOWN if is_seed else DatePrecision.DAY
                ),
                verification_status=VerificationStatus.VERIFIED,
                backfill=is_seed,
            ),
            grade=grade_,
        ),
    )


# ------------------------------------------------------------ reconcile duplicates
def test_reconcile_retires_seed_twin_keeps_live(tmp_path: Path) -> None:
    """Verify reconcile retires seed twin keeps live."""
    conn = db.connect(tmp_path / "t.db")
    _mk(
        conn,
        "seed:svpp_csv",
        "castle~FY25",
        "Castle Rock SD 401",
        500_000.0,
        "2025-10-01",
        "2028-09-30",
    )
    _mk(
        conn,
        "usaspending:16.071",
        "AWD1",
        "CASTLE ROCK SD 401",
        500_000.0,
        "2025-10-01",
        "2028-09-30",
    )  # same award, live source, different case
    assert db.reconcile_seed_duplicates(conn) == 1
    seed = conn.execute(
        "SELECT status, status_note FROM leads WHERE source='seed:svpp_csv'"
    ).fetchone()
    live = conn.execute(
        "SELECT status FROM leads WHERE source='usaspending:16.071'"
    ).fetchone()
    assert seed["status"] == "dead" and "superseded" in seed["status_note"]
    assert live["status"] == "new"  # live row untouched
    assert db.reconcile_seed_duplicates(conn) == 0  # idempotent


def test_reconcile_spares_seed_without_live_twin(tmp_path: Path) -> None:
    """Verify reconcile spares seed without live twin."""
    conn = db.connect(tmp_path / "t.db")
    _mk(
        conn,
        "seed:svpp_csv",
        "lonely~FY25",
        "Lonely District",
        250_000.0,
        "2025-10-01",
        "2028-09-30",
    )
    _mk(
        conn,
        "usaspending:16.071",
        "AWD2",
        "Different District",
        250_000.0,
        "2025-10-01",
        "2028-09-30",
    )  # same money, different entity -> no match
    assert db.reconcile_seed_duplicates(conn) == 0
    assert (
        conn.execute(
            "SELECT status FROM leads WHERE source='seed:svpp_csv'"
        ).fetchone()["status"]
        == "new"
    )


def test_retired_seed_leaves_proactive_candidates(tmp_path: Path) -> None:
    """Verify retired seed leaves proactive candidates."""
    conn = db.connect(tmp_path / "t.db")
    _mk(
        conn,
        "seed:svpp_csv",
        "castle~FY25",
        "Castle Rock SD 401",
        500_000.0,
        "2025-10-01",
        "2026-09-30",
    )  # would hit 'expiring' bucket
    _mk(
        conn,
        "usaspending:16.071",
        "AWD1",
        "Castle Rock SD 401",
        500_000.0,
        "2025-10-01",
        "2026-09-30",
    )
    db.reconcile_seed_duplicates(conn)
    candidates = db.nugget_candidates(conn, "C1")
    assert len(candidates) == 1  # the live row only
    assert candidates[0]["source"] == "usaspending:16.071"


# ------------------------------------------------------------ quality-gate ranking
def test_freshness_dominates_dollars() -> None:
    # Chase: 'freshness is everything' — old $500K must rank below fresh $100K.
    """Verify freshness dominates dollars."""
    old_big = lead_score("SVPP", 500_000.0, "2023-07-01", TODAY)
    fresh_small = lead_score("SVPP", 100_000.0, "2026-06-01", TODAY)
    assert fresh_small > old_big


def test_program_fit_downranks_software_heavy() -> None:
    """Verify program fit downranks software heavy."""
    svpp = lead_score("SVPP", 500_000.0, "2026-06-01", TODAY)
    stop = lead_score("STOP", 500_000.0, "2026-06-01", TODAY)
    assert svpp > stop  # STOP skews software/threat-assessment (FINDINGS)


def test_proactive_pick_orders_rows_by_freshness(tmp_path: Path) -> None:
    """Verify proactive pick orders rows by freshness."""
    conn = db.connect(tmp_path / "t.db")
    _mk(
        conn,
        "usaspending:16.071",
        "OLD",
        "Old Big District",
        500_000.0,
        "2022-10-01",
        "2028-09-30",
        event_date="2022-10-01",
    )
    _mk(
        conn,
        "usaspending:16.071",
        "FRESH",
        "Fresh District",
        150_000.0,
        "2026-05-01",
        "2029-09-30",
        event_date="2026-05-01",
    )
    choice = drip.pick(conn, "C1")
    assert choice is not None
    assert choice[1]["entity_name"] == "Fresh District"


def test_unknown_start_never_outranks_known_fresh() -> None:
    """Verify unknown start never outranks known fresh."""
    assert lead_score("SVPP", 500_000.0, "", TODAY) < lead_score(
        "SVPP", 500_000.0, "2026-06-01", TODAY
    )
