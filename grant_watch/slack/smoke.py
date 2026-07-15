"""Explicit Slack release smoke test with a mandatory dry-run-compatible path.

The message contains no lead, contact, CRM, or tenant data. It proves only that the
configured Grant bot can post to its configured channel.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol


class SlackPoster(Protocol):
    """Small Slack client surface needed by the release smoke test."""

    def chat_postMessage(self, *, channel: str, text: str) -> dict[str, object]:  # noqa: N802
        """Post one plain-text Slack message and return Slack's response mapping."""
        ...


def smoke_text(now: datetime | None = None) -> str:
    """Build the clearly labeled, factually limited release test message."""
    observed = now or datetime.now(timezone.utc)
    timestamp = observed.astimezone(timezone.utc).isoformat(timespec="seconds")
    return (
        f"🧪 Grant release smoke test — {timestamp}\n"
        "This confirms Grant can post to this channel. No leads, emails, "
        "Salesforce records, or tenant data were changed by this test."
    )


def post_smoke(client: SlackPoster | None, channel: str,
               dry_run: bool = False) -> str:
    """Dry-run or post the smoke message; require a Slack timestamp on success."""
    if not channel.strip():
        raise ValueError("Slack smoke test requires a channel")
    message = smoke_text()
    if dry_run:
        return f"[dry-run] would post to {channel}: {message}"
    if client is None:
        raise ValueError("Slack client is required outside dry-run")
    response = client.chat_postMessage(channel=channel, text=message)
    timestamp = str(response.get("ts") or "")
    if not timestamp:
        raise RuntimeError("Slack did not confirm the smoke-test message timestamp")
    return f"posted Slack release smoke test at {timestamp}"
