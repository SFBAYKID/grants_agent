"""Bounded LinkedIn search-result presentation for Grant's Slack conversations."""

from __future__ import annotations

from collections.abc import Callable

from ..enrich import finder

Progress = Callable[[str], None]


def find_person_linkedin(entity: str, state: str,
                         on_progress: Progress | None = None) -> str:
    """Return one possible, organization-bound LinkedIn result without an email."""
    person = finder.linkedin_person(entity, state, on_progress=on_progress)
    if person is None:
        return ("I couldn’t find a clear LinkedIn match tied to this organization. "
                "I won’t guess at a person.")
    role = person.title or "role not shown in the search result"
    return (
        "I found a possible LinkedIn contact:\n\n"
        f"• *Name:* {person.name}\n"
        f"• *Role:* {role}\n"
        f"• *Profile:* <{person.url}|LinkedIn>\n"
        "• *Verification:* matched in LinkedIn search results; no email verified"
    )
