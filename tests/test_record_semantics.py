"""Cross-consumer truth tests: every surface must describe a record the SAME way.

Record meaning was derived independently in five places, two of them from `lead_grade`.
When undated California AWARDS were regraded GOLD->SILVER, those two began calling real
awards "published solicitations" and relabelling an award's spend-window end a "response
deadline" — in Slack, in exports, in a permanent Salesforce note, and in outbound email.

These tests pin the rule: GRADE decides PRIORITY, EVENT TYPE decides WHAT HAPPENED.
Each one is written so that reverting to a grade-driven branch fails it.
"""

from __future__ import annotations

import json
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


# ------------------------------------------- payload must assert nothing prose denies
_ALL_EVENT_TYPES = [
    FundingEventType.AWARD_OBLIGATED,
    FundingEventType.AWARD_ANNOUNCED,
    FundingEventType.RFP_POSTED,
    FundingEventType.APPLICATION_WINDOW_OPENED,
    FundingEventType.RECORD_OBSERVED,
]


@pytest.mark.parametrize("event_type", _ALL_EVENT_TYPES)
def test_payload_asserts_nothing_the_semantics_deny(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, event_type: FundingEventType
) -> None:
    """THE C-1 REGRESSION, across EVERY event type.

    The prose surfaces all correctly refused to say money was awarded — and the payload
    shipped `amount_usd` anyway. Persequor is an LLM: `program='SVPP'` plus
    `amount_usd=487657` IS an award claim, however hedged `angle` is. Every fact-bearing
    field must be gated on the matching `asserts_*` facet, not merely on presence.
    """
    monkeypatch.setenv("OUTREACH_TEST_EMAIL", "chase@monarchconnected.com")
    conn = db.connect(tmp_path / "t.db")
    row = _lead(conn, event_type, LeadGrade.GOLD)
    meaning = semantics_for(row)
    brief = persequor_client.build_brief(
        row, None, "U01DPJVURHU", "chase@monarchconnected.com"
    )
    assert brief is not None
    if meaning.asserts_amount:
        assert brief["amount_usd"] == 487657
    else:
        assert brief["amount_usd"] is None, "a money figure escaped on a non-award"
    if meaning.asserts_dates:
        assert brief["window_end"] == "2028-09-30"
    else:
        assert brief["window_start"] is None and brief["window_end"] is None
        assert brief["expires_at"] is None
    assert brief["angle"] == meaning.angle


def test_payload_keeps_the_pinned_v1_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`outreach-request.v1` is an EXTERNAL pinned contract. An added key would 422
    every brief if Persequor forbids extras — which is unknown and unasked. Assert the
    exact serialized key set, so a future field cannot slip in unnoticed."""
    monkeypatch.setenv("OUTREACH_TEST_EMAIL", "chase@monarchconnected.com")
    conn = db.connect(tmp_path / "t.db")
    row = _lead(conn, FundingEventType.AWARD_OBLIGATED, LeadGrade.GOLD)
    brief = persequor_client.build_brief(
        row, None, "U01DPJVURHU", "chase@monarchconnected.com"
    )
    assert brief is not None
    assert set(json.loads(json.dumps(brief))) == {
        "schema", "request_id", "entity", "entity_type", "state", "program",
        "amount_usd", "window_start", "window_end", "source_url",
        "requested_by_slack", "send_as", "contact_name", "contact_email",
        "contact_title", "angle", "rep_notes", "expires_at", "slack_channel",
        "slack_thread_ts",
    }
    assert brief["schema"] == "outreach-request.v1"


@pytest.mark.parametrize("event_type", _ALL_EVENT_TYPES)
def test_fallback_draft_and_payload_describe_the_same_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, event_type: FundingEventType
) -> None:
    """`compose_draft` is FALLBACK copy — `submit_brief` POSTs first and the rep only
    sees the draft when submission FAILED. So this is not "what a human approves"; it
    is the guarantee that the two descriptions of one record cannot diverge."""
    monkeypatch.setenv("OUTREACH_TEST_EMAIL", "chase@monarchconnected.com")
    conn = db.connect(tmp_path / "t.db")
    row = _lead(conn, event_type, LeadGrade.SILVER)
    draft = persequor.compose_draft(row)
    brief = persequor_client.build_brief(
        row, None, "U01DPJVURHU", "chase@monarchconnected.com"
    )
    assert brief is not None
    money_in_draft = "487,657" in draft
    assert money_in_draft == (brief["amount_usd"] is not None), event_type
    date_in_draft = "2028-09-30" in draft
    assert date_in_draft == (brief["window_end"] is not None), event_type


@pytest.mark.parametrize("event_type", _ALL_EVENT_TYPES)
def test_salesforce_note_never_implies_an_unestablished_award(
    tmp_path: Path, event_type: FundingEventType
) -> None:
    """The CRM note is create-only and permanent. A bare `SVPP · $487,657` headline
    reads as an award no matter what the body says."""
    conn = db.connect(tmp_path / "t.db")
    row = _lead(conn, event_type, LeadGrade.GOLD)
    meaning = semantics_for(row)
    headline = sf._grant_headline(row)
    assert ("487,657" in headline) == meaning.asserts_amount, headline
    assert ("spend window" in headline) == meaning.asserts_award, headline


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


def test_event_type_is_required_and_cannot_default_to_unknown() -> None:
    """The default that MANUFACTURED unknown records is gone.

    `RawItem.event_type` used to default to RECORD_OBSERVED, so a source that forgot the
    field produced rows asserting nothing forever — and three test fixtures silently
    built "awards" that were not awards, which is how a grade-driven wording defect
    reached outbound email undetected. Omitting it must now be a construction error, not
    a silent downgrade."""
    with pytest.raises(TypeError, match="event_type"):
        RawItem(  # type: ignore[call-arg]
            source="test",
            item_id="X",
            title="t",
            entity="e",
            state="CA",
            program="p",
            amount=None,
            start="",
            end="",
            url="",
            raw={},
        )
