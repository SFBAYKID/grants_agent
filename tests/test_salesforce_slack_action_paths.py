"""Registered-handler tests for Salesforce Campaign confirm/cancel button paths.

These drive the ACTUAL Bolt callbacks registered by `salesforce_actions.register`,
not the helpers underneath them. The refusal branches (expired preview, wrong
channel, inactive actor, malformed payload, wrong approver) previously had no
coverage through the registered callback, so the audit/terminalize/reply wiring
was unproven. Every test also asserts that no Firecrawl-style HTTP and no
Salesforce authentication can occur on a refused click.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from grant_watch import db
from grant_watch.enrich import salesforce_campaign_gateway as gateway_module
from grant_watch.enrich import salesforce_campaigns as campaigns
from grant_watch.slack import grant

WORKSPACE = "TWORK"
CHANNEL = "CGRANTS"
THREAD_TS = "900.100"
REQUESTER = "UREP"
OTHER_USER = "UOTHER"
NONCE = "one-time-nonce"
PAST = "2026-07-01T00:00:00+00:00"
FUTURE = "2099-01-01T00:00:00+00:00"


class NoNetwork:
    """Stand-in for `requests` that fails loudly if any HTTP is attempted."""

    def post(self, *_args: Any, **_kwargs: Any) -> None:
        """Refuse every outbound POST during a refused approval click."""
        raise AssertionError("no Salesforce HTTP may occur on a refused click")

    def get(self, *_args: Any, **_kwargs: Any) -> None:
        """Refuse every outbound GET during a refused approval click."""
        raise AssertionError("no Salesforce HTTP may occur on a refused click")


class FakeActionClient:
    """In-memory Slack surface capturing replies, edits, and membership answers."""

    def __init__(self, *, member: bool = True, deleted: bool = False) -> None:
        """Configure the actor's membership and deactivation state."""
        self.updates: list[dict[str, Any]] = []
        self.replies: list[dict[str, Any]] = []
        self._member = member
        self._deleted = deleted

    def auth_test(self) -> dict[str, str]:
        """Return Grant's bot identity for handler registration."""
        return {"user_id": "UGRANT"}

    def users_info(self, user: str) -> dict[str, Any]:
        """Report whether the clicking actor is an active human."""
        assert user
        return {
            "user": {"deleted": self._deleted, "is_bot": False, "is_app_user": False}
        }

    def conversations_members(self, **kwargs: Any) -> dict[str, Any]:
        """Return one unpaginated membership page for the configured channel."""
        assert kwargs.get("channel")
        members = [REQUESTER, OTHER_USER] if self._member else []
        return {"members": members, "response_metadata": {"next_cursor": ""}}

    def chat_update(self, **kwargs: Any) -> dict[str, bool]:
        """Capture one terminalizing message edit."""
        self.updates.append(kwargs)
        return {"ok": True}

    def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:
        """Capture one threaded reply."""
        self.replies.append(kwargs)
        return {"ts": "901.200"}


class FakeBoltApp:
    """Capture Bolt decorators so tests can invoke the registered handlers."""

    latest: "FakeBoltApp | None" = None

    def __init__(self, token: str) -> None:
        """Create one app with a fake Slack client and handler registries."""
        assert token == "xoxb-test"
        self.client = FakeActionClient()
        self.events: dict[str, Callable[..., None]] = {}
        self.actions: dict[str, Callable[..., None]] = {}
        FakeBoltApp.latest = self

    def event(self, name: str) -> Callable[[Callable[..., None]], Callable[..., None]]:
        """Register one event callback exactly as Bolt's decorator does."""

        def register(handler: Callable[..., None]) -> Callable[..., None]:
            """Store and return the decorated callback."""
            self.events[name] = handler
            return handler

        return register

    def action(self, name: str) -> Callable[[Callable[..., None]], Callable[..., None]]:
        """Register one interactive-action callback."""

        def register(handler: Callable[..., None]) -> Callable[..., None]:
            """Store and return the decorated callback."""
            self.actions[name] = handler
            return handler

        return register


def _store_ready_action(conn: Any, action_id: str, expires_at: str) -> None:
    """Insert one immutable ready create_campaign preview with a chosen expiry."""
    payload_json = json.dumps(
        {"campaign": {"Name": "Edge Case Campaign"}}, separators=(",", ":")
    )
    now = "2026-07-20T00:00:00+00:00"
    with conn:
        conn.execute(
            """INSERT INTO crm_actions
                 (id,action_type,workspace,channel,thread_ts,requested_by,state,
                  payload_json,payload_hash,items_hash,nonce_hash,expires_at,
                  campaign_id,created_at,updated_at)
               VALUES (?,?,?,?,?,?,'ready',?,?,?,?,?,NULL,?,?)""",
            (
                action_id,
                "create_campaign",
                WORKSPACE,
                CHANNEL,
                THREAD_TS,
                REQUESTER,
                payload_json,
                campaigns._hash(payload_json),
                campaigns._hash(campaigns._stable_json([])),
                campaigns._hash(NONCE),
                expires_at,
                now,
                now,
            ),
        )


def _click(
    action_id: str, *, user: str, channel: str, value: str | None = None
) -> dict:
    """Build one realistic Slack interactive-button envelope."""
    button = json.dumps({"action_id": action_id, "nonce": NONCE}, separators=(",", ":"))
    return {
        "team_id": WORKSPACE,
        "user": {"id": user},
        "channel": {"id": channel},
        "container": {"thread_ts": THREAD_TS},
        "action_ts": "901.500",
        "message": {
            "ts": THREAD_TS,
            "text": "Campaign approval",
            "blocks": [
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "action_id": "salesforce_confirm",
                            "value": button,
                        },
                        {
                            "type": "button",
                            "action_id": "salesforce_cancel",
                            "value": action_id,
                        },
                    ],
                }
            ],
        },
        "actions": [
            {
                "action_id": "salesforce_confirm",
                "value": button if value is None else value,
            }
        ],
    }


