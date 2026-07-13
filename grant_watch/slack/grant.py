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
