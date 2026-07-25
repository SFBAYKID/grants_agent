"""Slack button handlers for audited Salesforce approvals and cancellations."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from slack_bolt import Ack, App
from slack_sdk import WebClient

from .. import db
from ..enrich import salesforce_campaigns as campaigns


def _action_timestamp(body: dict[str, Any]) -> str:
    """Return Slack's stable timestamp for idempotent approval-attempt auditing."""
    actions = body.get("actions") or [{}]
    message = body.get("message") or {}
    return str(
        body.get("action_ts")
        or actions[0].get("action_ts")
        or message.get("ts")
        or "missing"
    )


def _button_action_id(value: str) -> str:
    """Extract an exact action ID from confirm JSON or a plain cancel value."""
    try:
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return str(parsed.get("action_id") or "")
    except json.JSONDecodeError:
        pass
    return value


def _terminalize_action(
    client: WebClient,
    body: dict[str, Any],
    action_id: str,
    outcome: str,
) -> None:
    """Remove only the handled action's buttons while preserving sibling approvals."""
    message = body.get("message") or {}
    channel = str((body.get("channel") or {}).get("id") or "")
    message_ts = str(message.get("ts") or "")
    blocks = list(message.get("blocks") or [])
    if not channel or not message_ts or not blocks:
        return
    updated: list[dict[str, Any]] = []
    changed = False
    for block in blocks:
        if block.get("type") != "actions":
            updated.append(block)
            continue
        handles_action = False
        for element in block.get("elements") or []:
            value = str(element.get("value") or "")
            if _button_action_id(value) == action_id:
                handles_action = True
                break
        if handles_action:
            changed = True
            if outcome == "unknown":
                confirm_element = next(
                    (
                        dict(element)
                        for element in block.get("elements") or []
                        if element.get("action_id") == "salesforce_confirm"
                        and _button_action_id(str(element.get("value") or ""))
                        == action_id
                    ),
                    None,
                )
                if confirm_element is not None:
                    confirm_element["text"] = {
                        "type": "plain_text",
                        "text": "Reconcile Salesforce (read only)",
                    }
                    confirm_element.pop("style", None)
                    confirm_element.pop("confirm", None)
                    updated.append({"type": "actions", "elements": [confirm_element]})
                updated.append(
                    {
                        "type": "context",
                        "elements": [
                            {
                                "type": "mrkdwn",
                                "text": (
                                    "Salesforce outcome is unknown. Reconcile performs "
                                    "read-only verification and never retries the write."
                                ),
                            }
                        ],
                    }
                )
                continue
            updated.append(
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": f"Salesforce approval handled: {outcome}.",
                        }
                    ],
                }
            )
        else:
            updated.append(block)
    if not changed:
        return
    try:
        client.chat_update(
            channel=channel,
            ts=message_ts,
            text=str(message.get("text") or "Salesforce approval update"),
            blocks=updated,
        )
    except Exception:
        # Slack delivery can be retried independently; Salesforce audit state is durable.
        return


def _audit(
    conn: sqlite3.Connection,
    body: dict[str, Any],
    action_id: str,
    outcome: str,
    reason: str = "",
) -> None:
    """Write one secret-free action-attempt audit row."""
    from .grant import _interaction_thread_ts, _workspace_id

    campaigns.record_approval_attempt(
        conn,
        action_id=action_id,
        actor=str((body.get("user") or {}).get("id") or ""),
        workspace=_workspace_id(body),
        channel=str((body.get("channel") or {}).get("id") or ""),
        thread_ts=_interaction_thread_ts(body),
        action_ts=_action_timestamp(body),
        outcome=outcome,
        reason=reason,
    )


