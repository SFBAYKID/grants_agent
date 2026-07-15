"""Drip-engine tests: window math, pacing gates, message builders, claims,
engagement dedupe. All offline; the LLM layer is not exercised here (its failure
mode is tested by contract: bad output degrades to an honest 'didn't parse')."""

from __future__ import annotations

import random
from datetime import datetime, timezone
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
from grant_watch.slack import drip


def _mk_lead(conn, iid: str = "A1", entity: str = "Castle Rock School District 401",
             grade: LeadGrade = LeadGrade.GOLD, source: str = "usaspending:16.071",
             amount: float | None = 500_000.0, start: str = "2025-10-01",
             end: str = "2028-09-30", title: str = "SVPP award") -> int:
    event_type = (FundingEventType.APPLICATION_WINDOW_OPENED
                  if source in {"grants.gov", "ca-grants-portal"}
                  else FundingEventType.AWARD_OBLIGATED)
    db.upsert_lead(conn, Lead(
        item=RawItem(source=source, item_id=iid, title=title, entity=entity,
                     state="WA", program="SVPP", amount=amount, start=start,
                     end=end, url="https://x.gov/a", raw={}, event_type=event_type,
                     event_date=start, date_precision=DatePrecision.DAY,
                     verification_status=VerificationStatus.VERIFIED),
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
    go, reason = drip.pacing_ok(
        conn, "C1", datetime.now(timezone.utc), random.Random(1))
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
    assert "just got" not in text and "just landed" not in text


def test_unknown_event_date_is_disclosed_as_a_listing(tmp_path: Path) -> None:
    """A source without an award-action date never gets 'just received' wording."""
    conn = db.connect(tmp_path / "t.db")
    lead_id = _mk_lead(conn, start="")
    row = db.get_lead(conn, lead_id)
    assert row is not None
    text, _style = drip.build_nugget(row)
    assert "lists" in text or "Award record worth a look" in text
    assert "just" not in text.lower()


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


def test_needs_testing_event_cannot_enter_proactive_notifications(tmp_path: Path) -> None:
    """An unverified Oregon-style positive remains searchable but is never pushed."""
    conn = db.connect(tmp_path / "t.db")
    lead_id = _mk_lead(conn, iid="OREGON", entity="Oregon Test District")
    conn.execute(
        """UPDATE funding_events SET verification_status='needs-testing'
           WHERE lead_id=?""", (lead_id,))
    conn.commit()
    assert db.nugget_candidates(conn) == []
    assert all(
        lead_id not in {int(row["id"]) for row in rows}
        for rows in db.digest_leads(conn).values())


def test_pick_prioritizes_existing_salesforce_opportunity(tmp_path: Path) -> None:
    """A verified open CRM Opportunity outranks a slightly stronger net-new lead."""
    conn = db.connect(tmp_path / "t.db")
    sf_lead = _mk_lead(conn, iid="SF", entity="Salesforce District",
                       start="2026-05-01", amount=300_000.0)
    _mk_lead(conn, iid="NET", entity="Net New District",
             start="2026-06-01", amount=500_000.0)
    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO salesforce_lookup_state(lead_id,status,checked_at)
           VALUES (?,'found',?)""", (sf_lead, checked_at))
    conn.execute(
        """INSERT INTO salesforce_matches
             (lead_id,sobject,record_id,name,owner,link,confidence,account_id,
              stage,is_closed,checked_at)
           VALUES (?,'Opportunity','006SF','Security Upgrade','Anthony',
                   'https://sf.test/006SF','high','001SF','Prospecting',0,
                   ?)""", (sf_lead, checked_at))
    conn.commit()
    kind, row = drip.pick(conn, "C1")
    assert kind == "nugget" and row["entity_name"] == "Salesforce District"
    text, _style = drip.build_nugget(row)
    assert "https://sf.test/006SF" in text and "Anthony" in text


def test_unavailable_salesforce_snapshot_cannot_boost_stale_match(
        tmp_path: Path) -> None:
    """A retained link during an outage is history, not current Opportunity proof."""
    conn = db.connect(tmp_path / "t.db")
    sf_lead = _mk_lead(conn, iid="SF", entity="Salesforce District",
                       start="2026-05-01", amount=300_000.0)
    _mk_lead(conn, iid="NET", entity="Net New District",
             start="2026-06-01", amount=500_000.0)
    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO salesforce_lookup_state(lead_id,status,checked_at)
           VALUES (?,'unavailable',?)""", (sf_lead, checked_at))
    conn.execute(
        """INSERT INTO salesforce_matches
             (lead_id,sobject,record_id,name,owner,link,confidence,account_id,
              stage,is_closed,checked_at)
           VALUES (?,'Opportunity','006SF','Security Upgrade','Anthony',
                   'https://sf.test/006SF','high','001SF','Prospecting',0,?)""",
        (sf_lead, checked_at))
    conn.commit()
    kind, row = drip.pick(conn, "C1")
    assert kind == "nugget" and row["entity_name"] == "Net New District"


def test_bulletin_only_when_no_nuggets(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "t.db")
    _mk_lead(conn, iid="OPP1", entity="DOJ", grade=LeadGrade.WATCH,
             source="grants.gov", amount=None, end="2026-08-04", title="SVPP FY26")
    kind, row = drip.pick(conn, "C1")
    assert kind == "bulletin"


def test_california_opportunity_can_become_bulletin(tmp_path: Path) -> None:
    """A fresh official California window is eligible for lower-tier news."""
    conn = db.connect(tmp_path / "t.db")
    _mk_lead(conn, iid="CA-OPP", entity="California OES", grade=LeadGrade.WATCH,
             source="ca-grants-portal", amount=None, end="2026-08-04",
             title="School Security Grant")
    row = db.bulletin_candidates(conn)[0]
    text, style = drip.build_bulletin(row)
    assert "California Grants Portal" in text
    assert style == "bulletin-open"


def test_drip_dry_run_writes_nothing(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "t.db")
    _mk_lead(conn)
    out = drip.run_drip(None, "C1", conn, force=True, dry_run=True)
    assert out.startswith("[dry-run] would post nugget")
    assert conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM leads").fetchone()["status"] == "new"


class _SlackClient:
    """Offline Slack client that records successful proactive delivery attempts."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def chat_postMessage(self, **_kwargs: object) -> dict[str, str]:  # noqa: N802
        """Return a stable timestamp or simulate an ambiguous timeout."""
        self.calls += 1
        if self.fail:
            raise TimeoutError("ambiguous")
        return {"ts": "200.1"}


def test_delivery_reservation_prevents_duplicate_post(tmp_path: Path) -> None:
    """The same funding event can be proactively delivered only once per channel."""
    conn = db.connect(tmp_path / "t.db")
    _mk_lead(conn)
    client = _SlackClient()
    first = drip.run_drip(client, "C1", conn, force=True)
    second = drip.run_drip(client, "C1", conn, force=True)
    assert first.startswith("posted nugget")
    assert second == "skip: nothing new worth saying"
    assert client.calls == 1
    assert conn.execute(
        "SELECT state FROM notification_outbox").fetchone()["state"] == "delivered"


def test_ambiguous_slack_timeout_is_not_blindly_retried(tmp_path: Path) -> None:
    """A timeout remains unknown so Grant cannot create a duplicate notification."""
    conn = db.connect(tmp_path / "t.db")
    _mk_lead(conn)
    client = _SlackClient(fail=True)
    first = drip.run_drip(client, "C1", conn, force=True)
    second = drip.run_drip(client, "C1", conn, force=True)
    assert first.startswith("unknown:")
    assert "already reserved" in second
    assert client.calls == 1
    assert conn.execute(
        "SELECT state FROM notification_outbox").fetchone()["state"] == "unknown"


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
    points = conn.execute("SELECT SUM(points) FROM outcome_events").fetchone()[0]
    assert points == 5  # two replies at +2 and one reaction at +1
