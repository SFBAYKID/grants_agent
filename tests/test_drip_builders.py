"""Drip-engine tests: the message BUILDERS — what a posted card actually says.

Split out of test_drip.py (CLAUDE.md rule 4). Scheduling — when Grant is allowed to
post at all — lives in test_drip.py. All offline.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from drip_support import SlackClient, mk_lead, mk_rfp
from grant_watch import db
from grant_watch.models import LeadGrade
from grant_watch.slack import drip


# ------------------------------------------------------------------ builders
def test_nugget_is_short_and_factual(tmp_path: Path) -> None:
    """Verify nugget is short and factual."""
    conn = db.connect(tmp_path / "t.db")
    lead_id = mk_lead(conn)
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
    lead_id = mk_lead(conn, start="")
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
    lead_id = mk_lead(conn)
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
    lead_id = mk_lead(conn, entity="ABC Schools")
    row = db.get_lead(conn, lead_id)
    assert row is not None
    text, _style = drip.build_nugget(row)
    assert text.startswith("ABC Schools in Washington")


def test_all_caps_source_entity_is_human_formatted(tmp_path: Path) -> None:
    """Government-system uppercase names render as clean conversational prose."""
    conn = db.connect(tmp_path / "t.db")
    lead_id = mk_lead(conn, entity="CASTLE ROCK SCHOOL DISTRICT 401")
    row = db.get_lead(conn, lead_id)
    assert row is not None
    text, _style = drip.build_nugget(row)
    assert text.startswith("Castle Rock School District 401 in Washington")


def test_all_caps_entity_preserves_known_acronyms(tmp_path: Path) -> None:
    """Casing cleanup does not corrupt education acronyms or roman numerals."""
    conn = db.connect(tmp_path / "t.db")
    lead_id = mk_lead(conn, entity="ABC USD III SCHOOL DISTRICT")
    row = db.get_lead(conn, lead_id)
    assert row is not None
    text, _style = drip.build_nugget(row)
    assert text.startswith("ABC USD III School District in Washington")


@pytest.mark.parametrize("amount", [None, 0.0, -1.0, float("inf"), float("nan")])
def test_invalid_amount_fails_closed(tmp_path: Path, amount: float | None) -> None:
    """A non-finite or non-positive amount cannot enter a proactive award claim."""
    conn = db.connect(tmp_path / "t.db")
    lead_id = mk_lead(conn)
    conn.execute("UPDATE leads SET amount=? WHERE id=?", (amount, lead_id))
    conn.commit()
    row = db.get_lead(conn, lead_id)
    assert row is not None
    with pytest.raises(ValueError, match="finite positive amount"):
        drip.build_nugget(row)


def test_unverified_or_wrong_event_type_fails_closed(tmp_path: Path) -> None:
    """The builder independently enforces award evidence even outside candidate SQL."""
    conn = db.connect(tmp_path / "t.db")
    lead_id = mk_lead(conn)
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
    mk_lead(
        conn,
        iid="OPP1",
        entity="DOJ COPS Office",
        grade=LeadGrade.WATCH,
        source="grants.gov",
        amount=None,
        start="2026-07-01",
        end="2030-08-04",
        title="FY26 School Violence Prevention Program",
    )
    row = db.bulletin_candidates(conn, "C1")[0]
    text, style = drip.build_bulletin(row)
    assert "FY26 School Violence Prevention Program" in text
    assert (
        text
        == "FY26 School Violence Prevention Program is listed as open through 2030-08-04."
    )
    assert style == "bulletin-open"


def test_pick_prefers_top_scored_nugget(tmp_path: Path) -> None:
    """Verify pick prefers top scored nugget."""
    conn = db.connect(tmp_path / "t.db")
    mk_lead(conn, iid="OLD", entity="Old District", start="2022-10-01")
    mk_lead(
        conn, iid="FRESH", entity="Fresh District", start="2026-06-01", amount=150_000.0
    )
    kind, row = drip.pick(conn, "C1")
    assert kind == "nugget" and row["entity_name"] == "Fresh District"


def test_rfp_alert_is_short_human_and_actionable(tmp_path: Path) -> None:
    """The RFP alert names the entity, the subject, the deadline, and Chase's CTA."""
    conn = db.connect(tmp_path / "t.db")
    mk_rfp(conn)
    row = db.rfp_candidates(conn, "C1")[0]
    text, style = drip.build_rfp_alert(row)
    assert style == "rfp-open"
    assert text.startswith("City of Kemah has an open RFP for security cameras")
    assert "responses due 2030-12-31" in text
    assert text.endswith("Anybody want to talk?")


