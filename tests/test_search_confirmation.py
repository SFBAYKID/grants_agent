"""Offline regression tests for Grant's deterministic first-search gate."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from grant_watch.slack import conversation, tools


def test_initial_fully_specified_search_is_confirmed_before_tool_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even a complete first request cannot bypass Grant's confirm-first contract."""

    class FakeMessages:
        """Propose the exact lead query that must be gated before execution."""

        def create(self, **_kwargs: object) -> object:
            """Return one search tool call without executing a second model turn."""
            block = SimpleNamespace(
                type="tool_use",
                name="search_leads",
                input={
                    "state": "CA",
                    "org_type": "school",
                    "grade": "gold",
                    "limit": 1,
                    "result_scope": "top_n",
                },
                id="search-1",
            )
            return SimpleNamespace(stop_reason="tool_use", content=[block])

    class FakeAnthropic:
        """Expose the single-turn search proposal."""

        def __init__(self, **_kwargs: object) -> None:
            """Initialize the fake message client."""
            self.messages = FakeMessages()

    def forbidden_run_tool(*_args: object, **_kwargs: object) -> tuple[str, None]:
        """Fail if the database search occurs before confirmation."""
        raise AssertionError("search_leads must not execute before confirmation")

    monkeypatch.setattr(conversation, "Anthropic", FakeAnthropic)
    monkeypatch.setattr(tools, "run_tool", forbidden_run_tool)
    out = conversation.respond(
        "Give me one GOLD California school lead, just show it here.", None
    )
    assert out["reply"].startswith("Search plan:")
    assert "CA · school" in out["reply"]
    assert "gold" in out["reply"]
    assert "Reply yes" in out["reply"]


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
