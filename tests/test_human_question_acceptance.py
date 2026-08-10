"""Opt-in real-model acceptance matrix for realistic Grant conversations.

Default pytest skips these networked model checks. Operators run them explicitly with
``GRANT_LLM_ACCEPTANCE=1 python -m pytest tests/test_human_question_acceptance.py``.
Every Grant tool is replaced with a truthful canned outcome, so the suite exercises
language understanding and tool choice without web calls, writes, paid discovery,
Salesforce changes, contact persistence, or outreach submission.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from functools import lru_cache
from pathlib import Path

import pytest
from dotenv import load_dotenv

from acceptance_questions import QUESTIONS, HumanQuestion
from grant_watch import db
from grant_watch.slack import conversation, tools


@lru_cache(maxsize=1)
def _lead_row() -> sqlite3.Row:
    """One realistic lead as the FACTS boundary for lead-thread scenarios.

    Built in a throwaway database rather than read from the developer's real one.
    It used to call a bare `db.connect()` and load lead 231, which meant this suite
    silently depended on one machine's local data AND opened the live file — the
    conftest guard now refuses that, which is how the dependency came to light. The
    values below are copied from a genuine SVPP award so the FACTS block the model
    sees is the shape it sees in production.
    """
    path = Path(tempfile.mkdtemp(prefix="acceptance-")) / "facts.db"
    connection = db.connect(path)
    connection.execute(
        """INSERT INTO leads
             (id,source,source_item_id,entity_name,state,location_city,program,
              amount,funds_start,funds_end,detail_url,lead_grade,status)
           VALUES (231,'usaspending:16.071','ASST_NON_TEST_015',
                   'BIRMINGHAM COMMUNITY CHARTER HIGH SCHOOL','CA','Lake Balboa',
                   'SVPP',500000,'2025-10-01','2028-09-30',
                   'https://www.usaspending.gov/award/ASST_NON_TEST_015',
                   'gold','new')"""
    )
    connection.commit()
    row = db.get_lead(connection, 231)
    if row is None:  # pragma: no cover - the insert above cannot fail silently
        raise AssertionError("acceptance fixture lead could not be built")
    return row


def _canned_tool(
    calls: list[str],
    overrides: dict[str, str],
    name: str,
    args: dict[str, object],
    *_pos: object,
    **_kw: object,
) -> tuple[str, None]:
    """Return safe typed evidence while recording the model's actual tool choice."""
    del args
    calls.append(name)
    if name in overrides:
        return overrides[name], None
    outcomes = {
        "web_search": "No results found.",
        "lead_stats": "Counts by grade:\n- gold: 34\n- silver: 4\n- watch: 96\n- SVPP: 34",
        "find_contact": (
            "No verifiable contact found on the official website. Recorded as "
            "not_found; never guess an email."
        ),
        "salesforce_lookup": (
            "No visible Salesforce Account, Lead, or Contact match after a complete "
            "read-only search."
        ),
        "find_person_linkedin": (
            "LinkedIn: Vic Example, IT Systems Manager — "
            "https://www.linkedin.com/in/vic-example (candidate profile; no email verified)"
        ),
        "search_leads": (
            "Found 1 matching grant:\n- Lead #42 — Test School (CA, school) — "
            "SVPP · $500,000 · spend window 2025-10-01 to 2028-09-30."
        ),
        "salesforce_campaign_search": (
            "Found 1 Campaign result: 2026 School Security — "
            "https://example.my.salesforce.com/lightning/r/Campaign/701TEST/view. "
            "Ask the user to confirm this exact Campaign."
        ),
        "salesforce_campaign_create_preview": (
            "Campaign creation preview ready. Nothing has been written; tell the user "
            "to inspect and click the confirmation button."
        ),
        "salesforce_campaign_members_preview": (
            "Campaign member preview ready. Nothing has been written; click the "
            "confirmation button to execute."
        ),
        "salesforce_campaign_batch_preview": (
            "Complete state/tier batch frozen with one isolated Campaign preview per "
            "state. Nothing has been written; inspect each confirmation button."
        ),
    }
    return outcomes.get(name, f"Safe canned result for {name}."), None


