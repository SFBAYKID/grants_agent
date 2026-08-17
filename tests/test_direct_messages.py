"""Grant's direct-message venue: who gets in, who is turned away, and what it reads.

Grant was channel-only, and every rule in the listener was written for a ROOM. The
tests here pin the two halves that a "just allow DMs" change gets wrong: the channel
gate must still refuse DMs on its own (the DM path carries its own authorization,
rather than the channel rule being loosened), and the three room-shaped rules —
top-level chatter, an @mention meaning somebody else was addressed, and only speaking
under a post Grant made — must not silently eat a DM.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from grant_watch import db
from grant_watch.slack import grant, venues
from tests.test_slack_human_event_path import FakeBoltApp

ROSTER_USER = "U01DPJVURHU"  # Chase, config/reps.json
STRANGER = "U0NOTAREPXX"
DM = "D0BGW7EP3K5"


def _dm_event(
    text: str, ts: str, user: str = ROSTER_USER, **extra: Any
) -> dict[str, Any]:
    """One human-shaped `message.im` envelope as Slack delivers it."""
    event: dict[str, Any] = {
        "team": "TWORK",
        "channel": DM,
        "channel_type": "im",
        "user": user,
        "text": text,
        "ts": ts,
    }
    event.update(extra)
    return event


# --------------------------------------------------------------------- the gates
def test_channel_gate_still_refuses_every_dm(monkeypatch: pytest.MonkeyPatch) -> None:
    """The control. Allowing DMs must not have loosened the channel rule.

    If this ever passes for a DM, authorization stopped being two independent gates
    and became one weakened one — and `SLACK_CHANNEL_ID` stopped bounding Grant.
    """
    monkeypatch.setenv("SLACK_CHANNEL_ID", "CGRANT")
    assert venues.in_configured_channel({"channel": "CGRANT"}) is True
    assert venues.in_configured_channel({"channel": DM, "channel_type": "im"}) is False
    assert grant._in_configured_channel({"channel": DM, "channel_type": "im"}) is False


def test_only_a_roster_member_owns_a_dm_venue(monkeypatch: pytest.MonkeyPatch) -> None:
    """A DM is authorized by the PERSON, since there is no configured room to trust."""
    monkeypatch.setenv("SLACK_CHANNEL_ID", "CGRANT")
    assert venues.is_direct_message(_dm_event("hi", "1.0")) is True
    assert venues.may_converse(_dm_event("hi", "1.0")) is True
    # Not on config/reps.json — anyone in the workspace can open a DM with an app.
    assert venues.is_direct_message(_dm_event("hi", "1.0", user=STRANGER)) is False
    assert venues.may_converse(_dm_event("hi", "1.0", user=STRANGER)) is False
    # `channel_type` and the channel id must agree; either alone is not a DM.
    assert (
        venues.is_direct_message(
            {"channel": "CGRANT", "channel_type": "im", "user": ROSTER_USER}
        )
        is False
    )
    assert (
        venues.is_direct_message(
            {"channel": DM, "channel_type": "channel", "user": ROSTER_USER}
        )
        is False
    )


def test_an_unreadable_roster_authorizes_nobody(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken reps.json fails closed rather than killing the listener."""

    def boom(_slack_id: object) -> str | None:
        """Stand in for a malformed or missing config/reps.json."""
        raise ValueError("reps.json is not JSON")

    monkeypatch.setattr(venues.roster, "email_for_slack", boom)
    assert venues.is_approved_sender(ROSTER_USER) is False
    assert venues.is_direct_message(_dm_event("hi", "1.0")) is False


# ------------------------------------------------------------- through the handlers
def _app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> tuple[FakeBoltApp, list[dict[str, Any]]]:
    """Register the real Bolt handlers with the model turn captured, not run."""
    connection = db.connect(tmp_path / name)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CHANNEL_ID", "CGRANT")
    monkeypatch.setattr(grant, "App", FakeBoltApp)
    monkeypatch.setattr(grant.db, "connect", lambda *_a, **_k: connection)
    turns: list[dict[str, Any]] = []

    def capture(
        text: str, client: Any, channel: str, thread_ts: str | None, **kwargs: Any
    ) -> bool:
        """Record where a conversation turn was routed instead of calling the model."""
        turns.append({"text": text, "channel": channel, "thread_ts": thread_ts})
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="answered")
        return True

    monkeypatch.setattr(grant, "_converse_general", capture)
    grant.create_app()
    app = FakeBoltApp.latest
    assert app is not None
    return app, turns


def test_top_level_dm_is_answered_at_top_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point: a rep types in the DM and Grant answers, with no thread.

    Two separate defects are pinned. A DM has no `thread_ts`, so the room rule
    "top-level chatter isn't Grant's business" would have dropped it — accepted, no
    answer, no error. And Grant must reply at TOP LEVEL: threading each turn under
    its own message gives every follow-up a fresh empty thread, which in a DM is the
    same as not listening.
    """
    app, turns = _app(tmp_path, monkeypatch, "dm-top-level.db")
    app.events["message"](
        event=_dm_event("pull the California gold leads", "20.001"),
        body={"event_id": "Ev-dm-1", "team_id": "TWORK"},
        say=lambda **_k: None,
        client=app.client,
    )
    assert [turn["text"] for turn in turns] == ["pull the California gold leads"]
    assert turns[0]["channel"] == DM
    assert turns[0]["thread_ts"] is None
    assert app.client.messages[-1]["thread_ts"] == ""


def test_top_level_channel_chatter_is_still_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control for the test above: the room rule survives in the room.

    Grant must not start answering everything said in the team channel just because
    a DM is allowed to be thread-less.
    """
    app, turns = _app(tmp_path, monkeypatch, "channel-chatter.db")
    app.events["message"](
        event={
            "team": "TWORK",
            "channel": "CGRANT",
            "channel_type": "channel",
            "user": ROSTER_USER,
            "text": "anyone grabbing lunch",
            "ts": "20.002",
        },
        body={"event_id": "Ev-chan-1", "team_id": "TWORK"},
        say=lambda **_k: None,
        client=app.client,
    )
    assert turns == []