def test_rfp_alert_names_cameras_and_access_control(tmp_path: Path) -> None:
    """A dual-scope RFP is described as both."""
    conn = db.connect(tmp_path / "t.db")
    mk_rfp(conn, title="Access Control and Video Surveillance Camera System RFP")
    text, _ = drip.build_rfp_alert(db.rfp_candidates(conn, "C1")[0])
    assert "security cameras and access control" in text


def test_pick_prefers_a_gold_award_over_an_rfp(tmp_path: Path) -> None:
    """Grants outrank RFPs (Chase: an RFP is a lot of work and never beats a real award).
    The award here is >7 days old so it is a plain gold nugget, not platinum, yet wins
    over an open silver RFP."""
    conn = db.connect(tmp_path / "t.db")
    mk_lead(conn, iid="AWARD", entity="Fresh District", start="2026-06-01")
    mk_rfp(conn, iid="SRFP", entity="City of Kemah")  # silver open RFP
    kind, row = drip.pick(conn, "C1", today=date(2026, 7, 18))
    assert kind == "nugget" and row["entity_name"] == "Fresh District"


def test_pick_surfaces_platinum_for_a_fresh_security_grant(tmp_path: Path) -> None:
    """A verified SVPP award from the last few days is PLATINUM — the top card."""
    conn = db.connect(tmp_path / "t.db")
    mk_lead(conn, iid="PLAT", entity="Fresh District", start="2026-07-15")  # SVPP
    mk_rfp(conn, iid="SRFP", entity="City of Kemah")  # silver open RFP
    kind, row = drip.pick(conn, "C1", today=date(2026, 7, 18))
    assert kind == "platinum" and row["entity_name"] == "Fresh District"
    text, style = drip.build_platinum(row)
    assert style == "platinum"
    assert "just landed a verified" in text and "reaching out now" in text


def test_stale_award_is_not_platinum(tmp_path: Path) -> None:
    """An award older than the platinum window is a plain nugget, not platinum."""
    conn = db.connect(tmp_path / "t.db")
    mk_lead(conn, iid="OLD", entity="Old District", start="2026-06-01")  # >7 days
    kind, _row = drip.pick(conn, "C1", today=date(2026, 7, 18))
    assert kind == "nugget"


def test_pick_puts_a_silver_rfp_after_a_gold_award(tmp_path: Path) -> None:
    """An older (SILVER) open RFP ranks below a gold award, above a bulletin."""
    conn = db.connect(tmp_path / "t.db")
    mk_lead(conn, iid="AWARD", entity="Fresh District", start="2026-06-01")
    mk_rfp(conn, iid="SRFP", entity="City of Ames", grade=LeadGrade.SILVER)
    kind, row = drip.pick(conn, "C1")
    assert kind == "nugget" and row["entity_name"] == "Fresh District"


def test_pick_surfaces_a_silver_rfp_when_no_award(tmp_path: Path) -> None:
    """With no gold award, an open silver RFP still surfaces (before any bulletin)."""
    conn = db.connect(tmp_path / "t.db")
    mk_rfp(conn, iid="SRFP", entity="City of Ames", grade=LeadGrade.SILVER)
    kind, row = drip.pick(conn, "C1")
    assert kind == "rfp" and row["entity_name"] == "City of Ames"


