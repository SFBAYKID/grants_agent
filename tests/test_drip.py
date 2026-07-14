"""Drip-engine tests: window math, pacing gates, message builders, claims,
engagement dedupe. All offline; the LLM layer is not exercised here (its failure
mode is tested by contract: bad output degrades to an honest 'didn't parse')."""

from __future__ import annotations

import random
from datetime import datetime, timezone
from pathlib import Path

from grant_watch import db
from grant_watch.models import Lead, LeadGrade, RawItem
from grant_watch.slack import drip


def _mk_lead(conn, iid: str = "A1", entity: str = "Castle Rock School District 401",
             grade: LeadGrade = LeadGrade.GOLD, source: str = "usaspending:16.071",
             amount: float | None = 500_000.0, start: str = "2025-10-01",
             end: str = "2028-09-30", title: str = "SVPP award") -> int:
    db.upsert_lead(conn, Lead(
        item=RawItem(source=source, item_id=iid, title=title, entity=entity,
                     state="WA", program="SVPP", amount=amount, start=start,
                     end=end, url="https://x.gov/a", raw={}),
        grade=grade))
    return int(conn.execute("SELECT id FROM leads WHERE source_item_id=?",
                            (iid,)).fetchone()["id"])


# ------------------------------------------------------------------ window
def test_window_monday_morning_et_ok() -> None:
    # 13:30 UTC Monday = 8:30 ET / 5:30 PT (summer) -> inside
    assert drip.in_window(datetime(2026, 7, 13, 13, 30, tzinfo=timezone.utc))


def test_window_before_8am_et_closed() -> None:
    # 11:00 UTC = 7:00 ET -> outside
    assert not drip.in_window(datetime(2026, 7, 13, 11, 0, tzinfo=timezone.utc))


def test_window_after_5pm_pt_closed() -> None:
    # 00:30 UTC Tue = Mon 17:30 PT -> outside
    assert not drip.in_window(datetime(2026, 7, 14, 0, 30, tzinfo=timezone.utc))


def test_window_weekend_closed() -> None:
    assert not drip.in_window(datetime(2026, 7, 11, 16, 0, tzinfo=timezone.utc))  # Sat


# ------------------------------------------------------------------ pacing
def test_daily_cap_blocks(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "t.db")
    for i in range(drip.DAILY_CAP):
        db.record_post(conn, "nugget", None, "C1", f"111.{i}", "s")
    go, reason = drip.should_post(conn, "C1",
                                  datetime(2026, 7, 13, 15, 0, tzinfo=timezone.utc),
                                  random.Random(1))
    assert not go and "cap" in reason


def test_min_gap_blocks(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "t.db")
    # a post 30 real minutes ago (posts_today sees it; gap 30m < 90m)
    thirty_ago = (datetime.now(timezone.utc)
                  .replace(microsecond=0)).isoformat()
    conn.execute("INSERT INTO posts (kind, channel, ts, style, posted_at) "
                 "VALUES ('nugget','C1','111.0','s', ?)",
                 (thirty_ago,))
    conn.commit()
    now = datetime.now(timezone.utc)
    go, reason = drip.pacing_ok(conn, "C1", now, random.Random(1))
    assert not go and "since last post" in reason


def test_jitter_skip_when_rng_high(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "t.db")

    class AlwaysHigh(random.Random):
        def random(self) -> float:  # forces the jitter branch deterministically
            return 0.99

    go, reason = drip.pacing_ok(conn, "C1", datetime.now(timezone.utc), AlwaysHigh())
    assert not go and "jitter" in reason


def test_force_bypasses_everything(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "t.db")
    go, reason = drip.should_post(conn, "C1",
                                  datetime(2026, 7, 11, 3, 0, tzinfo=timezone.utc),
                                  random.Random(1), force=True)
    assert go and reason == "forced"


# ------------------------------------------------------------------ builders
def test_nugget_is_short_and_factual(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "t.db")
    lead_id = _mk_lead(conn)
    row = db.get_lead(conn, lead_id)
    text, style = drip.build_nugget(row)
    assert "$500K" in text
    assert "Castle Rock" in text
    assert text.count(".") <= 3                      # two sentences + maybe a link line
    assert "https://x.gov/a" in text                 # only the REAL link we hold
    assert style in ("ask-me", "window", "worth-a-look")


def test_bulletin_uses_opportunity_title(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "t.db")
    _mk_lead(conn, iid="OPP1", entity="DOJ COPS Office", grade=LeadGrade.WATCH,
             source="grants.gov", amount=None, start="2026-07-01", end="2026-08-04",
             title="FY26 School Violence Prevention Program")
    row = db.bulletin_candidates(conn)[0]
    text, style = drip.build_bulletin(row)
    assert "FY26 School Violence Prevention Program" in text
    assert "closes 2026-08-04" in text
    assert style == "bulletin-open"


def test_pick_prefers_top_scored_nugget(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "t.db")
    _mk_lead(conn, iid="OLD", entity="Old District", start="2022-10-01")
    _mk_lead(conn, iid="FRESH", entity="Fresh District", start="2026-06-01",
             amount=150_000.0)
    kind, row = drip.pick(conn, "C1")
    assert kind == "nugget" and row["entity_name"] == "Fresh District"


def test_bulletin_only_when_no_nuggets(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "t.db")
    _mk_lead(conn, iid="OPP1", entity="DOJ", grade=LeadGrade.WATCH,
             source="grants.gov", amount=None, end="2026-08-04", title="SVPP FY26")
    kind, row = drip.pick(conn, "C1")
    assert kind == "bulletin"


def test_drip_dry_run_writes_nothing(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "t.db")
    _mk_lead(conn)
    out = drip.run_drip(None, "C1", conn, force=True, dry_run=True)
    assert out.startswith("[dry-run] would post nugget")
    assert conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM leads").fetchone()["status"] == "new"


# ------------------------------------------------------------------ claims + points
def test_claim_first_click_wins(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "t.db")
    lead_id = _mk_lead(conn)
    assert db.claim_lead(conn, lead_id, "U_ANTHONY") is True
    assert db.claim_lead(conn, lead_id, "U_BRETT") is False       # loser
    assert db.claim_lead(conn, lead_id, "U_ANTHONY") is False     # no double-claim
    assert db.get_lead(conn, lead_id)["assigned_to"] == "U_ANTHONY"


def test_dead_lead_cannot_be_claimed(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "t.db")
    lead_id = _mk_lead(conn)
    db.set_lead_status(conn, lead_id, "dead", note="x")
    assert db.claim_lead(conn, lead_id, "U1") is False


def test_engagement_dedupes_per_user_and_kind(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "t.db")
    pid = db.record_post(conn, "nugget", None, "C1", "111.1", "ask-me")
    assert db.record_engagement(conn, pid, "U1", "reply") is True
    assert db.record_engagement(conn, pid, "U1", "reply") is False   # same user+kind
    assert db.record_engagement(conn, pid, "U1", "reaction") is True # new kind
    assert db.record_engagement(conn, pid, "U2", "reply") is True    # new user
    assert db.engagement_stats(conn)["total"] == 3
