"""Grant — the Socket Mode bot: drip-thread conversations (LLM + tools), digest
triage buttons, mentions, DMs, and the /grant slash command.

Run it (long-lived process; needs SLACK_BOT_TOKEN + SLACK_APP_TOKEN in .env):
    python -m grant_watch.slack.grant

Conversation rules (Chase, 2026-07-13): reps talk to Grant in THREADS under its
posts — no @ needed there; @Grant works too and routes to the same brain. Messages
mentioning @Persequor are ignored (that's their conversation). Friendly always; no
inline backticks anywhere (Slack renders them red, and red text is banned).

Digest-button flow: [Draft email] posts a copyable draft (the automated Persequor
handoff ships later — docs/workflow_design.md §4); [Mark contacted] / [Snooze] set
status; [Bad lead] opens a modal asking WHY (reason lands in leads.status_note).
"""

from __future__ import annotations

import os
import re
from typing import Any

from dotenv import load_dotenv
from slack_bolt import Ack, App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from .. import db
from . import digest, persequor

# NOTE: no inline backticks anywhere Grant speaks — Slack renders them as red text,
# and red text is banned (Chase's rule, 2026-07-13).
HELP_TEXT = (
    "Hey! I'm *Grant* — I watch government security-funding sources and surface the "
    "best leads here.\n• /grant status — lead counts by source\n"
    "• /grant digest — post the full digest now\n"
    "• Talk to me in any of my lead threads: claim a lead, ask questions, request a "
    "spreadsheet, or ask me to search for news.\n"
    "I never invent contacts or figures — if I don't know, I'll say so."
)


