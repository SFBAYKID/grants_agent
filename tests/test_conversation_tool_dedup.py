"""Per-turn tool deduplication for paid, external, and preview-producing calls."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from grant_watch.slack import conversation, tools


def test_single_execution_modes_cover_paid_search_and_contact_enrichment() -> None:
    """Only the explicitly slow/paid modes receive a per-human-turn cap."""
    assert conversation._single_execution_tool_key("web_search", {"query": "one"})
    assert conversation._single_execution_tool_key(
        "search_leads", {"with_contacts": True, "state": "CA"}
    )
    assert not conversation._single_execution_tool_key(
        "search_leads", {"with_contacts": False, "state": "CA"}
    )


def test_identical_model_tool_retry_executes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repeated failed web query receives cached evidence without a second call."""

    class FakeMessages:
        """Request the same web search twice, then report the cached failure."""

        calls = 0

        def create(self, **_kwargs: object) -> object:
            """Emit two identical tool requests followed by final JSON."""
            self.calls += 1
            if self.calls <= 2:
                block = SimpleNamespace(
                    type="tool_use",
                    name="web_search",
                    input={
                        "query": (
                            "official Test School security announcement"
                            if self.calls == 1
                            else "Test School board security news"
                        )
                    },
                    id=f"tool-{self.calls}",
                )
                return SimpleNamespace(stop_reason="tool_use", content=[block])
            return SimpleNamespace(
                stop_reason="end_turn",
                content=[
                    SimpleNamespace(
                        type="text",
                        text=(
                            '{"intent":"question","reply":"The web search failed; '
                            'I did not find an announcement."}'
                        ),
                    )
                ],
            )

    class FakeAnthropic:
        """Expose the scripted repeating-tool client."""

        def __init__(self, **_kwargs: object) -> None:
            """Initialize its messages resource."""
            self.messages = FakeMessages()

    executions: list[dict[str, object]] = []

    def fake_run_tool(
        _name: str, args: dict[str, object], *_pos: object, **_kw: object
    ) -> tuple[str, None]:
        """Record actual dispatches and return a stable outage result."""
        executions.append(args)
        return "ERROR: web search failed.", None

    monkeypatch.setattr(conversation, "Anthropic", FakeAnthropic)
    monkeypatch.setattr(tools, "run_tool", fake_run_tool)
    output = conversation.respond("Find an official announcement.", None)
    assert len(executions) == 1
    assert "failed" in output["reply"]


def test_different_successful_tool_arguments_each_execute_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deduplication never collapses two genuinely different queries."""

    class FakeMessages:
        """Request distinct lead statistics before returning a response."""

        calls = 0

        def create(self, **_kwargs: object) -> object:
            """Emit two distinct tool calls followed by final JSON."""
            self.calls += 1
            if self.calls <= 2:
                block = SimpleNamespace(
                    type="tool_use",
                    name="lead_stats",
                    input={"state": "CA" if self.calls == 1 else "WA"},
                    id=f"tool-{self.calls}",
                )
                return SimpleNamespace(stop_reason="tool_use", content=[block])
            return SimpleNamespace(
                stop_reason="end_turn",
                content=[
                    SimpleNamespace(
                        type="text",
                        text='{"intent":"question","reply":"Both counts are ready."}',
                    )
                ],
            )

    class FakeAnthropic:
        """Expose the distinct-query model script."""

        def __init__(self, **_kwargs: object) -> None:
            """Initialize its messages resource."""
            self.messages = FakeMessages()

    executions: list[str] = []

    def fake_run_tool(
        _name: str, args: dict[str, object], *_pos: object, **_kw: object
    ) -> tuple[str, None]:
        """Record the state filter for each real dispatch."""
        executions.append(str(args["state"]))
        return "Counts available.", None

    monkeypatch.setattr(conversation, "Anthropic", FakeAnthropic)
    monkeypatch.setattr(tools, "run_tool", fake_run_tool)
    conversation.respond("Compare California and Washington counts.", None)
    assert executions == ["CA", "WA"]


def test_paid_web_search_executes_once_even_when_model_changes_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One human turn consumes at most one successful web-search request."""

    class FakeMessages:
        """Request two related web searches before returning a summary."""

        calls = 0

        def create(self, **_kwargs: object) -> object:
            """Emit two distinct queries followed by final JSON."""
            self.calls += 1
            if self.calls <= 2:
                return SimpleNamespace(
                    stop_reason="tool_use",
                    content=[
                        SimpleNamespace(
                            type="tool_use",
                            name="web_search",
                            input={"query": f"school security news {self.calls}"},
                            id=f"web-{self.calls}",
                        )
                    ],
                )
            return SimpleNamespace(
                stop_reason="end_turn",
                content=[
                    SimpleNamespace(
                        type="text",
                        text='{"intent":"question","reply":"One search result."}',
                    )
                ],
            )

    class FakeAnthropic:
        """Expose the two-query model script."""

        def __init__(self, **_kwargs: object) -> None:
            """Initialize its messages resource."""
            self.messages = FakeMessages()

    executions: list[str] = []

    def fake_run_tool(
        _name: str, args: dict[str, object], *_pos: object, **_kw: object
    ) -> tuple[str, None]:
        """Record the only paid query that should execute."""
        executions.append(str(args["query"]))
        return "One official result.", None

    monkeypatch.setattr(conversation, "Anthropic", FakeAnthropic)
    monkeypatch.setattr(tools, "run_tool", fake_run_tool)
    conversation.respond("Find news about this school.", None)
    assert executions == ["school security news 1"]


