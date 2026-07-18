"""Stubbed-model conversation regression suite for Grant's Slack brain.

Why: envelope and router regressions previously shipped blind because nothing
exercised conversation.respond() with the Anthropic client replaced. This
suite stubs the model with canned final messages to prove three properties:
(1) deterministic router families answer WITHOUT constructing a model client,
(2) the owner's previously-failing prompts produce non-empty, jargon-free
replies (no snake_case tokens, no braces, no key=value pairs, no batch-id
timestamps, no Python error names), and (3) plain-prose model output passes
through verbatim instead of degrading to a fallback apology.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from grant_watch.slack import conversation, intent_router

# Deterministic degraded-path strings from conversation.py. A healthy prompt
# must never surface any of these.
FALLBACK_FRAGMENTS = (
    "Hmm, I fumbled that one",
    "That took more digging than I expected",
    "mind rephrasing",
)

_BATCH_ID_TIMESTAMP = re.compile(r"\d{8}T\d{6}Z")
_SNAKE_CASE_TOKEN = re.compile(r"\b[a-z0-9]+(?:_[a-z0-9]+)+\b")
_KEY_VALUE_PAIR = re.compile(r"\b\w+=\S")
_PYTHON_ERROR_NAME = re.compile(r"\b[A-Z][A-Za-z]*Error\b")


def assert_human_reply(reply: str) -> None:
    """One jargon lint for user-facing replies: plain sentences only."""
    assert reply and reply.strip(), "reply must be non-empty"
    scrubbed = re.sub(r"https?://\S+", "", reply)
    assert "{" not in scrubbed and "}" not in scrubbed, reply
    assert "tasks=" not in scrubbed, reply
    assert "schema v" not in scrubbed, reply
    assert _BATCH_ID_TIMESTAMP.search(scrubbed) is None, reply
    assert _SNAKE_CASE_TOKEN.search(scrubbed) is None, reply
    assert _KEY_VALUE_PAIR.search(scrubbed) is None, reply
    assert _PYTHON_ERROR_NAME.search(scrubbed) is None, reply
    for fragment in FALLBACK_FRAGMENTS:
        assert fragment.lower() not in reply.lower(), reply


class StubMessages:
    """Scripted messages resource returning one canned final text message."""

    def __init__(self, reply_text: str) -> None:
        """Remember the canned model output and start the call counter."""
        self.calls = 0
        self._reply_text = reply_text

    def create(self, **_kwargs: object) -> object:
        """Return the canned end_turn message and count the invocation."""
        self.calls += 1
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=self._reply_text)],
        )


def stub_model(monkeypatch: pytest.MonkeyPatch, reply_text: str) -> StubMessages:
    """Replace the Anthropic client with a canned single-reply stub."""
    messages = StubMessages(reply_text)

    class StubAnthropic:
        """Accept the real client-policy kwargs and expose the stub resource."""

        def __init__(self, **_kwargs: object) -> None:
            """Bind the shared scripted messages resource."""
            self.messages = messages

    monkeypatch.setattr(conversation, "Anthropic", StubAnthropic)
    return messages


def forbid_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if respond() constructs any model client at all."""

    def _forbidden(**_kwargs: object) -> None:
        """Deterministic prompts must never reach Anthropic."""
        raise AssertionError("Anthropic must not be constructed for this prompt")

    monkeypatch.setattr(conversation, "Anthropic", _forbidden)


# ---------------------------------------------------------------------------
# (a) Router families answer without the model being called.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "prompt",
    [
        "what can I ask you?",
        "What can you do?",
        "help",
        "how do I use you?",
        "what are your capabilities?",
    ],
)
def test_capability_family_answers_without_model(
    monkeypatch: pytest.MonkeyPatch, prompt: str
) -> None:
    """Capability help is a fixed truthful script, never a model turn."""
    forbid_model(monkeypatch)
    output = conversation.respond(prompt, None)
    assert output["intent"] == "question"
    assert_human_reply(output["reply"])
    for capability in ("Persequor", "Salesforce", "contact", "reviewed"):
        assert capability.lower() in output["reply"].lower()
    # Concrete example prompts ship with the answer.
    assert "Find gold school leads in Michigan" in output["reply"]


@pytest.mark.parametrize(
    "prompt",
    [
        "what sources have we reviewed?",
        "show me the sources you've reviewed",
        "list reviewed sources",
    ],
)
def test_reviewed_sources_family_answers_without_model(
    monkeypatch: pytest.MonkeyPatch, prompt: str
) -> None:
    """Loose reviewed-source listings render locally from validated evidence."""
    forbid_model(monkeypatch)
    output = conversation.respond(prompt, None)
    assert output["intent"] == "question"
    assert_human_reply(output["reply"])
    assert "reviewed" in output["reply"].lower()


@pytest.mark.parametrize(
    "prompt",
    [
        "source discovery status",
        "coverage",
        "how's our coverage looking in Texas?",
    ],
)
def test_discovery_status_family_answers_without_model(
    monkeypatch: pytest.MonkeyPatch, prompt: str
) -> None:
    """Status and coverage asks render locally from validated evidence."""
    forbid_model(monkeypatch)
    output = conversation.respond(prompt, None)
    assert output["intent"] == "question"
    assert_human_reply(output["reply"])


