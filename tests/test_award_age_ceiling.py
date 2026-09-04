"""The award-age ceiling: Grant never pushes an award older than six months, on any
surface, and stops chasing one the day it crosses the line.

Chase, 2026-09-04, on a manager nudge about a $499,730 award obligated October 2025:
"You're reminding everybody of a really really old lead ... We need to be reminding
people of the newest leads." Until then the only ceiling was the GOLD grade's twelve
months, so an eleven-month-old award was a card, a tag, and an escalation.

ONE constant, `scoring.CARD_MAX_AWARD_MONTHS`, is read by four surfaces: the rich card
policy (test_rich_policy.py pins that one), the fallback daily card, the daily list,
and the follow-up nudges. This file pins the last three and the helper itself, each
with a CONTROL proving the gate did not over-reach — an RFP card, an undated award,
and an award one day inside the line all still go out.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from drip_support import SlackClient, mk_lead, mk_rfp
from grant_watch import db, scoring
from grant_watch.slack import daily_list, drip

TODAY = date(2026, 9, 4)  # the day Chase asked; the cutoff is 2026-03-04


# ------------------------------------------------------------------ the helper
def test_the_cutoff_is_six_calendar_months_back() -> None:
    """Calendar months, not 180 days, so the rule reads the way a rep would say it."""
    assert scoring.CARD_MAX_AWARD_MONTHS == 6
    assert scoring.card_award_cutoff(TODAY) == date(2026, 3, 4)


def test_the_cutoff_clamps_the_day_and_wraps_the_year() -> None:
    """Aug 31 → Feb 28 (not a ValueError, not a silent 183-day window); Mar 30 → the
    previous September."""
    assert scoring.card_award_cutoff(date(2026, 8, 31)) == date(2026, 2, 28)
    assert scoring.card_award_cutoff(date(2026, 3, 30)) == date(2025, 9, 30)
    assert scoring.card_award_cutoff(date(2028, 8, 29)) == date(2028, 2, 29)  # leap


def test_freshness_is_inclusive_at_the_line_and_closed_on_bad_data() -> None:
    """Exactly six months is fresh; one day more is not; undated and future are
    never fresh, because a card's whole claim is recency and both are absent evidence."""
    assert scoring.award_is_card_fresh("2026-03-04", TODAY)
    assert not scoring.award_is_card_fresh("2026-03-03", TODAY)
    assert scoring.award_is_card_fresh("2026-09-04", TODAY)  # today itself
    assert not scoring.award_is_card_fresh("2026-09-05", TODAY)  # a future date
    assert not scoring.award_is_card_fresh("", TODAY)
    assert not scoring.award_is_card_fresh(None, TODAY)
    assert not scoring.award_is_card_fresh("not a date", TODAY)
    # The production shape: obligated 2025-10-10, judged the day the nudge went out.
    assert not scoring.award_is_card_fresh("2025-10-10", date(2026, 9, 4))


# ------------------------------------------------------------------ the daily card
def test_the_daily_card_never_picks_an_award_past_the_ceiling(tmp_path: Path) -> None:
    """Two gold awards, one seven months old and one five: only the five-month one is
    ever the card, whatever its score. The old one is STILL in `nugget_candidates` —
    the grade did not change, the push did."""
    conn = db.connect(tmp_path / "t.db")
    old = mk_lead(conn, iid="OLD", entity="Old District", start="2026-02-01")
    fresh = mk_lead(
        conn, iid="FRESH", entity="Fresh District", start="2026-04-01", amount=100_000.0
    )
    assert {int(r["id"]) for r in db.nugget_candidates(conn, "C1")} == {old, fresh}
    kind, row = drip.pick(conn, "C1", today=TODAY)
    assert kind == "nugget" and int(row["id"]) == fresh
    # CONTROL — the same pool judged when the old award was four months old picks it,
    # so the gate is the CLOCK, not the row.
    kind, row = drip.pick(conn, "C1", today=date(2026, 6, 1))
    assert kind == "nugget" and int(row["id"]) == old, "higher amount wins when fresh"