def create_app() -> App:
    """Build the Bolt app and register every handler. Split from main() so tests can
    construct the app without opening a socket."""
    app = App(token=os.environ["SLACK_BOT_TOKEN"])

    # ---------------------------------------------------------------- triage buttons
    @app.action("grant_mark_contacted")
    def mark_contacted(ack: Ack, body: dict[str, Any], client) -> None:
        ack()
        lead_id = int(body["actions"][0]["value"])
        db.set_lead_status(db.connect(), lead_id, "contacted")
        _thread_reply(client, body, f"✅ Marked lead #{lead_id} contacted.")

    @app.action("grant_snooze")
    def snooze(ack: Ack, body: dict[str, Any], client) -> None:
        ack()
        lead_id = int(body["actions"][0]["value"])
        db.set_lead_status(db.connect(), lead_id, "snoozed")
        _thread_reply(client, body, f"💤 Snoozed lead #{lead_id} — it can resurface later.")

    @app.action("grant_bad_lead")
    def bad_lead(ack: Ack, body: dict[str, Any], client) -> None:
        """Open a modal asking WHY — the reason is the feedback loop for scoring."""
        ack()
        lead_id = body["actions"][0]["value"]
        client.views_open(
            trigger_id=body["trigger_id"],
            view={
                "type": "modal", "callback_id": "grant_bad_lead_reason",
                "private_metadata": lead_id,
                "title": {"type": "plain_text", "text": "Bad lead — why?"},
                "submit": {"type": "plain_text", "text": "Save"},
                "blocks": [{
                    "type": "input", "block_id": "reason_block",
                    "label": {"type": "plain_text",
                              "text": "What made this a bad lead?"},
                    "element": {"type": "plain_text_input", "action_id": "reason",
                                "placeholder": {"type": "plain_text",
                                                "text": "e.g. money is for software, not cameras"}},
                }],
            })

    @app.view("grant_bad_lead_reason")
    def bad_lead_reason(ack: Ack, body: dict[str, Any], view: dict[str, Any]) -> None:
        ack()
        lead_id = int(view["private_metadata"])
        reason = view["state"]["values"]["reason_block"]["reason"]["value"] or ""
        db.set_lead_status(db.connect(), lead_id, "dead", note=reason)

    # ---------------------------------------------------------------- draft (interim)
    @app.action("grant_draft_email")
    def draft_email(ack: Ack, body: dict[str, Any], client) -> None:
        """Post the template draft in-thread for the rep to use MANUALLY.

        HONESTY NOTE (architectural-critic C1, 2026-07-13): the automated
        Grant→Persequor handoff is NOT wired yet — Persequor drops bot messages by
        design, so the old 'Approve & hand to Persequor' button was a no-op that
        falsely wrote contacted/sent_at. Until the real HTTP contract ships (see
        docs/workflow_design.md §4), this button only offers a copyable draft and
        says so plainly. No status changes, no outreach rows.
        """
        ack()
        conn = db.connect()
        lead_id = int(body["actions"][0]["value"])
        row = db.get_lead(conn, lead_id)
        if row is None:
            _thread_reply(client, body, f"⚠️ Lead #{lead_id} not found — stale button?")
            return
        draft = persequor.compose_draft(row)
        _thread_reply(client, body,
                      f"Draft for *{row['entity_name']}* — copy it into your own email "
                      f"if you want to send today:\n```{draft}```\n"
                      f"_Automated hand-off to Persequor isn't wired yet (in design). "
                      f"If you do send it, click ✅ Mark contacted so the lead is "
                      f"tracked honestly._")

    # ---------------------------------------------------------------- conversation
    bot_user_id: str = app.client.auth_test()["user_id"]
    persequor_id: str = os.environ.get("PERSEQUOR_USER_ID", "")

    @app.event("app_mention")
    def on_mention(event: dict[str, Any], say, client) -> None:
        """Mentions route to the SAME conversational brain as plain thread replies —
        reps shouldn't need the @, but using it must not degrade the experience."""
        text = re.sub(r"<@[^>]+>", "", event.get("text") or "").strip()
        thread_ts = event.get("thread_ts")
        conn = db.connect()
        post = db.find_post_by_ts(conn, event["channel"], thread_ts or "")
        if post is not None:
            _handle_drip_thread(conn, post, event, say, client)
        else:
            # A general question outside a lead thread: answer in a thread on it.
            _converse_general(text, say, event.get("thread_ts") or event["ts"])

    @app.event("message")
    def on_message(event: dict[str, Any], say, client) -> None:
        """DMs, and plain (no-@) thread replies under a drip post."""
        if event.get("bot_id"):  # never talk to bots — loop guard
            return
        text = event.get("text") or ""
        if f"<@{bot_user_id}>" in text:
            return  # the app_mention handler owns this one — no double replies
        if persequor_id and f"<@{persequor_id}>" in text:
            return  # they're talking to Persequor — Grant stays out of it (Chase's rule)
        if event.get("channel_type") == "im":
            _converse_general(text.strip(), say, None)
            return
        thread_ts = event.get("thread_ts")
        if not thread_ts:
            return  # top-level channel chatter isn't Grant's business
        conn = db.connect()
        post = db.find_post_by_ts(conn, event["channel"], thread_ts)
        if post is None:
            return  # a thread on someone else's message
        _handle_drip_thread(conn, post, event, say, client)

    @app.event("reaction_added")
    def on_reaction(event: dict[str, Any]) -> None:
        """A reaction on a drip post is engagement — the cheapest +1 there is."""
        item = event.get("item") or {}
        if item.get("type") != "message":
            return
        conn = db.connect()
        post = db.find_post_by_ts(conn, item.get("channel", ""), item.get("ts", ""))
        if post is not None:
            db.record_engagement(conn, int(post["id"]), event["user"], "reaction")

    @app.command("/grant")
    def slash_grant(ack: Ack, command: dict[str, Any], respond, client) -> None:
        ack()
        arg = (command.get("text") or "").strip().lower()
        if arg == "digest":
            conn = db.connect()
            n = digest.post_digest(client, os.environ["SLACK_CHANNEL_ID"], conn)
            respond(f"Posted the digest ({n} leads shown).")
        elif arg == "stats":
            stats = db.engagement_stats(db.connect())
            detail = ", ".join(f"{k}: {v}" for k, v in stats.items() if k != "total")
            respond(f"Grant's engagement score: *{stats['total']}* points"
                    f"{f' ({detail})' if detail else ''}.")
        else:
            respond(_answer(arg))

    return app


def _handle_drip_thread(conn, post, event: dict[str, Any], say, client) -> None:
    """A human spoke in a lead thread: award the point, understand the message,
    act on the intent, answer in the thread (uploading any files Grant produced).
    Any LLM failure degrades to an honest reply — never to a wrong action."""
    from . import conversation  # local import: poll/digest paths never need anthropic

    user = event["user"]
    text = re.sub(r"<@[^>]+>", "", event.get("text") or "").strip()
    db.record_engagement(conn, int(post["id"]), user, "reply")
    row = db.get_lead(conn, int(post["lead_id"])) if post["lead_id"] else None
    context = _thread_history(client, event["channel"], post["ts"])
    try:
        out = conversation.respond(text, row, thread_context=context)
    except Exception as exc:  # API down ≠ silence; reply honestly
        say(text=f"I'm having trouble thinking right now ({type(exc).__name__}) — "
                 f"give me a minute and try again.", thread_ts=post["ts"])
        return
    intent, reply, files = out["intent"], out["reply"], out.get("files", [])

    if intent == "draft_email" and row is not None:
        reply = _request_outreach(conn, row, user, say, event["channel"], post["ts"])
    elif intent == "claim" and row is not None:
        if db.claim_lead(conn, int(row["id"]), user):
            db.record_engagement(conn, int(post["id"]), user, "claim")
            reply = f"It's yours, <@{user}>. {reply}" if reply else f"It's yours, <@{user}>."
        elif row["assigned_to"] and row["assigned_to"] != user:
            reply = f"Already claimed by <@{row['assigned_to']}> — worth a quick word with them first."
    elif intent == "snooze" and row is not None:
        db.set_lead_status(conn, int(row["id"]), "snoozed")
    elif intent == "bad_lead" and row is not None:
        db.set_lead_status(conn, int(row["id"]), "dead", note=f"bad lead per <@{user}>: {text}")
    elif intent == "question":
        db.record_engagement(conn, int(post["id"]), user, "question")

    say(text=reply, thread_ts=post["ts"])
    for path in files:  # spreadsheets etc. — into the same thread, then cleaned up
        try:
            client.files_upload_v2(channel=event["channel"], thread_ts=post["ts"],
                                   file=path)
        finally:
            import contextlib
            import os as _os
            with contextlib.suppress(OSError):
                _os.remove(path)


