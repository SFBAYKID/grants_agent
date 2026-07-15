"""Slack release smoke tests; no test contacts Slack or mutates external state."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from grant_watch.slack import smoke


class FakeSlack:
    """Record the one allowed smoke call and return a stable Slack timestamp."""

    def __init__(self, include_timestamp: bool = True) -> None:
        self.include_timestamp = include_timestamp
        self.calls: list[tuple[str, str]] = []

    def chat_postMessage(self, *, channel: str, text: str) -> dict[str, object]:  # noqa: N802
        """Capture the exact channel/text without network access."""
        self.calls.append((channel, text))
        return {"ts": "123.456"} if self.include_timestamp else {}


def test_smoke_text_is_labeled_and_disclaims_other_writes() -> None:
    """The public test message cannot be mistaken for a real lead notification."""
    text = smoke.smoke_text(datetime(2026, 7, 14, tzinfo=timezone.utc))
    assert "Grant release smoke test" in text
    assert "No leads, emails, Salesforce records, or tenant data" in text


def test_smoke_dry_run_makes_no_slack_call() -> None:
    """Dry-run returns the exact proposed message without posting."""
    client = FakeSlack()
    result = smoke.post_smoke(client, "CGRANTS", dry_run=True)
    assert result.startswith("[dry-run]")
    assert client.calls == []


def test_smoke_requires_slack_timestamp_for_verified_success() -> None:
    """A response without a message timestamp is not reported as posted."""
    client = FakeSlack(include_timestamp=False)
    with pytest.raises(RuntimeError, match="timestamp"):
        smoke.post_smoke(client, "CGRANTS")


def test_smoke_live_path_posts_once() -> None:
    """The non-dry path makes one plain-text call to the configured channel."""
    client = FakeSlack()
    result = smoke.post_smoke(client, "CGRANTS")
    assert result == "posted Slack release smoke test at 123.456"
    assert len(client.calls) == 1
