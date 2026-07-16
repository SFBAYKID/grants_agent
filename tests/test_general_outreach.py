"""On-demand Slack-thread outreach regressions for honest server-side execution."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from grant_watch.slack import conversation, grant


class ThreadClient:
    """Record final Slack text while supplying a realistic recent search thread."""

    def __init__(self, history: list[str]) -> None:
        """Store human-readable history and initialize delivery capture."""
        self.history = history
        self.updates: list[str] = []

    def conversations_replies(self, **_kwargs: object) -> dict[str, object]:
        """Convert prefixed test history into Slack-like message dictionaries."""
        messages = []
        for line in self.history:
            is_grant = line.startswith("Grant:")
            messages.append(
                {
                    "text": line.split(":", 1)[-1].strip(),
                    "bot_id": "B1" if is_grant else "",
                    "user": "" if is_grant else "U1",
                }
            )
        return {"messages": messages}

    def chat_postMessage(self, **kwargs: object) -> dict[str, str]:
        """Create the progress message used by the handler."""
        self.updates.append(str(kwargs.get("text") or ""))
        return {"ts": "spinner.1"}

    def chat_update(self, **kwargs: object) -> dict[str, bool]:
        """Capture progress and final message edits."""
        self.updates.append(str(kwargs.get("text") or ""))
        return {"ok": True}

    def files_upload_v2(self, **_kwargs: object) -> dict[str, bool]:
        """Accept files; these scenarios intentionally create none."""
        return {"ok": True}


def test_single_lead_resolution_refuses_ambiguous_history() -> None:
    """Pronouns resolve only when one unique displayed Lead number exists."""
    assert grant._single_lead_id("email lead #42", []) == 42
    assert grant._single_lead_id("email that person", ["Grant: Lead #42"]) == 42
    assert (
        grant._single_lead_id(
            "email that person", ["Grant: Lead #42", "Grant: Lead #43"]
        )
        is None
    )


def test_general_thread_executes_real_outreach_for_one_resolved_lead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A confirmed handoff returns the server outcome, not model-authored success."""
    client = ThreadClient(
        [
            "rep: show one lead",
            "Grant: Lead #42 — Test School",
            "Grant: Want me to have Persequor draft the intro email?",
        ]
    )
    monkeypatch.setattr(
        conversation,
        "respond",
        lambda *_args, **_kwargs: {
            "intent": "draft_email",
            "reply": "On it — bringing in Persequor.",
            "files": [],
            "pending_crm_actions": [],
        },
    )
    monkeypatch.setattr(grant.db, "connect", lambda: SimpleNamespace())
    monkeypatch.setattr(
        grant.db,
        "get_lead",
        lambda _conn, lead_id: {"id": lead_id, "entity_name": "Test School"},
    )
    called: list[int] = []

    def fake_outreach(
        _conn: object,
        row: dict[str, object],
        _user: str,
        _status: object,
        _channel: str,
        _thread_ts: str,
        _request_token: str,
    ) -> str:
        """Return the authoritative outcome produced by the server action."""
        called.append(int(row["id"]))
        return "No verified email; no outreach request was sent."

    monkeypatch.setattr(grant, "_request_outreach", fake_outreach)
    delivered = grant._converse_general(
        "Yes, have Persequor draft it.",
        client,
        "C1",
        "thread.1",
        user="U1",
        workspace="T1",
        request_token="event.1",
    )
    assert delivered is True
    assert called == [42]
    assert client.updates[-1] == "No verified email; no outreach request was sent."


def test_general_thread_never_claims_handoff_when_lead_is_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multiple displayed leads require a Lead number and perform no outreach."""
    client = ThreadClient(["Grant: Lead #42", "Grant: Lead #43"])
    monkeypatch.setattr(
        conversation,
        "respond",
        lambda *_args, **_kwargs: {
            "intent": "draft_email",
            "reply": "On it.",
            "files": [],
            "pending_crm_actions": [],
        },
    )
    monkeypatch.setattr(grant.db, "connect", lambda: SimpleNamespace())

    def forbidden(*_args: object, **_kwargs: object) -> str:
        """Fail if an ambiguous pronoun reaches the external handoff."""
        raise AssertionError("ambiguous lead must not trigger outreach")

    monkeypatch.setattr(grant, "_request_outreach", forbidden)
    delivered = grant._converse_general(
        "Email that person.", client, "C1", "thread.1", user="U1"
    )
    assert delivered is True
    assert "exact Lead number" in client.updates[-1]
    assert "no outreach request was sent" in client.updates[-1]
