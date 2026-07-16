"""Regression tests for Grant's channel-only, natural-language-first interface."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from grant_watch.slack import grant


def test_no_slash_command_is_registered_or_advertised() -> None:
    """Grant exposes no slash-command handler, help menu, or command response."""
    source = inspect.getsource(grant)
    assert "@app.command" not in source
    assert "HELP_TEXT" not in source
    assert "DIGEST_DISABLED_TEXT" not in source
    assert "/grant" not in source


def test_unrelated_thread_is_rejected_before_event_receipt_write() -> None:
    """Plain Playground chatter cannot create durable Slack receipt rows."""
    source = inspect.getsource(grant.create_app)
    message_start = source.index("def on_message")
    lookup = source.index("post = db.find_post_by_ts", message_start)
    claim = source.index("db.claim_slack_event", lookup)
    assert lookup < claim


def test_conversations_are_limited_to_configured_channel(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Mentions and thread replies fail closed outside the configured channel."""
    monkeypatch.setenv("SLACK_CHANNEL_ID", "CPLAYGROUND")
    assert grant._in_configured_channel({
        "channel": "CPLAYGROUND", "channel_type": "channel",
    })
    assert not grant._in_configured_channel({
        "channel": "COTHER", "channel_type": "channel",
    })
    assert not grant._in_configured_channel({
        "channel": "CPLAYGROUND", "channel_type": "im",
    })