def test_with_only_old_gold_the_card_falls_through_to_an_rfp(tmp_path: Path) -> None:
    """An empty fresh pool behaves exactly like an empty pool: the ladder continues to
    silver RFPs and bulletins. It must never reach back for the old award."""
    conn = db.connect(tmp_path / "t.db")
    mk_lead(conn, iid="OLD", entity="Old District", start="2025-10-10")
    mk_rfp(conn, iid="R1", end="2031-12-31")
    choice = drip.pick(conn, "C1", today=TODAY)
    assert choice is not None and choice[0] == "rfp"
    # And with no RFP either, the honest answer is nothing at all.
    alone = db.connect(tmp_path / "alone.db")
    mk_lead(alone, iid="OLD", entity="Old District", start="2025-10-10")
    assert drip.pick(alone, "C1", today=TODAY) is None


def test_the_boundary_day_is_inside_for_the_daily_card(tmp_path: Path) -> None:
    """Exactly six months old still posts; the drip and the policy must agree on
    inclusivity or a card could be eligible on one path and not the other."""
    conn = db.connect(tmp_path / "t.db")
    mk_lead(conn, iid="EDGE", entity="Edge District", start="2026-03-04")
    choice = drip.pick(conn, "C1", today=TODAY)
    assert choice is not None and choice[1]["entity_name"] == "Edge District"
    assert drip.pick(conn, "C1", today=date(2026, 9, 5)) is None


def test_run_drip_judges_the_ceiling_on_the_tick_s_own_clock(tmp_path: Path) -> None:
    """The tick passes `now.date()` through; a wall-clock read here would be the
    2026-08-26 poll-lease defect again."""
    conn = db.connect(tmp_path / "t.db")
    mk_lead(conn, iid="OLD", entity="Old District", start="2025-10-10")
    client = SlackClient()
    at = datetime(2026, 9, 4, 17, 0, tzinfo=timezone.utc)
    assert drip.run_drip(client, "C1", conn, force=True, now=at).startswith("skip:")
    assert client.calls == 0
    early = datetime(2026, 1, 15, 17, 0, tzinfo=timezone.utc)
    assert drip.run_drip(client, "C1", conn, force=True, now=early).startswith(
        "posted nugget"
    )
    assert "about 3 months ago" in client.last_kwargs["text"]


# ------------------------------------------------------------------ the daily list
def test_the_list_is_variable_length_and_stops_at_the_ceiling(tmp_path: Path) -> None:
    """Twenty-five is a cap, not a quota. Three fresh awards and thirty old ones make
    a list of three — never three plus twenty-two stale ones to look busy."""
    conn = db.connect(tmp_path / "t.db")
    for n in range(30):
        mk_lead(conn, iid=f"OLD{n}", entity=f"Old {n}", start="2025-10-10")
    for n, day in enumerate(("2026-08-30", "2026-08-31", "2026-09-01")):
        mk_lead(conn, iid=f"NEW{n}", entity=f"New {n}", start=day)
    rows = daily_list.candidates(conn, "C1", 25, today=TODAY)
    assert [r["entity_name"] for r in rows] == ["New 2", "New 1", "New 0"]
    # CONTROL — the same pool judged in December 2025 lists the whole cohort.
    assert len(daily_list.candidates(conn, "C1", 25, today=date(2025, 12, 1))) == 25


def test_the_list_boundary_matches_the_card_s(tmp_path: Path) -> None:
    """Inclusive at exactly six months, and a future date is never listed."""
    conn = db.connect(tmp_path / "t.db")
    mk_lead(conn, iid="EDGE", entity="Edge", start="2026-03-04")
    mk_lead(conn, iid="OUT", entity="Out", start="2026-03-03")
    mk_lead(conn, iid="FUT", entity="Future", start="2026-09-05")
    assert [r["entity_name"] for r in daily_list.candidates(conn, "C1", 25, TODAY)] == [
        "Edge"
    ]


