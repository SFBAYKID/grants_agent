"""Phase 3 tests: digest block building, the honest outreach draft, the approval
workflow's DB transitions, and digest surfacing. All pure/offline — Slack itself is
never contacted here (live posting is verified manually via the CLI)."""

from __future__ import annotations

from pathlib import Path

from grant_watch import db
from grant_watch.models import Lead, LeadGrade, RawItem
from grant_watch.slack import digest, persequor


def _seeded_conn(tmp_path: Path):
    """A throwaway DB with one open-window GOLD, one SILVER, one expiring GOLD."""
    conn = db.connect(tmp_path / "t.db")
    rows = [
        ("usaspending:16.071", "G1", "Castle Rock SD 401", 500_000.0,
         "2025-10-01", "2028-09-30", LeadGrade.GOLD),
        ("sam.gov", "S1", "JBLM Security Cameras", None,
         "2026-07-01", "2026-07-22", LeadGrade.SILVER),
        ("seed:svpp_csv", "E1", "Tustin USD", 250_000.0,
         "2023-10-01", "2026-09-30", LeadGrade.GOLD),  # expires within 90 days of 'now'
    ]
    for source, iid, entity, amount, start, end, grade_ in rows:
        db.upsert_lead(conn, Lead(
            item=RawItem(source=source, item_id=iid, title="t", entity=entity,
                         state="WA", program="SVPP", amount=amount,
                         start=start, end=end, url="https://x.gov/a", raw={}),
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
                         url="", raw={}),
            grade=LeadGrade.GOLD))
    blocks, shown = digest.build_digest_blocks(db.digest_leads(conn))
    assert len(shown) == digest.GOLD_CAP
    assert "+5 more in the database" in str(blocks)  # overflow declared, not dropped


def test_post_digest_dry_run_writes_nothing(tmp_path: Path, capsys) -> None:
    conn = _seeded_conn(tmp_path)
    n = digest.post_digest(None, "C000", conn, dry_run=True)  # no client needed dry
    assert n >= 3
    assert "[dry-run]" in capsys.readouterr().out
    still_new = conn.execute("SELECT COUNT(*) FROM leads WHERE status='new'").fetchone()[0]
    assert still_new == 3  # statuses untouched


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


def test_handoff_text_mentions_persequor_and_approver() -> None:
    text = persequor.build_handoff_text("Castle Rock SD 401", "U123", "DRAFT BODY")
    assert "@Persequor" in text or "<@" in text
    assert "<@U123>" in text
    assert "DRAFT BODY" in text


# ------------------------------------------------------------------ workflow states
def test_outreach_approval_records_gate(tmp_path: Path) -> None:
    conn = _seeded_conn(tmp_path)
    lead_id = conn.execute("SELECT id FROM leads LIMIT 1").fetchone()[0]
    oid = db.create_outreach(conn, lead_id, "draft body")
    row = conn.execute("SELECT approved_by, sent_at FROM outreach WHERE id=?", (oid,)).fetchone()
    assert row["approved_by"] is None and row["sent_at"] is None  # gate closed
    db.approve_outreach(conn, oid, "U123")
    row = conn.execute("SELECT approved_by, sent_at FROM outreach WHERE id=?", (oid,)).fetchone()
    assert row["approved_by"] == "U123" and row["sent_at"] is not None


def test_bad_lead_reason_lands_in_status_note(tmp_path: Path) -> None:
    conn = _seeded_conn(tmp_path)
    lead_id = conn.execute("SELECT id FROM leads LIMIT 1").fetchone()[0]
    db.set_lead_status(conn, lead_id, "dead", note="money is for software, not cameras")
    row = db.get_lead(conn, lead_id)
    assert row["status"] == "dead"
    assert "software" in row["status_note"]
