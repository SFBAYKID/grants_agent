"""Offline tests for honest outreach drafts and Slack workflow DB transitions."""

from __future__ import annotations

import sqlite3
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
from grant_watch.slack import persequor


def _seeded_conn(tmp_path: Path) -> sqlite3.Connection:
    """A throwaway DB with one open-window GOLD, one SILVER, one expiring GOLD."""
    conn = db.connect(tmp_path / "t.db")
    rows = [
        ("usaspending:16.071", "G1", "Castle Rock SD 401", 500_000.0,
         "2025-10-01", "2028-09-30", LeadGrade.GOLD),
        ("sam.gov", "S1", "JBLM Security Cameras", None,
         "2026-07-01", "2026-07-22", LeadGrade.SILVER),
        ("usaspending:16.071", "E1", "Tustin USD", 250_000.0,
         "2023-10-01", "2026-09-30", LeadGrade.GOLD),  # expires within 90 days of 'now'
    ]
    for source, iid, entity, amount, start, end, grade_ in rows:
        event_type = (FundingEventType.RFP_POSTED if grade_ == LeadGrade.SILVER
                      else FundingEventType.AWARD_OBLIGATED)
        db.upsert_lead(conn, Lead(
            item=RawItem(source=source, item_id=iid, title="t", entity=entity,
                         state="WA", program="SVPP", amount=amount,
                         start=start, end=end, url="https://x.gov/a", raw={},
                         event_type=event_type, event_date=start,
                         date_precision=DatePrecision.DAY,
                         verification_status=VerificationStatus.VERIFIED),
            grade=grade_))
    return conn


# ------------------------------------------------------------------ outreach draft
def test_draft_is_honest(tmp_path: Path) -> None:
    conn = _seeded_conn(tmp_path)
    row = conn.execute("SELECT * FROM leads WHERE entity_name LIKE 'Castle%'").fetchone()
    draft = persequor.compose_draft(row)
    assert "Monarch Connected" in draft            # sender identified
    assert "unsubscribe" in draft                  # opt-out present
    assert "$500,000" in draft                     # real award figure
    assert "[RECIPIENT" in draft                   # no fabricated contact
    assert "2028-09-30" in draft                   # real spend window
    assert "Congratulations" not in draft           # record existence != relationship


def test_silver_draft_describes_solicitation_not_award(tmp_path: Path) -> None:
    """An RFP fallback never congratulates the issuer for receiving an award."""
    conn = _seeded_conn(tmp_path)
    row = conn.execute("SELECT * FROM leads WHERE lead_grade='silver'").fetchone()
    draft = persequor.compose_draft(row)
    assert "solicitation" in draft
    assert "response deadline" in draft
    assert "award" not in draft.lower()


def test_bad_lead_reason_lands_in_status_note(tmp_path: Path) -> None:
    conn = _seeded_conn(tmp_path)
    lead_id = conn.execute("SELECT id FROM leads LIMIT 1").fetchone()[0]
    db.set_lead_status(conn, lead_id, "dead", note="money is for software, not cameras")
    row = db.get_lead(conn, lead_id)
    assert row["status"] == "dead"
    assert "software" in row["status_note"]
