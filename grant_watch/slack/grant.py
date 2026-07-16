"""Grant — proactive-thread conversations and @mentions in one Slack channel.

Run it (long-lived process; needs SLACK_BOT_TOKEN + SLACK_APP_TOKEN in .env):
    python -m grant_watch.slack.grant

Conversation rules (Chase, 2026-07-13): reps talk to Grant in THREADS under its
posts — no @ needed there; @Grant works too and routes to the same brain. Messages
mentioning @Persequor are ignored (that's their conversation). Friendly always; no
inline backticks anywhere (Slack renders them red, and red text is banned).

There are no slash commands, menus, DMs, or buttons on initial alerts. Humans use
natural language after @Grant or in the thread under Grant's proactive message.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from collections.abc import Callable
from typing import Any, Protocol  # Slack Bolt event/view payloads are runtime-shaped.
from weakref import WeakValueDictionary

from dotenv import load_dotenv
from slack_bolt import Ack, App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient

from .. import db
from ..spreadsheets import GeneratedArtifact


class SlackFileClient(Protocol):
    """Narrow Slack client surface needed to upload generated artifacts."""

    def files_upload_v2(self, **kwargs: object) -> object:
        """Upload one file to a channel or thread."""
        ...


# Per-thread locks serialize long turns without dropping the second human message.
# Slack event identity itself is persisted in ``slack_event_receipts`` so restarts and
# redelivery cannot duplicate tool calls or external actions.
_dedup_lock = threading.Lock()
_thread_locks: WeakValueDictionary[str, threading.Lock] = WeakValueDictionary()


def _thread_lock(thread_key: str) -> threading.Lock:
    """Return a shared lock for one Slack thread, creating it race-safely."""
    with _dedup_lock:
        lock = _thread_locks.get(thread_key)
        if lock is None:
            lock = threading.Lock()
            _thread_locks[thread_key] = lock
        return lock


def _workspace_id(body: dict[str, Any], event: dict[str, Any] | None = None) -> str:
    """Extract a Slack workspace ID across Events and Interactivity envelopes."""
    team = body.get("team") or {}
    return str(body.get("team_id") or team.get("id") or (event or {}).get("team") or "")


def _active_human_channel_member(client: WebClient, user_id: str, channel: str) -> bool:
    """Recheck active human identity and configured-channel membership at commit."""
    try:
        user = client.users_info(user=user_id).get("user") or {}
        if user.get("deleted") or user.get("is_bot") or user.get("is_app_user"):
            return False
        cursor = ""
        while True:
            response = client.conversations_members(
                channel=channel, limit=200, cursor=cursor or None
            )
            if user_id in response.get("members", []):
                return True
            cursor = str(
                (response.get("response_metadata") or {}).get("next_cursor") or ""
            )
            if not cursor:
                return False
    except Exception:
        return False


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


def _interaction_thread_ts(body: dict[str, Any]) -> str:
    """Return the immutable Slack thread root for an interactive button payload."""
    message = body.get("message") or {}
    container = body.get("container") or {}
    return str(
        container.get("thread_ts")
        or message.get("thread_ts")
        or message.get("ts")
        or ""
    )


def _in_configured_channel(event: dict[str, Any]) -> bool:
    """Allow conversations only in Grant's explicitly configured test channel."""
    configured = os.environ.get("SLACK_CHANNEL_ID", "").strip()
    item = event.get("item") or {}
    channel = event.get("channel") or item.get("channel")
    return bool(
        configured and channel == configured and event.get("channel_type") != "im"
    )


