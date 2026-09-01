"""Every award card states how OLD the award is, not just when it happened.

A rep read "Federal funds obligated October 10, 2025" on a card, phoned the district,
and was told it would have been great if he had called a year ago — the replacement
was already finishing with a competitor. The date was on that card. Measured
afterwards: every award card this product has ever posted was between 277 and 653 days
old, median 301, and 34 of the 44 carried NO date at all. The age is the one number a
rep needs to judge an award lead, and it was the one number no card ever printed.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from grant_watch import db
from grant_watch.presentation import award_age_phrase
from grant_watch.slack import drip

from drip_support import mk_lead

TODAY = date(2026, 9, 1)


@pytest.fixture()
def gold_row(tmp_path: Path) -> sqlite3.Row:
    """One verified gold award, obligated 2025-10-01 — the real cohort's shape."""
    conn = db.connect(tmp_path / "age.db")
    row = db.get_lead(conn, mk_lead(conn))
    assert row is not None
    return row


def test_the_legacy_card_now_carries_the_date_and_the_age(
    gold_row: sqlite3.Row,
) -> None:
    """This is the card 34 of 44 posts used, and it had no temporal content at all."""
    text, _style = drip.build_nugget(gold_row, TODAY)
    assert "October 1, 2025" in text
    assert "about 11 months ago" in text


def test_the_card_is_still_one_sentence(gold_row: sqlite3.Row) -> None:
    """Chase's rule for this card. The age is a clause, never a second sentence."""
    text, _style = drip.build_nugget(gold_row, TODAY)
    assert text.count(".") == 1
    assert "\n" not in text


def test_the_age_moves_with_the_caller_s_clock(gold_row: sqlite3.Row) -> None:
    """The renderer takes the tick's clock, not the wall clock.

    The 2026-08-26 poll-lease defect was exactly this asymmetry — one function on an
    injected clock and its neighbour on the real one, which passes on a fast machine
    and fails on a slow one for reasons nobody changed.
    """
    assert "19 days ago" in drip.build_nugget(gold_row, date(2025, 10, 20))[0]
    assert "about 11 months ago" in drip.build_nugget(gold_row, TODAY)[0]
    assert "about 2 years ago" in drip.build_nugget(gold_row, date(2028, 1, 1))[0]


def test_an_undated_award_states_no_age_rather_than_guessing(
    tmp_path: Path,
) -> None:
    """No date means no phrase — never "about 0 months ago", never a raise.

    Three real cards were posted in July from events the pollers had not yet dated. A
    renderer that raised would quarantine a genuine lead over a missing nicety; one
    that guessed would assert a recency nothing measured.
    """
    conn = db.connect(tmp_path / "undated.db")
    lead_id = mk_lead(conn)
    conn.execute(
        "UPDATE funding_events SET occurred_on=NULL WHERE id="
        "(SELECT current_event_id FROM leads WHERE id=?)",
        (lead_id,),
    )
    conn.commit()
    row = db.get_lead(conn, lead_id)
    assert row is not None
    text, _style = drip.build_nugget(row, TODAY)
    assert "ago" not in text
    assert text.count(".") == 1
    assert text.endswith("funding award.")


@pytest.mark.parametrize(
    ("occurred", "expected"),
    [
        ("2026-09-01", "today"),
        ("2026-08-31", "1 day ago"),
        ("2026-08-20", "12 days ago"),
        ("2026-08-02", "30 days ago"),  # days win right up to the boundary
        ("2026-07-20", "about 1 month ago"),  # 43 days rounds to one month
        ("2025-10-10", "about 11 months ago"),
        ("2025-08-15", "over a year ago"),
        ("2023-01-05", "about 4 years ago"),
        ("", ""),
        ("not-a-date", ""),
        ("2027-01-01", ""),  # a future award date is bad data, not a fresh lead
    ],
)
def test_age_phrases_read_the_way_a_person_would_say_them(
    occurred: str, expected: str
) -> None:
    """Including the two that must say NOTHING."""
    assert award_age_phrase(occurred, TODAY) == expected


def test_the_rich_card_face_states_the_age_too() -> None:
    """The other renderer. Post 48 — the card that prompted this — was this shape.

    It already printed "Federal funds obligated October 10, 2025" in plain sight, and
    the rep still could not tell at a glance that the money was ten months gone. The
    date was never the missing piece; the age was.
    """
    from dataclasses import replace as _replace

    from grant_watch.campaign import card
    from grant_watch.campaign.snapshot import FrozenSnapshot
    from tests.test_rich_snapshot import _draft

    draft = _replace(_draft(), award_date="2025-10-10", award_date_precision="day")
    draft = _replace(draft, fallback_text=card.fallback_text(draft, TODAY))
    rendered = card.render(
        FrozenSnapshot("a" * 32, 1, "2026-09-01T18:00:00+00:00", draft), TODAY
    )
    face = str(rendered.blocks)
    assert "October 10, 2025" in face
    assert "about 11 months ago" in face, "the age must be on the card face itself"
    assert "about 11 months ago" in rendered.text, "and in the notification text"


def test_the_rich_card_age_takes_the_caller_s_clock() -> None:
    """The control: it is not a constant string baked into the renderer."""
    from dataclasses import replace as _replace

    from grant_watch.campaign import card
    from tests.test_rich_snapshot import _draft

    draft = _replace(_draft(), award_date="2025-10-10", award_date_precision="day")
    assert "12 days ago" in card.fallback_text(draft, date(2025, 10, 22))
    assert "about 11 months ago" in card.fallback_text(draft, TODAY)
