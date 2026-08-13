"""Slack approval-card blocks for human-confirmed Salesforce writes.

Split out of `grant.py` at the 1000-line cap (rule 4). These are PURE renderers: they
take an already-frozen preview and emit Slack blocks, so they hold no Slack client, no
database handle and no app state, and a change here cannot alter what a confirmation
actually writes — the action id and nonce are carried through verbatim.
"""

from __future__ import annotations

import json
from typing import Any  # Slack block payloads are runtime-shaped.


def _crm_action_blocks(actions: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Render exact immutable previews with one-time confirm/cancel buttons."""
    blocks: list[dict[str, Any]] = []
    for action in actions:
        value = json.dumps(
            {
                "action_id": action["action_id"],
                "nonce": action["nonce"],
            },
            separators=(",", ":"),
        )
        preview_blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": chunk}}
            for chunk in _split_slack_text(action["preview"])
        ]
        blocks.extend(
            [
                *preview_blocks,
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": f"Approval expires {action['expires_at']}.",
                        }
                    ],
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "action_id": "salesforce_confirm",
                            "text": {
                                "type": "plain_text",
                                "text": "Confirm in Salesforce",
                            },
                            "style": "primary",
                            "value": value,
                            "confirm": {
                                "title": {
                                    "type": "plain_text",
                                    "text": "Confirm Salesforce write",
                                },
                                "text": {
                                    "type": "mrkdwn",
                                    "text": "Create exactly the records in this preview?",
                                },
                                "confirm": {"type": "plain_text", "text": "Confirm"},
                                "deny": {"type": "plain_text", "text": "Go back"},
                            },
                        },
                        {
                            "type": "button",
                            "action_id": "salesforce_cancel",
                            "text": {"type": "plain_text", "text": "Cancel"},
                            "value": action["action_id"],
                        },
                    ],
                },
            ]
        )
    return blocks


def _split_slack_text(value: str, cap: int = 2_800) -> list[str]:
    """Split long frozen previews at line boundaries under Slack's section limit."""
    chunks: list[str] = []
    current = ""
    for line in value.splitlines() or [value]:
        candidate = f"{current}\n{line}".strip() if current else line
        if current and len(candidate) > cap:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or [""]
