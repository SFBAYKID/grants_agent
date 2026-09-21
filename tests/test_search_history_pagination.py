"""Long-thread date constraints must use complete history, never an old first page."""

from __future__ import annotations

from datetime import date

import pytest

from grant_watch.slack import venues
from grant_watch.slack.search_recency import recent_award_scope


class HistoryPages:
    """Return bounded Slack-shaped pages, optionally failing after an old page."""

    def __init__(self, pages: list[dict[str, object] | Exception]) -> None:
        """Keep fixtures and observed cursors private to one test."""
        self.pages = pages
        self.cursors: list[object] = []

    def conversations_replies(self, **kwargs: object) -> dict[str, object]:
        """Record each requested cursor and return or raise its scripted outcome."""
        self.cursors.append(kwargs.get("cursor"))
        page = self.pages[len(self.cursors) - 1]
        if isinstance(page, Exception):
            raise page
        return page


def test_newer_page_override_supersedes_original_recent_request() -> None:
    """The old limit=12 bug must not resurrect recency after a human changes dates."""
    first = [{"text": "Find schools awarded grants recently", "user": "U1"}]
    first += [{"text": f"bot result {index}", "bot_id": "B1"} for index in range(12)]
    client = HistoryPages(
        [
            {
                "messages": first,
                "has_more": True,
                "response_metadata": {"next_cursor": "page2"},
            },
            {
                "messages": [
                    {"text": "all years instead", "user": "U1"},
                    {"text": "NY too", "user": "U1"},
                ]
            },
        ]
    )
    history = venues.thread_history(client, "C1", "1.0")  # type: ignore[arg-type]
    assert client.cursors == [None, "page2"]
    assert history[-2:] == ["rep: all years instead", "rep: NY too"]
    assert recent_award_scope("NY too", history, today=date(2026, 9, 8)) is None


def test_original_scope_survives_more_than_twelve_messages() -> None:
    """The guard sees the root constraint even though the model reads only recent turns."""
    client = HistoryPages(
        [
            {
                "messages": [
                    {"text": "Find schools awarded grants recently", "user": "U1"}
                ],
                "has_more": True,
                "response_metadata": {"next_cursor": "page2"},
            },
            {
                "messages": [{"text": "results", "bot_id": "B1"} for _ in range(20)]
                + [
                    {
                        "text": "can you put these new york schools in a spreadsheet",
                        "user": "U1",
                    }
                ]
            },
        ]
    )
    history = venues.thread_history(client, "C1", "1.0")  # type: ignore[arg-type]
    scope = recent_award_scope("NY too", history, today=date(2026, 9, 8))
    assert scope is not None and scope.start == date(2026, 3, 8)


@pytest.mark.parametrize(
    "last",
    [
        RuntimeError("unavailable"),
        {"messages": [], "has_more": True},
        {
            "messages": [],
            "has_more": True,
            "response_metadata": {"next_cursor": "page2"},
        },
    ],
)
def test_partial_or_looping_history_never_returns_old_scope(
    last: dict[str, object] | Exception,
) -> None:
    """Missing pages might contain a newer override, so incomplete context is discarded."""
    client = HistoryPages(
        [
            {
                "messages": [{"text": "recent awards", "user": "U1"}],
                "has_more": True,
                "response_metadata": {"next_cursor": "page2"},
            },
            last,
        ]
    )
    assert venues.thread_history(client, "C1", "1.0") == []  # type: ignore[arg-type]
    assert len(client.cursors) == 2


def test_history_page_cap_discards_incomplete_context() -> None:
    """A pathological thread cannot monopolize Slack or leave an obsolete prefix."""
    client = HistoryPages(
        [
            {
                "messages": [{"text": "recent awards", "user": "U1"}],
                "has_more": True,
                "response_metadata": {"next_cursor": str(index)},
            }
            for index in range(5)
        ]
    )
    assert venues.thread_history(client, "C1", "1.0") == []  # type: ignore[arg-type]
    assert len(client.cursors) == 5
