"""Slack Bolt registration for immutable rich-card button actions.

Split from ``grant.py`` to keep the primary event router under the repository's hard
line cap. The handlers delegate all authorization/idempotency to campaign actions and
use Grant's shared thread/context helpers.
"""

from __future__ import annotations

from typing import Any

from slack_bolt import Ack, App
from slack_sdk import WebClient

from .. import db


def _context(
    body: dict[str, Any], client: WebClient
) -> tuple[str, str, str, str, bool]:
    """Extract and re-verify the common rich-card action context."""
    from . import grant

    user = str((body.get("user") or {}).get("id") or "")
    channel = str((body.get("channel") or {}).get("id") or "")
    return (
        grant._workspace_id(body),
        channel,
        grant._interaction_thread_ts(body),
        user,
        grant._active_human_channel_member(client, user, channel),
    )


def register(app: App) -> None:
    """Register draft-request and not-relevant callbacks on Grant's Bolt app."""
    from . import grant

    @app.action("rich_persequor_draft")
    def rich_persequor_draft(ack: Ack, body: dict[str, Any], client: WebClient) -> None:
        """Request one human-reviewed Persequor draft from frozen card facts."""
        ack()
        from ..campaign import actions

        snapshot_id = str((body.get("actions") or [{}])[0].get("value") or "")
        workspace, channel, thread_ts, user, member = _context(body, client)
        nonce = str(body.get("action_ts") or body.get("trigger_id") or "")
        try:
            result = actions.request_draft(
                db.connect(),
                snapshot_id,
                workspace=workspace,
                channel=channel,
                thread_ts=thread_ts,
                requester=user,
                requester_is_member=member,
                nonce=nonce,
            )
        except (PermissionError, ValueError) as exc:
            grant._thread_reply(client, body, f"No draft was requested: {str(exc)}.")
            return
        grant._thread_reply(client, body, result.message)

    @app.action("rich_not_relevant")
    def rich_not_relevant(ack: Ack, body: dict[str, Any], client: WebClient) -> None:
        """Record deduplicated not-relevant feedback for this frozen card."""
        ack()
        from ..campaign import actions

        snapshot_id = str((body.get("actions") or [{}])[0].get("value") or "")
        workspace, channel, thread_ts, user, member = _context(body, client)
        nonce = str(body.get("action_ts") or body.get("trigger_id") or "")
        try:
            result = actions.mark_not_relevant(
                db.connect(),
                snapshot_id,
                workspace=workspace,
                channel=channel,
                thread_ts=thread_ts,
                requester=user,
                requester_is_member=member,
                nonce=nonce,
            )
        except (PermissionError, ValueError) as exc:
            grant._thread_reply(client, body, f"Nothing was changed: {str(exc)}.")
            return
        grant._thread_reply(client, body, result.message)