def create_app() -> App:
    """Build the Bolt app and register every handler. Split from main() so tests can
    construct the app without opening a socket."""
    app = App(token=os.environ["SLACK_BOT_TOKEN"])

    # ------------------------------------------------------ Salesforce approvals
    @app.action("salesforce_confirm")
    def salesforce_confirm(ack: Ack, body: dict[str, Any], client: WebClient) -> None:
        """Execute one requester-bound, immutable Salesforce create preview."""
        ack()
        from ..enrich import salesforce_campaigns as campaigns

        user_id = str((body.get("user") or {}).get("id") or "")
        channel = str((body.get("channel") or {}).get("id") or "")
        if not _in_configured_channel({"channel": channel}):
            _thread_reply(
                client,
                body,
                "Salesforce was not changed because this is not the Grant channel.",
            )
            return
        workspace = _workspace_id(body)
        thread_ts = _interaction_thread_ts(body)
        try:
            value = json.loads(str(body["actions"][0]["value"]))
            action_id = str(value["action_id"])
            nonce = str(value["nonce"])
        except (KeyError, TypeError, json.JSONDecodeError):
            _thread_reply(
                client, body, "Salesforce approval data was malformed; nothing changed."
            )
            return
        if not _active_human_channel_member(client, user_id, channel):
            _thread_reply(
                client,
                body,
                "I couldn't verify you as an active member of this Grant channel, "
                "so Salesforce was not changed.",
            )
            return
        conn = db.connect()
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
        except (PermissionError, TimeoutError) as exc:
            _thread_reply(client, body, f"Salesforce was not changed: {str(exc)}")
            return
        except ValueError:
            try:
                result = campaigns.stored_action_result(
                    conn, action_id, workspace, channel, thread_ts, user_id
                )
            except (PermissionError, ValueError) as exc:
                _thread_reply(client, body, f"Salesforce was not changed: {str(exc)}")
                return
        if result.added > 0:
            added_rows = conn.execute(
                """SELECT lead_id FROM crm_action_items
                   WHERE action_id=? AND state='added' AND lead_id IS NOT NULL""",
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
        _thread_reply(client, body, result.message)

    @app.action("salesforce_cancel")
    def salesforce_cancel(ack: Ack, body: dict[str, Any], client: WebClient) -> None:
        """Cancel a ready Salesforce preview for its initiating user."""
        ack()
        from ..enrich import salesforce_campaigns as campaigns

        action_id = str(body["actions"][0]["value"])
        user_id = str((body.get("user") or {}).get("id") or "")
        channel = str((body.get("channel") or {}).get("id") or "")
        if not _in_configured_channel({"channel": channel}):
            _thread_reply(
                client,
                body,
                "Nothing was changed because this is not the Grant channel.",
            )
            return
        if campaigns.cancel_action(db.connect(), action_id, user_id):
            _thread_reply(client, body, "Cancelled — Salesforce was not changed.")
        else:
            _thread_reply(
                client,
                body,
                "That preview was already handled or belongs to another user; "
                "Salesforce was not changed by this click.",
            )

    # ---------------------------------------------------------------- conversation
    bot_user_id: str = app.client.auth_test()["user_id"]
    persequor_id: str = os.environ.get("PERSEQUOR_USER_ID", "")

    @app.event("app_mention")
    def on_mention(
        event: dict[str, Any],
        body: dict[str, Any],
        say: Callable[..., object],
        client: WebClient,
    ) -> None:
        """Handle @Grant only in the configured channel; ignore every other venue."""
        if (
            not _in_configured_channel(event)
            or event.get("bot_id")
            or event.get("subtype")
            or not str(event.get("user") or "")
        ):
            return
        text = re.sub(r"<@[^>]+>", "", event.get("text") or "").strip()
        thread_ts = event.get("thread_ts")
        thread_key = f"{event['channel']}:{thread_ts or event['ts']}"
        event_id = str(body.get("event_id", ""))
        workspace = _workspace_id(body, event)
        conn = db.connect()
        if not db.claim_slack_event(
            conn,
            event_id,
            workspace,
            str(event["channel"]),
            str(thread_ts or event["ts"]),
            str(event.get("user") or ""),
        ):
            return
        try:
            delivered = True
            with _thread_lock(thread_key):
                post = db.find_post_by_ts(conn, event["channel"], thread_ts or "")
                if post is not None:
                    delivered = _handle_drip_thread(
                        conn, post, event, say, client, workspace=workspace
                    )
                else:
                    db.register_conversation_thread(
                        conn,
                        workspace,
                        str(event["channel"]),
                        str(thread_ts or event["ts"]),
                        str(event["user"]),
                    )
                    delivered = _converse_general(
                        text,
                        client,
                        event["channel"],
                        event.get("thread_ts") or event["ts"],
                        user=event.get("user", ""),
                        workspace=workspace,
                        request_token=str(
                            event.get("ts") or event.get("event_ts") or ""
                        ),
                    )
        except Exception as exc:
            db.finish_slack_event(
                conn,
                event_id,
                error=f"{type(exc).__name__}: {str(exc)[:300]}",
                action_state="unknown",
                delivery_state="unknown",
            )
            return
        if delivered:
            db.finish_slack_event(conn, event_id)
        else:
            db.finish_slack_event(
                conn,
                event_id,
                error="final Slack response was not confirmed",
                action_state="complete",
                delivery_state="failed",
            )

    @app.event("message")
    def on_message(
        event: dict[str, Any],
        body: dict[str, Any],
        say: Callable[..., object],
        client: WebClient,
    ) -> None:
        """Handle plain replies only under Grant's configured-channel alerts."""
        if (
            event.get("bot_id")
            or event.get("app_id")
            or event.get("subtype")
            or not str(event.get("user") or "")
        ):
            return
        if not _in_configured_channel(event):
            return
        text = event.get("text") or ""
        if f"<@{bot_user_id}>" in text:
            return  # the app_mention handler owns this one — no double replies
        if persequor_id and f"<@{persequor_id}>" in text:
            return  # they're talking to Persequor — Grant stays out of it (Chase's rule)
        thread_ts = event.get("thread_ts")
        if not thread_ts or not text.strip():
            return  # top-level channel chatter isn't Grant's business
        thread_key = f"{event['channel']}:{thread_ts or event['ts']}"
        event_id = str(body.get("event_id", ""))
        workspace = _workspace_id(body, event)
        conn = db.connect()
        post = db.find_post_by_ts(conn, event["channel"], thread_ts)
        general_thread = db.is_conversation_thread(
            conn, workspace, str(event["channel"]), str(thread_ts)
        )
        if post is None and not general_thread:
            return
        if not db.claim_slack_event(
            conn,
            event_id,
            workspace,
            str(event["channel"]),
            str(thread_ts or event["ts"]),
            str(event.get("user") or ""),
        ):
            return
        try:
            delivered = True
            with _thread_lock(thread_key):
                if post is not None:
                    delivered = _handle_drip_thread(
                        conn, post, event, say, client, workspace=workspace
                    )
                else:
                    db.touch_conversation_thread(
                        conn, workspace, str(event["channel"]), str(thread_ts)
                    )
                    delivered = _converse_general(
                        text.strip(),
                        client,
                        str(event["channel"]),
                        str(thread_ts),
                        user=str(event["user"]),
                        workspace=workspace,
                        request_token=str(
                            event.get("ts") or event.get("event_ts") or ""
                        ),
                    )
        except Exception as exc:
            db.finish_slack_event(
                conn,
                event_id,
                error=f"{type(exc).__name__}: {str(exc)[:300]}",
                action_state="unknown",
                delivery_state="unknown",
            )
            return
        if delivered:
            db.finish_slack_event(conn, event_id)
        else:
            db.finish_slack_event(
                conn,
                event_id,
                error="final Slack response was not confirmed",
                action_state="complete",
                delivery_state="failed",
            )

    @app.event("reaction_added")
    def on_reaction(event: dict[str, Any]) -> None:
        """A reaction on a drip post is engagement — the cheapest +1 there is."""
        if not _in_configured_channel(event):
            return
        item = event.get("item") or {}
        if item.get("type") != "message":
            return
        conn = db.connect()
        post = db.find_post_by_ts(conn, item.get("channel", ""), item.get("ts", ""))
        if post is not None:
            db.record_engagement(conn, int(post["id"]), event["user"], "reaction")

    return app


class _Status:
    """A single Slack message that shows a rotating spinner + a short (<=6 word) phrase
    while Grant works, then is edited into the final answer — so a rep watches Grant
    think instead of staring at an empty thread (Chase, 2026-07-14). Every Slack call
    is wrapped: a spinner hiccup must never break the turn."""

    _FRAMES = ("/", "—", "\\", "|")

    def __init__(self, client: WebClient, channel: str, thread_ts: str | None) -> None:
        """Initialize one best-effort status message for a Slack turn."""
        self._client = client
        self._channel = channel
        self._thread_ts = thread_ts
        self._i = 0
        self.ts: str | None = None

    def start(self) -> None:
        """Post the initial spinner without allowing a Slack error to abort the turn."""
        try:
            r = self._client.chat_postMessage(
                channel=self._channel, thread_ts=self._thread_ts, text="/ Thinking…"
            )
            self.ts = r["ts"]
        except Exception:
            self.ts = None

    def update(self, phrase: str) -> None:
        """Advance the spinner and display a short progress phrase when available."""
        if not self.ts:
            return
        self._i = (self._i + 1) % len(self._FRAMES)
        try:
            self._client.chat_update(
                channel=self._channel,
                ts=self.ts,
                text=f"{self._FRAMES[self._i]} {phrase}…",
            )
        except Exception:
            pass

    def finalize(
        self, text: str, extra_blocks: list[dict[str, Any]] | None = None
    ) -> bool:
        """Replace the spinner with the final answer (or post it if the spinner died)."""
        blocks = None
        if extra_blocks:
            blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
            blocks.extend(extra_blocks)
        if self.ts:
            try:
                self._client.chat_update(
                    channel=self._channel, ts=self.ts, text=text, blocks=blocks
                )
                return True
            except Exception:
                pass
        try:
            self._client.chat_postMessage(
                channel=self._channel,
                thread_ts=self._thread_ts,
                text=text,
                blocks=blocks,
            )
            return True
        except Exception:
            return False


def _handle_drip_thread(
    conn: sqlite3.Connection,
    post: sqlite3.Row,
    event: dict[str, Any],
    say: Callable[..., object],
    client: WebClient,
    workspace: str = "",
) -> bool:
    """A human spoke in a lead thread: award the point, understand the message,
    act on the intent, answer in the thread (uploading any files Grant produced).
    Any LLM failure degrades to an honest reply — never to a wrong action."""
    from . import conversation  # local import: scheduled poll/drip paths need no LLM

    user = event["user"]
    text = re.sub(r"<@[^>]+>", "", event.get("text") or "").strip()
    db.record_engagement(conn, int(post["id"]), user, "reply")
    row = db.get_lead(conn, int(post["lead_id"])) if post["lead_id"] else None
    context = _thread_history(client, event["channel"], post["ts"])
    status = _Status(client, event["channel"], post["ts"])
    status.start()
    try:
        out = conversation.respond(
            text,
            row,
            thread_context=context,
            on_progress=status.update,
            requester_slack=user,
            workspace=workspace,
            channel=event["channel"],
            thread_ts=post["ts"],
        )
    except Exception as exc:  # API down ≠ silence; reply honestly
        return status.finalize(
            f"I'm having trouble thinking right now ({type(exc).__name__}) "
            f"— give me a minute and try again."
        )
    intent, reply, files = out["intent"], out["reply"], out.get("files", [])
    pending_actions = out.get("pending_crm_actions", [])

    if intent == "draft_email" and row is not None:
        reply = _request_outreach(
            conn,
            row,
            user,
            status,
            event["channel"],
            post["ts"],
            str(event.get("ts") or event.get("event_ts") or ""),
        )
    elif intent == "snooze" and row is not None:
        db.set_lead_status(conn, int(row["id"]), "snoozed")
        db.record_outcome(
            conn,
            int(row["id"]),
            int(post["id"]),
            user,
            "snoozed",
            f"thread:{post['id']}:{event.get('ts', '')}:snoozed",
        )
    elif intent == "bad_lead" and row is not None:
        db.set_lead_status(
            conn, int(row["id"]), "dead", note=f"bad lead per <@{user}>: {text}"
        )
        db.record_outcome(
            conn,
            int(row["id"]),
            int(post["id"]),
            user,
            "bad_lead",
            f"thread:{post['id']}:{event.get('ts', '')}:bad-lead",
        )
    elif intent == "question":
        db.record_engagement(conn, int(post["id"]), user, "question")

    failures = _deliver_artifacts(client, event["channel"], post["ts"], files)
    return status.finalize(
        _with_upload_warning(reply, failures), _crm_action_blocks(pending_actions)
    )


def _request_outreach(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    user: str,
    status: _Status,
    channel: str,
    thread_ts: str,
    request_token: str,
) -> str:
    """The draft_email action: verified contact (enriching on the spot if needed) ->
    outreach-request.v1 brief -> Persequor. Every branch replies truthfully; the
    interim copyable draft remains the fallback while Persequor's endpoint is dark.
    Progress flows through the spinner (status.update), not separate messages."""
    from .. import persequor_client
    from . import persequor as draft_templates

    send_as = persequor_client.rep_email_for(user)
    if send_as is None:
        return (
            "You're not on the rep roster yet, so I can't request outreach under "
            "your name — Chase can add you to config/reps.json."
        )

    contacts = [
        c
        for c in db.contacts_for_lead(conn, int(row["id"]))
        if c["contact_status"] == "verified"
    ]
    contact = contacts[0] if contacts else None
    if contact is None:
        from . import tools as t

        t.find_contact(int(row["id"]), status.update)
        contacts = [
            c
            for c in db.contacts_for_lead(conn, int(row["id"]))
            if c["contact_status"] == "verified"
        ]
        contact = contacts[0] if contacts else None

    request_id = persequor_client.request_id_for(
        row, user, channel, thread_ts, request_token
    )
    brief = persequor_client.build_brief(
        row,
        contact,
        user,
        send_as,
        slack_channel=channel,
        slack_thread_ts=thread_ts,
        request_id=request_id,
    )
    if brief is None:
        return (
            "I couldn't verify a contact for them (nothing I can prove from "
            "their site), and there's no test address configured — so no email "
            "request from me. If you know the right person, tell me here."
        )
    status.update("Sending to Persequor")
    state_, msg = persequor_client.submit_brief(conn, int(row["id"]), brief)
    if state_ == "submitted":
        found = (
            f" Contact on file: {contact['name']} ({contact['title']})."
            if contact is not None
            else ""
        )
        return msg + found
    # Endpoint dark or refused: fall back to the honest copyable draft.
    draft = draft_templates.compose_draft(row)
    return (
        f"{msg}\nMeanwhile, here's a copyable draft so you're not blocked:\n"
        f"```{draft}```"
    )


def _thread_history(client: WebClient, channel: str, thread_ts: str) -> list[str]:
    """Recent thread turns as 'Grant: ...' / 'rep: ...' lines, so the offer→confirm
    flow works (Grant remembers it just offered Persequor). Failure -> no context,
    never a crash."""
    try:
        resp = client.conversations_replies(channel=channel, ts=thread_ts, limit=12)
    except Exception:
        return []
    lines: list[str] = []
    for m in resp.get("messages", []):
        who = "Grant" if m.get("bot_id") or m.get("app_id") else "rep"
        txt = re.sub(r"<@[^>]+>", "", m.get("text") or "").strip()
        if txt:
            lines.append(f"{who}: {txt}")
    return lines[-10:]


def _single_lead_id(text: str, context: list[str]) -> int | None:
    """Resolve one explicit or recently displayed lead without guessing among many."""
    explicit = [int(value) for value in re.findall(r"\blead\s*#\s*(\d+)\b", text, re.I)]
    if explicit:
        return explicit[-1] if len(set(explicit)) == 1 else None
    recent = [
        int(value)
        for line in context[-10:]
        for value in re.findall(r"\blead\s*#\s*(\d+)\b", line, re.I)
    ]
    unique = set(recent)
    return recent[-1] if len(unique) == 1 else None


def _converse_general(
    text: str,
    client: WebClient,
    channel: str,
    thread_ts: str | None,
    user: str = "",
    workspace: str = "",
    request_token: str = "",
) -> bool:
    """Answer a configured-channel @mention with tools and a visible status update."""
    from . import conversation

    if not text.strip():
        # A bare "@Grant" with no ask: greet deterministically (no LLM, no spinner) so
        # the rep always gets the same warm invitation to say what they need.
        try:
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text="Hey! What can I help you with?",
            )
            return True
        except Exception:
            return False

    status = _Status(client, channel, thread_ts)
    status.start()
    try:
        context = _thread_history(client, channel, thread_ts) if thread_ts else []
        out = conversation.respond(
            text,
            None,
            on_progress=status.update,
            thread_context=context or None,
            requester_slack=user,
            workspace=workspace,
            channel=channel,
            thread_ts=thread_ts or "",
        )
        artifacts = out.get("files", [])
        failures = _deliver_artifacts(client, channel, thread_ts, artifacts)
        reply = str(out["reply"])
        if out.get("intent") == "draft_email":
            lead_id = _single_lead_id(text, context)
            row = db.get_lead(db.connect(), lead_id) if lead_id is not None else None
            if row is None:
                reply = (
                    "Tell me the exact Lead number you want to use. I won't guess "
                    "between organizations, and no outreach request was sent."
                )
            else:
                reply = _request_outreach(
                    db.connect(),
                    row,
                    user,
                    status,
                    channel,
                    thread_ts or "",
                    request_token,
                )
        return status.finalize(
            _with_upload_warning(reply, failures),
            _crm_action_blocks(out.get("pending_crm_actions", [])),
        )
    except Exception:
        return status.finalize(_fallback_answer(text))


