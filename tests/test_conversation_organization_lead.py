"""Deterministic Slack routing for explicit organization-only Lead requests."""

from __future__ import annotations

import json

import pytest

from grant_watch.slack import conversation


@pytest.mark.parametrize("user_text", [
    "Just create a lead.",
    "Standalone lead, yes.",
    "Can you add this to Salesforce?",
    "Create it anyway.",
])
def test_explicit_request_routes_directly_to_organization_preview(
        monkeypatch: pytest.MonkeyPatch, user_text: str) -> None:
    """Grant does not restart contact research or ask for a Campaign after a direct ask."""
    monkeypatch.setattr(conversation, "_has_verified_person", lambda _lead_id: False)
    monkeypatch.setattr(
        conversation, "Anthropic",
        lambda: (_ for _ in ()).throw(AssertionError("model must not handle this route")))
    calls: list[int] = []

    def preview(lead_id: int, _user: str, _workspace: str,
                _channel: str, _thread: str) -> str:
        """Return one immutable fake action marker."""
        calls.append(lead_id)
        value = {
            "action_id": "action-1", "nonce": "nonce-1",
            "preview": "Create one organization-only Lead?",
            "expires_at": "2026-07-16T01:00:00+00:00",
        }
        return f"<grant-crm-action>{json.dumps(value)}</grant-crm-action>"

    monkeypatch.setattr(
        conversation.tools, "salesforce_organization_lead_create_preview", preview)
    result = conversation.respond(
        user_text, {"id": 42}, requester_slack="UCHASE", workspace="TWORK",
        channel="CGRANTS", thread_ts="1.1",
        thread_context=["Grant: I can prepare a standalone Lead."])  # type: ignore[arg-type]

    assert calls == [42]
    assert len(result["pending_crm_actions"]) == 1
    assert "organization-only Salesforce Lead" in result["reply"]
    assert "Campaign" not in result["reply"]


def test_preview_failure_is_brief_and_nontechnical(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A reader failure does not expose exception names or imply a write happened."""
    monkeypatch.setattr(conversation, "_has_verified_person", lambda _lead_id: False)
    monkeypatch.setattr(
        conversation.tools, "salesforce_organization_lead_create_preview",
        lambda *_args: "ERROR: Lead preview failed (ConnectionError): unavailable")
    result = conversation.respond(
        "Just create a lead", {"id": 42}, requester_slack="UCHASE",
        workspace="TWORK", channel="CGRANTS", thread_ts="1.1")  # type: ignore[arg-type]

    assert result["pending_crm_actions"] == []
    assert "ConnectionError" not in result["reply"]
    assert "Nothing was changed" in result["reply"]
