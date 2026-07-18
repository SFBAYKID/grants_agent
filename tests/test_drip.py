"""Drip-engine tests: window math, pacing gates, message builders,
engagement dedupe. All offline; the LLM layer is not exercised here (its failure
mode is tested by contract: bad output degrades to an honest 'didn't parse')."""

from __future__ import annotations

import random
import sqlite3
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
from grant_watch.slack import drip


def _mk_lead(
    conn: sqlite3.Connection,
    iid: str = "A1",
    entity: str = "Castle Rock School District 401",
    grade: LeadGrade = LeadGrade.GOLD,
    source: str = "usaspending:16.071",
    amount: float | None = 500_000.0,
    start: str = "2025-10-01",
    end: str = "2028-09-30",
    title: str = "SVPP award",
) -> int:
    """Provide test-local behavior for mk lead."""
    event_type = (
        FundingEventType.APPLICATION_WINDOW_OPENED
        if source in {"grants.gov", "ca-grants-portal"}
        else FundingEventType.AWARD_OBLIGATED
    )
    db.upsert_lead(
        conn,
        Lead(
            item=RawItem(
                source=source,
                item_id=iid,
                title=title,
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
            grade=grade,
        ),
    )
    return int(
        conn.execute("SELECT id FROM leads WHERE source_item_id=?", (iid,)).fetchone()[
            "id"
        ]
    )


# ------------------------------------------------------------------ window
def test_window_monday_morning_et_ok() -> None:
    # 13:30 UTC Monday = 8:30 ET / 5:30 PT (summer) -> inside
    """Verify window monday morning et ok."""
    assert drip.in_window(datetime(2026, 7, 13, 13, 30, tzinfo=timezone.utc))


def test_window_before_8am_et_closed() -> None:
    # 11:00 UTC = 7:00 ET -> outside
    """Verify window before 8am et closed."""
    assert not drip.in_window(datetime(2026, 7, 13, 11, 0, tzinfo=timezone.utc))


def test_window_after_5pm_pt_closed() -> None:
    # 00:30 UTC Tue = Mon 17:30 PT -> outside
    """Verify window after 5pm pt closed."""
    assert not drip.in_window(datetime(2026, 7, 14, 0, 30, tzinfo=timezone.utc))


def test_window_weekend_closed() -> None:
    """Verify window weekend closed."""
    assert not drip.in_window(datetime(2026, 7, 11, 16, 0, tzinfo=timezone.utc))  # Sat


# ------------------------------------------------------------------ pacing
def test_daily_cap_blocks(tmp_path: Path) -> None:
    """Verify daily cap blocks."""
    conn = db.connect(tmp_path / "t.db")
    for i in range(drip.DAILY_CAP):
        db.record_post(conn, "nugget", None, "C1", f"111.{i}", "s")
    go, reason = drip.pacing_ok(
        conn, "C1", datetime.now(timezone.utc), random.Random(1)
    )
    assert not go and "cap" in reason


def test_min_gap_blocks(tmp_path: Path) -> None:
    """Verify min gap blocks."""
    conn = db.connect(tmp_path / "t.db")
    # a post 30 real minutes ago (posts_today sees it; gap 30m < 90m)
    thirty_ago = (datetime.now(timezone.utc).replace(microsecond=0)).isoformat()
    conn.execute(
        "INSERT INTO posts (kind, channel, ts, style, posted_at) "
        "VALUES ('nugget','C1','111.0','s', ?)",
        (thirty_ago,),
    )
    conn.commit()
    now = datetime.now(timezone.utc)
    go, reason = drip.pacing_ok(conn, "C1", now, random.Random(1))
    assert not go and "since last post" in reason


def test_jitter_skip_when_rng_high(tmp_path: Path) -> None:
    """Verify jitter skip when rng high."""
    conn = db.connect(tmp_path / "t.db")

    class AlwaysHigh(random.Random):
        def random(self) -> float:  # forces the jitter branch deterministically
            """Provide test-local behavior for random."""
            return 0.99

    go, reason = drip.pacing_ok(conn, "C1", datetime.now(timezone.utc), AlwaysHigh())
    assert not go and "jitter" in reason


def test_force_bypasses_everything(tmp_path: Path) -> None:
    """Verify force bypasses everything."""
    conn = db.connect(tmp_path / "t.db")
    go, reason = drip.should_post(
        conn,
        "C1",
        datetime(2026, 7, 11, 3, 0, tzinfo=timezone.utc),
        random.Random(1),
        force=True,
    )
    assert go and reason == "forced"


# ------------------------------------------------------------------ builders
def test_nugget_is_short_and_factual(tmp_path: Path) -> None:
    """Verify nugget is short and factual."""
    conn = db.connect(tmp_path / "t.db")
    lead_id = _mk_lead(conn)
    row = db.get_lead(conn, lead_id)
    text, style = drip.build_nugget(row)
    assert text == (
        "Castle Rock School District 401 in Washington has a verified "
        "$500,000 SVPP funding award."
    )
    assert style == "award-brief"
    assert text.count(".") == 1 and "\n" not in text
    assert "http" not in text and "Salesforce" not in text


def test_unknown_event_date_is_disclosed_as_a_listing(tmp_path: Path) -> None:
    """A source without an award-action date never gets 'just received' wording."""
    conn = db.connect(tmp_path / "t.db")
    lead_id = _mk_lead(conn, start="")
    row = db.get_lead(conn, lead_id)
    assert row is not None
    text, _style = drip.build_nugget(row)
    assert text == (
        "Castle Rock School District 401 in Washington has a verified "
        "$500,000 SVPP funding award."
    )
    assert "received" not in text.lower()


def test_source_text_cannot_inject_mentions_links_or_extra_sentences(
    tmp_path: Path,
) -> None:
    """Untrusted source fields remain inert inside the one-sentence Slack alert."""
    conn = db.connect(tmp_path / "t.db")
    lead_id = _mk_lead(conn)
    conn.execute(
        """UPDATE leads SET entity_name='<@U123> District.\nSecond sentence?',
                            program='SVPP <https://evil.test|click>'
           WHERE id=?""",
        (lead_id,),
    )
    conn.commit()
    row = db.get_lead(conn, lead_id)
    assert row is not None
    text, _style = drip.build_nugget(row)
    assert text.count(".") == 1 and "\n" not in text
    assert "<@" not in text and "http" not in text and "|" not in text


def test_official_acronym_capitalization_is_preserved(tmp_path: Path) -> None:
    """Minimal alerts do not rewrite official organization acronyms."""
    conn = db.connect(tmp_path / "t.db")
    lead_id = _mk_lead(conn, entity="ABC Schools")
    row = db.get_lead(conn, lead_id)
    assert row is not None
    text, _style = drip.build_nugget(row)
    assert text.startswith("ABC Schools in Washington")


def test_all_caps_source_entity_is_human_formatted(tmp_path: Path) -> None:
    """Government-system uppercase names render as clean conversational prose."""
    conn = db.connect(tmp_path / "t.db")
    lead_id = _mk_lead(conn, entity="CASTLE ROCK SCHOOL DISTRICT 401")
    row = db.get_lead(conn, lead_id)
    assert row is not None
    text, _style = drip.build_nugget(row)
    assert text.startswith("Castle Rock School District 401 in Washington")


def test_all_caps_entity_preserves_known_acronyms(tmp_path: Path) -> None:
    """Casing cleanup does not corrupt education acronyms or roman numerals."""
    conn = db.connect(tmp_path / "t.db")
    lead_id = _mk_lead(conn, entity="ABC USD III SCHOOL DISTRICT")
    row = db.get_lead(conn, lead_id)
    assert row is not None
    text, _style = drip.build_nugget(row)
    assert text.startswith("ABC USD III School District in Washington")


@pytest.mark.parametrize("amount", [None, 0.0, -1.0, float("inf"), float("nan")])
def test_invalid_amount_fails_closed(tmp_path: Path, amount: float | None) -> None:
    """A non-finite or non-positive amount cannot enter a proactive award claim."""
    conn = db.connect(tmp_path / "t.db")
    lead_id = _mk_lead(conn)
    conn.execute("UPDATE leads SET amount=? WHERE id=?", (amount, lead_id))
    conn.commit()
    row = db.get_lead(conn, lead_id)
    assert row is not None
    with pytest.raises(ValueError, match="finite positive amount"):
        drip.build_nugget(row)


def test_unverified_or_wrong_event_type_fails_closed(tmp_path: Path) -> None:
    """The builder independently enforces award evidence even outside candidate SQL."""
    conn = db.connect(tmp_path / "t.db")
    lead_id = _mk_lead(conn)
    conn.execute(
        "UPDATE funding_events SET verification_status='needs-testing' WHERE lead_id=?",
        (lead_id,),
    )
    conn.commit()
    row = db.get_lead(conn, lead_id)
    assert row is not None
    with pytest.raises(ValueError, match="verified"):
        drip.build_nugget(row)


def test_bulletin_uses_opportunity_title(tmp_path: Path) -> None:
    """Verify bulletin uses opportunity title."""
    conn = db.connect(tmp_path / "t.db")
    _mk_lead(
        conn,
        iid="OPP1",
        entity="DOJ COPS Office",
        grade=LeadGrade.WATCH,
        source="grants.gov",
        amount=None,
        start="2026-07-01",
        end="2026-08-04",
        title="FY26 School Violence Prevention Program",
    )
    row = db.bulletin_candidates(conn)[0]
    text, style = drip.build_bulletin(row)
    assert "FY26 School Violence Prevention Program" in text
    assert (
        text
        == "FY26 School Violence Prevention Program is listed as open through 2026-08-04."
    )
    assert style == "bulletin-open"


def test_pick_prefers_top_scored_nugget(tmp_path: Path) -> None:
    """Verify pick prefers top scored nugget."""
    conn = db.connect(tmp_path / "t.db")
    _mk_lead(conn, iid="OLD", entity="Old District", start="2022-10-01")
    _mk_lead(
        conn, iid="FRESH", entity="Fresh District", start="2026-06-01", amount=150_000.0
    )
    kind, row = drip.pick(conn, "C1")
    assert kind == "nugget" and row["entity_name"] == "Fresh District"


def test_needs_testing_event_cannot_enter_proactive_notifications(
    tmp_path: Path,
) -> None:
    """An unverified Oregon-style positive remains searchable but is never pushed."""
    conn = db.connect(tmp_path / "t.db")
    lead_id = _mk_lead(conn, iid="OREGON", entity="Oregon Test District")
    conn.execute(
        """UPDATE funding_events SET verification_status='needs-testing'
           WHERE lead_id=?""",
        (lead_id,),
    )
    conn.commit()
    assert db.nugget_candidates(conn) == []
    assert drip.pick(conn, "C1") is None


def test_pick_prioritizes_existing_salesforce_opportunity(tmp_path: Path) -> None:
    """A verified open CRM Opportunity outranks a slightly stronger net-new lead."""
    conn = db.connect(tmp_path / "t.db")
    sf_lead = _mk_lead(
        conn,
        iid="SF",
        entity="Salesforce District",
        start="2026-05-01",
        amount=300_000.0,
    )
    _mk_lead(
        conn, iid="NET", entity="Net New District", start="2026-06-01", amount=500_000.0
    )
    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO salesforce_lookup_state(lead_id,status,checked_at)
           VALUES (?,'found',?)""",
        (sf_lead, checked_at),
    )
    conn.execute(
        """INSERT INTO salesforce_matches
             (lead_id,sobject,record_id,name,owner,link,confidence,account_id,
              stage,is_closed,checked_at)
           VALUES (?,'Opportunity','006SF','Security Upgrade','Anthony',
                   'https://sf.test/006SF','high','001SF','Prospecting',0,
                   ?)""",
        (sf_lead, checked_at),
    )
    conn.commit()
    kind, row = drip.pick(conn, "C1")
    assert kind == "nugget" and row["entity_name"] == "Salesforce District"
    text, _style = drip.build_nugget(row)
    assert "https://sf.test/006SF" not in text and "Anthony" not in text


def test_unavailable_salesforce_snapshot_cannot_boost_stale_match(
    tmp_path: Path,
) -> None:
    """A retained link during an outage is history, not current Opportunity proof."""
    conn = db.connect(tmp_path / "t.db")
    sf_lead = _mk_lead(
        conn,
        iid="SF",
        entity="Salesforce District",
        start="2026-05-01",
        amount=300_000.0,
    )
    _mk_lead(
        conn, iid="NET", entity="Net New District", start="2026-06-01", amount=500_000.0
    )
    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO salesforce_lookup_state(lead_id,status,checked_at)
           VALUES (?,'unavailable',?)""",
        (sf_lead, checked_at),
    )
    conn.execute(
        """INSERT INTO salesforce_matches
             (lead_id,sobject,record_id,name,owner,link,confidence,account_id,
              stage,is_closed,checked_at)
           VALUES (?,'Opportunity','006SF','Security Upgrade','Anthony',
                   'https://sf.test/006SF','high','001SF','Prospecting',0,?)""",
        (sf_lead, checked_at),
    )
    conn.commit()
    kind, row = drip.pick(conn, "C1")
    assert kind == "nugget" and row["entity_name"] == "Net New District"


