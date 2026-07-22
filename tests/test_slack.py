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
        (
            "usaspending:16.071",
            "G1",
            "Castle Rock SD 401",
            500_000.0,
            "2025-10-01",
            "2028-09-30",
            LeadGrade.GOLD,
        ),
        (
            "sam.gov",
            "S1",
            "JBLM Security Cameras",
            None,
            "2026-07-01",
            "2026-07-22",
            LeadGrade.SILVER,
        ),
        (
            "usaspending:16.071",
            "E1",
            "Tustin USD",
            250_000.0,
            "2023-10-01",
            "2026-09-30",
            LeadGrade.GOLD,
        ),  # expires within 90 days of 'now'
    ]
    for source, iid, entity, amount, start, end, grade_ in rows:
        event_type = (
            FundingEventType.RFP_POSTED
            if grade_ == LeadGrade.SILVER
            else FundingEventType.AWARD_OBLIGATED
        )
        db.upsert_lead(
            conn,
            Lead(
                item=RawItem(
                    source=source,
                    item_id=iid,
                    title="t",
                    entity=entity,
                    state="WA",
                    program="SVPP",
                    amount=amount,
                    start=start,
                    end=end,
                    url="https://x.gov/a",
                    raw={},
                    event_type=event_type,
                    event_date=start,
                    date_precision=DatePrecision.DAY,
                    verification_status=VerificationStatus.VERIFIED,
                ),
                grade=grade_,
            ),
        )
    return conn


# ------------------------------------------------------------------ outreach draft
def test_draft_is_honest(tmp_path: Path) -> None:
    """Verify draft is honest."""
    conn = _seeded_conn(tmp_path)
    lead_id = conn.execute(
        "SELECT id FROM leads WHERE entity_name LIKE 'Castle%'"
    ).fetchone()["id"]
    row = db.get_lead(conn, lead_id)  # the joined shape production actually passes
    draft = persequor.compose_draft(row)
    assert "Monarch Connected" in draft  # sender identified
    assert "unsubscribe" in draft  # opt-out present
    assert "$500,000" in draft  # real award figure
    assert "[RECIPIENT" in draft  # no fabricated contact
    assert "2028-09-30" in draft  # real spend window
    assert "Congratulations" not in draft  # record existence != relationship


def test_silver_rfp_draft_describes_a_solicitation(tmp_path: Path) -> None:
    """A SILVER lead whose event is an RFP is a solicitation — wording follows the
    event, and this case happens to agree with the old grade-driven wording."""
    conn = _seeded_conn(tmp_path)
    lead_id = conn.execute(
        """SELECT l.id FROM leads l JOIN funding_events e ON e.id=l.current_event_id
           WHERE l.lead_grade='silver' AND e.event_type='rfp_posted'"""
    ).fetchone()["id"]
    draft = persequor.compose_draft(db.get_lead(conn, lead_id))
    assert "solicitation" in draft
    assert "response deadline" in draft
    assert "award" not in draft.lower()


def test_silver_award_draft_never_calls_an_award_a_solicitation(
    tmp_path: Path,
) -> None:
    """THE C1 REGRESSION. A SILVER lead whose event is an AWARD must still be described
    as an award. The old wording keyed off `lead_grade`, so when ~351 undated California
    awards were regraded GOLD->SILVER on 2026-07-22 every one of them would have been
    described to a school administrator as having "published a solicitation", with the
    award's SPEND-WINDOW end relabelled a "response deadline". Two false claims, in a
    real email."""
    conn = _seeded_conn(tmp_path)
    lead_id = conn.execute(
        """SELECT l.id FROM leads l JOIN funding_events e ON e.id=l.current_event_id
           WHERE e.event_type IN ('award_announced','award_obligated') LIMIT 1"""
    ).fetchone()["id"]
    conn.execute("UPDATE leads SET lead_grade='silver' WHERE id=?", (lead_id,))
    conn.commit()
    draft = persequor.compose_draft(db.get_lead(conn, lead_id))
    assert "solicitation" not in draft.lower(), draft
    assert "response deadline" not in draft.lower(), draft
    assert "spend window" in draft.lower(), draft


def test_draft_without_a_joined_event_claims_nothing(tmp_path: Path) -> None:
    """A bare `leads` row degrades to wording that asserts no award, no solicitation
    and no deadline — never a crash, and never an inference from the grade."""
    conn = _seeded_conn(tmp_path)
    row = conn.execute("SELECT * FROM leads LIMIT 1").fetchone()
    draft = persequor.compose_draft(row)
    assert "solicitation" not in draft.lower()
    assert "deadline" not in draft.lower()
    assert "spend window" not in draft.lower()


def test_bad_lead_reason_lands_in_status_note(tmp_path: Path) -> None:
    """Verify bad lead reason lands in status note."""
    conn = _seeded_conn(tmp_path)
    lead_id = conn.execute("SELECT id FROM leads LIMIT 1").fetchone()[0]
    db.set_lead_status(conn, lead_id, "dead", note="money is for software, not cameras")
    row = db.get_lead(conn, lead_id)
    assert row["status"] == "dead"
    assert "software" in row["status_note"]


def test_orphaned_spinner_sweep_finalizes_stale_progress_messages() -> None:
    """A crashed turn's spinner is edited into an honest interruption notice.

    Chase's rule (2026-07-18): the SYSTEM notices a stall — a rep must never
    stare at "/ Searching for the contact…" forever after a restart."""
    import time

    from grant_watch.slack import grant as grant_mod

    now = time.time()

    class FakeClient:
        """Minimal Slack client covering the sweep's four calls."""

        def __init__(self) -> None:
            """Seed one stale spinner, one fresh spinner, one human message."""
            self.updated: list[tuple[str, str]] = []
            self._history = {
                "messages": [
                    {"user": "UBOT", "ts": f"{now - 600:.6f}", "text": "/ Searching for the contact…", "reply_count": 0},
                    {"user": "UBOT", "ts": f"{now - 10:.6f}", "text": "| Thinking…", "reply_count": 0},
                    {"user": "UHUMAN", "ts": f"{now - 900:.6f}", "text": "/ Searching for the contact…", "reply_count": 0},
                ]
            }

        def auth_test(self) -> dict[str, str]:
            """Identify the bot user."""
            return {"user_id": "UBOT"}

        def conversations_history(self, **_k: object) -> dict[str, object]:
            """Return the seeded channel history."""
            return self._history

        def conversations_replies(self, **_k: object) -> dict[str, object]:
            """No thread replies in this scenario."""
            return {"messages": []}

        def chat_update(self, channel: str, ts: str, text: str) -> None:
            """Record the finalization edit."""
            self.updated.append((ts, text))

    client = FakeClient()
    fixed = grant_mod.sweep_orphaned_spinners(client, "C123")
    assert fixed == 1
    assert len(client.updated) == 1
    ts, text = client.updated[0]
    assert abs(float(ts) - (now - 600)) < 1  # only the STALE bot spinner
    assert "interrupted" in text
