"""Drip-engine tests: window math, pacing gates, message builders,
engagement dedupe. All offline; the LLM layer is not exercised here (its failure
mode is tested by contract: bad output degrades to an honest 'didn't parse')."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, time, timedelta, timezone
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
    backfill: bool = False,
) -> int:
    """Provide test-local behavior for mk lead.

    `backfill` reproduces what every award poller actually sets for an award obligated
    more than 90 days ago — the shape ALL 638 production gold leads have — which
    db.upsert_lead stores as suppressed=1."""
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
                backfill=backfill,
            ),
            grade=grade,
        ),
    )
    return int(
        conn.execute("SELECT id FROM leads WHERE source_item_id=?", (iid,)).fetchone()[
            "id"
        ]
    )


def _mk_rfp(
    conn: sqlite3.Connection,
    iid: str = "R1",
    entity: str = "City of Kemah",
    grade: LeadGrade = LeadGrade.SILVER,  # RFPs are silver at best (never gold)
    end: str = "2030-12-31",
    title: str = "Video Surveillance Camera Systems RFP",
    url: str = "https://www.kemahtx.gov/bids",
) -> int:
    """Insert one open physical-security RFP lead (source='rfp', RFP_POSTED)."""
    db.upsert_lead(
        conn,
        Lead(
            item=RawItem(
                source="rfp",
                item_id=iid,
                title=title,
                entity=entity,
                state="TX",
                program="RFP:security",
                amount=None,
                start="2030-01-01",
                end=end,
                url=url,
                raw={},
                event_type=FundingEventType.RFP_POSTED,
                event_date="2030-01-01",
                date_precision=DatePrecision.DAY,
                verification_status=VerificationStatus.VERIFIED,
                evidence_excerpt=f"Proposals due 2030-12-31 — {title}",
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


def test_window_7am_et_open() -> None:
    # 11:00 UTC = 7:00 ET (summer) -> inside; the window opens at 7am ET (Chase).
    """Verify the window opens at 7am ET."""
    assert drip.in_window(datetime(2026, 7, 13, 11, 0, tzinfo=timezone.utc))


def test_window_before_7am_et_closed() -> None:
    # 10:30 UTC = 6:30 ET -> outside (before the 7am ET open).
    """Verify before 7am ET is still closed."""
    assert not drip.in_window(datetime(2026, 7, 13, 10, 30, tzinfo=timezone.utc))


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
    go, reason = drip.pacing_ok(conn, "C1", datetime.now(timezone.utc))
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
    # urgent bypasses the 1/day cap but still respects the 90-minute gap
    go, reason = drip.pacing_ok(conn, "C1", now, urgent=True)
    assert not go and "since last post" in reason


def test_tick_before_todays_slot_holds(tmp_path: Path) -> None:
    """A tick earlier than today's target time waits — this is what stops 4 AM cards."""
    conn = db.connect(tmp_path / "t.db")
    # 13:00 UTC = 06:00 PT, before any slot in the 10:00-11:30 PT band.
    early = datetime(2026, 7, 22, 13, 0, tzinfo=timezone.utc)
    go, reason = drip.pacing_ok(conn, "C1", early)
    assert not go and "slot" in reason


def test_tick_after_todays_slot_posts(tmp_path: Path) -> None:
    """Once the target time passes, the card is eligible."""
    conn = db.connect(tmp_path / "t.db")
    # 20:00 UTC = 13:00 PT, after the whole band.
    late = datetime(2026, 7, 22, 20, 0, tzinfo=timezone.utc)
    go, reason = drip.pacing_ok(conn, "C1", late)
    assert go and reason == "eligible"


def test_daily_slot_is_stable_within_a_day_and_moves_between_days() -> None:
    """Every tick of one day must agree on the target, or the goalpost re-randomizes
    each 30 minutes and the front-loading this replaced comes straight back."""
    day = date(2026, 7, 22)
    assert drip.daily_slot(day, "C1") == drip.daily_slot(day, "C1")
    week = {drip.daily_slot(date(2026, 7, d), "C1") for d in range(20, 25)}
    assert len(week) > 1, "slot never varies; the card would land at a fixed time"