def test_bulletin_only_when_no_nuggets(tmp_path: Path) -> None:
    """Verify bulletin only when no nuggets."""
    conn = db.connect(tmp_path / "t.db")
    _mk_lead(
        conn,
        iid="OPP1",
        entity="DOJ",
        grade=LeadGrade.WATCH,
        source="grants.gov",
        amount=None,
        end="2026-08-04",
        title="SVPP FY26",
    )
    kind, row = drip.pick(conn, "C1")
    assert kind == "bulletin"


def test_california_opportunity_can_become_bulletin(tmp_path: Path) -> None:
    """A fresh official California window is eligible for lower-tier news."""
    conn = db.connect(tmp_path / "t.db")
    _mk_lead(
        conn,
        iid="CA-OPP",
        entity="California OES",
        grade=LeadGrade.WATCH,
        source="ca-grants-portal",
        amount=None,
        end="2026-08-04",
        title="School Security Grant",
    )
    row = db.bulletin_candidates(conn)[0]
    text, style = drip.build_bulletin(row)
    assert text == "School Security Grant is listed as open through 2026-08-04."
    assert style == "bulletin-open"


def test_drip_dry_run_writes_nothing(tmp_path: Path) -> None:
    """Verify drip dry run writes nothing."""
    conn = db.connect(tmp_path / "t.db")
    _mk_lead(conn)
    out = drip.run_drip(None, "C1", conn, force=True, dry_run=True)
    assert out.startswith("[dry-run] would post nugget")
    assert conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM leads").fetchone()["status"] == "new"