def test_corrected_tool_arguments_reexecute_after_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One validation error must not brick the tool for corrected retries.

    Live failure 2026-07-18: search_leads errored once on bad arguments and the
    name-keyed error cache then served that same error to every corrected call,
    draining all tool turns. A corrected call must actually execute."""

    class FakeMessages:
        """Retry a failed tool with corrected arguments, then summarize."""

        calls = 0

        def create(self, **_kwargs: object) -> object:
            """Emit a bad call, then a corrected call, then final JSON."""
            self.calls += 1
            if self.calls <= 2:
                block = SimpleNamespace(
                    type="tool_use",
                    name="lead_stats",
                    input=(
                        {"state": "TX", "bogus": "x"}
                        if self.calls == 1
                        else {"state": "TX"}
                    ),
                    id=f"tool-{self.calls}",
                )
                return SimpleNamespace(stop_reason="tool_use", content=[block])
            return SimpleNamespace(
                stop_reason="end_turn",
                content=[
                    SimpleNamespace(
                        type="text",
                        text='{"intent":"question","reply":"Texas counts ready."}',
                    )
                ],
            )

    class FakeAnthropic:
        """Expose the error-then-corrected model script."""

        def __init__(self, **_kwargs: object) -> None:
            """Initialize its messages resource."""
            self.messages = FakeMessages()

    executions: list[dict[str, object]] = []

    def fake_run_tool(
        _name: str, args: dict[str, object], *_pos: object, **_kw: object
    ) -> tuple[str, None]:
        """Error on the bogus call; succeed on the corrected one."""
        executions.append(args)
        if "bogus" in args:
            return "ERROR: unknown argument bogus.", None
        return "Counts available.", None

    monkeypatch.setattr(conversation, "Anthropic", FakeAnthropic)
    monkeypatch.setattr(tools, "run_tool", fake_run_tool)
    output = conversation.respond("How many Texas leads do we have?", None)
    assert len(executions) == 2  # the corrected call really ran
    assert executions[1] == {"state": "TX"}
    assert output["reply"] == "Texas counts ready."


def test_tool_budget_exhaustion_forces_final_no_tools_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Draining every tool turn ends in an honest model summary, not a dead end."""

    class FakeMessages:
        """Call tools forever until the loop forces a final no-tools answer."""

        calls = 0
        finalizer_seen = False

        def create(self, **kwargs: object) -> object:
            """Return tool_use whenever tools are offered; else final JSON."""
            self.calls += 1
            if "tools" in kwargs:
                block = SimpleNamespace(
                    type="tool_use",
                    name="lead_stats",
                    input={"state": f"S{self.calls}"},  # always novel: no cache
                    id=f"tool-{self.calls}",
                )
                return SimpleNamespace(stop_reason="tool_use", content=[block])
            FakeMessages.finalizer_seen = True
            return SimpleNamespace(
                stop_reason="end_turn",
                content=[
                    SimpleNamespace(
                        type="text",
                        text=(
                            '{"intent":"question","reply":"Here is what the '
                            'stats showed so far; one thing went unchecked."}'
                        ),
                    )
                ],
            )

    class FakeAnthropic:
        """Expose the never-finishing model script."""

        def __init__(self, **_kwargs: object) -> None:
            """Initialize its messages resource."""
            self.messages = FakeMessages()

    def fake_run_tool(
        _name: str, _args: dict[str, object], *_pos: object, **_kw: object
    ) -> tuple[str, None]:
        """Return a bland result that never satisfies the scripted model."""
        return "Counts available.", None

    monkeypatch.setattr(conversation, "Anthropic", FakeAnthropic)
    monkeypatch.setattr(tools, "run_tool", fake_run_tool)
    output = conversation.respond("Compare every state.", None)
    assert FakeMessages.finalizer_seen  # the no-tools finalizer actually ran
    assert "what the stats showed" in output["reply"]
    assert "hit my limit" not in output["reply"]
