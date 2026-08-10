"""Buying contacts in bulk must be bounded, targeted, and never pay twice.

This module spends real money, so the tests that matter are the ones about the
ceiling holding and the credit going to the right person.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from grant_watch import contact_fill, db


@dataclass(frozen=True)
class _Match:
    """A ZoomInfo search hit, with only the fields ranking reads."""

    person_id: str
    job_title: str
    has_email: bool = True
    contact_accuracy_score: float = 90.0


def test_a_decision_maker_outranks_a_lacrosse_coach() -> None:
    """ZoomInfo returns whoever it has; an unranked list buys the wrong person.

    A real search of a school district returned Head Custodian, Head Lacrosse Coach
    and Head Volleyball Coach alongside the Interim CTO. Each costs the same credit.
    """
    ranked = contact_fill.rank_candidates(
        [
            _Match("1", "Head Lacrosse Coach"),
            _Match("2", "Chief Technology Officer"),
            _Match("3", "Head Custodian"),
            _Match("4", "Chief Business Officer"),
        ]
    )
    assert [m.person_id for m in ranked][:2] == ["2", "4"]


def test_a_contactable_person_outranks_a_higher_accuracy_one_without_email() -> None:
    """A perfect record with no way to reach them is worth less than a reachable one."""
    ranked = contact_fill.rank_candidates(
        [
            _Match(
                "1",
                "Director of Technology",
                has_email=False,
                contact_accuracy_score=99,
            ),
            _Match(
                "2", "Director of Technology", has_email=True, contact_accuracy_score=70
            ),
        ]
    )
    assert [m.person_id for m in ranked] == ["2", "1"]


def test_a_superintendent_still_beats_nobody() -> None:
    """Small districts have no technology title at all; ranking must not empty out."""
    ranked = contact_fill.rank_candidates([_Match("1", "Superintendent")])
    assert [m.person_id for m in ranked] == ["1"]


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    """A migrated database, never the developer's own."""
    return db.connect(tmp_path / "fill.db")


def _lead(conn: sqlite3.Connection, name: str) -> int:
    """One lead with the columns the preview reads."""
    cur = conn.execute(
        """INSERT INTO leads
             (entity_name,state,source,source_item_id,detail_url,lead_grade)
           VALUES (?,?,?,?,?,?)""",
        (name, "CA", "test", name, f"https://example.gov/{name}", "gold"),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def _patch_search(
    monkeypatch: pytest.MonkeyPatch, per_org: int, calls: list[int] | None = None
) -> None:
    """Make the FREE search return a fixed roster, recording each lead it saw."""
    from grant_watch.enrich import zoominfo_enrichment

    def fake_preview(_conn: object, lead_id: int, **_kw: object) -> object:
        """A preview built without touching the network."""
        if calls is not None:
            calls.append(lead_id)
        return zoominfo_enrichment.ZoomInfoPreview(
            lead_id=lead_id,
            entity_name="Test District",
            matches=tuple(
                _Match(f"{lead_id}-{i}", "Chief Technology Officer")
                for i in range(per_org)
            ),
            consumed=0,
            limit=1000,
        )

    monkeypatch.setattr(zoominfo_enrichment, "preview_for_lead", fake_preview)


def test_the_credit_ceiling_stops_the_run_before_money_moves(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ceiling is a LIMIT, not a report written after the spending.

    Five leads at two credits each is ten; a budget of five must buy two leads and
    decline the rest rather than discovering the overrun mid-purchase.
    """
    _patch_search(monkeypatch, per_org=3)
    ids = [_lead(conn, f"District {i}") for i in range(5)]
    out = contact_fill.fill_contacts(conn, ids, max_credits=5, dry_run=True)
    assert out.credits_spent <= 5
    assert out.filled == 2
    assert out.skipped_budget == 3
    conn.close()


def test_a_lead_that_already_has_a_contact_is_never_re_bought(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Paying twice for the same person is the easiest money to waste."""
    searched: list[int] = []
    _patch_search(monkeypatch, per_org=2, calls=searched)
    lead_id = _lead(conn, "Already Known")
    conn.execute(
        """INSERT INTO contacts (lead_id,name,title,email,contact_status)
           VALUES (?,?,?,?,'vendor_licensed')""",
        (lead_id, "Vic Chalabian", "IT Manager", "v@example.org"),
    )
    conn.commit()
    out = contact_fill.fill_contacts(conn, [lead_id], max_credits=50, dry_run=True)
    assert out.skipped_have_contact == 1
    assert out.credits_spent == 0
    assert searched == [], "it searched a lead it had no intention of buying"
    conn.close()


def test_a_linkedin_only_row_does_not_count_as_having_a_contact(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """73% of production contacts carry no email, phone or mobile at all.

    Treating those as "has a contact" would skip exactly the leads this exists for.
    """
    _patch_search(monkeypatch, per_org=2)
    lead_id = _lead(conn, "Only LinkedIn")
    conn.execute(
        """INSERT INTO contacts (lead_id,name,title,contact_status)
           VALUES (?,?,?,'linkedin_only')""",
        (lead_id, "Someone", "Teacher"),
    )
    conn.commit()
    out = contact_fill.fill_contacts(conn, [lead_id], max_credits=50, dry_run=True)
    assert out.skipped_have_contact == 0
    assert out.filled == 1
    conn.close()


def test_a_dry_run_spends_nothing_and_still_reports_the_bill(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator must be able to see the exact cost before authorising it."""
    _patch_search(monkeypatch, per_org=4)
    ids = [_lead(conn, f"D{i}") for i in range(3)]
    out = contact_fill.fill_contacts(conn, ids, max_credits=100, dry_run=True)
    assert out.credits_spent == 6  # 3 leads x PER_LEAD
    assert contact_fill.zoominfo_credits.usage(conn)[0] == 0, "a dry run billed"
    conn.close()


def test_no_matches_is_not_an_error(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Small rural districts are simply absent from ZoomInfo."""
    _patch_search(monkeypatch, per_org=0)
    out = contact_fill.fill_contacts(
        conn, [_lead(conn, "Tiny School")], max_credits=10, dry_run=True
    )
    assert out.skipped_no_match == 1
    assert out.credits_spent == 0
    conn.close()


def test_a_zero_budget_buys_nothing(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The degenerate case must decline rather than divide by zero or buy one anyway."""
    _patch_search(monkeypatch, per_org=2)
    out = contact_fill.fill_contacts(
        conn, [_lead(conn, "D")], max_credits=0, dry_run=True
    )
    assert out.credits_spent == 0
    assert out.filled == 0
    conn.close()