def test_daily_slot_always_lands_inside_the_configured_band() -> None:
    """Across a year of dates the slot never escapes the band."""
    start, end = drip.slot_band()
    for day in (date(2026, 1, 1) + timedelta(days=n) for n in range(0, 365, 7)):
        assert start <= drip.daily_slot(day, "C1") <= end


def test_slot_band_is_env_tunable_without_a_deploy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chase wants to try ~10:45 PT and re-tune after watching engagement."""
    monkeypatch.setenv("DRIP_SLOT_START_PT", "10:45")
    monkeypatch.setenv("DRIP_SLOT_END_PT", "11:15")
    assert drip.slot_band() == (time(10, 45), time(11, 15))
    assert time(10, 45) <= drip.daily_slot(date(2026, 7, 22), "C1") <= time(11, 15)


@pytest.mark.parametrize(
    ("start", "end"), [("garbage", "11:30"), ("10:00", "oops"), ("13:00", "09:00")]
)
def test_malformed_or_inverted_band_still_yields_a_usable_slot(
    start: str, end: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bad env value must never silence the daily card entirely."""
    monkeypatch.setenv("DRIP_SLOT_START_PT", start)
    monkeypatch.setenv("DRIP_SLOT_END_PT", end)
    slot = drip.daily_slot(date(2026, 7, 22), "C1")
    assert isinstance(slot, time)


