"""Offline regression tests for Grant's deterministic first-search gate."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from grant_watch.slack import conversation, tools


def _search_proposal_client(tool_input: dict[str, object]) -> type:
    """Build a fake Anthropic client that proposes one search then summarizes."""

    class FakeMessages:
        """Propose the scripted lead query, then return final JSON."""

        calls = 0

        def create(self, **_kwargs: object) -> object:
            """Return the search tool call, then a final text answer."""
            FakeMessages.calls += 1
            if FakeMessages.calls == 1:
                block = SimpleNamespace(
                    type="tool_use",
                    name="search_leads",
                    input=dict(tool_input),
                    id="search-1",
                )
                return SimpleNamespace(stop_reason="tool_use", content=[block])
            return SimpleNamespace(
                stop_reason="end_turn",
                content=[
                    SimpleNamespace(
                        type="text",
                        text='{"intent":"question","reply":"Here are the results."}',
                    )
                ],
            )

    class FakeAnthropic:
        """Expose the scripted search-proposal client."""

        def __init__(self, **_kwargs: object) -> None:
            """Initialize the fake message client."""
            self.messages = FakeMessages()

    return FakeAnthropic


def test_anchored_first_search_executes_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A state/org-anchored ask runs right away — no plan recitation, no friction."""
    executions: list[dict[str, object]] = []

    def recording_run_tool(
        _name: str, args: dict[str, object], *_pos: object, **_kw: object
    ) -> tuple[str, None]:
        """Record the read-only search execution."""
        executions.append(args)
        return "Lead #1 Example School District (CA) — $100,000 SVPP.", None

    monkeypatch.setattr(
        conversation,
        "Anthropic",
        _search_proposal_client(
            {"state": "CA", "org_type": "school", "grade": "gold", "limit": 1}
        ),
    )
    monkeypatch.setattr(tools, "run_tool", recording_run_tool)
    out = conversation.respond(
        "Give me one GOLD California school lead, just show it here.", None
    )
    assert len(executions) == 1  # the anchored search really ran
    assert out["reply"] == "Here are the results."


def test_fully_open_ended_search_gets_one_scoping_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A no-anchor ask is scoped with ONE friendly question before any query runs."""

    def forbidden_run_tool(*_args: object, **_kwargs: object) -> tuple[str, None]:
        """Fail if the open-ended search executes before scoping."""
        raise AssertionError("search_leads must not execute before scoping")

    monkeypatch.setattr(
        conversation, "Anthropic", _search_proposal_client({"limit": 5})
    )
    monkeypatch.setattr(tools, "run_tool", forbidden_run_tool)
    out = conversation.respond("show me some leads", None)
    assert out["reply"].startswith("Quick scoping question")
    assert "everywhere" in out["reply"]
    assert "schools, cities" in out["reply"]


def test_scoping_question_is_never_asked_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the scoping question, even an everything answer executes the search."""
    executions: list[dict[str, object]] = []

    def recording_run_tool(
        _name: str, args: dict[str, object], *_pos: object, **_kw: object
    ) -> tuple[str, None]:
        """Record the post-scoping execution."""
        executions.append(args)
        return "Lead #2 Example City (WA) — $50,000 grant.", None

    monkeypatch.setattr(
        conversation, "Anthropic", _search_proposal_client({"limit": 5})
    )
    monkeypatch.setattr(tools, "run_tool", recording_run_tool)
    context = [
        "Chase: show me some leads",
        "Grant: Quick scoping question so I pull the right things: should I look "
        "everywhere or focus on one state? And do you care about a particular kind "
        "of organization — schools, cities — or everything that qualifies?",
    ]
    out = conversation.respond(
        "everywhere, everything that qualifies", None, thread_context=context
    )
    assert len(executions) == 1
    assert out["reply"] == "Here are the results."