def register(app: App) -> None:
    """Register requester-bound Salesforce confirm and cancel handlers."""
    from .grant import (
        _active_human_channel_member,
        _in_configured_channel,
        _interaction_thread_ts,
        _thread_reply,
        _workspace_id,
    )

    @app.action("salesforce_confirm")
    def salesforce_confirm(ack: Ack, body: dict[str, Any], client: WebClient) -> None:
        """Execute or read-only reconcile one immutable Salesforce approval."""
        ack()
        user_id = str((body.get("user") or {}).get("id") or "")
        channel = str((body.get("channel") or {}).get("id") or "")
        try:
            value = json.loads(str(body["actions"][0]["value"]))
            action_id = str(value["action_id"])
            nonce = str(value["nonce"])
        except (KeyError, TypeError, json.JSONDecodeError):
            _thread_reply(
                client, body, "Salesforce approval data was malformed; nothing changed."
            )
            return
        conn = db.connect()
        if not _in_configured_channel({"channel": channel}):
            _audit(conn, body, action_id, "rejected", "unconfigured channel")
            _thread_reply(
                client,
                body,
                "Salesforce was not changed because this is not the Grant channel.",
            )
            return
        if not _active_human_channel_member(client, user_id, channel):
            _audit(conn, body, action_id, "rejected", "inactive or nonmember actor")
            _thread_reply(
                client,
                body,
                "I couldn't verify you as an active member of this Grant channel, "
                "so Salesforce was not changed.",
            )
            return
        workspace = _workspace_id(body)
        thread_ts = _interaction_thread_ts(body)
        replay = False
        try:
            result = campaigns.confirm_action(
                conn,
                campaigns.SalesforceCampaignGateway(),
                action_id,
                nonce,
                workspace,
                channel,
                thread_ts,
                user_id,
            )
        except TimeoutError:
            _audit(conn, body, action_id, "expired", "approval preview expired")
            _terminalize_action(client, body, action_id, "expired")
            _thread_reply(
                client,
                body,
                "Salesforce was not changed - this approval preview expired. "
                "Ask me again and I'll rebuild it with current data.",
            )
            return
        except PermissionError as exc:
            _audit(conn, body, action_id, "rejected", str(exc))
            _thread_reply(client, body, f"Salesforce was not changed: {str(exc)}")
            return
        except ValueError:
            try:
                result = campaigns.stored_action_result(
                    conn, action_id, workspace, channel, thread_ts, user_id
                )
                replay = True
            except (PermissionError, ValueError) as exc:
                _audit(conn, body, action_id, "rejected", str(exc))
                _thread_reply(client, body, f"Salesforce was not changed: {str(exc)}")
                return
        if result.added > 0:
            added_rows = conn.execute(
                """SELECT lead_id FROM crm_action_items
                   WHERE action_id=? AND state='added'
                     AND verification_state='verified' AND lead_id IS NOT NULL""",
                (action_id,),
            ).fetchall()
            for item in added_rows:
                lead_id = int(item["lead_id"])
                db.record_outcome(
                    conn,
                    lead_id,
                    None,
                    user_id,
                    "campaign_added",
                    f"salesforce-action:{action_id}:{lead_id}",
                )
        outcome = "replayed" if replay else result.state.value
        _audit(conn, body, action_id, outcome)
        _terminalize_action(client, body, action_id, outcome)
        _thread_reply(client, body, result.message)

    @app.action("salesforce_cancel")
    def salesforce_cancel(ack: Ack, body: dict[str, Any], client: WebClient) -> None:
        """Cancel a ready Salesforce preview for its initiating user."""
        ack()
        action_id = str(body["actions"][0]["value"])
        user_id = str((body.get("user") or {}).get("id") or "")
        channel = str((body.get("channel") or {}).get("id") or "")
        conn = db.connect()
        if not _in_configured_channel({"channel": channel}):
            _audit(conn, body, action_id, "cancel_rejected", "unconfigured channel")
            _thread_reply(
                client,
                body,
                "Nothing was changed because this is not the Grant channel.",
            )
            return
        if campaigns.cancel_action(conn, action_id, user_id):
            _audit(conn, body, action_id, "cancelled")
            _terminalize_action(client, body, action_id, "cancelled")
            _thread_reply(client, body, "Cancelled — Salesforce was not changed.")
        else:
            _audit(conn, body, action_id, "cancel_rejected", "wrong user or state")
            _thread_reply(
                client,
                body,
                "That preview was already handled or belongs to another user; "
                "Salesforce was not changed by this click.",
            )