@pytest.mark.skipif(
    os.environ.get("GRANT_LLM_ACCEPTANCE") != "1",
    reason="real-model acceptance requires explicit GRANT_LLM_ACCEPTANCE=1",
)
@pytest.mark.parametrize("case", QUESTIONS, ids=lambda case: case.case_id)
def test_real_model_understands_human_question_families(
    monkeypatch: pytest.MonkeyPatch, case: HumanQuestion
) -> None:
    """Exercise the current model and enforce each scenario's minimum safe outcome."""
    load_dotenv()
    calls: list[str] = []
    monkeypatch.setattr(
        tools,
        "run_tool",
        lambda name, args, *pos, **kw: _canned_tool(
            calls, dict(case.tool_results), name, args, *pos, **kw
        ),
    )
    output = conversation.respond(
        case.question,
        _lead_row() if case.lead_thread else None,
        thread_context=list(case.context) or None,
        requester_slack="U_TEST",
        workspace="T_TEST",
        channel="C_TEST",
        thread_ts="THREAD_TEST",
    )
    reply = str(output["reply"])
    assert output["intent"] in case.allowed_intents
    if case.family == "lead-search" and not case.context:
        # AN ANCHORED ASK RUNS. THE PLAN-AND-CONFIRM FLOW WAS DELIBERATELY REMOVED.
        #
        # This block used to demand a "Search plan: … reply yes and I'll run it"
        # preamble for every context-free search. That behaviour was replaced on
        # 2026-07-18 (ce1295a, "run anchored asks, scope open ones, never dead-end")
        # after Chase's feedback that being made to restate a request in Grant's
        # format is the fastest way to lose a rep — and every one of these cases
        # names a state, a programme, an amount or an org type, so every one is
        # anchored.
        #
        # The tests were never updated, so they demanded the removed flow for three
        # weeks and the suite sat at ~22 red. A test asserting a behaviour the
        # product deliberately dropped is worse than no test: it reads as a
        # regression every run, so people stop reading it.
        assert "search_leads" in calls, (
            "an anchored ask must run, not interrogate the rep"
        )
        assert not reply.lower().startswith("search plan:"), (
            "the plan-and-confirm preamble was removed on 2026-07-18"
        )
        assert "reply yes" not in reply.lower()
    for tool_name in case.expected_tools:
        assert tool_name in calls
        assert calls.count(tool_name) == 1
    for tool_name in case.forbidden_tools:
        assert tool_name not in calls
    for fragment in case.expected_reply:
        assert fragment.lower() in reply.lower()
    for alternatives in case.expected_any:
        assert any(fragment.lower() in reply.lower() for fragment in alternatives)
    for forbidden in case.forbidden_reply:
        assert forbidden.lower() not in reply.lower()


@pytest.mark.skipif(
    os.environ.get("GRANT_LLM_ACCEPTANCE") != "1",
    reason="real-model acceptance requires explicit GRANT_LLM_ACCEPTANCE=1",
)
def test_campaign_settings_followup_passes_exact_preview_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model carries the named Campaign into one exact, fully specified preview."""
    load_dotenv()
    captured: list[tuple[str, dict[str, object]]] = []

    def canned_tool(
        name: str,
        args: dict[str, object],
        *_pos: object,
        **_kw: object,
    ) -> tuple[str, None]:
        """Capture exact model arguments while keeping every tool write-free."""
        captured.append((name, dict(args)))
        if name == "salesforce_campaign_create_preview":
            return "Campaign preview ready; click its confirmation button.", None
        return f"Safe canned result for {name}.", None

    monkeypatch.setattr(tools, "run_tool", canned_tool)
    output = conversation.respond(
        "Use Type Other, Status Planned, Active, with no dates.",
        None,
        thread_context=[
            "Chase: I need a new Campaign.",
            "Grant: What would you like to name it?",
            "Chase: Name it 2026 California School Security.",
            "Grant: What Type, Status, Active setting, and dates should I use?",
        ],
        requester_slack="U_TEST",
        workspace="T_TEST",
        channel="C_TEST",
        thread_ts="THREAD_TEST",
    )
    preview_calls = [
        args for name, args in captured if name == "salesforce_campaign_create_preview"
    ]
    assert len(preview_calls) == 1
    args = preview_calls[0]
    assert args["name"] == "2026 California School Security"
    assert args["campaign_type"] == "Other"
    assert args["status"] == "Planned"
    assert args["is_active"] is True
    assert args["date_mode"] == "none"
    assert not args.get("start_date")
    assert not args.get("end_date")
    assert "preview" in str(output["reply"]).lower()


def test_matrix_covers_every_documented_human_question_family() -> None:
    """Keep the acceptance corpus broad when Grant gains or loses capabilities."""
    required = {
        "source-discovery",
        "lead-search",
        "lead-stats",
        "lead-evidence",
        "date-truth",
        "contact",
        "linkedin",
        "salesforce-read",
        "salesforce-write",
        "web-research",
        "outreach",
        "lead-management",
        "chitchat",
        "truthfulness",
        "safety",
    }
    observed = {case.family for case in QUESTIONS}
    assert observed == required
    assert len({case.case_id for case in QUESTIONS}) == len(QUESTIONS)
    assert len(QUESTIONS) >= 55
    expected_tools = {tool for case in QUESTIONS for tool in case.expected_tools}
    schema_tools = {str(schema["name"]) for schema in tools.TOOL_SCHEMAS}
    # Source inventory questions intentionally bypass model tool selection and route
    # through the same deterministic implementation before Anthropic is constructed.
    assert schema_tools - {"source_inventory_status"} <= expected_tools


def test_acceptance_module_contains_no_external_write_implementation() -> None:
    """The real-model matrix may select tools but cannot implement external writes."""
    source = Path(__file__).read_text()
    forbidden_calls = tuple(
        left + right
        for left, right in (
            ("requests.", "post("),
            ("chat_", "postMessage("),
            ("submit_", "brief("),
        )
    )
    assert not any(call in source for call in forbidden_calls)