def test_count_and_format_followup_confirms_existing_search_plan() -> None:
    """A shape-only answer completes the already-visible plan."""
    context = [
        "Grant: Search plan: location=CA; organization=school; program=SVPP; "
        "date=no date filter; grade=gold; results=count not chosen; "
        "format=format not chosen."
    ]
    assert conversation._search_plan_confirmed("Top 10, Excel please.", context)


def test_bare_affirmative_without_a_prior_plan_cannot_start_search() -> None:
    """A stray yes is not authorization for an unseen database query."""
    assert not conversation._search_plan_confirmed("Yes, go ahead.", None)


@pytest.mark.parametrize(
    "correction",
    (
        "Actually make that Texas, top 10 in Excel.",
        "Yes, but use SILVER leads instead.",
        "Go ahead, but only cities.",
        "Run it, but change the program to NSGP.",
    ),
)
def test_material_filter_correction_requires_a_new_confirmation(
    correction: str,
) -> None:
    """A changed filter cannot silently execute under an earlier confirmation."""
    context = [
        "Grant: Search plan: location=CA; organization=school; program=SVPP; "
        "date=no date filter; grade=gold; results=top 5; "
        "format=listed here in the thread. Reply yes and I’ll run it."
    ]
    assert not conversation._search_plan_confirmed(correction, context)


@pytest.mark.parametrize(
    ("query", "field", "start", "end"),
    (
        (
            "Show five leads Grant discovered during June 2026 here.",
            "discovered",
            "2026-06-01",
            "2026-06-30",
        ),
        (
            "Show five award spend windows ending in August 2026 here.",
            "spend_end",
            "2026-08-01",
            "2026-08-31",
        ),
        (
            "List five Grants.gov opportunities closing in August 2026 here.",
            "opportunity_close",
            "2026-08-01",
            "2026-08-31",
        ),
    ),
)
def test_explicit_month_date_semantics_are_preserved(
    query: str, field: str, start: str, end: str
) -> None:
    """Model omissions cannot erase an explicit supported date meaning."""
    arguments = conversation._basic_search_arguments(query)
    assert arguments["date_field"] == field
    assert arguments["date_from"] == start
    assert arguments["date_to"] == end
    assert arguments["limit"] == 5


def test_pronoun_only_salesforce_call_requires_attached_lead() -> None:
    """A model cannot query Salesforce for an unidentified pronoun."""
    error = conversation._contextual_tool_error(
        "salesforce_lookup", {"entity": "this organization"}, None
    )
    assert "which entity" in error


def test_model_supplied_entity_must_appear_in_the_human_request() -> None:
    """A model cannot fill an unidentified pronoun with an unrelated organization."""
    error = conversation._contextual_tool_error(
        "salesforce_lookup",
        {"entity": "Invented Unified School District"},
        None,
        "Is this already in Salesforce?",
    )
    assert "which entity" in error


def test_named_salesforce_call_without_lead_remains_supported() -> None:
    """A direct organization name is sufficient even outside a lead thread."""
    assert (
        conversation._contextual_tool_error(
            "salesforce_lookup",
            {"entity": "Birmingham Community Charter High School"},
            None,
            "Is Birmingham already in Salesforce?",
        )
        == ""
    )


def test_lead_bound_linkedin_call_is_not_blocked() -> None:
    """An explicit lead_id supplies identity even when the reply text lacks the name."""
    assert (
        conversation._contextual_tool_error(
            "find_person_linkedin",
            {"entity": "Chicago Jewish Day School", "state": "IL", "lead_id": 3485},
            None,
            "yes — try LinkedIn for lead #3485",
        )
        == ""
    )


def test_contact_by_entity_name_is_not_blocked() -> None:
    """find_contact with an explicit entity name passes the gate in general threads."""
    assert (
        conversation._contextual_tool_error(
            "find_contact",
            {"entity": "Chicago Jewish Day School", "state": "IL"},
            None,
            "get me the contact for Chicago Jewish Day School",
        )
        == ""
    )
