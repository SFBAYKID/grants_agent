"""Cross-consumer truth tests: every surface must describe a record the SAME way.

Record meaning was derived independently in five places, two of them from `lead_grade`.
When undated California AWARDS were regraded GOLD->SILVER, those two began calling real
awards "published solicitations" and relabelling an award's spend-window end a "response
deadline" — in Slack, in exports, in a permanent Salesforce note, and in outbound email.

These tests pin the rule: GRADE decides PRIORITY, EVENT TYPE decides WHAT HAPPENED.
Each one is written so that reverting to a grade-driven branch fails it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from grant_watch import db, persequor_client
from grant_watch.enrich import salesforce_contact_records as sf
from grant_watch.models import (
    DatePrecision,
    FundingEventType,
    Lead,
    LeadGrade,
    RawItem,
    VerificationStatus,
)
from grant_watch.record_semantics import RecordKind, semantics_for
from grant_watch.slack import persequor, search_presentation


def _lead(
    conn: sqlite3.Connection,
    event_type: FundingEventType,
    grade: LeadGrade,
    iid: str = "L1",
) -> sqlite3.Row:
    """Insert one lead with an explicit event type and grade, return the JOINED row."""
    db.upsert_lead(
        conn,
        Lead(
            item=RawItem(
                source="usaspending:16.071",
                item_id=iid,
                title="Security work",
                entity="Montebello Unified School District",
                state="CA",
                program="SVPP",
                amount=487_657.0,
                start="2025-10-01",
                end="2028-09-30",
                url="https://www.usaspending.gov/award/X",
                raw={},
                event_type=event_type,
                event_date="2025-10-10",
                date_precision=DatePrecision.DAY,
                verification_status=VerificationStatus.VERIFIED,
            ),
            grade=grade,
        ),
    )
    row = conn.execute(
        "SELECT id FROM leads WHERE source_item_id=?", (iid,)
    ).fetchone()
    joined = db.get_lead(conn, int(row["id"]))
    assert joined is not None
    return joined


# ------------------------------------------------------------------ the core rule
@pytest.mark.parametrize(
    ("event_type", "expected"),
    [
        (FundingEventType.AWARD_OBLIGATED, RecordKind.AWARD),
        (FundingEventType.AWARD_ANNOUNCED, RecordKind.AWARD),
        (FundingEventType.RFP_POSTED, RecordKind.SOLICITATION),
        (FundingEventType.APPLICATION_WINDOW_OPENED, RecordKind.FUNDING_OPPORTUNITY),
        (FundingEventType.RECORD_OBSERVED, RecordKind.UNKNOWN),
    ],
)
def test_kind_follows_the_event_not_the_grade(
    tmp_path: Path, event_type: FundingEventType, expected: RecordKind
) -> None:
    """The same event type yields the same kind under EVERY grade."""
    conn = db.connect(tmp_path / "t.db")
    for index, grade in enumerate(
        (LeadGrade.GOLD, LeadGrade.SILVER, LeadGrade.WATCH)
    ):
        row = _lead(conn, event_type, grade, iid=f"L{index}")
        assert semantics_for(row).kind is expected, f"{grade} changed the kind"


def test_a_silver_award_is_an_award_on_every_surface(tmp_path: Path) -> None:
    """THE REGRESSION. One silver AWARD row, checked across all five consumers — each
    of which must call it an award and none of which may call it a solicitation."""
    conn = db.connect(tmp_path / "t.db")
    row = _lead(conn, FundingEventType.AWARD_OBLIGATED, LeadGrade.SILVER)

    date_context = search_presentation.window_label(row)
    assert "spend window" in date_context
    assert "response due" not in date_context

    assert search_presentation.entity_role_for_row(row) == "award recipient"

    angle = persequor_client._angle(row)
    assert "award" in angle and "solicitation" not in angle

    draft = persequor.compose_draft(row)
    assert "spend window" in draft.lower()
    assert "solicitation" not in draft.lower()
    assert "response deadline" not in draft.lower()

    note = sf._grant_summary(row)
    assert "grant" in note and "solicitation" not in note
    assert "spend window" in sf._grant_headline(row)


def test_a_gold_solicitation_is_a_solicitation_on_every_surface(
    tmp_path: Path,
) -> None:
    """The mirror image: a GOLD grade must not turn an RFP into an award."""
    conn = db.connect(tmp_path / "t.db")
    row = _lead(conn, FundingEventType.RFP_POSTED, LeadGrade.GOLD)

    assert "response due" in search_presentation.window_label(row)
    assert "spend window" not in search_presentation.window_label(row)
    assert search_presentation.entity_role_for_row(row) == "posting organization"
    assert "solicitation" in persequor_client._angle(row)
    draft = persequor.compose_draft(row)
    assert "solicitation" in draft.lower() and "spend window" not in draft.lower()
    assert "solicitation" in sf._grant_summary(row)
    assert "spend window" not in sf._grant_headline(row)


def test_unknown_event_claims_nothing_anywhere(tmp_path: Path) -> None:
    """`record_observed` is what migration 6 backfilled onto pre-existing leads, so
    this is a common shape. It must assert no award, no solicitation, and no date
    purpose — rather than guessing one from the grade."""
    conn = db.connect(tmp_path / "t.db")
    row = _lead(conn, FundingEventType.RECORD_OBSERVED, LeadGrade.GOLD)
    draft = persequor.compose_draft(row).lower()
    for claim in ("solicitation", "spend window", "response deadline", "application deadline"):
        assert claim not in draft, claim
    assert "purpose unverified" in search_presentation.window_label(row)
    assert "dates unverified" in sf._grant_summary(row)


# ------------------------------------------------- preview and payload must agree
def test_preview_and_payload_never_disagree_about_dates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The human approves `compose_draft`; Persequor writes from `build_brief`. They
    derive from the same object, so a date the preview omits cannot ride along in the
    payload — which it previously did."""
    monkeypatch.setenv("OUTREACH_TEST_EMAIL", "chase@monarchconnected.com")
    conn = db.connect(tmp_path / "t.db")
    for index, event_type in enumerate(
        (FundingEventType.AWARD_OBLIGATED, FundingEventType.RECORD_OBSERVED)
    ):
        row = _lead(conn, event_type, LeadGrade.SILVER, iid=f"P{index}")
        draft = persequor.compose_draft(row)
        brief = persequor_client.build_brief(
            row, None, "U01DPJVURHU", "chase@monarchconnected.com"
        )
        assert brief is not None
        shows_date_in_preview = "2028-09-30" in draft
        ships_date_in_payload = brief["window_end"] is not None
        assert shows_date_in_preview == ships_date_in_payload, event_type
        assert brief["window_meaning"] == (
            semantics_for(row).window_noun or "unknown"
        )


# ------------------------------------------------- no internal identifiers escape
@pytest.mark.parametrize(
    "event_type",
    [
        FundingEventType.AWARD_OBLIGATED,
        FundingEventType.RFP_POSTED,
        FundingEventType.APPLICATION_WINDOW_OPENED,
        FundingEventType.RECORD_OBSERVED,
    ],
)
def test_outbound_copy_never_contains_an_internal_source_key(
    tmp_path: Path, event_type: FundingEventType
) -> None:
    """A draft once read "A public seed:svpp_csv record lists Alpine Union…". Internal
    identifiers must never reach a school administrator's inbox."""
    conn = db.connect(tmp_path / "t.db")
    row = _lead(conn, event_type, LeadGrade.GOLD)
    draft = persequor.compose_draft(row)
    body = draft.split("Subject:", 1)[1]
    assert str(row["source"]) not in body, body
    for token in ("usaspending:", "ca-grants-award:", "seed:", "16.071"):
        assert token not in body, token
