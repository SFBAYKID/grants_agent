"""Truthful application answers and bounded deterministic LinkedIn research tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from grant_watch import db
from grant_watch.models import (
    FundingEventType,
    Lead,
    LeadGrade,
    RawItem,
    VerificationStatus,
)
from grant_watch.slack import conversation


def _award(tmp_path: Path) -> sqlite3.Row:
    """Persist one award whose source contains no application-submission fields."""
    conn = db.connect(tmp_path / "application.db")
    db.upsert_lead(conn, Lead(RawItem(
        source="usaspending:16.071", item_id="15JCOPS25GG01291SSIX",
        title="SVPP school security and technology",
        entity="BIRMINGHAM COMMUNITY CHARTER HIGH SCHOOL", state="CA",
        program="SVPP", amount=500_000, start="2025-10-01", end="2028-09-30",
        url="https://www.usaspending.gov/award/ASST_NON_15JCOPS25GG01291SSIX_015",
        raw={}, event_type=FundingEventType.AWARD_OBLIGATED,
        verification_status=VerificationStatus.VERIFIED,
        evidence_excerpt="SVPP school security and technology"), LeadGrade.GOLD))
    row = db.get_lead(conn, 1)
    assert row is not None
    return row


def _forbid_model(**_kwargs: object) -> object:
    """Fail if a deterministic research question reaches the language model."""
    raise AssertionError("deterministic research route called the model")


@pytest.mark.parametrize("question", [
    "Who applied for this?",
    "Hey Grant, how applied for this?",
    "Where was the application submitted?",
])
def test_award_record_never_becomes_proof_of_applicant(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, question: str) -> None:
    """Recipient identity cannot fabricate an applicant, parent, portal, or method."""
    monkeypatch.setattr(conversation, "Anthropic", _forbid_model)
    result = conversation.respond(question, _award(tmp_path))
    reply = result["reply"]
    assert "Confirmed award recipient:* Birmingham Community Charter High School" in reply
    assert "Applicant or submitter:* not published" in reply
    assert "Application portal or submission method:* not published" in reply
    assert "Montebello" not in reply and "applied straight" not in reply
    assert "15JCOPS25GG01291SSIX" in reply


def test_explicit_linkedin_request_bypasses_model_and_returns_final_reply(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A LinkedIn request makes one bounded tool call and cannot stay in a model loop."""
    monkeypatch.setattr(conversation, "Anthropic", _forbid_model)
    calls: list[tuple[str, str]] = []

    def lookup(entity: str, state: str, _progress: object, **_context: object) -> str:
        """Return one possible search-result match."""
        calls.append((entity, state))
        return "I found a possible LinkedIn contact:\n\n• *Name:* Pat Person"

    monkeypatch.setattr(conversation.tools, "find_person_linkedin", lookup)
    result = conversation.respond(
        "Sure yes look at linkedin", _award(tmp_path),
        on_progress=lambda _message: None, requester_slack="UCHASE",
        workspace="TWORK", channel="CGRANTS", thread_ts="1.1")
    assert calls == [("BIRMINGHAM COMMUNITY CHARTER HIGH SCHOOL", "CA")]
    assert result["reply"].startswith("I found a possible LinkedIn contact")
    assert result["pending_crm_actions"] == []


def test_linkedin_failure_always_returns_honest_final_message(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Unexpected research failure resolves the turn without exposing internals."""
    monkeypatch.setattr(
        conversation.tools, "find_person_linkedin",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("secret detail")))
    result = conversation.respond("Please search LinkedIn", _award(tmp_path))
    assert "couldn’t complete" in result["reply"]
    assert "won’t guess" in result["reply"]
    assert "secret detail" not in result["reply"]
