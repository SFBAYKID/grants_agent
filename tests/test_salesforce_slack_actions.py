"""Slack Campaign approval audit and action-specific rendering tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from grant_watch import db
from grant_watch.enrich import salesforce_campaigns as campaigns
from grant_watch.slack.conversation import _extract_pending_actions
from grant_watch.slack.salesforce_actions import _terminalize_action


class UpdateClient:
    """Capture one Slack message update without external I/O."""

    def __init__(self) -> None:
        """Initialize an empty captured Slack update."""
        self.update: dict[str, Any] = {}

    def chat_update(self, **kwargs: Any) -> None:
        """Store the exact action-specific replacement blocks."""
        self.update = kwargs


def test_approval_attempt_audit_is_idempotent_and_secret_free(tmp_path: Path) -> None:
    """Slack retries collapse by action/actor/action_ts/outcome without nonce storage."""
    conn = db.connect(tmp_path / "audit.db")
    values = {
        "action_id": "action-1",
        "actor": "UREP",
        "workspace": "TWORK",
        "channel": "CGRANTS",
        "thread_ts": "123.4",
        "action_ts": "124.5",
        "outcome": "rejected",
        "reason": "Only the initiating user may approve this action",
    }
    campaigns.record_approval_attempt(conn, **values)
    campaigns.record_approval_attempt(conn, **values)
    row = conn.execute(
        """SELECT actor_slack,outcome,reason FROM crm_campaign_approval_attempts"""
    ).fetchone()
    assert tuple(row) == (
        "UREP",
        "rejected",
        "Only the initiating user may approve this action",
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM crm_campaign_approval_attempts").fetchone()[
            0
        ]
        == 1
    )
    columns = {
        str(item[1])
        for item in conn.execute("PRAGMA table_info(crm_campaign_approval_attempts)")
    }
    assert not {"nonce", "payload", "message_body"} & columns


def test_terminalization_removes_only_the_clicked_campaign_action() -> None:
    """Completing IL cannot remove TX's independent confirmation controls."""
    client = UpdateClient()
    body = {
        "channel": {"id": "CGRANTS"},
        "message": {
            "ts": "125.6",
            "text": "Campaign approvals",
            "blocks": [
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "value": '{"action_id":"action-1","nonce":"secret"}',
                        }
                    ],
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "value": '{"action_id":"action-10","nonce":"secret"}',
                        }
                    ],
                },
            ],
        },
    }
    _terminalize_action(client, body, "action-1", "complete")
    blocks = client.update["blocks"]
    assert blocks[0]["type"] == "context"
    assert blocks[1]["type"] == "actions"
    assert "action-10" in blocks[1]["elements"][0]["value"]


def test_unknown_outcome_keeps_a_read_only_reconciliation_control() -> None:
    """An uncertain Salesforce result remains safely reconcilable from Slack."""
    client = UpdateClient()
    value = '{"action_id":"action-1","nonce":"secret"}'
    body = {
        "channel": {"id": "CGRANTS"},
        "message": {
            "ts": "125.6",
            "text": "Campaign approval",
            "blocks": [
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "action_id": "salesforce_confirm",
                            "value": value,
                            "style": "primary",
                            "confirm": {
                                "title": {"type": "plain_text", "text": "Write"}
                            },
                        },
                        {
                            "type": "button",
                            "action_id": "salesforce_cancel",
                            "value": "action-1",
                        },
                    ],
                }
            ],
        },
    }
    _terminalize_action(client, body, "action-1", "unknown")
    reconcile = client.update["blocks"][0]["elements"][0]
    assert reconcile["value"] == value
    assert "Reconcile" in reconcile["text"]["text"]
    assert "confirm" not in reconcile and "style" not in reconcile
    assert "read-only" in client.update["blocks"][1]["elements"][0]["text"]


def test_multiple_batch_markers_are_all_renderable() -> None:
    """One tool result can safely produce an isolated button for every Campaign."""
    marker = (
        '<grant-crm-action>{"action_id":"a","nonce":"n","preview":"A",'
        '"expires_at":"e"}</grant-crm-action>'
    )
    clean, actions = _extract_pending_actions(f"summary\n{marker}\n{marker}")
    assert clean == "summary"
    assert [action["action_id"] for action in actions] == ["a", "a"]