def _state(conn: Any, action_id: str) -> str:
    """Return the stored state for one action."""
    row = conn.execute(
        "SELECT state FROM crm_actions WHERE id=?", (action_id,)
    ).fetchone()
    return str(row["state"])


def _audits(conn: Any) -> list[tuple[str, str]]:
    """Return every recorded approval-attempt outcome and reason."""
    return [
        (str(row["outcome"]), str(row["reason"] or ""))
        for row in conn.execute(
            "SELECT outcome,reason FROM crm_campaign_approval_attempts ORDER BY id"
        )
    ]


@pytest.fixture
def action_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, FakeBoltApp]:
    """Build the real Bolt registration over an isolated migrated database."""
    conn = db.connect(tmp_path / "slack-actions.db")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CHANNEL_ID", CHANNEL)
    monkeypatch.setattr(grant, "App", FakeBoltApp)
    monkeypatch.setattr(db, "connect", lambda *_a, **_k: conn)
    monkeypatch.setattr(gateway_module, "requests", NoNetwork())
    grant.create_app()
    app = FakeBoltApp.latest
    assert app is not None
    return conn, app


def test_expired_preview_click_terminalizes_and_writes_no_salesforce(
    action_app: tuple[Any, FakeBoltApp],
) -> None:
    """A stale ready preview refuses the write, expires durably, and says so."""
    conn, app = action_app
    _store_ready_action(conn, "action-expired", PAST)
    app.actions["salesforce_confirm"](
        ack=lambda: None,
        body=_click("action-expired", user=REQUESTER, channel=CHANNEL),
        client=app.client,
    )
    assert _state(conn, "action-expired") == "expired"
    assert ("expired", "approval preview expired") in _audits(conn)
    assert "expired" in app.client.replies[-1]["text"]
    context = app.client.updates[-1]["blocks"][0]
    assert context["type"] == "context"
    assert "expired" in context["elements"][0]["text"]


def test_click_outside_the_configured_channel_changes_nothing(
    action_app: tuple[Any, FakeBoltApp],
) -> None:
    """A button replayed in another channel is refused before any Salesforce work."""
    conn, app = action_app
    _store_ready_action(conn, "action-channel", FUTURE)
    app.actions["salesforce_confirm"](
        ack=lambda: None,
        body=_click("action-channel", user=REQUESTER, channel="CELSEWHERE"),
        client=app.client,
    )
    assert _state(conn, "action-channel") == "ready"
    assert ("rejected", "unconfigured channel") in _audits(conn)
    assert "not the Grant channel" in app.client.replies[-1]["text"]
    assert app.client.updates == []


def test_deactivated_actor_cannot_confirm(
    action_app: tuple[Any, FakeBoltApp], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deactivated Slack account cannot approve a Salesforce write."""
    conn, app = action_app
    monkeypatch.setattr(app.client, "_deleted", True, raising=False)
    _store_ready_action(conn, "action-actor", FUTURE)
    app.actions["salesforce_confirm"](
        ack=lambda: None,
        body=_click("action-actor", user=REQUESTER, channel=CHANNEL),
        client=app.client,
    )
    assert _state(conn, "action-actor") == "ready"
    assert ("rejected", "inactive or nonmember actor") in _audits(conn)
    assert "active member" in app.client.replies[-1]["text"]


def test_malformed_button_payload_changes_nothing(
    action_app: tuple[Any, FakeBoltApp],
) -> None:
    """A corrupted button value is reported without touching state or audit."""
    conn, app = action_app
    _store_ready_action(conn, "action-malformed", FUTURE)
    app.actions["salesforce_confirm"](
        ack=lambda: None,
        body=_click(
            "action-malformed", user=REQUESTER, channel=CHANNEL, value="not-json"
        ),
        client=app.client,
    )
    assert _state(conn, "action-malformed") == "ready"
    assert _audits(conn) == []
    assert "malformed" in app.client.replies[-1]["text"]


def test_a_second_user_cannot_approve_another_reps_preview(
    action_app: tuple[Any, FakeBoltApp],
) -> None:
    """Requester binding survives at click time for an active channel member."""
    conn, app = action_app
    _store_ready_action(conn, "action-owner", FUTURE)
    app.actions["salesforce_confirm"](
        ack=lambda: None,
        body=_click("action-owner", user=OTHER_USER, channel=CHANNEL),
        client=app.client,
    )
    assert _state(conn, "action-owner") == "ready"
    outcomes = _audits(conn)
    assert any(
        outcome == "rejected" and "initiating user" in reason
        for outcome, reason in outcomes
    )
    assert "initiating user" in app.client.replies[-1]["text"]


def test_cancel_by_another_user_is_refused(
    action_app: tuple[Any, FakeBoltApp],
) -> None:
    """Only the initiating rep may cancel; a stranger's click changes nothing."""
    conn, app = action_app
    _store_ready_action(conn, "action-cancel", FUTURE)
    body = _click("action-cancel", user=OTHER_USER, channel=CHANNEL)
    body["actions"] = [{"action_id": "salesforce_cancel", "value": "action-cancel"}]
    app.actions["salesforce_cancel"](
        ack=lambda: None,
        body=body,
        client=app.client,
    )
    assert _state(conn, "action-cancel") == "ready"
    assert ("cancel_rejected", "wrong user or state") in _audits(conn)
    assert "another user" in app.client.replies[-1]["text"]