def _deliver_artifacts(
    client: SlackFileClient,
    channel: str,
    thread_ts: str | None,
    artifacts: list[GeneratedArtifact],
) -> int:
    """Upload every artifact through one path and always release its temp storage."""
    failures = 0
    for artifact in artifacts:
        try:
            kwargs: dict[str, object] = {"channel": channel, "file": str(artifact.path)}
            if thread_ts:
                kwargs["thread_ts"] = thread_ts
            client.files_upload_v2(**kwargs)
        except Exception:
            # Slack retries could duplicate the whole event; contain the upload error and
            # report it in the existing response instead of escaping the handler.
            failures += 1
        finally:
            artifact.cleanup()
    return failures


def _with_upload_warning(reply: str, failures: int) -> str:
    """Append one honest delivery warning when Slack rejected an attachment."""
    if failures == 0:
        return reply
    noun = "file" if failures == 1 else "files"
    return (
        f"{reply}\nI created the {noun}, but Slack could not attach "
        f"{failures} of them. Please try the export again."
    )


def _fallback_answer(query: str) -> str:
    """Give a natural, menu-free fallback when the conversational model is down."""
    if not query.strip():
        return "What would you like me to find?"
    return "I'm having trouble thinking right now. Please try that question again in a minute."


def _thread_reply(
    client: WebClient,
    body: dict[str, Any],
    text: str,
    extra_blocks: list[dict[str, Any]] | None = None,
) -> None:
    """Reply in the thread under the message containing an interactive button."""
    msg = body["message"]
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}] + (
        extra_blocks or []
    )
    client.chat_postMessage(
        channel=body["channel"]["id"],
        thread_ts=msg.get("thread_ts") or msg["ts"],
        text=text,
        blocks=blocks,
    )


def main() -> None:
    """Start the Socket Mode listener (blocks forever; Ctrl-C to stop)."""
    load_dotenv()
    if not os.environ.get("SLACK_CHANNEL_ID", "").strip():
        raise RuntimeError(
            "SLACK_CHANNEL_ID must be the Monarch Bot Playground channel"
        )
    handler = SocketModeHandler(create_app(), os.environ["SLACK_APP_TOKEN"])
    print("Grant is listening (Socket Mode)…")
    handler.start()


if __name__ == "__main__":
    main()