def test_missing_channel_configuration_fails_closed(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing channel ID cannot accidentally enable Grant workspace-wide."""
    monkeypatch.delenv("SLACK_CHANNEL_ID", raising=False)
    assert not grant._in_configured_channel({
        "channel": "CPLAYGROUND", "channel_type": "channel",
    })


def test_reaction_channel_is_read_from_nested_item(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Slack reaction events use item.channel and still respect the Playground gate."""
    monkeypatch.setenv("SLACK_CHANNEL_ID", "CPLAYGROUND")
    assert grant._in_configured_channel({"item": {"channel": "CPLAYGROUND"}})
    assert not grant._in_configured_channel({"item": {"channel": "COTHER"}})


def test_fallback_is_natural_and_menu_free() -> None:
    """Model outages never reintroduce commands or a help/status menu."""
    assert grant._fallback_answer("") == "What would you like me to find?"
    fallback = grant._fallback_answer("show me California")
    assert "command" not in fallback.lower()
    assert "help" not in fallback.lower()
    assert "status" not in fallback.lower()


def test_natural_help_requests_are_deterministic() -> None:
    """Users can learn Grant without slash commands, menus, or exact wording."""
    assert grant._is_help_request("How do I use you?")
    assert grant._is_help_request("what can you do")
    assert grant._is_help_request("How can I use you")
    assert not grant._is_help_request("show me California grants")


def test_thread_facts_include_evidence_and_read_only_crm_context(tmp_path: Path) -> None:
    """Details hidden from the alert remain available for truthful thread answers."""
    from grant_watch import db
    from grant_watch.models import (
        DatePrecision, FundingEventType, Lead, LeadGrade, RawItem, VerificationStatus,
    )
    from grant_watch.slack import conversation

    conn = db.connect(tmp_path / "facts.db")
    db.upsert_lead(conn, Lead(item=RawItem(
        source="usaspending:16.071", item_id="A1", title="Award",
        entity="ABC SCHOOLS", state="CA", program="SVPP", amount=500_000.0,
        start="2026-07-01", end="2028-09-30", url="https://official.test/A1",
        raw={}, event_type=FundingEventType.AWARD_OBLIGATED,
        event_date="2026-07-01", date_precision=DatePrecision.DAY,
        source_locator="A1",
        verification_status=VerificationStatus.VERIFIED,
        evidence_excerpt="Official obligation record"), grade=LeadGrade.GOLD))
    row = db.get_lead(conn, 1)
    facts = conversation.lead_facts(row)
    assert "entity: ABC Schools" in facts
    assert "event_type: award_obligated" in facts
    assert "event_evidence: Official obligation record" in facts
    assert "source_record: USASpending award A1 (direct record)" in facts
    assert "source_url: https://official.test/A1" in facts
    assert "salesforce_status: (not checked)" in facts


def test_claim_intent_is_rejected_as_a_non_action() -> None:
    """Legacy model output cannot assign or claim a Grant lead."""
    from grant_watch.slack import conversation

    parsed = conversation._parse_final('{"intent":"claim","reply":"It is yours."}')
    assert parsed["intent"] == "question"
    assert not hasattr(__import__("grant_watch.db", fromlist=["claim_lead"]), "claim_lead")


def test_parse_final_removes_red_inline_code_but_preserves_email_fence() -> None:
    """Server-side sanitation backs up the prompt's no-red-text instruction."""
    from grant_watch.slack import conversation

    parsed = conversation._parse_final(
        '{"intent":"question","reply":"Use `this`, then:\\n```\\nEmail body\\n```"}')
    assert "`this`" not in parsed["reply"]
    assert "```\nEmail body\n```" in parsed["reply"]


def test_dense_contact_reply_is_spaced_into_scannable_sections() -> None:
    """Long model paragraphs become an intro, bullets, and a separate question."""
    from grant_watch.slack import conversation

    dense = (
        "I found Mona Miller, Director of Curriculum and Instructional Services, "
        "on the district's public LinkedIn page. No verified email appeared on the "
        "official website, so I left email blank. Her role is curriculum-focused, "
        "not facilities or technology. Would you like me to search for a facilities "
        "or technology contact next?"
    )
    formatted = conversation._format_slack_reply(dense)
    assert "\n\n• No verified email" in formatted
    assert "\n• Her role is curriculum-focused" in formatted
    assert "\n\nWould you like me" in formatted


def test_existing_lists_get_blank_lines_without_rewriting_content() -> None:
    """Already structured facts keep their exact wording with Slack-safe spacing."""
    from grant_watch.slack import conversation

    formatted = conversation._format_slack_reply(
        "Here is what I found:\n• Name: Mona Miller\n• Email: Not found\nWhat next?")
    assert formatted == (
        "Here is what I found:\n\n• Name: Mona Miller\n• Email: Not found\n\nWhat next?")


def test_salesforce_preview_uses_numbered_steps_and_spaced_fields() -> None:
    """CRM previews never repeat model prose or run their field list into the header."""
    actions = [{"action_id": "a", "nonce": "n", "expires_at": "later",
                "preview": "Create this Lead?\n• Name: Mona\n• Company: Corning\nNo Campaign."}]
    intro = grant._reply_with_action_steps("Dense model summary", actions)
    blocks = grant._crm_action_blocks(actions)
    assert intro == (
        "I prepared the Salesforce preview.\n\n"
        "1. Review the verified fields below.\n"
        "2. Click *Confirm in Salesforce* only if everything looks right.")
    assert blocks[0]["text"]["text"] == (
        "Create this Lead?\n\n• Name: Mona\n• Company: Corning\n\nNo Campaign.")


def test_formatter_is_idempotent_and_preserves_urls_and_email() -> None:
    """Repeated delivery guards cannot corrupt links, addresses, or spacing."""
    from grant_watch.slack import conversation

    text = (
        "I found Mona Miller at https://www.linkedin.com/in/mona-miller-1. "
        "The district page lists office@district.k12.ca.us for general questions. "
        "I did not find a verified direct email for Mona. Would you like me to keep looking?"
    )
    once = conversation._format_slack_reply(text)
    assert conversation._format_slack_reply(once) == once
    assert "https://www.linkedin.com/in/mona-miller-1" in once
    assert "office@district.k12.ca.us" in once


def test_status_final_delivery_applies_same_spacing_guard() -> None:
    """Replies that bypass model parsing still receive the final Slack formatting guard."""
    class FakeClient:
        """Capture the final spinner edit without contacting Slack."""

        def __init__(self) -> None:
            self.text = ""

        def chat_update(self, **kwargs: object) -> None:
            """Record final formatted text."""
            self.text = str(kwargs["text"])

    client = FakeClient()
    status = grant._Status(client, "C", "1.1")  # type: ignore[arg-type]
    status.ts = "2.2"
    dense = (
        "I found a verified contact on the official district website. "
        "Salesforce has no matching Lead, Contact, or Account for the organization. "
        "The contact's role is curriculum-focused rather than facilities-focused. "
        "Would you like me to keep searching for a technology contact?"
    )
    assert status.finalize(dense)
    assert "\n\n• Salesforce" in client.text
    assert "\n\nWould you like me" in client.text


def test_digest_poster_module_remains_absent() -> None:
    """The earlier multi-lead poster cannot return through this UX correction."""
    assert not Path(grant.__file__).with_name("digest.py").exists()


def test_startup_requires_explicit_playground_channel(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Grant cannot start with workspace-wide routing caused by a missing channel."""
    monkeypatch.setattr(grant, "load_dotenv", lambda: None)
    monkeypatch.delenv("SLACK_CHANNEL_ID", raising=False)
    with pytest.raises(RuntimeError, match="Monarch Bot Playground"):
        grant.main()
