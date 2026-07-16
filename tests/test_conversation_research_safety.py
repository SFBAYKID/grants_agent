"""Truthful application answers and bounded deterministic LinkedIn research tests."""

from __future__ import annotations

import json
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


def test_location_question_uses_verified_official_site_route(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An exact-location request does not stop at the award's state field."""
    monkeypatch.setattr(
        conversation.organization, "find_organization_details",
        lambda entity, state, _progress: (
            f"{entity} is at 17000 Haynes Street, Van Nuys, {state}."))
    result = conversation.respond(
        "Where is the school located?", _award(tmp_path),
        on_progress=lambda _message: None)
    assert "17000 Haynes Street" in result["reply"]
    assert "Van Nuys" in result["reply"]


def test_approval_date_never_substitutes_spend_or_discovery_date(tmp_path: Path) -> None:
    """An unknown approval date is answered deterministically from award evidence."""
    result = conversation.respond("When was the funding approved?", _award(tmp_path))
    assert "Approval date:* not published" in result["reply"]
    assert "2025-10-01 to 2028-09-30" in result["reply"]
    assert "this is not an approval date" in result["reply"]
    assert "15JCOPS25GG01291SSIX" in result["reply"]


def test_exact_award_record_is_always_a_clickable_record_link(tmp_path: Path) -> None:
    """The exact-award route cannot let the model strip the record URL."""
    result = conversation.respond(
        "Show me the exact government award record", _award(tmp_path))
    assert "<https://www.usaspending.gov/award/ASST_NON_15JCOPS25GG01291SSIX_015|" in result["reply"]


def test_malformed_model_output_fails_closed_without_developer_language() -> None:
    """Broken prose plus partial JSON is never copied into Slack."""
    raw = ("The enrichment tool (salesforce_lead_enrichment_preview) takes a contact ID. "
           '... as tool {"intent": "question", "reply": "broken"}')
    result = conversation._parse_final(raw)
    assert result["reply"] == (
        "I couldn’t finish that request safely. Nothing was changed. Please try again.")
    assert "intent" not in result["reply"] and "salesforce_" not in result["reply"]


def test_valid_json_with_internal_tool_language_also_fails_closed() -> None:
    """A syntactically valid envelope cannot expose an internal tool name."""
    raw = json.dumps({"intent": "question",
                      "reply": "I need salesforce_lead_enrichment_preview and a contact ID."})
    assert conversation._parse_final(raw)["reply"].startswith("I couldn’t finish")


@pytest.mark.parametrize("reply", [
    "Use salesforce_organization_lead_enrichment_preview next.",
    "The request failed with ValueError.",
    "ERROR: the Salesforce request did not finish.",
])
def test_every_internal_reply_shape_fails_closed(reply: str) -> None:
    """Valid JSON still cannot carry tool identifiers or technical error language."""
    raw = json.dumps({"intent": "question", "reply": reply})
    assert conversation._parse_final(raw)["reply"].startswith("I couldn’t finish")


def test_canned_developer_style_opener_is_removed() -> None:
    """Grant starts with the answer instead of canned acknowledgment language."""
    raw = json.dumps({"intent": "question",
                      "reply": "Great question. The verified address is 17000 Haynes St."})
    assert conversation._parse_final(raw)["reply"].startswith("The verified address")


def test_bot_history_cannot_select_a_grant_lead() -> None:
    """Only a rep turn may carry a Grant lead reference into a fresh-chat follow-up."""
    history = ["rep: Use Grant lead 231.", "Grant: I also know Grant lead 999."]
    assert conversation._explicit_grant_lead_id("Fill its address", history) == 231


def test_multiple_grant_leads_in_one_turn_are_ambiguous() -> None:
    """A CRM preview cannot silently pick one record from a multi-lead request."""
    assert conversation._explicit_grant_lead_id(
        "Use Grant leads 231 and Grant lead 232") is None


def test_fresh_chat_lead_resolution_reads_only_the_exact_local_id(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A fresh-chat identifier resolves against the tenant-local database and can be stale."""
    database = tmp_path / "application.db"
    _award(tmp_path)
    real_connect = db.connect
    monkeypatch.setattr(conversation.db, "connect", lambda: real_connect(database))
    assert conversation._load_referenced_lead("Use Grant lead 1", None)["id"] == 1
    assert conversation._load_referenced_lead("Use Grant lead 999", None) is None


def test_fresh_chat_org_enrichment_uses_deterministic_preview(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An explicit Grant lead in a fresh chat bypasses the model and needs no email."""
    row = _award(tmp_path)
    monkeypatch.setattr(conversation, "_load_referenced_lead", lambda *_args: row)
    monkeypatch.setattr(conversation, "Anthropic", _forbid_model)
    calls: list[int] = []

    def preview(lead_id: int, *_args: str) -> str:
        """Return one immutable fake organization enrichment action."""
        calls.append(lead_id)
        marker = {"action_id": "action-1", "nonce": "nonce-1",
                  "preview": "Fill the blank address fields?",
                  "expires_at": "2026-07-16T01:00:00+00:00"}
        return f"<grant-crm-action>{json.dumps(marker)}</grant-crm-action>"

    monkeypatch.setattr(
        conversation.tools, "salesforce_organization_lead_enrichment_preview", preview)
    result = conversation.respond(
        "Use Grant lead 231. Update the existing Salesforce Lead's blank address, "
        "website, phone, and notes. Show a preview only.", None,
        requester_slack="UCHASE", workspace="TWORK", channel="CGRANTS", thread_ts="2.2")
    assert calls == [1] and len(result["pending_crm_actions"]) == 1
    assert "No email is required" in result["reply"]


def test_no_email_person_is_researched_instead_of_discarded(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A selected no-email person cannot silently become an organization-only Lead."""
    row = _award(tmp_path)

    class DummyConnection:
        """Minimal connection whose lifecycle can be verified by the route."""

        def close(self) -> None:
            """Accept route cleanup."""

    monkeypatch.setattr(conversation.db, "connect", lambda: DummyConnection())
    monkeypatch.setattr(
        conversation.linkedin_candidates, "active_candidate", lambda *_args: None)
    requested: list[str] = []

    def lookup(*_args: object, **kwargs: object) -> str:
        """Capture the exact user-named person bound to the search."""
        requested.append(str(kwargs.get("person_name") or ""))
        return "I found Vartan Chalabian on LinkedIn; no email verified."

    monkeypatch.setattr(conversation.tools, "find_person_linkedin", lookup)
    monkeypatch.setattr(
        conversation.tools, "salesforce_organization_lead_create_preview",
        lambda *_args: (_ for _ in ()).throw(AssertionError("person was discarded")))
    result = conversation.respond(
        "The person is Vartan Chalabian. Prepare a Salesforce Lead preview; "
        "there is no verified email.", row, requester_slack="UCHASE",
        workspace="TWORK", channel="CGRANTS", thread_ts="2.2")
    assert "Vartan Chalabian" in result["reply"]
    assert requested == ["Vartan Chalabian"]
    assert result["pending_crm_actions"] == []


def test_non_direct_published_source_is_not_called_exact_award(tmp_path: Path) -> None:
    """Dataset and parent-award locators remain useful but are never labeled exact."""
    row = _award(tmp_path)
    values = dict(row)
    values["source"] = "ca-grants-portal"
    reply = conversation._exact_award_record_reply(values)  # type: ignore[arg-type]
    assert "don’t have a direct record-level award URL" in reply
    assert "exact government award record" not in reply


@pytest.mark.parametrize("phrase", [
    "Can you add this lead to a Salesforce Campaign?",
    "Yes, add this lead to that campaign.",
    "Create an Opportunity for this lead.",
])
def test_campaign_and_opportunity_requests_never_become_org_leads(phrase: str) -> None:
    """CRM collection/deal language cannot trigger standalone Lead creation."""
    assert conversation._explicit_lead_creation_request(phrase) is False