def test_needs_testing_event_cannot_enter_proactive_notifications(
    tmp_path: Path,
) -> None:
    """An unverified Oregon-style positive remains searchable but is never pushed."""
    conn = db.connect(tmp_path / "t.db")
    lead_id = mk_lead(conn, iid="OREGON", entity="Oregon Test District")
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
    sf_lead = mk_lead(
        conn,
        iid="SF",
        entity="Salesforce District",
        start="2026-05-01",
        amount=300_000.0,
    )
    mk_lead(
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
    sf_lead = mk_lead(
        conn,
        iid="SF",
        entity="Salesforce District",
        start="2026-05-01",
        amount=300_000.0,
    )
    mk_lead(
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
    mk_lead(
        conn,
        iid="OPP1",
        entity="DOJ",
        grade=LeadGrade.WATCH,
        source="grants.gov",
        amount=None,
        end="2030-08-04",
        title="SVPP FY26",
    )
    kind, row = drip.pick(conn, "C1")
    assert kind == "bulletin"


def test_california_opportunity_can_become_bulletin(tmp_path: Path) -> None:
    """A fresh official California window is eligible for lower-tier news."""
    conn = db.connect(tmp_path / "t.db")
    mk_lead(
        conn,
        iid="CA-OPP",
        entity="California OES",
        grade=LeadGrade.WATCH,
        source="ca-grants-portal",
        amount=None,
        end="2030-08-04",
        title="School Security Grant",
    )
    row = db.bulletin_candidates(conn, "C1")[0]
    text, style = drip.build_bulletin(row)
    assert text == "School Security Grant is listed as open through 2030-08-04."
    assert style == "bulletin-open"


def test_drip_dry_run_writes_nothing(tmp_path: Path) -> None:
    """Verify drip dry run writes nothing."""
    conn = db.connect(tmp_path / "t.db")
    mk_lead(conn)
    out = drip.run_drip(None, "C1", conn, force=True, dry_run=True)
    assert out.startswith("[dry-run] would post nugget")
    assert conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM leads").fetchone()["status"] == "new"


def test_delivery_reservation_prevents_duplicate_post(tmp_path: Path) -> None:
    """The same funding event can be proactively delivered only once per channel."""
    conn = db.connect(tmp_path / "t.db")
    mk_lead(conn)
    client = SlackClient()
    first = drip.run_drip(client, "C1", conn, force=True)
    second = drip.run_drip(client, "C1", conn, force=True)
    assert first.startswith("posted nugget")
    assert second == "skip: nothing new worth saying"
    assert client.calls == 1
    # The rich-layout restyle (Chase 2026-08-05): blocks carry the same strings.
    assert client.last_kwargs["blocks"][0]["text"]["text"] == "GOLD · Verified award"
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
    mk_lead(conn)
    client = SlackClient(fail=True)
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
    lead_id = mk_lead(conn, iid="P1", start=recent, end="2031-09-30")
    client = SlackClient()
    out = drip.run_drip(client, "C1", conn, force=True)
    assert out.startswith("posted platinum")
    assert client.calls == 1
    assert conn.execute("SELECT kind FROM posts").fetchone()["kind"] == "platinum"
    assert (
        conn.execute("SELECT state FROM notification_outbox").fetchone()["state"]
        == "delivered"
    )
    assert (
        conn.execute("SELECT status FROM leads WHERE id=?", (lead_id,)).fetchone()[
            "status"
        ]
        == "surfaced"
    )


def test_run_drip_posts_open_rfp_end_to_end(tmp_path: Path) -> None:
    """An open silver RFP (no gold award available) posts as an 'rfp' kind and records
    cleanly. Guards C1: 'rfp' was also missing from the posts.kind CHECK."""
    conn = db.connect(tmp_path / "t.db")
    mk_rfp(conn, iid="R9", grade=LeadGrade.SILVER, end="2031-12-31")
    client = SlackClient()
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