def test_a_dm_naming_a_colleague_is_still_for_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "Look up <@U…>'s leads" is a request TO Grant when nobody else is present.

    In a channel an @mention of somebody else means Grant stays out of it (Chase's
    rule). A DM has exactly two participants, so the same rule would throw away an
    ordinary request — and leave no trace of having done so.
    """
    app, turns = _app(tmp_path, monkeypatch, "dm-mention.db")
    app.events["message"](
        event=_dm_event("what is <@U01E908206M> sitting on this week", "20.003"),
        body={"event_id": "Ev-dm-2", "team_id": "TWORK"},
        say=lambda **_k: None,
        client=app.client,
    )
    assert len(turns) == 1

    # Control: the same text in the channel is still somebody else's business.
    app.events["message"](
        event={
            "team": "TWORK",
            "channel": "CGRANT",
            "channel_type": "channel",
            "user": ROSTER_USER,
            "text": "what is <@U01E908206M> sitting on this week",
            "ts": "20.004",
            "thread_ts": "20.000",
        },
        body={"event_id": "Ev-chan-2", "team_id": "TWORK"},
        say=lambda **_k: None,
        client=app.client,
    )
    assert len(turns) == 1


def test_a_stranger_is_declined_once_and_never_costs_a_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Someone off the roster gets one honest line and no work is done for them.

    Silence would be the worse answer — the person deliberately typed at Grant, so
    nothing arriving reads as broken rather than declined — but the reply must be a
    fixed string, never a model turn, and never repeated on every message.
    """
    app, turns = _app(tmp_path, monkeypatch, "dm-stranger.db")
    for index, ts in enumerate(("20.005", "20.006")):
        app.events["message"](
            event=_dm_event("send me every lead you have", ts, user=STRANGER),
            body={"event_id": f"Ev-dm-x{index}", "team_id": "TWORK"},
            say=lambda **_k: None,
            client=app.client,
        )
    assert turns == []
    declines = [
        message
        for message in app.client.messages
        if message["text"] == venues.UNKNOWN_SENDER_REPLY
    ]
    assert len(declines) == 1
    assert declines[0]["channel"] == DM


def test_dm_mention_also_answers_at_top_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`@Grant` typed inside a DM fires app_mention, and must land the same way."""
    app, turns = _app(tmp_path, monkeypatch, "dm-mention-handler.db")
    app.events["app_mention"](
        event=_dm_event("<@UGRANT> what is new in Texas", "20.007"),
        body={"event_id": "Ev-dm-3", "team_id": "TWORK"},
        say=lambda **_k: None,
        client=app.client,
    )
    assert [turn["thread_ts"] for turn in turns] == [None]
    assert turns[0]["text"] == "what is new in Texas"


# --------------------------------------------------------------- what Grant reads
class _HistoryClient:
    """Slack double returning DM history the way Slack really orders it."""

    def __init__(self) -> None:
        """Store a two-turn DM, NEWEST first, as `conversations.history` does."""
        self.calls: list[str] = []

    def conversations_history(self, **kwargs: object) -> dict[str, object]:
        """Return the DM's recent messages, newest first."""
        self.calls.append(f"history:{kwargs.get('channel')}")
        return {
            "messages": [
                {"text": "want me to draft it?", "bot_id": "BGRANT"},
                {"text": "find the Modesto contact", "user": ROSTER_USER},
            ]
        }

    def conversations_replies(self, **kwargs: object) -> dict[str, object]:
        """Fail the test loudly if a thread-less DM is read as a thread."""
        self.calls.append(f"replies:{kwargs.get('ts')}")
        return {"messages": [{"text": "want me to draft it?", "bot_id": "BGRANT"}]}


def test_a_thread_less_dm_is_read_from_its_own_history_in_order() -> None:
    """A DM's memory is the DM, oldest first.

    `conversations.replies` on a top-level DM message returns that ONE message, so
    Grant would arrive with no memory and a bare "yes" would lose its antecedent —
    the Kerry bug, reproduced in the one venue where people type consecutive
    sentences. `conversations.history` returns NEWEST first while `replies` returns
    oldest first, so reading it raw hands the model the conversation backwards.
    """
    client = _HistoryClient()
    lines = venues.thread_history(client, DM, None)  # type: ignore[arg-type]
    assert lines == [
        "rep: find the Modesto contact",
        "Grant: want me to draft it?",
    ]
    assert client.calls == [f"history:{DM}"]


def test_a_threaded_conversation_still_reads_as_a_thread() -> None:
    """The control: nothing about the existing channel-thread path moved."""
    client = _HistoryClient()
    assert venues.thread_history(client, "CGRANT", "9.001") == [  # type: ignore[arg-type]
        "Grant: want me to draft it?"
    ]
    assert client.calls == ["replies:9.001"]


def test_history_failure_costs_context_not_the_turn() -> None:
    """An unreadable DM leaves Grant answering without memory, never crashing."""

    class Broken:
        """Slack double whose history call fails."""

        def conversations_history(self, **_kwargs: object) -> dict[str, object]:
            """Raise the way a scope or rate-limit error would."""
            raise RuntimeError("missing_scope")

    assert venues.thread_history(Broken(), DM, None) == []  # type: ignore[arg-type]
