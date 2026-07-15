"""Slack delivery tests for generated search artifacts and failure cleanup."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from grant_watch.slack import conversation, grant, tools
from grant_watch.spreadsheets import GeneratedArtifact


class FakeSlackClient:
    """Small Slack client double that records status updates and file uploads."""

    def __init__(self, fail_upload: bool = False) -> None:
        self.fail_upload = fail_upload
        self.uploads: list[dict[str, object]] = []
        self.updates: list[str] = []

    def chat_postMessage(self, **kwargs: object) -> dict[str, str]:
        """Create the spinner message and return a stable fake timestamp."""
        return {"ts": "spinner.1"}

    def chat_update(self, **kwargs: object) -> None:
        """Record status/final text for assertions."""
        self.updates.append(str(kwargs.get("text", "")))

    def files_upload_v2(self, **kwargs: object) -> object:
        """Record a live artifact path or raise the configured upload failure."""
        if self.fail_upload:
            raise RuntimeError("simulated Slack upload failure")
        file_path = Path(str(kwargs["file"]))
        assert file_path.exists()
        self.uploads.append(kwargs)
        return {"ok": True}


def _artifact() -> GeneratedArtifact:
    """Create one owned workbook for a delivery test."""
    _, artifact = tools.make_spreadsheet("delivery.xlsx", [["name"], ["District"]])
    return artifact


def test_dm_search_uploads_and_cleans_artifact(monkeypatch: pytest.MonkeyPatch) -> None:
    """A DM export uploads to the DM channel without a thread timestamp."""
    artifact = _artifact()

    def fake_respond(*_args: object, **_kwargs: object) -> dict[str, object]:
        """Return the prepared artifact without calling the model."""
        return {"intent": "question", "reply": "Attached.", "files": [artifact]}

    monkeypatch.setattr(conversation, "respond", fake_respond)
    client = FakeSlackClient()
    grant._converse_general("export", client, "D123", None, user="U1")

    assert len(client.uploads) == 1
    assert client.uploads[0]["channel"] == "D123"
    assert "thread_ts" not in client.uploads[0]
    assert not artifact.path.exists()
    assert not artifact.path.parent.exists()


def test_mention_search_uploads_in_requested_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """A general mention export uses the mention's thread anchor and then cleans up."""
    artifact = _artifact()

    def fake_respond(*_args: object, **_kwargs: object) -> dict[str, object]:
        """Return the prepared artifact without calling the model."""
        return {"intent": "question", "reply": "Attached.", "files": [artifact]}

    monkeypatch.setattr(conversation, "respond", fake_respond)
    client = FakeSlackClient()
    grant._converse_general("export", client, "C123", "thread.1", user="U1")

    assert client.uploads[0]["thread_ts"] == "thread.1"
    assert not artifact.path.exists()


def test_upload_failure_is_reported_and_contained(monkeypatch: pytest.MonkeyPatch) -> None:
    """Slack upload failure yields an honest reply, cleanup, and no escaping exception."""
    artifact = _artifact()

    def fake_respond(*_args: object, **_kwargs: object) -> dict[str, object]:
        """Return the prepared artifact without calling the model."""
        return {"intent": "question", "reply": "Export ready.", "files": [artifact]}

    monkeypatch.setattr(conversation, "respond", fake_respond)
    client = FakeSlackClient(fail_upload=True)
    grant._converse_general("export", client, "D123", None, user="U1")

    assert any("Slack could not attach" in text for text in client.updates)
    assert not artifact.path.exists()


def test_proactive_thread_uses_shared_delivery_helper() -> None:
    """The helper used by proactive lead threads uploads every artifact and cleans each."""
    first = _artifact()
    second = _artifact()
    client = FakeSlackClient()
    failures = grant._deliver_artifacts(
        client, "C123", "proactive.1", [first, second])
    assert failures == 0
    assert [upload["thread_ts"] for upload in client.uploads] == [
        "proactive.1", "proactive.1"]
    assert not first.path.exists() and not second.path.exists()


