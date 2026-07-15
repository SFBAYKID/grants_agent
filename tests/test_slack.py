"""Phase 3 tests: digest block building, the honest outreach draft, the approval
workflow's DB transitions, and digest surfacing. All pure/offline — Slack itself is
never contacted here (live posting is verified manually via the CLI)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from grant_watch import db
from grant_watch.models import (
    DatePrecision,
    FundingEventType,
    Lead,
    LeadGrade,
    RawItem,
    VerificationStatus,
)
from grant_watch.slack import digest, persequor


def _seeded_conn(tmp_path: Path):
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


# ------------------------------------------------------------------ digest blocks
def test_digest_blocks_have_buttons_and_ids(tmp_path: Path) -> None:
    conn = _seeded_conn(tmp_path)
    blocks, shown = digest.build_digest_blocks(db.digest_leads(conn))
    assert len(shown) >= 3
    actions = [b for b in blocks if b["type"] == "actions"]
    assert actions, "every lead must carry triage buttons"
    ids = {e["action_id"] for b in actions for e in b["elements"]}
    assert ids == {"grant_draft_email", "grant_mark_contacted",
                   "grant_snooze", "grant_bad_lead"}
    assert len(blocks) <= 50, "Slack hard-caps messages at 50 blocks"


def test_digest_empty_db_says_all_quiet(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "empty.db")
    blocks, shown = digest.build_digest_blocks(db.digest_leads(conn))
    assert shown == []
    assert "all quiet" in str(blocks)


def test_digest_caps_are_summarized_not_silent(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "big.db")
    for i in range(digest.GOLD_CAP + 5):
        db.upsert_lead(conn, Lead(
            item=RawItem(source="usaspending:16.071", item_id=f"G{i}", title="t",
                         entity=f"District {i}", state="WA", program="SVPP",
                         amount=100_000.0, start="2025-10-01", end="2028-09-30",
                         url="", raw={}, event_type=FundingEventType.AWARD_OBLIGATED,
                         event_date="2026-07-01", date_precision=DatePrecision.DAY,
                         verification_status=VerificationStatus.VERIFIED),
            grade=LeadGrade.GOLD))
    blocks, shown = digest.build_digest_blocks(db.digest_leads(conn))
    assert len(shown) == digest.GOLD_CAP
    assert "+5 more in the database" in str(blocks)  # overflow declared, not dropped


def test_digest_prioritizes_and_links_fresh_salesforce_opportunity(
        tmp_path: Path) -> None:
    """Weekly output follows the same CRM-first priority as proactive drips."""
    conn = _seeded_conn(tmp_path)
    db.upsert_lead(conn, Lead(
        item=RawItem(source="usaspending:16.071", item_id="NET", title="t",
                     entity="Net New District", state="WA", program="SVPP",
                     amount=900_000.0, start="2026-07-01", end="2028-09-30",
                     url="", raw={}, event_type=FundingEventType.AWARD_OBLIGATED,
                     event_date="2026-07-01", date_precision=DatePrecision.DAY,
                     verification_status=VerificationStatus.VERIFIED),
        grade=LeadGrade.GOLD))
    sf_lead = int(conn.execute(
        "SELECT id FROM leads WHERE source_item_id='G1'").fetchone()["id"])
    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO salesforce_lookup_state(lead_id,status,checked_at)
           VALUES (?,'found',?)""", (sf_lead, checked_at))
    conn.execute(
        """INSERT INTO salesforce_matches
             (lead_id,sobject,record_id,name,owner,link,confidence,account_id,
              stage,is_closed,checked_at)
           VALUES (?,'Opportunity','006SF','Security Upgrade','Anthony',
                   'https://sf.test/006SF','high','001SF','Prospecting',0,?)""",
        (sf_lead, checked_at))
    conn.commit()
    blocks, shown = digest.build_digest_blocks(db.digest_leads(conn))
    assert shown[0] == sf_lead
    assert "https://sf.test/006SF" in str(blocks)
    assert "Anthony" in str(blocks)


def test_post_digest_dry_run_writes_nothing(tmp_path: Path, capsys) -> None:
    conn = _seeded_conn(tmp_path)
    n = digest.post_digest(None, "C000", conn, dry_run=True)  # no client needed dry
    assert n >= 3
    assert "[dry-run]" in capsys.readouterr().out
    still_new = conn.execute("SELECT COUNT(*) FROM leads WHERE status='new'").fetchone()[0]
    assert still_new == 3  # statuses untouched


class _DigestSlack:
    """Offline digest client with optional ambiguous delivery failure."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def chat_postMessage(self, **_kwargs: object) -> dict[str, str]:  # noqa: N802
        """Return a confirmed timestamp or model a network timeout."""
        self.calls += 1
        if self.fail:
            raise TimeoutError("ambiguous")
        return {"ts": "300.1"}


def test_digest_delivery_is_reserved_before_post(tmp_path: Path) -> None:
    """Repeated same-day runs cannot post the same weekly digest twice."""
    conn = _seeded_conn(tmp_path)
    client = _DigestSlack()
    first = digest.post_digest(client, "CGRANTS", conn)
    second = digest.post_digest(client, "CGRANTS", conn)
    assert first >= 3 and second == 0
    assert client.calls == 1
    assert conn.execute(
        "SELECT state FROM notification_outbox").fetchone()["state"] == "delivered"


def test_digest_timeout_is_unknown_and_never_blindly_retried(tmp_path: Path) -> None:
    """An ambiguous timeout reserves the date and blocks a duplicate retry."""
    conn = _seeded_conn(tmp_path)
    client = _DigestSlack(fail=True)
    with pytest.raises(TimeoutError):
        digest.post_digest(client, "CGRANTS", conn)
    assert digest.post_digest(client, "CGRANTS", conn) == 0
    assert client.calls == 1
    assert conn.execute(
        "SELECT state FROM notification_outbox").fetchone()["state"] == "unknown"


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