def test_force_bypasses_everything(tmp_path: Path) -> None:
    """Verify force bypasses everything."""
    conn = db.connect(tmp_path / "t.db")
    go, reason = drip.should_post(
        conn,
        "C1",
        datetime(2026, 7, 11, 3, 0, tzinfo=timezone.utc),
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
    row = db.bulletin_candidates(conn, "C1")[0]
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


def test_rfp_alert_is_short_human_and_actionable(tmp_path: Path) -> None:
    """The RFP alert names the entity, the subject, the deadline, and Chase's CTA."""
    conn = db.connect(tmp_path / "t.db")
    _mk_rfp(conn)
    row = db.rfp_candidates(conn, "C1")[0]
    text, style = drip.build_rfp_alert(row)
    assert style == "rfp-open"
    assert text.startswith("City of Kemah has an open RFP for security cameras")
    assert "responses due 2030-12-31" in text
    assert text.endswith("Anybody want to talk?")


def test_rfp_alert_names_cameras_and_access_control(tmp_path: Path) -> None:
    """A dual-scope RFP is described as both."""
    conn = db.connect(tmp_path / "t.db")
    _mk_rfp(conn, title="Access Control and Video Surveillance Camera System RFP")
    text, _ = drip.build_rfp_alert(db.rfp_candidates(conn, "C1")[0])
    assert "security cameras and access control" in text


def test_pick_prefers_a_gold_award_over_an_rfp(tmp_path: Path) -> None:
    """Grants outrank RFPs (Chase: an RFP is a lot of work and never beats a real award).
    The award here is >7 days old so it is a plain gold nugget, not platinum, yet wins
    over an open silver RFP."""
    conn = db.connect(tmp_path / "t.db")
    _mk_lead(conn, iid="AWARD", entity="Fresh District", start="2026-06-01")
    _mk_rfp(conn, iid="SRFP", entity="City of Kemah")  # silver open RFP
    kind, row = drip.pick(conn, "C1", today=date(2026, 7, 18))
    assert kind == "nugget" and row["entity_name"] == "Fresh District"


def test_pick_surfaces_platinum_for_a_fresh_security_grant(tmp_path: Path) -> None:
    """A verified SVPP award from the last few days is PLATINUM — the top card."""
    conn = db.connect(tmp_path / "t.db")
    _mk_lead(conn, iid="PLAT", entity="Fresh District", start="2026-07-15")  # SVPP
    _mk_rfp(conn, iid="SRFP", entity="City of Kemah")  # silver open RFP
    kind, row = drip.pick(conn, "C1", today=date(2026, 7, 18))
    assert kind == "platinum" and row["entity_name"] == "Fresh District"
    text, style = drip.build_platinum(row)
    assert style == "platinum"
    assert "just landed a verified" in text and "reaching out now" in text


def test_stale_award_is_not_platinum(tmp_path: Path) -> None:
    """An award older than the platinum window is a plain nugget, not platinum."""
    conn = db.connect(tmp_path / "t.db")
    _mk_lead(conn, iid="OLD", entity="Old District", start="2026-06-01")  # >7 days
    kind, _row = drip.pick(conn, "C1", today=date(2026, 7, 18))
    assert kind == "nugget"


def test_pick_puts_a_silver_rfp_after_a_gold_award(tmp_path: Path) -> None:
    """An older (SILVER) open RFP ranks below a gold award, above a bulletin."""
    conn = db.connect(tmp_path / "t.db")
    _mk_lead(conn, iid="AWARD", entity="Fresh District", start="2026-06-01")
    _mk_rfp(conn, iid="SRFP", entity="City of Ames", grade=LeadGrade.SILVER)
    kind, row = drip.pick(conn, "C1")
    assert kind == "nugget" and row["entity_name"] == "Fresh District"


def test_pick_surfaces_a_silver_rfp_when_no_award(tmp_path: Path) -> None:
    """With no gold award, an open silver RFP still surfaces (before any bulletin)."""
    conn = db.connect(tmp_path / "t.db")
    _mk_rfp(conn, iid="SRFP", entity="City of Ames", grade=LeadGrade.SILVER)
    kind, row = drip.pick(conn, "C1")
    assert kind == "rfp" and row["entity_name"] == "City of Ames"


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
    assert db.nugget_candidates(conn, "C1") == []
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
    row = db.bulletin_candidates(conn, "C1")[0]
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
    assert client.last_kwargs["mrkdwn"] is True  # source renders as a hyperlink
    assert client.last_kwargs["unfurl_links"] is False
    assert client.last_kwargs["unfurl_media"] is False
    assert client.last_kwargs["text"] == (
        "Castle Rock School District 401 in Washington has a verified "
        "$500,000 SVPP funding award."
        "\n\n<@U01E908206M> — Washington is your territory. "
        "Want me to find the right contact?"
        "\n\n<https://x.gov/a|View the source record>"
    )
    assert (
        conn.execute("SELECT state FROM notification_outbox").fetchone()["state"]
        == "delivered"
    )


def test_ambiguous_slack_timeout_is_not_blindly_retried(tmp_path: Path) -> None:
    """A timeout remains unknown so Grant cannot create a duplicate notification.

    The observable changed with the C1 wedge fix; the invariant did not. The ambiguous
    lead is now excluded from the candidate queries outright, so a later tick reports
    having nothing to say rather than 'already reserved'. What must never change is that
    it is not re-sent — asserted on the Slack call count and the retained 'unknown'
    state, not on the wording of a skip message. See tests/test_drip_pacing.py for the
    wedge regression itself.
    """
    conn = db.connect(tmp_path / "t.db")
    _mk_lead(conn)
    client = _SlackClient(fail=True)
    first = drip.run_drip(client, "C1", conn, force=True)
    second = drip.run_drip(client, "C1", conn, force=True)
    assert first.startswith("unknown:")
    assert second.startswith("skip:")
    assert client.calls == 1
    assert (
        conn.execute("SELECT state FROM notification_outbox").fetchone()["state"]
        == "unknown"
    )


def test_run_drip_posts_platinum_end_to_end(tmp_path: Path) -> None:
    """A fresh (<=7-day) verified SVPP award posts as PLATINUM and records cleanly.

    Guards C1: 'platinum' was not in the posts.kind CHECK, so record_post crashed the
    tick AFTER the Slack message was sent. This is the designed happy path for a fresh
    award — it must complete end-to-end."""
    conn = db.connect(tmp_path / "t.db")
    recent = (date.today() - timedelta(days=2)).isoformat()
    lead_id = _mk_lead(conn, iid="P1", start=recent, end="2031-09-30")
    client = _SlackClient()
    out = drip.run_drip(client, "C1", conn, force=True)
    assert out.startswith("posted platinum")
    assert client.calls == 1
    assert conn.execute("SELECT kind FROM posts").fetchone()["kind"] == "platinum"
    assert (
        conn.execute("SELECT state FROM notification_outbox").fetchone()["state"]
        == "delivered"
    )
    assert (
        conn.execute(
            "SELECT status FROM leads WHERE id=?", (lead_id,)
        ).fetchone()["status"]
        == "surfaced"
    )


def test_run_drip_posts_open_rfp_end_to_end(tmp_path: Path) -> None:
    """An open silver RFP (no gold award available) posts as an 'rfp' kind and records
    cleanly. Guards C1: 'rfp' was also missing from the posts.kind CHECK."""
    conn = db.connect(tmp_path / "t.db")
    _mk_rfp(conn, iid="R9", grade=LeadGrade.SILVER, end="2031-12-31")
    client = _SlackClient()
    out = drip.run_drip(client, "C1", conn, force=True)
    assert out.startswith("posted rfp")
    assert conn.execute("SELECT kind FROM posts").fetchone()["kind"] == "rfp"
    assert (
        conn.execute("SELECT state FROM notification_outbox").fetchone()["state"]
        == "delivered"
    )


def test_posts_kind_accepts_all_four_drip_kinds(tmp_path: Path) -> None:
    """posts.kind accepts every kind pick() can emit (migration 10), so a live post can
    never violate the CHECK after the message is already in Slack."""
    conn = db.connect(tmp_path / "t.db")
    for index, kind in enumerate(("platinum", "nugget", "rfp", "bulletin")):
        assert db.record_post(conn, kind, None, "C1", f"9.{index}", "s") > 0


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
    # hyperlinked source on its own line after a blank line (Chase 2026-07-19)
    assert "\n\n<https://x.gov/a|View the source record>" in posted
    # The claim sentence remains a single inert sentence before the source.
    sentence = posted.split("\n\n")[0]
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


# ---------------------------------------------- territory tagging + gold reachability
def test_backfilled_gold_award_still_reaches_the_daily_card(tmp_path: Path) -> None:
    """A gold award older than the 90-day backfill cutoff must still be postable.

    Regression for the production state measured 2026-07-22: every award poller marks
    anything obligated more than 90 days ago as backfill, db.upsert_lead turns that into
    suppressed=1, and nugget_candidates required suppressed=0. Result: 638 of 638 gold
    leads were invisible, pick() fell past GOLD every tick, and the channel got a silver
    RFP every day. The real FY25 SVPP cohort was obligated 2025-10-10 — verified live
    against the USASpending API — so this is the ONLY shape gold currently comes in.
    """
    conn = db.connect(tmp_path / "t.db")
    _mk_lead(conn, iid="BACKFILL", entity="Montebello Unified School District",
             start="2025-10-10", end="2028-09-30", backfill=True)
    assert conn.execute(
        "SELECT suppressed FROM funding_events"
    ).fetchone()["suppressed"] == 1, "fixture must reproduce the suppressed shape"
    assert len(db.nugget_candidates(conn, "C1")) == 1
    kind, row = drip.pick(conn, "C1")
    assert kind == "nugget"
    assert row["entity_name"] == "Montebello Unified School District"


def test_gold_award_outranks_an_open_rfp_even_when_backfilled(tmp_path: Path) -> None:
    """Chase's ladder holds: an award in hand beats a solicitation, backfilled or not."""
    conn = db.connect(tmp_path / "t.db")
    _mk_rfp(conn, iid="R_LOSE", end="2031-12-31")
    _mk_lead(conn, iid="G_WIN", entity="Castle Rock School District 401",
             start="2025-10-10", end="2028-09-30")
    kind, _ = drip.pick(conn, "C1")
    assert kind == "nugget"


def test_nugget_never_repeats_an_already_posted_lead(tmp_path: Path) -> None:
    """A lead already in `posts` can never be picked again, even if a later poll resets
    its status to 'new' (upsert_lead does exactly that when a new event lands)."""
    conn = db.connect(tmp_path / "t.db")
    lead_id = _mk_lead(conn, iid="ONCE", start="2025-10-10", end="2028-09-30")
    db.record_post(conn, "nugget", lead_id, "C1", "1.0", "award-brief")
    conn.execute("UPDATE leads SET status='new' WHERE id=?", (lead_id,))
    conn.commit()
    assert db.nugget_candidates(conn, "C1") == []


def test_sibling_rfps_do_not_render_as_the_same_card(tmp_path: Path) -> None:
    """Two trade packages of ONE project must not produce identical text.

    This is the literal complaint ('Grant keeps posting the exact same message'):
    production leads #9533 and #9565 are different SCI Pine Grove solicitations sharing
    an agency and a deadline, and the card printed neither title."""
    conn = db.connect(tmp_path / "t.db")
    # The titles are VERBATIM from production leads #9533 and #9565. An earlier version
    # of this test used shortened stand-ins, so it passed while the real cards still
    # rendered identically — the fixture has to carry the real length or it guards
    # nothing. They share a 76-character prefix and differ only in the final words.
    _mk_rfp(conn, iid="HVAC", entity="Pennsylvania Department of Corrections",
            title="Sci Pine Grove - Control Room, Security Cameras and Other Facility "
                  "Upgrades - General and HVAC Construction",
            url="https://starbridge.ai/rfp/sci-pine-grove-hvac")
    _mk_rfp(conn, iid="PLUMB", entity="Pennsylvania Department of Corrections",
            title="SCI Pine Grove - Control Room, Security Cameras and Other Facility "
                  "Upgrades - Plumbing Construction *REBID*",
            url="https://starbridge.ai/rfp/sci-pine-grove-plumbing")
    rendered = {drip.build_rfp_alert(row)[0] for row in db.rfp_candidates(conn, "C1")}
    assert len(rendered) == 2, f"cards are indistinguishable: {rendered}"
    # The DISCRIMINATING words must survive shortening, not just the shared prefix.
    assert any("HVAC" in text for text in rendered), rendered
    assert any("Plumbing" in text for text in rendered), rendered


def test_rfp_card_stays_one_readable_sentence(tmp_path: Path) -> None:
    """A very long solicitation title is trimmed at a word boundary, not mid-word."""
    conn = db.connect(tmp_path / "t.db")
    _mk_rfp(conn, iid="LONG", title=(
        "Video Surveillance Camera Systems and Related Electronic Security "
        "Infrastructure Replacement Program for Multiple Municipal Facilities"))
    text, _ = drip.build_rfp_alert(db.rfp_candidates(conn, "C1")[0])
    assert "…" in text and text.endswith("Anybody want to talk?")
    assert len(text) < 260
    assert "  " not in text


def test_posted_card_tags_the_territory_owner(tmp_path: Path) -> None:
    """End-to-end: the message that reaches Slack @-mentions the state's rep."""
    conn = db.connect(tmp_path / "t.db")
    _mk_lead(conn, iid="PA1", entity="Bethlehem Area School District",
             start="2025-10-10", end="2028-09-30")
    conn.execute("UPDATE leads SET state='PA' WHERE source_item_id='PA1'")
    conn.commit()
    client = _SlackClient()
    assert drip.run_drip(client, "C1", conn, force=True).startswith("posted")
    text = str(client.last_kwargs["text"])
    assert "<@U08C1NBH875>" in text  # Brett D'Ambrosio owns Pennsylvania
    assert "Pennsylvania is your territory" in text
    assert "in Pennsylvania has a verified" in text


def test_posted_card_for_an_unowned_state_goes_out_untagged(tmp_path: Path) -> None:
    """No rep owns New York, so the card ships with no mention — never a wrong one."""
    conn = db.connect(tmp_path / "t.db")
    _mk_lead(conn, iid="NY1", start="2025-10-10", end="2028-09-30")
    conn.execute("UPDATE leads SET state='NY' WHERE source_item_id='NY1'")
    conn.commit()
    client = _SlackClient()
    assert drip.run_drip(client, "C1", conn, force=True).startswith("posted")
    text = str(client.last_kwargs["text"])
    assert "<@" not in text
    assert "in New York has a verified" in text


def test_award_card_spells_out_a_state_beyond_the_original_five(
    tmp_path: Path,
) -> None:
    """Polling is nationwide; a Texas award used to render the bare code 'in TX'."""
    conn = db.connect(tmp_path / "t.db")
    _mk_lead(conn, iid="TX1", start="2025-10-10", end="2028-09-30")
    conn.execute("UPDATE leads SET state='TX' WHERE source_item_id='TX1'")
    conn.commit()
    text, _ = drip.build_nugget(db.nugget_candidates(conn, "C1")[0])
    assert "in Texas has a verified" in text


# --------------------------------------------- cap must survive a bookkeeping failure
def test_cap_holds_when_recording_a_confirmed_send_fails(tmp_path: Path) -> None:
    """THE FLOOD REGRESSION (found in review of 264b0e2, 2026-07-22).

    `record_post` runs AFTER chat_postMessage. If it raises — full disk, lock, a CHECK
    violation — the message is in Slack but `posts` has no row. Every cap in pacing_ok
    used to be counted from `posts` alone, so the next tick read zero posts and skipped
    the daily cap, the absolute cap AND the min-gap rule. `mark_surfaced` still excluded
    the sent lead, so pick() simply returned the NEXT of 544 and posted it, once every
    30 minutes until the window closed — up to 13 cards, each pinging a rep's phone.

    The reservation in notification_outbox is written BEFORE the Slack call, so it is
    the one signal that cannot be missing for a delivered message.
    """
    conn = db.connect(tmp_path / "t.db")
    for index in range(4):
        _mk_lead(conn, iid=f"G{index}", entity=f"District {index}",
                 start="2025-10-10", end="2028-09-30", backfill=True)
    client = _SlackClient()
    real_record_post = db.record_post

    def exploding_record_post(*args: object, **kwargs: object) -> int:
        """Simulate the disk filling up between the send and the bookkeeping."""
        raise sqlite3.OperationalError("database or disk is full")

    db.record_post = exploding_record_post  # type: ignore[assignment]
    try:
        first = drip.run_drip(client, "C1", conn, force=True)
        assert first.startswith("posted") and "recording it hit" in first
        assert client.calls == 1
        assert conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0
        # The reservation survived even though the posts row did not.
        assert len(db.delivery_attempts_today(conn, "C1")) == 1
        # A LATER tick (past the gap, past the slot) must still refuse.
        later = datetime.now(timezone.utc) + timedelta(hours=3)
        go, reason = drip.pacing_ok(conn, "C1", later)
        assert not go, f"cap went blind after a failed record_post: {reason}"
        assert "cap" in reason
    finally:
        db.record_post = real_record_post  # type: ignore[assignment]
    assert client.calls == 1, "a second card was posted after a bookkeeping failure"


def test_amountless_gold_lead_cannot_wedge_the_drip(tmp_path: Path) -> None:
    """`_award_facts` raises without a positive amount and cmd_drip has no handler, so
    such a lead would crash every tick forever while never being marked surfaced."""
    conn = db.connect(tmp_path / "t.db")
    _mk_lead(conn, iid="NOAMT", amount=None, start="2025-10-10", end="2028-09-30",
             backfill=True)
    conn.execute("UPDATE leads SET lead_grade='gold' WHERE source_item_id='NOAMT'")
    conn.commit()
    assert db.nugget_candidates(conn, "C1") == []


def test_urgent_card_still_waits_for_the_band_to_open(tmp_path: Path) -> None:
    """Urgent may skip the day's random target, but not the workday. Without this floor
    an exceptional award posted at the first 04:00 PT tick — the exact front-loading the
    slot design removed."""
    conn = db.connect(tmp_path / "t.db")
    dawn = datetime(2026, 7, 22, 11, 0, tzinfo=timezone.utc)  # 04:00 PT
    go, reason = drip.pacing_ok(conn, "C1", dawn, urgent=True)
    assert not go and "holding until" in reason
    noon = datetime(2026, 7, 22, 19, 0, tzinfo=timezone.utc)  # 12:00 PT
    go, _ = drip.pacing_ok(conn, "C1", noon, urgent=True)
    assert go