class _SlackClient:
    """Offline Slack client that records successful proactive delivery attempts."""

    def __init__(self, fail: bool = False) -> None:
        """Initialize the test double."""
        self.fail = fail
        self.calls = 0
        self.last_kwargs: dict[str, object] = {}

    def chat_postMessage(self, **kwargs: object) -> dict[str, str]:  # noqa: N802
        """Return a stable timestamp or simulate an ambiguous timeout."""
        self.calls += 1
        self.last_kwargs = kwargs
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
    assert "blocks" not in client.last_kwargs
    assert client.last_kwargs["mrkdwn"] is False
    assert client.last_kwargs["unfurl_links"] is False
    assert client.last_kwargs["unfurl_media"] is False
    assert client.last_kwargs["text"] == (
        "Castle Rock School District 401 in Washington has a verified "
        "$500,000 SVPP funding award.\nSource: https://x.gov/a"
    )
    assert (
        conn.execute("SELECT state FROM notification_outbox").fetchone()["state"]
        == "delivered"
    )


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
    assert (
        conn.execute("SELECT state FROM notification_outbox").fetchone()["state"]
        == "unknown"
    )


# ------------------------------------------------------------------ engagement points
def test_engagement_dedupes_per_user_and_kind(tmp_path: Path) -> None:
    """Verify engagement dedupes per user and kind."""
    conn = db.connect(tmp_path / "t.db")
    pid = db.record_post(conn, "nugget", None, "C1", "111.1", "ask-me")
    assert db.record_engagement(conn, pid, "U1", "reply") is True
    assert db.record_engagement(conn, pid, "U1", "reply") is False  # same user+kind
    assert db.record_engagement(conn, pid, "U1", "reaction") is True  # new kind
    assert db.record_engagement(conn, pid, "U2", "reply") is True  # new user
    assert db.engagement_stats(conn)["total"] == 3
    points = conn.execute("SELECT SUM(points) FROM outcome_events").fetchone()[0]
    assert points == 5  # two replies at +2 and one reaction at +1


