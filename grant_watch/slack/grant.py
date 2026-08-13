"""Grant — proactive-thread conversations and @mentions in one Slack channel.

Run it (long-lived process; needs SLACK_BOT_TOKEN + SLACK_APP_TOKEN in .env):
    python -m grant_watch.slack.grant

Conversation rules (Chase, 2026-07-13): reps talk to Grant in THREADS under its
posts — no @ needed there; @Grant works too and routes to the same brain. Messages
mentioning @Persequor are ignored (that's their conversation). Friendly always; no
inline backticks anywhere (Slack renders them red, and red text is banned).

There are no slash commands, menus, or DMs. Legacy alerts use threads; the feature-off
rich campaign also registers its explicit draft/feedback buttons here.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
from collections.abc import Callable
from typing import Any, Protocol  # Slack Bolt event/view payloads are runtime-shaped.
from weakref import WeakValueDictionary

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient

from .. import db
from ..config import configured_channel_ids
from ..spreadsheets import GeneratedArtifact
from . import nudge_threads
from .approval_blocks import _crm_action_blocks
from .nudge_silence import NON_HUMAN_SUBTYPES


class SlackFileClient(Protocol):
    """Narrow Slack client surface needed to upload generated artifacts."""

    def files_upload_v2(self, **kwargs: object) -> object:
        """Upload one file to a channel or thread."""
        ...


# Per-thread locks serialize turns; persisted receipts make restarts/redelivery safe.
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
    """Allow conversations only in Grant's explicitly configured channel(s).

    `SLACK_CHANNEL_ID` may list several channels (e.g. production plus the dev
    playground); a mention in ANY of them is honored, but never a DM."""
    allowed = set(configured_channel_ids())
    item = event.get("item") or {}
    channel = event.get("channel") or item.get("channel")
    return bool(allowed and channel in allowed and event.get("channel_type") != "im")


def create_app() -> App:
    """Build the Bolt app and register every handler. Split from main() so tests can
    construct the app without opening a socket."""
    app = App(token=os.environ["SLACK_BOT_TOKEN"])

    # ------------------------------------------------------ Salesforce approvals
    from . import salesforce_actions

    salesforce_actions.register(app)

    from . import proactive_actions

    proactive_actions.register(app)

    # ---------------------------------------------------------------- conversation
    bot_user_id: str = app.client.auth_test()["user_id"]

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
            or str(event.get("subtype") or "") in NON_HUMAN_SUBTYPES
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
                        from_app=bool(event.get("app_id")),
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
        """Handle plain replies only under Grant's configured-channel alerts.

        THE SUBTYPE RULE IS A DENY LIST, NOT "HAS A SUBTYPE". Rejecting every message
        carrying a subtype silently drops three ordinary human replies — `file_share`
        (a screenshot, or the spreadsheet Grant asked for), `thread_broadcast` (the
        "also send to channel" checkbox) and `me_message`. `nudge_silence._is_human`
        was corrected on 2026-08-11 for exactly this, and CLAUDE.md recorded the cause
        as "the check inherited the listener's blind spot" — but the LISTENER kept the
        blind spot, so a rep ticking "also send to channel" got silence from Grant,
        with no error and no receipt row to explain it.
        """
        if (
            event.get("bot_id")
            or event.get("app_id")
            or str(event.get("subtype") or "") in NON_HUMAN_SUBTYPES
            or not str(event.get("user") or "")
        ):
            return
        if not _in_configured_channel(event):
            return
        text = event.get("text") or ""
        if f"<@{bot_user_id}>" in text:
            return  # the app_mention handler owns this one — no double replies
        if re.search(r"<@[^>]+>", text):
            # Any OTHER @mention — a different agent like @Persequor, or a teammate —
            # means this message is addressed to someone else, so Grant stays out of it
            # (Chase's rule). Grant's own mention already returned just above, so a plain
            # follow-up (no @mention) is the only thing Grant continues a thread on.
            return
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
            # A THREAD GRANT ITSELF OPENED COUNTS. Top-level follow-ups
            # (`CHANNEL_POST_KINDS`) create a new root with neither a `posts` row nor a
            # conversation row, so answering Grant's own "Want me to find a contact?"
            # was discarded right here — above `claim_slack_event`, which is why it
            # left no receipt and no error to find. Registering the thread on first
            # reply means the rest of this handler, and every later turn, treats it
            # like any other conversation.
            if not nudge_threads.is_nudge_thread(
                conn, str(event["channel"]), str(thread_ts)
            ):
                return
            db.register_conversation_thread(
                conn,
                workspace,
                str(event["channel"]),
                str(thread_ts),
                str(event["user"]),
            )
            general_thread = True
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
                        from_app=bool(event.get("app_id")),
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
    frozen = None
    if post["snapshot_id"]:
        from ..campaign import snapshot as rich_snapshot

        frozen = rich_snapshot.load(conn, str(post["snapshot_id"]))
        row = rich_snapshot.lead_context(frozen) if frozen is not None else None
    else:
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

    if intent == "draft_email" and frozen is not None:
        from ..campaign import actions

        try:
            action_result = actions.request_draft(
                conn,
                frozen.id,
                workspace=workspace,
                channel=event["channel"],
                thread_ts=post["ts"],
                requester=user,
                requester_is_member=_active_human_channel_member(
                    client, user, event["channel"]
                ),
                nonce=str(event.get("ts") or event.get("event_ts") or ""),
            )
            reply = action_result.message
        except (PermissionError, ValueError) as exc:
            reply = f"No draft was requested: {str(exc)}."
    elif intent == "draft_email" and row is not None:
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
    elif intent == "bad_lead" and frozen is not None:
        from ..campaign import actions

        try:
            action_result = actions.mark_not_relevant(
                conn,
                frozen.id,
                workspace=workspace,
                channel=event["channel"],
                thread_ts=post["ts"],
                requester=user,
                requester_is_member=_active_human_channel_member(
                    client, user, event["channel"]
                ),
                nonce=str(event.get("ts") or event.get("event_ts") or ""),
            )
            reply = action_result.message
        except (PermissionError, ValueError) as exc:
            reply = f"Nothing was changed: {str(exc)}."
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
    outcome = status.finalize(
        _with_upload_warning(reply, failures), _crm_action_blocks(pending_actions)
    )
    # NOT from an app-authored message. The message handler filters both `bot_id`
    # and `app_id`; this one filters `bot_id`, `subtype` and empty `user` — but not
    # `app_id`, so a message sent through the Claude app @-mentioning Grant reaches
    # here. That is not hypothetical: the turn that died mid-flight today was exactly
    # such a message. Capture would then write a "memory" about a colleague whose
    # verbatim evidence is an app's words, in the one table whose entire safety story
    # is "only what a person actually said".
    if not event.get("app_id"):
        _remember_from(conn, user, text, event["channel"], str(post["ts"]))
    return outcome


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
    from_app: bool = False,
) -> bool:
    """Answer a configured-channel @mention with tools and a visible status update.

    `from_app` exists because this function cannot see the Slack event. The guard that
    keeps app-authored messages out of `user_memory` was first added at the OTHER call
    site — the drip-thread one — which is the minor path. This is the ordinary @mention
    route, and it was still unguarded, so the fix had landed where it did not matter.
    """
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
        # WHAT GRANT OFFERED, STATED AS A FACT, BEFORE THE MODEL CLASSIFIES ANYTHING.
        #
        # Grant's first proactive follow-up asked Kerry "I can now — want me to send
        # it?" and she replied "Yes". The model read that as `draft_email` — prospect
        # outreach — because the sentence Grant had quoted back to her CONTAINS an
        # email address, and a bare "Yes" carries no words of its own to correct it.
        #
        # My first attempt intercepted the misclassification AFTER the fact and sent
        # the email itself. That was worse: it called `email_results` with no search
        # spec, which renders empty, which would have mailed her "I couldn't find
        # anything matching that." A confident false negative in her inbox is worse
        # than the wrong question in Slack.
        #
        # So the fix belongs upstream. The model is missing one fact — what was just
        # offered — and the honest place to get it is the ledger of what was actually
        # delivered. Given that fact it routes correctly and builds a real spec from
        # the thread, which is the thing it is good at and the intercept was not.
        context = _with_pending_offer(context, channel, thread_ts)
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
            # ONE connection for this whole branch. Opening a second one here was
            # caught immediately by the conftest guard that fails any test touching
            # the real database — the same guard that has now caught three of these.
            outreach_conn = db.connect()
            lead_id = _single_lead_id(text, context)
            row = db.get_lead(outreach_conn, lead_id) if lead_id is not None else None
            if row is None:
                reply = (
                    "Tell me the exact Lead number you want to use. I won't guess "
                    "between organizations, and no outreach request was sent."
                )
            else:
                reply = _request_outreach(
                    outreach_conn,
                    row,
                    user,
                    status,
                    channel,
                    thread_ts or "",
                    request_token,
                )
        outcome = status.finalize(
            _with_upload_warning(reply, failures),
            _crm_action_blocks(out.get("pending_crm_actions", [])),
        )
        # An app-authored message must not become a "memory" about a colleague. The
        # caller knows; this function cannot, so it is told.
        if not from_app:
            _remember_from(db.connect(), user, text, channel, thread_ts or "")
        return outcome
    except Exception:
        return status.finalize(_fallback_answer(text))


def _with_pending_offer(
    context: list[str] | None, channel: str, thread_ts: str
) -> list[str]:
    """Prepend what Grant offered in this thread, when it is still awaiting an answer.

    Best-effort and silent: if the ledger cannot be read, the conversation proceeds
    exactly as it did before. A missing hint costs a worse answer; a raised exception
    would cost the whole turn.
    """
    items = list(context or [])
    if not thread_ts:
        return items
    try:
        from .. import db as _db
        from . import nudges as _nudges

        conn = _db.connect_readonly()
        try:
            offered = _nudges.pending_capability_offer(conn, channel, str(thread_ts))
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — a hint is never worth a failed turn
        return items
    if not offered:
        return items
    return [
        "SYSTEM FACT: Grant proactively offered this person the "
        f"'{offered}' capability in this thread and is waiting on their answer. "
        "If they are agreeing, do that thing for THEM — it is not a request to "
        "contact a prospect.",
        *items,
    ]


def _remember_from(
    conn: sqlite3.Connection, user: str, said: str, channel: str, thread_ts: str
) -> None:
    """Notice anything durable in what a colleague just said.

    Called AFTER the reply is on screen, deliberately. Capture is worth roughly a
    second on a long message and nothing on a short one, and none of that should sit
    between a person and their answer. Silent and best-effort: if it fails, Grant is
    forgetful, which is exactly what it was before.
    """
    try:
        from anthropic import Anthropic

        from .. import user_memory

        client = Anthropic()

        def ask_model(prompt: str) -> str:
            """One cheap pass over a single message."""
            reply = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=400,
                messages=[{"role": "user", "content": prompt}],
            )
            return "".join(
                b.text for b in reply.content if getattr(b, "type", "") == "text"
            )

        # Uses the connection it is GIVEN and closes nothing. An earlier version
        # opened its own and closed it, which severed the handler's connection
        # wherever `db.connect` hands back a shared handle — caught by two event-path
        # tests that drive the real handler rather than the helper.
        #
        # Callers therefore own the lifetime. `_handle_drip_thread` passes the
        # handler's own connection; `_converse_general` has none in scope and opens
        # one for this call, matching the two adjacent `db.connect()` calls already
        # there rather than inventing a third pattern.
        user_memory.capture(
            conn,
            slack_user=user,
            said=said,
            ask_model=ask_model,
            audience=channel,
            thread_ts=thread_ts,
        )
    except Exception:  # noqa: BLE001 — memory is an enhancement, never a dependency
        return


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


# The orphan-spinner sweep used to live here. It scanned `primary_channel_id()`'s
# last 50 messages at boot, and it is gone because both bounds were wrong in the
# same way: Chase's question died in the PLAYGROUND, so the one channel it looked at
# was the wrong one, and in a channel shared with another project's bot 50 messages
# covers about a day. `slack.watchdog` replaces it by starting from the receipt row
# that records the death — which names the exact channel and thread, works for DMs,
# needs no pagination, and also clears the stale `processing` row the old sweep left
# behind.


def main() -> None:
    """Start the Socket Mode listener (blocks forever; Ctrl-C to stop)."""
    load_dotenv()
    if not configured_channel_ids():
        raise RuntimeError(
            "SLACK_CHANNEL_ID must name at least one channel "
            "(comma-separated to serve several, e.g. production plus playground)"
        )
    app = create_app()
    # NO WATCHDOG PASS HERE, deliberately. Running it at boot cost two properties
    # that are worth more than the ~10 minutes it saved, and the guardian caught both
    # on the deployed bytes:
    #
    #   1. It needed a writable `db.connect()`, which IS the migration runner — so a
    #      plain restart silently applied migrations. The deploy protocol is built on
    #      restarts being inert; migrations are applied deliberately, with the bot
    #      down and `schema_migrations` checked afterwards.
    #   2. It ran with `dry_run=False`, which made restarting the process a
    #      message-mutating act. Editing someone's thread should be something a job
    #      does on a schedule, never a side effect of `systemctl restart`.
    #
    # The cron tick (every 10 minutes, 24/7) already covers the restart case, which
    # is the common one — it just resolves the spinner a few minutes later instead of
    # instantly. That is the right trade.
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    print("Grant is listening (Socket Mode)…")
    handler.start()


if __name__ == "__main__":
    main()