def _request_outreach(conn, row, user: str, say, channel: str, thread_ts: str) -> str:
    """The draft_email action: verified contact (enriching on the spot if needed) ->
    outreach-request.v1 brief -> Persequor. Every branch replies truthfully; the
    interim copyable draft remains the fallback while Persequor's endpoint is dark."""
    from .. import persequor_client
    from . import persequor as draft_templates

    send_as = persequor_client.rep_email_for(user)
    if send_as is None:
        return ("You're not on the rep roster yet, so I can't request outreach under "
                "your name — Chase can add you to config/reps.json.")

    contacts = [c for c in db.contacts_for_lead(conn, int(row["id"]))
                if c["contact_status"] == "verified"]
    contact = contacts[0] if contacts else None
    if contact is None:
        say(text=f"On it — digging up the right contact at {row['entity_name']} "
                 f"first (takes about half a minute)…", thread_ts=thread_ts)
        from . import tools as t
        t.find_contact(int(row["id"]), row["entity_name"], row["state"] or "")
        contacts = [c for c in db.contacts_for_lead(conn, int(row["id"]))
                    if c["contact_status"] == "verified"]
        contact = contacts[0] if contacts else None

    brief = persequor_client.build_brief(row, contact, user, send_as,
                                         slack_channel=channel, slack_thread_ts=thread_ts)
    if brief is None:
        return ("I couldn't verify a contact for them (nothing I can prove from "
                "their site), and there's no test address configured — so no email "
                "request from me. If you know the right person, tell me here.")
    state_, msg = persequor_client.submit_brief(conn, int(row["id"]), brief)
    if state_ == "submitted":
        found = (f" Contact on file: {contact['name']} ({contact['title']})."
                 if contact is not None else "")
        return msg + found
    # Endpoint dark or refused: fall back to the honest copyable draft.
    draft = draft_templates.compose_draft(row)
    return (f"{msg}\nMeanwhile, here's a copyable draft so you're not blocked:\n"
            f"```{draft}```")


def _thread_history(client, channel: str, thread_ts: str) -> list[str]:
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


def _converse_general(text: str, say, thread_ts: str | None) -> None:
    """Friendly LLM reply outside a lead thread (mention or DM), tools included.
    Falls back to the canned help text if the API is unavailable."""
    from . import conversation

    try:
        out = conversation.respond(text, None)
        say(text=out["reply"], thread_ts=thread_ts)
    except Exception:
        say(text=_answer(text.lower()), thread_ts=thread_ts)


def _answer(query: str) -> str:
    """Deterministic fallback Q&A (used when the LLM is unreachable): status/help,
    honest and friendly, no inline code styling (red text is banned)."""
    if "status" in query:
        lines = [f"• {source} — {grade_}: {count}"
                 for source, grade_, count in db.status_summary(db.connect())]
        return "Here's where we stand:\n" + "\n".join(lines)
    if query in ("", "help") or "help" in query:
        return HELP_TEXT
    return ("My brain's having a slow moment — I can do status or help right now. "
            "Try me again in a minute for the good stuff.")


def _thread_reply(client, body: dict[str, Any], text: str,
                  extra_blocks: list[dict[str, Any]] | None = None) -> None:
    """Reply in the thread under the digest message the button lives on."""
    msg = body["message"]
    blocks = ([{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
              + (extra_blocks or []))
    client.chat_postMessage(channel=body["channel"]["id"],
                            thread_ts=msg.get("thread_ts") or msg["ts"],
                            text=text, blocks=blocks)


def main() -> None:
    """Start the Socket Mode listener (blocks forever; Ctrl-C to stop)."""
    load_dotenv()
    handler = SocketModeHandler(create_app(), os.environ["SLACK_APP_TOKEN"])
    print("Grant is listening (Socket Mode)…")
    handler.start()


if __name__ == "__main__":
    main()
