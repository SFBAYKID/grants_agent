"""Human-shaped Slack event-envelope tests through Grant's registered Bolt handlers."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from grant_watch import db
from grant_watch.slack import grant


class FakeSlackClient:
    """In-memory Slack surface that preserves thread messages and edits."""

    def __init__(self) -> None:
        """Initialize the bot identity, message store, and deterministic timestamps."""
        self.messages: list[dict[str, Any]] = []
        self.next_ts = 100

    def auth_test(self) -> dict[str, str]:
        """Return the Grant bot identity expected during handler registration."""
        return {"user_id": "UGRANT"}

    def chat_postMessage(self, **kwargs: object) -> dict[str, str]:
        """Store one bot message in the requested channel/thread."""
        self.next_ts += 1
        ts = str(self.next_ts)
        self.messages.append(
            {
                "ts": ts,
                "thread_ts": str(kwargs.get("thread_ts") or ""),
                "channel": str(kwargs.get("channel") or ""),
                "text": str(kwargs.get("text") or ""),
                "bot_id": "BGRANT",
            }
        )
        return {"ts": ts}

    def chat_update(self, **kwargs: object) -> dict[str, bool]:
        """Replace the matching spinner with its final Grant response."""
        ts = str(kwargs.get("ts") or "")
        for message in self.messages:
            if message["ts"] == ts:
                message["text"] = str(kwargs.get("text") or "")
                return {"ok": True}
        raise AssertionError(f"unknown Slack timestamp {ts}")

    def conversations_replies(self, **kwargs: object) -> dict[str, object]:
        """Return the root plus all stored messages under one thread."""
        root = str(kwargs.get("ts") or "")
        return {
            "messages": [
                message
                for message in self.messages
                if message["ts"] == root or message.get("thread_ts") == root
            ]
        }

    def files_upload_v2(self, **_kwargs: object) -> dict[str, bool]:
        """Accept an upload; these source-status scenarios create no files."""
        return {"ok": True}


class FakeBoltApp:
    """Capture Bolt decorators so tests can invoke the actual registered handlers."""

    latest: "FakeBoltApp | None" = None

    def __init__(self, token: str) -> None:
        """Create one app with a fake Slack client and handler registries."""
        assert token == "xoxb-test"
        self.client = FakeSlackClient()
        self.events: dict[str, Callable[..., None]] = {}
        self.actions: dict[str, Callable[..., None]] = {}
        FakeBoltApp.latest = self

    def event(self, name: str) -> Callable[[Callable[..., None]], Callable[..., None]]:
        """Register one event callback exactly as Slack Bolt's decorator does."""

        def register(handler: Callable[..., None]) -> Callable[..., None]:
            """Store and return the decorated callback."""
            self.events[name] = handler
            return handler

        return register

    def action(self, name: str) -> Callable[[Callable[..., None]], Callable[..., None]]:
        """Register one interactive-action callback."""

        def register(handler: Callable[..., None]) -> Callable[..., None]:
            """Store and return the decorated callback."""
            self.actions[name] = handler
            return handler

        return register


def _register_human_message(
    client: FakeSlackClient,
    text: str,
    ts: str,
    thread_ts: str = "",
) -> None:
    """Place a human-authored message into fake Slack history before delivery."""
    client.messages.append(
        {
            "ts": ts,
            "thread_ts": thread_ts,
            "channel": "CGRANT",
            "text": text,
            "user": "UCHASE",
        }
    )


def test_human_mention_and_plain_followup_traverse_registered_handlers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A human mention creates a thread whose plain follow-up reaches Grant once."""
    connection = db.connect(tmp_path / "human-events.db")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CHANNEL_ID", "CGRANT")
    monkeypatch.setattr(grant, "App", FakeBoltApp)
    monkeypatch.setattr(grant.db, "connect", lambda *_args, **_kwargs: connection)
    grant.create_app()
    app = FakeBoltApp.latest
    assert app is not None

    root_ts = "10.001"
    first_text = "<@UGRANT> show school district research coverage in California"
    _register_human_message(app.client, first_text, root_ts)
    mention_event = {
        "team": "TWORK",
        "channel": "CGRANT",
        "user": "UCHASE",
        "text": first_text,
        "ts": root_ts,
        "channel_type": "channel",
    }
    app.events["app_mention"](
        event=mention_event,
        body={"event_id": "Ev-human-1", "team_id": "TWORK"},
        say=lambda **_kwargs: None,
        client=app.client,
    )
    first_reply = app.client.messages[-1]
    assert first_reply["thread_ts"] == root_ts
    assert "school districts: 975 total" in first_reply["text"]
    assert db.is_conversation_thread(connection, "TWORK", "CGRANT", root_ts)

    followup_ts = "10.002"
    followup_text = "What has Grant actually reviewed in New Hampshire?"
    _register_human_message(app.client, followup_text, followup_ts, root_ts)
    message_event = {
        "team": "TWORK",
        "channel": "CGRANT",
        "user": "UCHASE",
        "text": followup_text,
        "ts": followup_ts,
        "thread_ts": root_ts,
        "channel_type": "channel",
    }
    app.events["message"](
        event=message_event,
        body={"event_id": "Ev-human-2", "team_id": "TWORK"},
        say=lambda **_kwargs: None,
        client=app.client,
    )
    second_reply = app.client.messages[-1]
    assert second_reply["thread_ts"] == root_ts
    assert "nh.strafford_county.bids" in second_reply["text"]

    before_redelivery = len(app.client.messages)
    app.events["message"](
        event=message_event,
        body={"event_id": "Ev-human-2", "team_id": "TWORK"},
        say=lambda **_kwargs: None,
        client=app.client,
    )
    assert len(app.client.messages) == before_redelivery

    receipts = connection.execute(
        "SELECT event_id, state, delivery_state FROM slack_event_receipts ORDER BY event_id"
    ).fetchall()
    assert [tuple(row) for row in receipts] == [
        ("Ev-human-1", "complete", "delivered"),
        ("Ev-human-2", "complete", "delivered"),
    ]


def test_bot_authored_mention_is_ignored_before_any_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Grant cannot trigger its own mention path or create a misleading receipt."""
    connection = db.connect(tmp_path / "bot-events.db")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CHANNEL_ID", "CGRANT")
    monkeypatch.setattr(grant, "App", FakeBoltApp)
    monkeypatch.setattr(grant.db, "connect", lambda *_args, **_kwargs: connection)
    grant.create_app()
    app = FakeBoltApp.latest
    assert app is not None
    app.events["app_mention"](
        event={
            "channel": "CGRANT",
            "user": "UGRANT",
            "bot_id": "BGRANT",
            "text": "<@UGRANT> show status",
            "ts": "20.001",
        },
        body={"event_id": "Ev-bot", "team_id": "TWORK"},
        say=lambda **_kwargs: None,
        client=app.client,
    )
    assert app.client.messages == []
    count = connection.execute("SELECT COUNT(*) FROM slack_event_receipts").fetchone()[
        0
    ]
    assert count == 0
