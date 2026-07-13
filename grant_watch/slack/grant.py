"""Grant — the Socket Mode bot: triage buttons, the approve-to-email flow, mentions,
DMs, and the /grant slash command.

Run it (long-lived process; needs SLACK_BOT_TOKEN + SLACK_APP_TOKEN in .env):
    python -m grant_watch.slack.grant

Flow per CLAUDE.md rule 10 — Grant PROPOSES, a human APPROVES, Persequor SENDS:
  [Draft email]     -> compose draft, store in `outreach`, post in-thread with
                       [Approve & hand to Persequor] [Discard]
  [Approve...]      -> record approver in `outreach.approved_by`, post the handoff
                       message mentioning @Persequor, lead -> 'contacted'
  [Mark contacted]  -> lead status 'contacted'
  [Snooze]          -> lead status 'snoozed'
  [Bad lead]        -> modal asks WHY; reason lands in leads.status_note (feeds scoring)
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

HELP_TEXT = (
    "I'm *Grant* 🦉 — I watch government security-funding sources weekly and surface "
    "leads here.\n• `/grant status` — lead counts by source\n"
    "• `/grant digest` — post the weekly digest now\n"
    "• Digest buttons: draft an outreach email (a human approves before anything "
    "sends), mark contacted, snooze, or flag a bad lead.\n"
    "_I never invent contacts or figures; if I don't know, I say so._"
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

    # ---------------------------------------------------------------- draft -> approve
    @app.action("grant_draft_email")
    def draft_email(ack: Ack, body: dict[str, Any], client) -> None:
        """Compose the honest template draft and post it in-thread for review."""
        ack()
        conn = db.connect()
        lead_id = int(body["actions"][0]["value"])
        row = db.get_lead(conn, lead_id)
        if row is None:
            _thread_reply(client, body, f"⚠️ Lead #{lead_id} not found — stale button?")
            return
        draft = persequor.compose_draft(row)
        outreach_id = db.create_outreach(conn, lead_id, draft)
        _thread_reply(client, body,
                      f"Draft for *{row['entity_name']}* (review before anything sends):\n"
                      f"```{draft}```",
                      extra_blocks=[{
                          "type": "actions", "block_id": f"outreach-{outreach_id}",
                          "elements": [
                              {"type": "button", "action_id": "grant_approve_send",
                               "style": "primary",
                               "text": {"type": "plain_text",
                                        "text": "Approve & hand to Persequor"},
                               "value": f"{outreach_id}:{lead_id}"},
                              {"type": "button", "action_id": "grant_discard_draft",
                               "text": {"type": "plain_text", "text": "Discard"},
                               "value": f"{outreach_id}:{lead_id}"},
                          ]}])

    @app.action("grant_approve_send")
    def approve_send(ack: Ack, body: dict[str, Any], client) -> None:
        """THE approval gate: only here does the draft move toward an actual send."""
        ack()
        conn = db.connect()
        outreach_id, lead_id = (int(x) for x in body["actions"][0]["value"].split(":"))
        approver = body["user"]["id"]
        row = db.get_lead(conn, lead_id)
        draft_row = conn.execute("SELECT draft FROM outreach WHERE id = ?",
                                 (outreach_id,)).fetchone()
        if row is None or draft_row is None:
            _thread_reply(client, body, "⚠️ Draft or lead vanished — nothing handed off.")
            return
        db.approve_outreach(conn, outreach_id, approver)
        db.set_lead_status(conn, lead_id, "contacted")
        _thread_reply(client, body,
                      persequor.build_handoff_text(row["entity_name"], approver,
                                                   draft_row["draft"]))

    @app.action("grant_discard_draft")
    def discard_draft(ack: Ack, body: dict[str, Any], client) -> None:
        ack()
        outreach_id, _ = (int(x) for x in body["actions"][0]["value"].split(":"))
        conn = db.connect()
        conn.execute("DELETE FROM outreach WHERE id = ? AND approved_by IS NULL",
                     (outreach_id,))
        conn.commit()
        _thread_reply(client, body, "🗑️ Draft discarded — nothing was sent.")

    # ---------------------------------------------------------------- conversation
    @app.event("app_mention")
    def on_mention(event: dict[str, Any], say) -> None:
        say(text=_answer(re.sub(r"<@[^>]+>", "", event.get("text") or "").strip()),
            thread_ts=event.get("thread_ts") or event["ts"])

    @app.event("message")
    def on_dm(event: dict[str, Any], say) -> None:
        # Only respond to direct messages from humans (no channels, no bot echo loops).
        if event.get("channel_type") == "im" and not event.get("bot_id"):
            say(text=_answer((event.get("text") or "").strip()))

    @app.command("/grant")
    def slash_grant(ack: Ack, command: dict[str, Any], respond, client) -> None:
        ack()
        arg = (command.get("text") or "").strip().lower()
        if arg == "digest":
            conn = db.connect()
            n = digest.post_digest(client, os.environ["SLACK_CHANNEL_ID"], conn)
            respond(f"Posted the digest ({n} leads shown).")
        else:
            respond(_answer(arg))

    return app


def _answer(query: str) -> str:
    """Tiny deterministic Q&A: status/help. Anything it can't answer, it says so
    honestly rather than improvising (no LLM in the loop yet — Phase 2+)."""
    if "status" in query:
        lines = [f"• {source} — {grade_}: {count}"
                 for source, grade_, count in db.status_summary(db.connect())]
        return "Current lead counts:\n" + "\n".join(lines)
    if query in ("", "help") or "help" in query:
        return HELP_TEXT
    return ("I can answer `status` or `help` right now (and `/grant digest` posts the "
            "digest). Deeper questions come once contact enrichment lands — I won't "
            "make answers up in the meantime.")


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