def test_router_leaves_lead_search_and_chitchat_to_the_model() -> None:
    """The router is surgical: unrouted messages return None untouched."""
    for prompt in (
        "find gold school leads in Michigan",
        "break down our leads by program and grade",
        "thanks, Grant!",
        "who is the contact at Mt. Morris?",
    ):
        assert intent_router.deterministic_reply(prompt, None) is None


# ---------------------------------------------------------------------------
# (b) The owner's six previously-failing prompts, with phrasing variations.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "prompt",
    [
        "find gold school leads in Michigan",
        "Can you find gold school leads in Michigan?",
        "Find me gold leads for schools in Michigan please",
        "show gold school leads in MI",
    ],
)
def test_gold_lead_search_reaches_the_model_path(
    monkeypatch: pytest.MonkeyPatch, prompt: str
) -> None:
    """A real lead search reaches the model and returns its reply unmangled."""
    canned = (
        "Here are the gold school leads I found for Michigan — strong districts "
        "with open spend windows. Want the full list right here?"
    )
    stub = stub_model(
        monkeypatch, '{"intent": "question", "reply": "' + canned + '"}'
    )
    output = conversation.respond(prompt, None)
    assert stub.calls >= 1, "this prompt must reach the model/tools path"
    assert output["reply"] == canned
    assert_human_reply(output["reply"])


@pytest.mark.parametrize(
    "prompt",
    [
        "what kinds of things can I ask you?",
        "What kind of things can I ask?",
        "what sorts of questions can I ask you, Grant?",
    ],
)
def test_capability_question_regression(
    monkeypatch: pytest.MonkeyPatch, prompt: str
) -> None:
    """'What can I ask you?' produces the concrete capabilities answer."""
    forbid_model(monkeypatch)
    output = conversation.respond(prompt, None)
    assert_human_reply(output["reply"])
    assert "Persequor" in output["reply"]
    assert "Salesforce" in output["reply"]


@pytest.mark.parametrize(
    "prompt",
    [
        "break down our leads by program and grade",
        "can you break down our leads by program and grade?",
        "give me a breakdown of our leads by grade",
        "break the leads down by state",
    ],
)
def test_lead_breakdown_regression(
    monkeypatch: pytest.MonkeyPatch, prompt: str
) -> None:
    """Breakdown asks flow to the model and come back jargon-free."""
    canned = (
        "Quick breakdown: most of our leads are gold school leads, and Michigan "
        "and Texas lead the pack. Want it split by program instead?"
    )
    stub = stub_model(
        monkeypatch, '{"intent": "question", "reply": "' + canned + '"}'
    )
    output = conversation.respond(prompt, None)
    assert stub.calls >= 1
    assert output["reply"] == canned
    assert_human_reply(output["reply"])


@pytest.mark.parametrize(
    "prompt",
    [
        "who got security funding last month?",
        "who received security funding last month?",
        "which districts got grants last month?",
        "who was awarded funding in June 2026?",
    ],
)
def test_award_timing_regression(
    monkeypatch: pytest.MonkeyPatch, prompt: str
) -> None:
    """Award-received asks get the honest deterministic clarification."""
    forbid_model(monkeypatch)
    output = conversation.respond(prompt, None)
    assert_human_reply(output["reply"])
    # All three honest date meanings are offered; funds receipt is disclaimed.
    assert "never knows when money actually hit" in output["reply"]
    assert "award-announcement" in output["reply"]
    assert "discovered" in output["reply"].lower()
    assert "spend window" in output["reply"].lower()


@pytest.mark.parametrize(
    "prompt",
    [
        "run source discovery for Texas",
        "start source discovery in Texas",
        "go run source discovery for Texas right now",
        "launch discovery for Texas",
        "run a discovery search for TX",
    ],
)
def test_paid_discovery_refusal_regression(
    monkeypatch: pytest.MonkeyPatch, prompt: str
) -> None:
    """The paid-discovery refusal is plain language and says what Grant CAN do."""
    forbid_model(monkeypatch)
    output = conversation.respond(prompt, None)
    assert_human_reply(output["reply"])
    assert "paid discovery runs are disabled" in output["reply"]
    assert "What I can do" in output["reply"]


@pytest.mark.parametrize(
    "prompt",
    [
        "list the last 5 reviewed sources",
        "show the last 5 reviewed sources",
        "what are the last five reviewed sources?",
        "what sources have we reviewed?",
        "show me the sources you've reviewed",
    ],
)
def test_reviewed_sources_listing_regression(
    monkeypatch: pytest.MonkeyPatch, prompt: str
) -> None:
    """Reviewed-source listings answer deterministically in plain sentences."""
    forbid_model(monkeypatch)
    output = conversation.respond(prompt, None)
    assert_human_reply(output["reply"])
    assert "we've reviewed" in output["reply"]


# ---------------------------------------------------------------------------
# (c) Plain-prose model output passes through verbatim, never to a fallback.
# ---------------------------------------------------------------------------


def test_plain_text_model_reply_passes_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A prose (non-JSON) final message reaches the user exactly as written."""
    prose = (
        "Happy to dig in — that award's spend window runs through September "
        "2028, so there's still plenty of time to reach out."
    )
    stub = stub_model(monkeypatch, prose)
    output = conversation.respond("tell me about that spend window", None)
    assert stub.calls == 1
    assert output["intent"] == "question"
    assert output["reply"] == prose
    assert_human_reply(output["reply"])
