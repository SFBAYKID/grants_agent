"""Two guards where a false positive destroys something a human cannot get back.

`bad_lead` sets leads.status='dead' and scores the lead -8. The override that forces
it fired on the mere PHRASE, so "that's not a bad lead" and "why did you call this a
bad lead?" both destroyed the lead they were defending.

`find_person_linkedin` answered "who works here?" when the rep asked "where is Jane
Smith?" — returning a real but different person under the name the rep supplied, and
persisting them toward a Salesforce person Lead when a lead id was in play. That is
not a fabricated contact; it is a real human misattributed, which is worse.
"""

from __future__ import annotations

from typing import Any

import pytest

from grant_watch.enrich import finder
from grant_watch.slack.conversation import _normalize_action_intent


def _intent(text: str) -> str:
    """Run the deterministic intent gate over one user utterance."""
    return str(
        _normalize_action_intent(text, [], {"intent": "question", "reply": "ok"})[
            "intent"
        ]
    )


@pytest.mark.parametrize(
    "text",
    [
        "that's not a bad lead",
        "this isn't a bad lead",
        "that was never a bad lead",
        "why did you call this a bad lead?",
        "is this a bad lead?",
        "what makes it a bad lead",
    ],
)
def test_a_negated_or_questioning_phrase_never_destroys_the_lead(text: str) -> None:
    """Defending or asking about a lead must not kill it."""
    assert _intent(text) != "bad_lead"


@pytest.mark.parametrize(
    "text",
    [
        "bad lead",
        "mark this as a bad lead",
        "can you mark this as a bad lead?",
        "kill this lead",
        "this is not a good lead",
    ],
)
def test_a_genuine_kill_request_still_works(text: str) -> None:
    """The guard must not become so cautious that a real request stops working.

    "this is not a good lead" is included deliberately: it contains a negator, but
    the negator is part of the bad-lead phrase itself rather than inverting it.
    """
    assert _intent(text) == "bad_lead"


class _Result(dict):
    """One Firecrawl search result."""


def _linkedin_results(*titles: str) -> list[dict[str, Any]]:
    """Build LinkedIn-shaped search results from 'Name - Title - Org' strings."""
    return [
        {"url": f"https://www.linkedin.com/in/person{n}", "title": f"{t} | LinkedIn"}
        for n, t in enumerate(titles)
    ]


def test_a_named_person_search_never_returns_a_different_human(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Asking for Jane Smith must yield Jane Smith or an honest nothing.

    The first result here is a real, plausible, name-shaped person at the right
    organization — exactly what the old code returned under the requested name.
    """
    monkeypatch.setattr(
        finder,
        "_search",
        lambda q, limit=5: _linkedin_results(
            "Robert Alvarez - Director of Technology - Alief ISD",
            "Jane Smith - Grants Coordinator - Alief ISD",
        ),
    )
    found = finder.linkedin_person("Alief ISD", "TX", person_name="Jane Smith")
    assert found is not None
    assert found["name"] == "Jane Smith"


def test_a_named_person_who_is_absent_returns_nothing_rather_than_a_substitute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No match must be a not-found, never the next plausible profile."""
    monkeypatch.setattr(
        finder,
        "_search",
        lambda q, limit=5: _linkedin_results(
            "Robert Alvarez - Director of Technology - Alief ISD",
            "Maria Chen - Superintendent - Alief ISD",
        ),
    )
    assert finder.linkedin_person("Alief ISD", "TX", person_name="Jane Smith") is None


def test_an_unnamed_role_search_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a person_name the tool still answers "who runs technology here?"."""
    monkeypatch.setattr(
        finder,
        "_search",
        lambda q, limit=5: _linkedin_results(
            "Robert Alvarez - Director of Technology - Alief ISD"
        ),
    )
    found = finder.linkedin_person("Alief ISD", "TX")
    assert found is not None
    assert found["name"] == "Robert Alvarez"


def test_the_named_search_query_targets_the_person_not_the_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Role keywords would outrank the name and surface the title-holder instead."""
    seen: dict[str, str] = {}

    def capture(query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Record the query the finder built."""
        seen["query"] = query
        return []

    monkeypatch.setattr(finder, "_search", capture)
    finder.linkedin_person("Alief ISD", "TX", person_name="Jane Smith")
    assert '"Jane Smith"' in seen["query"]
    assert "superintendent" not in seen["query"].lower()


@pytest.mark.parametrize(
    "requested, found, expected",
    [
        ("Jane Smith", "Jane Smith", True),
        ("Jane Smith", "Jane A. Smith", True),
        ("Jane Smith", "J. Smith", True),
        ("Jane Smith", "Dr. Jane Smith", True),
        ("Jane Smith", "Robert Smith", False),
        ("Jane Smith", "Jane Doe", False),
        ("Jane Smith", "", False),
    ],
)
def test_name_matching_admits_variants_but_not_other_people(
    requested: str, found: str, expected: bool
) -> None:
    """Surname must agree exactly and the first initial must agree."""
    assert finder.names_match(requested, found) is expected