def test_model_failure_after_artifact_creation_cleans_file(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """An exception on a later model turn cannot leak an already-created workbook."""
    artifact = _artifact()

    class FakeMessages:
        """Emit one tool call, then fail the next model request."""

        calls = 0

        def create(self, **_kwargs: object) -> object:
            """Return one tool-use response before simulating an API outage."""
            self.calls += 1
            if self.calls == 1:
                block = SimpleNamespace(type="tool_use", name="search_leads",
                                        input={}, id="tool-1")
                return SimpleNamespace(stop_reason="tool_use", content=[block])
            raise RuntimeError("simulated model failure")

    class FakeAnthropic:
        """Expose the fake messages resource used by conversation.respond."""

        def __init__(self) -> None:
            self.messages = FakeMessages()

    def fake_run_tool(*_args: object, **_kwargs: object
                      ) -> tuple[str, GeneratedArtifact | None]:
        """Return the prepared artifact as if search_leads created it."""
        return "Spreadsheet created.", artifact

    monkeypatch.setattr(conversation, "Anthropic", FakeAnthropic)
    monkeypatch.setattr(tools, "run_tool", fake_run_tool)
    with pytest.raises(RuntimeError, match="model failure"):
        conversation.respond("export", None)
    assert not artifact.path.exists()


# ------------------------------------------------------------ greeting + idempotency
def test_empty_mention_greets_without_calling_the_model(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare @Grant greets deterministically and never invokes the LLM."""
    def must_not_run(*_a: object, **_k: object) -> dict[str, object]:
        raise AssertionError("empty mention must not call the model")

    monkeypatch.setattr(conversation, "respond", must_not_run)
    posted: list[str] = []
    client = SimpleNamespace(
        chat_postMessage=lambda **kw: posted.append(str(kw.get("text", ""))) or {"ts": "1"})
    grant._converse_general("   ", client, "C1", None, user="U1")
    assert posted and "help you with" in posted[0]


def test_claim_turn_dedups_redelivered_event() -> None:
    """The same Slack event_id delivered twice is processed once."""
    assert grant._claim_turn("evt-A", "chanA:1") is True
    assert grant._claim_turn("evt-A", "chanA:2") is False  # redelivered event_id
    grant._release_turn("chanA:1")


def test_claim_turn_blocks_concurrent_same_thread_then_frees() -> None:
    """A thread already in flight refuses a second run until released (no double-spend)."""
    assert grant._claim_turn("evt-B1", "chanB:9") is True
    assert grant._claim_turn("evt-B2", "chanB:9") is False  # thread already in flight
    grant._release_turn("chanB:9")
    assert grant._claim_turn("evt-B3", "chanB:9") is True   # freed -> allowed again
    grant._release_turn("chanB:9")


def test_respond_dispatches_with_contacts_second_step(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A 'yes, top 5' reply flows a search_leads(with_contacts=True) call through the loop."""
    captured: dict[str, object] = {}

    class FakeMessages:
        """Turn 1 asks for the enrichment tool; turn 2 returns the final JSON."""

        calls = 0

        def create(self, **_kwargs: object) -> object:
            """Script the two-step tool loop."""
            self.calls += 1
            if self.calls == 1:
                block = SimpleNamespace(
                    type="tool_use", name="search_leads",
                    input={"state": "IL", "with_contacts": True, "limit": 5}, id="t1")
                return SimpleNamespace(stop_reason="tool_use", content=[block])
            block = SimpleNamespace(
                type="text",
                text='{"intent": "question", "reply": "Top 5 with contacts, attached."}')
            return SimpleNamespace(stop_reason="end_turn", content=[block])

    class FakeAnthropic:
        """Expose the scripted messages resource."""

        def __init__(self) -> None:
            self.messages = FakeMessages()

    def fake_run_tool(name: str, args: dict[str, object], *_a: object,
                      **_k: object) -> tuple[str, object]:
        """Capture the dispatched tool call instead of touching the database."""
        captured["name"] = name
        captured["args"] = args
        return "Found and exported all 5 matches.", None

    monkeypatch.setattr(conversation, "Anthropic", FakeAnthropic)
    monkeypatch.setattr(tools, "run_tool", fake_run_tool)
    out = conversation.respond("yes, top 5 with contacts", None)
    assert captured["name"] == "search_leads"
    assert captured["args"].get("with_contacts") is True
    assert "contacts" in out["reply"]
