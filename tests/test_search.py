"""On-demand search tool: filters, org-type name matching, export, safety."""

from __future__ import annotations

from pathlib import Path

from openpyxl import load_workbook

from grant_watch import db
from grant_watch.models import Lead, LeadGrade, RawItem
from grant_watch.slack.tools import search_leads


def _db(tmp_path: Path):
    conn = db.connect(tmp_path / "s.db")
    rows = [
        ("usaspending:16.071", "A1", "Tustin Unified School District", "CA", "SVPP",
         500_000.0, "2025-10-01", "2026-09-30", LeadGrade.GOLD),
        ("usaspending:16.071", "A2", "City of Austin", "TX", "NSGP",
         120_000.0, "2025-11-01", "2028-09-30", LeadGrade.GOLD),
        ("usaspending:16.071", "A3", "Fresno County", "CA", "STOP",
         80_000.0, "2024-01-01", "2025-01-01", LeadGrade.WATCH),
        ("sam.gov", "A4", "Small Charter Academy", "CA", "RFP:sam.gov",
         None, "2026-07-01", "2026-08-01", LeadGrade.SILVER),
    ]
    for src, iid, name, st, prog, amt, s, e, g in rows:
        db.upsert_lead(conn, Lead(item=RawItem(source=src, item_id=iid, title="t",
                                               entity=name, state=st, program=prog,
                                               amount=amt, start=s, end=e,
                                               url="https://x.gov/a", raw={}), grade=g))
    return tmp_path / "s.db"


def test_filter_by_state_and_grade(tmp_path: Path) -> None:
    text, _ = search_leads(state="CA", grade="gold", db_path=_db(tmp_path))
    assert "Tustin" in text and "Austin" not in text  # TX excluded


def test_org_type_school_matches_by_name(tmp_path: Path) -> None:
    text, _ = search_leads(state="CA", org_type="school", db_path=_db(tmp_path))
    assert "Tustin Unified School District" in text
    assert "Charter Academy" in text          # 'academy' pattern
    assert "Fresno County" not in text        # county, not school


def test_org_type_city(tmp_path: Path) -> None:
    text, _ = search_leads(org_type="city", db_path=_db(tmp_path))
    assert "Austin" in text and "Tustin" not in text


def test_amount_filter(tmp_path: Path) -> None:
    text, _ = search_leads(amount_min=200_000, db_path=_db(tmp_path))
    assert "Tustin" in text and "Austin" not in text  # 120k excluded


def test_export_produces_xlsx(tmp_path: Path) -> None:
    text, path = search_leads(state="CA", export=True, db_path=_db(tmp_path))
    assert path is not None and "Found" in text
    wb = load_workbook(path)
    assert wb.active["A1"].value == "entity_name"   # header row
    Path(path).unlink()


def test_no_match_is_honest(tmp_path: Path) -> None:
    text, path = search_leads(state="ZZ", db_path=_db(tmp_path))
    assert "No grants matched" in text and path is None


def test_dead_leads_excluded(tmp_path: Path) -> None:
    dbp = _db(tmp_path)
    conn = db.connect(dbp)
    conn.execute("UPDATE leads SET status='dead' WHERE source_item_id='A1'")
    conn.commit()
    text, _ = search_leads(state="CA", grade="gold", db_path=dbp)
    assert "Tustin" not in text  # dead lead never surfaces in search