def test_bulletin_relevance_rejects_health_sector_noise() -> None:
    """Precision-first bulletins: strong security phrase required, health excluded.

    Live miss 2026-07-18: "Maternal Health Emergency Management Training (MHEMT)"
    reached the channel by matching the bare word "emergency"."""
    relevant = drip._BULLETIN_RELEVANT_RE
    offtopic = drip._BULLETIN_OFFTOPIC_RE

    def passes(title: str) -> bool:
        """Apply the same accept/reject pair pick() uses."""
        return bool(relevant.search(title)) and not bool(offtopic.search(title))

    assert not passes("Maternal Health Emergency Management Training (MHEMT)")
    assert not passes("School Lunch Modernization Program")
    assert not passes("Behavioral Health Emergency Response Grants")
    assert passes("School Violence Prevention Program (SVPP)")
    assert passes("Nonprofit Security Grant Program")
    assert passes("Campus Safety and Access Control Modernization")


def test_proactive_alert_carries_a_validated_source_line(tmp_path: Path) -> None:
    """Every posted funding alert appends a safe Source line (Chase's rule).

    The sentence itself stays inert; the URL comes only from the stored
    per-record link, hardened through _safe_url."""
    conn = db.connect(tmp_path / "t.db")
    _mk_lead(conn)
    client = _SlackClient()
    drip.run_drip(client, "C1", conn, force=True)
    posted = client.last_kwargs["text"]
    assert "\nSource: https://x.gov/a" in posted
    # The claim sentence remains a single inert sentence before the source.
    sentence = posted.split("\nSource:")[0]
    assert sentence.count(".") == 1 and "\n" not in sentence


def test_source_line_fails_closed_on_missing_or_unsafe_url(tmp_path: Path) -> None:
    """No stored URL, or an unsafe one, yields no Source line rather than a bad one."""
    conn = db.connect(tmp_path / "t.db")
    lead_id = _mk_lead(conn)
    conn.execute("UPDATE leads SET detail_url=NULL WHERE id=?", (lead_id,))
    conn.commit()
    row = db.get_lead(conn, lead_id)
    assert drip.source_line(row) == ""
    conn.execute(
        "UPDATE leads SET detail_url='http://insecure.test/a?token=secret' WHERE id=?",
        (lead_id,),
    )
    conn.commit()
    row = db.get_lead(conn, lead_id)
    assert drip.source_line(row) == ""  # http + credential query -> unavailable