def test_a_quiet_day_posts_a_short_list_not_an_old_one(tmp_path: Path) -> None:
    """`run` carries the tick's date into the query, so the posted list is the short
    honest one and the old awards are neither shown nor consumed."""
    conn = db.connect(tmp_path / "t.db")
    for n in range(10):
        mk_lead(conn, iid=f"OLD{n}", entity=f"Old {n}", start="2025-10-10")
    mk_lead(conn, iid="NEW", entity="New District", start="2026-08-30")

    class _Slack:
        kwargs: dict[str, object] = {}

        def chat_postMessage(self, **kwargs: object) -> dict[str, str]:  # noqa: N802
            """Record the one post and answer with a Slack-shaped ts."""
            self.kwargs = kwargs
            return {"ts": "1788300000.000100"}

    client = _Slack()
    at = datetime(2026, 9, 4, 18, 2, tzinfo=timezone.utc)
    outcome = daily_list.run(client, "C1", conn, limit=25, now=at)
    assert outcome.startswith("posted"), outcome
    consumed = conn.execute("SELECT COUNT(*) FROM daily_list_items").fetchone()[0]
    assert consumed == 1
    assert "Old" not in str(client.kwargs.get("text", ""))
    assert "New District" in str(client.kwargs.get("text", ""))


# ------------------------------------------------------------------ the follow-ups
def _isolated(tmp_path: Path, label: str) -> Path:
    """A directory of its own per scenario; `_conn` opens a file inside it."""
    sub = tmp_path / label
    sub.mkdir()
    return sub


def _card_kinds(conn: object, now: datetime) -> set[str]:
    """The card follow-up kinds `nudges.candidates` would raise for the one card."""
    from grant_watch.slack import nudges

    return {
        c.subject_kind
        for c in nudges.candidates(conn, now)  # type: ignore[arg-type]
        if c.subject_kind in {"card_unengaged", "card_escalated"}
    }


def test_an_old_award_card_is_neither_chased_nor_escalated(tmp_path: Path) -> None:
    """The Cuba City shape: a gold card posted two days ago about an award obligated
    2025-10-10, judged on 2026-09-04. No follow-up to the room, no escalation to the
    manager — nobody gets tagged about it again."""
    from nudge_helpers import NOW, _card, _conn

    judged = datetime(2026, 9, 4, 18, 0, tzinfo=timezone.utc)
    conn = _conn(tmp_path)
    _card(conn, judged - timedelta(days=2), awarded_on="2025-10-10")
    assert _card_kinds(conn, judged) == set()
    conn.close()
    # CONTROL — the identical card judged when the award was six weeks old raises
    # both, so the gate is the award's age and nothing else about the row.
    conn = _conn(_isolated(tmp_path, "ctl"))
    _card(conn, NOW - timedelta(days=2), awarded_on="2026-07-01")
    assert _card_kinds(conn, NOW) == {"card_unengaged", "card_escalated"}
    conn.close()


def test_the_gate_reads_only_evidence_of_age_never_its_absence(tmp_path: Path) -> None:
    """Three controls, each a card the ceiling must leave alone: an RFP card whose
    event is old (a deadline, not an obligation); an award card with an event but no
    date; and a card with no event at all — the shape every pre-existing follow-up
    test uses."""
    from nudge_helpers import NOW, _card, _conn

    for label, kwargs in {
        "rfp": {
            "kind": "rfp",
            "style": "silver",
            "awarded_on": "2024-01-01",
            "event_type": "solicitation_published",
        },
        "undated": {"awarded_on": ""},
        "no-event": {},
    }.items():
        conn = _conn(_isolated(tmp_path, label))
        _card(conn, NOW - timedelta(days=2), **kwargs)  # type: ignore[arg-type]
        assert "card_unengaged" in _card_kinds(conn, NOW), f"{label} card was gated"
        conn.close()


def test_the_follow_up_line_is_the_card_line(tmp_path: Path) -> None:
    """A card exactly at the ceiling is still chased; one day past it is not. Judged in
    business time, so a card is not dropped at 17:00 Pacific because UTC rolled over."""
    from nudge_helpers import _card, _conn

    # 2026-09-04 23:30 UTC is still 2026-09-04 in Pacific; the cutoff is 2026-03-04.
    judged = datetime(2026, 9, 4, 23, 30, tzinfo=timezone.utc)
    conn = _conn(_isolated(tmp_path, "edge"))
    _card(conn, judged - timedelta(days=2), awarded_on="2026-03-04")
    assert "card_unengaged" in _card_kinds(conn, judged)
    conn.close()
    conn = _conn(_isolated(tmp_path, "past"))
    _card(conn, judged - timedelta(days=2), awarded_on="2026-03-03")
    assert _card_kinds(conn, judged) == set()
    conn.close()
